"""The name reveal must never hold the ceremony open on a slow model.

MEASURED on the fleet box 2026-08-26, driving the real ceremony end to end:
the reveal took 60.45s and the journal recorded

    INFO:hevolve.hart:LLM name gen: local endpoint unreachable (timed out)
                       - using curated fallback

so the human waited a full minute to be handed the curated name that costs
nothing to produce. That is the single most emotionally loaded screen in the
product ("Your secret name is..."), and it was the slowest.

Why it was that slow: when the model is unreachable the generator made THREE
blocking LLM calls in series (generate, validate the candidates, then validate
the curated fallbacks against the SAME dead endpoint) plus up to five cloud
uniqueness checks at 5s each.

Two fixes, both pinned here:
  * generate_hart_name(use_llm=False) is an instant curated path: no model call,
    no cloud round trips, local uniqueness still enforced.
  * the session PREWARMS the name the moment both answers are known, behind the
    ~8.5s of scripted pause the ceremony already plays, and the reveal waits
    only a short grace period for it.

Run:
  pytest tests/unit/test_onboarding_reveal_latency.py -v --noconftest
"""

import threading
import time

import pytest

from hartos import hart_onboarding as ho


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Name uniqueness reads the registry; keep it off a real DB."""
    monkeypatch.setattr(ho.HARTNameRegistry, 'get_all_names',
                        staticmethod(lambda: set()), raising=False)
    monkeypatch.setattr(ho.HARTNameRegistry, 'is_available',
                        staticmethod(lambda name: True), raising=False)


def _session():
    s = object.__new__(ho.HARTOnboardingSession)
    s.user_id = 'u1'
    s.phase = 'reveal'
    s.language = 'en'
    s.locale = 'en_US'
    s.passion_key = 'building_coding'
    s.escape_key = 'nature_open'
    s.voice_transcript = ''
    s.generated_name = None
    s._prewarm_thread = None
    s._prewarm_result = None
    s._prewarm_token = 0
    s._prewarm_lock = threading.Lock()
    # Keep the assertion on timing/selection, not on response shaping.
    s._response = lambda **kw: kw
    return s


# ── the instant path really is instant ───────────────────────────────────────

def test_use_llm_false_makes_no_model_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("use_llm=False must not call the model")

    monkeypatch.setattr(ho, '_llm_generate_direct', _boom)
    monkeypatch.setattr(ho, '_validate_names_cross_language', _boom)

    t0 = time.monotonic()
    out = ho.generate_hart_name(
        language='en', passion_key='building_coding', escape_key='nature_open',
        use_llm=False)
    elapsed = time.monotonic() - t0

    assert out and out.get('name'), "the instant path must still produce a name"
    assert elapsed < 1.0, "curated generation took %.2fs" % elapsed


def test_use_llm_false_skips_cloud_uniqueness_round_trips(monkeypatch):
    calls = []
    monkeypatch.setattr(ho.HARTNameRegistry, 'is_available',
                        staticmethod(lambda name: calls.append(name) or True),
                        raising=False)
    monkeypatch.setattr(ho, '_llm_generate_direct', lambda *a, **k: None)

    ho.generate_hart_name(language='en', passion_key='building_coding',
                          escape_key='nature_open', use_llm=False)

    assert calls == [], \
        "instant path made %d cloud checks; five at 5s each is another 25s of " \
        "ceremony stall" % len(calls)


def test_llm_path_is_still_the_default():
    """The fast path must be opt-in: a healthy node still gets a model name."""
    import inspect
    sig = inspect.signature(ho.generate_hart_name)
    assert sig.parameters['use_llm'].default is True


# ── the reveal is bounded regardless of the model ────────────────────────────

def test_reveal_does_not_wait_for_a_slow_model(monkeypatch):
    """The 60.45s bug, as a test."""
    def _slow(*a, **k):
        if k.get('use_llm', True):
            time.sleep(8)           # stands in for the unreachable endpoint
        return {'name': 'curated', 'candidates': ['curated'], 'dimensions': {},
                'emoji_combo': '', 'element': '', 'spirit': ''}

    monkeypatch.setattr(ho, 'generate_hart_name', _slow)
    s = _session()
    s._start_prewarm()              # cooking, will not land in time

    t0 = time.monotonic()
    out = s._do_reveal()
    elapsed = time.monotonic() - t0

    assert elapsed < s.REVEAL_WAIT_S + 2.0, \
        "reveal blocked %.2fs; it must fall back to a curated name" % elapsed
    assert out.get('hart_name') == 'curated'


def test_reveal_uses_the_prewarmed_name_when_it_lands(monkeypatch):
    """Bounded must not mean the model never wins: a quick generation is used."""
    def _quick(*a, **k):
        if k.get('use_llm', True):
            time.sleep(0.1)
            return {'name': 'modelname', 'candidates': ['modelname'],
                    'dimensions': {}, 'emoji_combo': '', 'element': '',
                    'spirit': ''}
        return {'name': 'curated', 'candidates': ['curated'], 'dimensions': {},
                'emoji_combo': '', 'element': '', 'spirit': ''}

    monkeypatch.setattr(ho, 'generate_hart_name', _quick)
    s = _session()
    s._start_prewarm()
    time.sleep(0.4)                 # the scripted pause, compressed

    out = s._do_reveal()
    assert out.get('hart_name') == 'modelname', \
        "a generation that finished in time must be the name that is revealed"


def test_answering_the_escape_question_starts_the_prewarm(monkeypatch):
    """The whole point: generation begins BEHIND the scripted acknowledgements,
    not when the client finally asks to reveal."""
    started = []
    monkeypatch.setattr(ho.HARTOnboardingSession, '_start_prewarm',
                        lambda self: started.append(True))

    s = _session()
    s.phase = 'escape'
    s.escape_key = None
    s._line = lambda key: ''
    s.advance(action='answer', data={'key': 'nature_open'})

    assert started, \
        "the prewarm must start when the last answer arrives, otherwise the " \
        "model latency lands on the reveal"


def test_try_another_is_also_bounded(monkeypatch):
    """'Try another' restarts generation, and must be bounded the same way."""
    def _slow(*a, **k):
        if k.get('use_llm', True):
            time.sleep(8)
        return {'name': 'curated2', 'candidates': ['curated2'], 'dimensions': {},
                'emoji_combo': '', 'element': '', 'spirit': ''}

    monkeypatch.setattr(ho, 'generate_hart_name', _slow)
    s = _session()
    s.generated_name = {'name': 'previous'}

    t0 = time.monotonic()
    s._do_reveal(alternative=True)
    elapsed = time.monotonic() - t0

    assert elapsed < s.REVEAL_WAIT_S + 2.0, \
        "'try another' blocked %.2fs" % elapsed


# ── the steps Nunba's ceremony sends reach the prewarm ───────────────────────
#
# Nunba's ceremony drives this session through /api/hart/advance.  It used to
# send only select_language and the two answers, never the bare steps for the
# greeting and the two acknowledgements, so the greeting branch swallowed the
# passion answer, the escape answer landed in 'passion', and _start_prewarm
# never ran.  These tests drive a REAL session with the ceremony's steps.

CEREMONY_STEPS_TO_ESCAPE = [
    ('select_language', {'language': 'en', 'locale': 'en_US'}),
    (None, None),                                     # greeting -> passion
    ('answer', {'key': 'building_coding'}),           # passion -> ack_passion
    (None, None),                                     # ack_passion -> escape
    ('answer', {'key': 'nature_open'}),               # escape -> ack_escape
]


def _named(name, *others):
    return {'name': name, 'candidates': [name, *others], 'dimensions': {},
            'emoji_combo': 'x', 'element': '', 'spirit': '',
            'hart_tag': '@' + name}


def test_the_ceremony_steps_start_the_prewarm_with_both_answers(monkeypatch):
    seen = []

    def _gen(*a, **k):
        seen.append(dict(k))
        return _named('modelname', 'backup')

    monkeypatch.setattr(ho, 'generate_hart_name', _gen)
    s = ho.HARTOnboardingSession('ceremony-key')
    for action, data in CEREMONY_STEPS_TO_ESCAPE:
        s.advance(action=action, data=data)

    assert s.phase == 'ack_escape'
    assert s._prewarm_thread is not None, "the escape answer must start it"
    s._prewarm_thread.join(2)
    model_calls = [k for k in seen if k.get('use_llm')]
    assert model_calls and model_calls[0]['passion_key'] == 'building_coding'
    assert model_calls[0]['escape_key'] == 'nature_open'


def test_the_reveal_answers_with_the_name_and_its_backups(monkeypatch):
    monkeypatch.setattr(ho, 'generate_hart_name',
                        lambda *a, **k: _named('modelname', 'backup1', 'backup2'))
    s = ho.HARTOnboardingSession('ceremony-key')
    for action, data in CEREMONY_STEPS_TO_ESCAPE:
        s.advance(action=action, data=data)
    s.advance()                                       # ack_escape -> pre_reveal
    out = s.advance()                                 # pre_reveal -> reveal

    assert out['phase'] == 'reveal'
    assert out['hart_name'] == 'modelname'
    assert out['hart_tag'] == '@modelname'
    assert out['candidates'] == ['backup1', 'backup2'],         "the client seals through /api/hart/seal and needs the backups for a 409"


def test_one_model_call_per_session_however_often_it_is_asked(monkeypatch):
    """A second prewarm while one is cooking starts no second model thread,
    so a slow model holds one llama-server slot per ceremony, not one per call."""
    release = threading.Event()
    calls = []

    def _slow(*a, **k):
        calls.append(1)
        release.wait(5)
        return _named('modelname')

    monkeypatch.setattr(ho, 'generate_hart_name', _slow)
    s = _session()
    s._start_prewarm()
    s._start_prewarm()
    time.sleep(0.2)
    release.set()
    s._prewarm_thread.join(2)
    assert len(calls) == 1
