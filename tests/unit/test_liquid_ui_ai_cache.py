"""/api/ui must never block on inference.

WHY THIS EXISTS (measured on the fleet box 2026-08-26):
  generate_ui() called the model bus synchronously on the waitress request
  thread with a 15s budget. Real generation on that node measured 50.7s for a
  1024-token /v1/chat (llama.cpp, two pinned cores), and 19.8s cold / 3.7s warm
  for a 16-token prompt. So the 15s timeout was unreachable BY CONSTRUCTION:
  every /api/ui call spent 15s failing and then returned the static layout it
  could have returned instantly. The journal showed
      "AI UI generation failed: ... Read timed out. (read timeout=15)"
  on every call, and curl measured the endpoint at >10s.

  That is the same defect _ConnectivityCache was written to fix (synchronous
  probes on the request thread saturating a small pool so every other shell
  fetch queues behind them), only worse, and on a box whose shell polls it.

These tests pin the two properties that make it safe: the request path is
non-blocking, and a slow generation still eventually upgrades the answer.

Run:
  pytest tests/unit/test_liquid_ui_ai_cache.py -v --noconftest
"""

import threading
import time

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


STATIC = {'source': 'static', 'components': [{'type': 'card'}],
          'context_summary': 'static'}
AI = {'source': 'ai', 'components': [{'type': 'card', 'title': 'AI'}],
      'context_summary': 'ai'}


def _svc(ai_delay=0.0, ai_result=None):
    """A LiquidUIService with ONLY the AI-cache state initialised.

    object.__new__ avoids standing up ports/threads/DB for a test about one
    method; the attributes below are exactly what generate_ui touches.
    """
    s = object.__new__(LiquidUIService)
    s._ai_ui_lock = threading.Lock()
    s._ai_ui = None
    s._ai_ui_at = 0.0
    s._ai_ui_busy = False
    s._model_available = True
    s._calls = []

    def _fake_ai(context):
        s._calls.append(time.monotonic())
        if ai_delay:
            time.sleep(ai_delay)
        return ai_result if ai_result is not None else AI

    s._generate_ai_ui = _fake_ai
    s._generate_static_ui = lambda context: STATIC
    return s


def _wait_idle(s, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with s._ai_ui_lock:
            if not s._ai_ui_busy:
                return True
        time.sleep(0.02)
    return False


def test_request_path_does_not_wait_for_inference():
    """The whole bug: a 50s generation must not be on the request path."""
    s = _svc(ai_delay=2.0)

    t0 = time.monotonic()
    out = s.generate_ui({'system': {}})
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, (
        "generate_ui blocked %.2fs on inference; the whole point is that the "
        "request thread returns immediately and the model works in the "
        "background" % elapsed)
    assert out['source'] == 'static', \
        "with a cold cache the caller must get the instant static layout"
    assert _wait_idle(s), "the background refresher never finished"


def test_slow_generation_still_upgrades_the_answer():
    """Non-blocking must not mean never-AI: a later call gets the AI layout."""
    s = _svc(ai_delay=0.2)

    assert s.generate_ui({'system': {}})['source'] == 'static'
    assert _wait_idle(s)

    out = s.generate_ui({'system': {}})
    assert out['source'] == 'ai', \
        "once the background generation lands, callers must get the AI layout"


def test_refresh_is_not_duplicated_while_one_is_in_flight():
    """Inference costs ~50s of CPU on two cores here; a polling shell must not
    stack one generation per poll."""
    s = _svc(ai_delay=1.0)

    for _ in range(8):
        s.generate_ui({'system': {}})

    assert _wait_idle(s, timeout=6.0)
    assert len(s._calls) == 1, \
        "8 concurrent polls produced %d generations; refreshes must coalesce" \
        % len(s._calls)


def test_a_failed_generation_does_not_poison_the_cache():
    """A static fallback must not be cached as if it were an AI answer, or the
    TTL would suppress every later attempt."""
    s = _svc(ai_delay=0.0, ai_result=STATIC)   # AI path degraded to static

    s.generate_ui({'system': {}})
    assert _wait_idle(s)

    with s._ai_ui_lock:
        assert s._ai_ui is None, \
            "only a real 'ai' answer may fill the cache"

    s.generate_ui({'system': {}})
    assert _wait_idle(s)
    assert len(s._calls) == 2, "a failed refresh must be retried, not pinned"


def test_no_inference_when_no_model():
    """An appliance with no model must not spawn refreshers at all."""
    s = _svc()
    s._model_available = False

    assert s.generate_ui({'system': {}})['source'] == 'static'
    assert s._calls == [], "no model means no generation attempts"


@pytest.mark.parametrize('attr', ['AI_UI_TTL_S', 'AI_UI_TIMEOUT_S'])
def test_budgets_are_sized_for_real_hardware(attr):
    """The old 15s call budget could never complete a 50.7s generation, and a
    TTL shorter than the generation itself would keep a slow box permanently
    generating."""
    val = getattr(LiquidUIService, attr)
    assert val >= 60, "%s=%s is below the measured 50.7s generation" % (attr, val)
