"""Latency budgets are ENFORCED, not just documented.

CLAUDE.md's review checklist has named the hot-path budgets (chat 1.5s,
draft 300ms, cache <1ms) for a long time and NOTHING asserted them, while
six unrelated test files each carried their own inline literal. This suite
is the enforcement half: every budget lives in core.constants.LATENCY_BUDGETS
and is checked here against real code, so a regression fails a test instead
of being discovered on the steward's hardware.

Measurement discipline (a flaky perf test is worse than none):
  * Ceilings are asserted, never equality — a fast machine must never fail.
  * The BEST of N runs is compared, not the mean: CI runners are noisy and
    the question is "can this code meet the budget", not "was this shared
    runner busy". A single slow sample is scheduler noise; a slow best-of-N
    is a real regression.
  * Only PURE-COMPUTE paths are timed here. Anything needing a GPU, a model
    server or the network is measured on the node, not in a unit test —
    asserting those here would be theatre.

    python -m pytest tests/unit/test_latency_budgets.py -q --noconftest
"""
import time

import pytest

from core.constants import LATENCY_BUDGETS, latency_budget


def best_of(fn, n=5):
    """Fastest of n runs, in seconds. See the measurement note above."""
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ═══════════════════════════════════════════════════════════════════════
# The budget registry itself
# ═══════════════════════════════════════════════════════════════════════

class TestBudgetRegistry:
    def test_hot_path_budgets_match_the_documented_contract(self):
        """CLAUDE.md's numbers ARE the contract; drift silently makes the
        documentation a lie."""
        assert latency_budget("chat_turn_overhead_s") == 1.5
        assert latency_budget("draft_classify_s") == 0.3
        assert latency_budget("cache_lookup_ms") == 1.0

    def test_unknown_budget_raises_rather_than_defaulting(self):
        """An enforcement point that silently stops enforcing is worse than
        no enforcement — a typo must fail loudly."""
        with pytest.raises(KeyError):
            latency_budget("chat_turn_overhead")      # missing _s

    def test_every_budget_is_a_positive_finite_ceiling(self):
        for name, val in LATENCY_BUDGETS.items():
            assert isinstance(val, (int, float)), f"{name} is not numeric"
            assert 0 < val < 3600, f"{name}={val} is not a usable ceiling"

    def test_user_context_budget_agrees_with_the_registry(self):
        """core.user_context enforces the chat hot-path wall-clock cap. Two
        copies of one number is exactly the drift this registry removes.

        Skips (never fails) when core.user_context cannot be imported for a
        reason that is NOT about latency. It imports python-dateutil, which
        is declared in requirements.txt:136 but absent from the bare
        interpreter this repo's orphaned venv forces tests onto — so the
        ModuleNotFoundError read as "a latency budget regressed" when
        nothing had. A red that means "your dev box lacks a dep" must not
        wear the same colour as a real budget violation; the skip names the
        missing module so it stays actionable rather than invisible.
        """
        try:
            from core.user_context import DEFAULT_BUDGET_SECONDS
        except ModuleNotFoundError as e:
            pytest.skip(f"core.user_context needs {e.name!r} "
                        f"(declared in requirements.txt, not installed here)")
        assert DEFAULT_BUDGET_SECONDS == latency_budget("chat_turn_overhead_s")


# ═══════════════════════════════════════════════════════════════════════
# Measured enforcement — real functions, real clock
# ═══════════════════════════════════════════════════════════════════════

class TestMeasuredHotPaths:
    def test_language_lookup_is_cache_fast(self):
        """The per-turn language/script lookups sit on the chat hot path and
        are pure dict work — they must be cache-class, not merely 'fast'."""
        from core.constants import (NON_LATIN_SCRIPT_LANGS,
                                    NON_LATIN_SCRIPT_NAMES,
                                    SUPPORTED_LANG_DICT)

        def lookup():
            for code in ("en", "ta", "hi", "zh", "de", "sat"):
                _ = code in NON_LATIN_SCRIPT_LANGS
                _ = NON_LATIN_SCRIPT_NAMES.get(code)
                _ = SUPPORTED_LANG_DICT.get(code)

        elapsed_ms = best_of(lookup) * 1000
        budget = latency_budget("cache_lookup_ms")
        assert elapsed_ms < budget, (
            f"hot-path language lookup took {elapsed_ms:.3f}ms, "
            f"budget {budget}ms — something turned a dict read into I/O")

    def test_regional_tone_prompt_build_is_under_the_draft_budget(self):
        """get_regional_tone_prompt runs before EVERY non-English turn,
        including the draft path, so it must fit well inside the draft
        classify budget on its own."""
        from core.agent_personality import get_regional_tone_prompt

        def build():
            for lang in ("ta", "hi", "es", "ja", "ar"):
                get_regional_tone_prompt(lang)

        elapsed = best_of(build)
        budget = latency_budget("draft_classify_s")
        assert elapsed < budget, (
            f"tone-prompt build took {elapsed:.3f}s, budget {budget}s — this "
            f"is string assembly and must never approach the draft budget")

    def test_tts_engine_selection_is_not_on_a_slow_path(self):
        """Engine selection happens per utterance; it is table lookup over
        LANG_ENGINE_PREFERENCE and must stay cache-class."""
        from integrations.channels.media import tts_router

        def select():
            for lang in ("en", "hi", "ta", "zh"):
                _ = tts_router.LANG_ENGINE_PREFERENCE.get(lang, [])

        elapsed_ms = best_of(select) * 1000
        budget = latency_budget("cache_lookup_ms")
        assert elapsed_ms < budget, (
            f"TTS engine table lookup took {elapsed_ms:.3f}ms, "
            f"budget {budget}ms")

    def test_latency_budget_lookup_is_itself_free(self):
        """The enforcement mechanism must not be measurable overhead, or
        callers will avoid it."""
        elapsed_ms = best_of(lambda: [latency_budget(k)
                                      for k in LATENCY_BUDGETS]) * 1000
        assert elapsed_ms < latency_budget("cache_lookup_ms")
