"""Behavioural tests for core.prompt_difficulty — the no-LLM difficulty router.

This module was 0% covered (no test file, no test importing it) despite being on
EVERY hosted-user chat turn (the central cloud node has no local draft model, so
`speculative_dispatcher.dispatch_draft_first` returns `no_draft_model` and this
is the only thing assessing difficulty). Its whole contract is "cheap,
deterministic, and being wrong toward the small model is the expensive
direction", so the asymmetric rules below are exactly what must not regress.

These call the real functions and assert observable outputs (label/score/why and
the routing verdict) — no grep/source-shape checks.
"""
from __future__ import annotations

from core.prompt_difficulty import (
    assess, is_casual, resolve_casual_conv, EASY, HARD, ORDINARY,
)


# ── assess(): the unambiguous-easy cases ────────────────────────────────────
class TestAssessEasy:
    def test_empty_is_easy(self):
        assert assess('') == (EASY, 0, 'empty')

    def test_whitespace_only_is_easy(self):
        assert assess('   \n  ')[0] == EASY

    def test_none_is_easy_not_crash(self):
        # `prompt or ''` guards None — a router must never raise on a bad turn.
        assert assess(None) == (EASY, 0, 'empty')

    def test_bare_pleasantry_is_easy(self):
        for p in ('hi', 'hello', 'thanks', 'thank you', 'how are you', 'ok'):
            label, score, why = assess(p)
            assert (label, score, why) == (EASY, 0, 'pleasantry'), p

    def test_pleasantry_matched_whole_string_not_substring(self):
        # "which hint helps here" contains "hi" but is NOT a pleasantry — the
        # docstring's exact stated reason for whole-string matching.
        assert assess('which hint helps here')[0] != EASY or \
            assess('which hint helps here')[2] != 'pleasantry'

    def test_pleasantry_ignores_trailing_punctuation(self):
        # low = text.lower().rstrip('.!') — "Thanks!" and "hello." still match.
        assert assess('Thanks!')[2] == 'pleasantry'
        assert assess('hello.')[2] == 'pleasantry'

    def test_short_and_plain_stray_remark_is_easy(self):
        # score 0, <=8 words, no '?', no interrogative/task-verb opener.
        label, score, why = assess('that worked nicely')
        assert label == EASY and why == 'short-and-plain'


# ── assess(): brevity does NOT buy EASY when it's a question or an instruction ─
class TestAssessShortButNotEasy:
    def test_short_interrogative_is_not_easy(self):
        # "what is the capital of France" — six words, still wants a real answer.
        label, _, _ = assess('what is the capital of France')
        assert label != EASY

    def test_short_task_verb_is_not_easy(self):
        # "summarise this" — three words of real work.
        label, _, _ = assess('summarise this')
        assert label != EASY

    def test_question_mark_short_is_not_easy(self):
        assert assess('done?')[0] != EASY


# ── assess(): the HARD signals ──────────────────────────────────────────────
class TestAssessHard:
    def test_code_fence_is_hard(self):
        label, score, why = assess('```\nprint(1)\n```')
        assert label == HARD and 'code' in why and score >= 35

    def test_sql_is_code(self):
        assert 'code' in assess('SELECT id FROM users where x=1')[2]

    def test_reasoning_why_is_hard(self):
        label, score, why = assess('why does this algorithm work')
        assert label == HARD and 'reasoning' in why

    def test_multi_step_is_flagged(self):
        assert 'multi-step' in assess('walk me through this step-by-step')[2]

    def test_technical_density_flagged(self):
        assert 'technical' in assess('there is a deadlock in the scheduler')[2]

    def test_math_flagged(self):
        assert 'math' in assess('what is the complexity, is it O(n log n)')[2]

    def test_hard_wins_ties_threshold_is_30(self):
        # reasoning alone = 30 → exactly at the HARD threshold.
        assert assess('explain this')[0] == HARD

    def test_score_is_clamped_to_100(self):
        # Pile on every signal; score must saturate at 100, never overflow.
        p = ('```def f(): pass```  why compare the trade-offs step-by-step in '
             'detail, thoroughly, with the algorithm complexity O(n^2)?  and?\n\n'
             + 'word ' * 130)
        label, score, why = assess(p)
        assert label == HARD and 0 <= score <= 100 and score == 100

    def test_multi_question_and_structured_signals(self):
        why = assess('do this?\nand that?\nand also this?')[2]
        assert 'multi-question' in why and 'structured' in why


# ── assess(): length buckets ────────────────────────────────────────────────
class TestAssessLength:
    def test_long_bucket(self):
        assert 'long' in assess('word ' * 50)[2]

    def test_very_long_bucket(self):
        assert 'very-long' in assess('word ' * 130)[2]

    def test_ordinary_when_no_signal_but_shaped_like_a_request(self):
        # A plain-ish question with no reasoning marker: ORDINARY, not EASY,
        # not HARD — the deliberate middle the docstring describes.
        label, score, _ = assess('what time is it there')
        assert label == ORDINARY and score < 30


# ── is_casual(): only EASY qualifies ────────────────────────────────────────
class TestIsCasual:
    def test_pleasantry_is_casual(self):
        assert is_casual('hey') is True

    def test_ordinary_is_not_casual(self):
        # asymmetric-cost rule: an unremarkable question is NOT casual.
        assert is_casual('what is the capital of France') is False

    def test_hard_is_not_casual(self):
        assert is_casual('explain why this deadlocks') is False


# ── resolve_casual_conv(): the caller's value is a hint, never the decision ──
class TestResolveCasualConv:
    def test_no_client_value_is_assessed(self):
        casual, why = resolve_casual_conv('hi', client_value=None)
        assert casual is True and why.startswith('assessed:')

    def test_no_client_value_hard_prompt_not_casual(self):
        casual, why = resolve_casual_conv('explain why this deadlocks', None)
        assert casual is False and why.startswith('assessed:')

    def test_client_says_casual_but_hard_is_overridden_to_full(self):
        # THE point of the module: a caller cannot see difficulty; a "casual"
        # claim on a hard prompt is not honoured (the expensive-direction guard).
        casual, why = resolve_casual_conv('why does this segfault', client_value=True)
        assert casual is False and why.startswith('override:')

    def test_client_says_full_on_easy_is_honoured(self):
        # Asking for more care than needed is the caller's prerogative.
        casual, why = resolve_casual_conv('hi', client_value=False)
        assert casual is False and why == 'client:False'

    def test_client_says_casual_on_easy_is_honoured(self):
        casual, why = resolve_casual_conv('hi', client_value=True)
        assert casual is True and why == 'client:True'

    def test_client_truthy_non_bool_is_coerced(self):
        # bool(client_value) — a truthy non-bool hint on an easy prompt.
        casual, why = resolve_casual_conv('hi', client_value=1)
        assert casual is True and why == 'client:True'
