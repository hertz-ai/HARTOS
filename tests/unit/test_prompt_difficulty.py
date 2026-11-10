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


# ── Imperatives on the person's OWN data are never EASY ─────────────────────
class TestOwnDataImperativesAreNotEasy:
    """The asymmetric-cost rule applied to the cases that actually fabricated.

    These four shapes reach the fast path with no tools if _TASK_VERB does
    not list their opener, which means remember() / open_file_gui() /
    list_book_chapters() cannot run and the model describes work it never
    did. MEASURED 2026-11-07 before the fix: every prompt below returned
    EASY / 'short-and-plain'. They are the same cases the route-level
    keyword tuple at Nunba chatbot_routes.py:3478 exists to catch, so this
    class is what allows that shadow table to be deleted.
    """

    def test_remember_is_not_easy(self):
        assert assess('remember that my exam is on friday')[0] != EASY
        assert is_casual("Remember my sister's birthday is the 4th") is False

    def test_forget_is_not_easy(self):
        assert is_casual("don't forget my dentist appointment") is False
        assert is_casual('forget about the gym thing') is False

    def test_open_is_not_easy(self):
        assert is_casual('open my downloads folder') is False

    def test_a_question_about_her_own_book_is_not_easy(self):
        # No book phrasing anywhere in this module: these resolve through
        # _INTERROGATIVE, which is why no phrase table is needed.
        for q in ('In my book, how many metres does the Drellith ridge rise?',
                  'how tall is the ridge in chapter 3',
                  'what does my textbook say about nesting',
                  'how many nests were counted'):
            assert is_casual(q) is False, q

    def test_pleasantries_still_take_the_fast_path(self):
        # The widening must not drag chit-chat onto the full path.
        for p in ('hi', 'hii', 'thanks', 'good morning', 'nice one',
                  'ok cool', 'that worked', 'lol', 'see you'):
            assert is_casual(p) is True, p


class TestTheWiringInChat:
    """The resolver had ZERO callers for four months, so its CONTRACT was
    never pinned where /chat relies on it.  REVIEW 2026-11-10 rejected the
    first wiring (58be9ddfd) for two reproduced criticals; these pins are
    what would have caught both.
    """

    def _entry_src(self):
        import os
        root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            '..', '..'))
        with open(os.path.join(root, 'hart_intelligence_entry.py'),
                  encoding='utf-8', errors='surrogateescape') as fh:
            return fh.read()

    def test_a_false_hint_is_always_honoured(self):
        """C1's contract.  An absent flag binds to False, and False must
        never be second-guessed: ~11 internal callers omit it on purpose,
        commercial_api.py:548 so a billed call never gets the casual tool
        set.  If this ever returns True, those callers silently change tier.
        """
        for p in ('set an alarm for 6am', 'send an email to ravi',
                  'take a screenshot', 'delete my account', 'continue',
                  'approved, generate the recipe', 'why is the sky blue',
                  'manaivikku oru email anuppu', 'envia un correo a ravi'):
            casual, why = resolve_casual_conv(p, False)
            assert casual is False, (p, why)

    def _run_bind(self, payload):
        """EXEC the real bind lines out of hart_intelligence_entry.py.

        A source grep still passes against a file that no longer calls the
        resolver at all, which is why REVIEW 2026-11-10 killed 4 mutants
        against the greps and 2 SURVIVED: (i) both literals kept but an
        absent flag handed to the resolver as None, (ii) the resolve call
        deleted outright. Running the lines catches both.
        """
        import textwrap
        src = self._entry_src()
        start = src.index("    casual_conv = data.get('casual_conv'")
        end = src.index("    autonomous = data.get('autonomous'", start)
        ns = {'data': payload}
        exec(compile(textwrap.dedent(src[start:end]), 'entry-bind', 'exec'), ns)
        return ns['casual_conv']

    def _run_resolve(self, prompt, casual_conv):
        """EXEC the real resolve block out of /chat, guard and all."""
        import textwrap
        src = self._entry_src()
        start = src.index('    if isinstance(prompt, str):')
        marker = src.index('app.logger.info("casual_conv resolved', start)
        end = src.index(chr(10), marker) + 1

        class _Logger:
            @staticmethod
            def info(*a, **k):
                pass

        class _App:
            logger = _Logger

        ns = {'prompt': prompt, 'casual_conv': casual_conv, 'app': _App}
        exec(compile(textwrap.dedent(src[start:end]), 'entry-resolve', 'exec'),
             ns)
        return ns['casual_conv']

    def test_an_absent_flag_binds_to_false(self):
        """C1 itself. ~11 internal callers omit the flag on purpose; if it
        binds to None they are routed by an English-only regex instead."""
        assert self._run_bind({}) is False

    def test_a_json_null_binds_to_false(self):
        """C1's remainder, found by review: the key IS present, so the
        default above it cannot fire. A client sending
        {"casual_conv": null} with "hi" went casual where the old code
        went full."""
        assert self._run_bind({'casual_conv': None}) is False

    def test_a_stated_hint_survives_the_bind(self):
        assert self._run_bind({'casual_conv': True}) is True
        assert self._run_bind({'casual_conv': False}) is False

    def test_the_resolve_block_overrides_a_casual_claim_on_a_hard_prompt(self):
        """The only thing the wiring does on the Nunba path. Kills the
        'resolve call deleted' mutant: without it the hint comes back
        unchanged."""
        assert self._run_resolve('how does a transformer work', True) is False

    def test_the_resolve_block_never_makes_a_full_request_casual(self):
        """The hint is only ever overridden toward MORE capability."""
        for p in ('delete my account', 'send an email to ravi',
                  'take a screenshot', 'hi'):
            assert self._run_resolve(p, False) is False

    def test_the_resolve_block_skips_a_non_string_prompt(self):
        """prompt is data.get('prompt', None) and assess() calls .strip(),
        so an unguarded resolve raises on a line every /chat call runs."""
        assert self._run_resolve(123, True) is True
        assert self._run_resolve(None, False) is False

    def test_an_ordinary_tool_prompt_with_a_casual_hint_is_still_casual(self):
        """C2, pinned as the KNOWN HOLE it is, so that no one reads the
        _TASK_VERB list and concludes these are handled.

        The override fires at HARD (score >= 30) only, and these assess
        ORDINARY, so a caller asserting 'casual' wins.  Nunba always sends
        a bool (hartos_backend_adapter.py:917), so on the live path the
        remember/forget/open verbs do NOTHING.  Closing this means
        overriding on TOOL-NEED as well as on difficulty, which changes the
        owner's 2026-08-24 session-shape design -- the owner's call.

        If this test starts failing, the behaviour was changed: check that
        the owner asked for it before making the test green again.
        """
        for p in ('remember that my exam is on friday',
                  'open my downloads folder',
                  "don't forget my dentist appointment",
                  'find my resume', 'summarize the pdf'):
            casual, why = resolve_casual_conv(p, True)
            assert casual is True and why == 'client:True', (p, why)
