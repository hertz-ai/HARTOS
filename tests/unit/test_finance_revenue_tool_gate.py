"""finance/revenue Tier-2 tools must be REACHABLE by some live path.

MEASURED 2026-09-07, and this is the whole defect in one line: on the live
install `register_finance_tools` has ZERO production callers (only
tests/e2e/test_e2e_pipelines.py:615), and `register_revenue_tools` is
dispatched on a tag that nothing can produce.  So neither tool pack can
ever attach to an agent, by any path.

HOW THAT WAS ESTABLISHED (each step measured, not inferred):

1. The live StatusVerifier reported a Finance agent's action 2 as 'error'
   twelve times ("Closed error on 2026-09-06 and unchanged by this twelfth
   nudge"), naming manage_invite_participation as never attaching.  That
   message is written by an LLM, so it only LOCATED the suspect.

2. register_finance_tools: defined integrations/agent_engine/finance_tools.py:24,
   called ONLY from tests/e2e/test_e2e_pipelines.py:615.  Zero production
   callers -- the LLM's claim, independently confirmed.

3. Both legs run the same family dispatch (reuse_recipe.py:2514-2549,
   create_recipe.py:2060-2097) over marketing / ip_protection / self_build /
   outreach / sales / revenue / news.  finance is absent from both.

4. The gate reads `goal_tags`.  Across 2,296 saved agents in
   ~/Documents/Nunba/data/prompts, exactly ONE carries any goal_tags at all
   (value: 'coding').  The sole writer of goal_tags in the whole repo is
   journey_engine.py:505 `goal_tags=['research']` -- a tag no consumer
   tests for -- and create_recipe.py:1867 hardcodes
   `resolve_goal_tags(None, task)`.  The stored-tag arm is therefore dead
   in practice, and Tier-2 loading rests entirely on the lexical
   detect_goal_tags(text).

5. detect_goal_tags emits: marketing, coding, ip_protection, self_build,
   outreach, sales, news, media.  No finance.  No revenue.

=> revenue is dispatched but its tag is unreachable; finance is neither
   detected nor dispatched.  Adding only a dispatch branch would have been
   a VACUOUS fix -- the branch could never be true.  Both halves are needed.

KEYWORD PRECISION IS MEASURED, NOT GUESSED.  marketing_tools._mentions
documents a prior incident where loose keywords produced 361 spurious tag
assignments ('ad ' inside "re[ad ]engagement").  Every candidate here was
counted against the 884 real agent goals that carry goal text:

    'financial health'    8  (0.9%)     accepted
    'api revenue'         7  (0.8%)     accepted
    'finance agent'       4  (0.5%)     accepted
    'expense'             4  (0.5%)     accepted
    'accounting'          3  (0.3%)     accepted
    'revenue split'      37  (4.2%)     accepted
    'pricing'            33  (3.7%)     accepted
    ---- rejected as too broad ----
    'budget'            180 (20.4%)     "token budget", "time budget"
    'sustainab'         107 (12.1%)     generic English
    'revenue' (bare)    146 (16.5%)     OWNER CALL: grants adjust_pricing,
                                        a MUTATING tool, to 1 agent in 6

    python -m pytest tests/unit/test_finance_revenue_tool_gate.py --noconftest -q
"""
import re

import pytest

REPO = __file__.rsplit("tests", 1)[0]


def _src(rel):
    return open(REPO + rel, encoding="utf-8", errors="replace").read()


def _detect():
    """Import just the detector.

    marketing_tools imports cleanly (no Flask app context needed for
    detect_goal_tags), so this exercises the REAL function rather than
    asserting on its source -- the tag vocabulary is behaviour, not text.
    """
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    return detect_goal_tags


class TestTagsAreProducible:

    def test_finance_goal_gets_finance_tag(self):
        d = _detect()
        assert 'finance' in d("Review the platform financial health and the "
                              "revenue split compliance"), (
            "a finance goal produces no 'finance' tag, so the finance tool "
            "pack can never be registered for it")

    def test_revenue_goal_gets_revenue_tag(self):
        d = _detect()
        assert 'revenue' in d("Track api revenue and adjust pricing"), (
            "reuse_recipe.py:2533 dispatches on 'revenue', but nothing "
            "produces that tag -- the branch is unreachable")

    def test_a_plain_goal_gets_neither(self):
        """Precision guard: the new keywords must not fire on unrelated work."""
        d = _detect()
        tags = d("Write a short bedtime story about a sleepy fox")
        assert 'finance' not in tags and 'revenue' not in tags, tags

    def test_the_real_seeded_goal_description_alone_tags_finance(self):
        """The ACTUAL seed text, description only -- not the title.

        This caught a half-vacuous first fix.  goal_seeding.py:263
        'bootstrap_finance_agent' names its four tools with UNDERSCORES
        (get_financial_health, track_revenue_split, ...), while the first
        keyword set used the space forms ('financial health').  _mentions
        matches verbatim, so the description scored NO finance tag at all;
        only the title 'Finance Agent Vijai' happened to match, meaning the
        fix worked or not depending on whether a caller passes the title.

        goal_manager.py:930-934 tells this agent "YOUR TOOLS: 1.
        get_financial_health ... 4. manage_invite_participation" -- it is
        promised four tools by name and, before this fix, given none.

        Exact tool names are also the most precise keyword available:
        measured 4 of 884 real goals (0.45%) each.
        """
        d = _detect()
        desc = (
            'Make the business self-sustaining with Vijai personality: '
            '1) Use get_financial_health to monitor platform revenue and costs, '
            '2) Use track_revenue_split to verify 90/9/1 compliance every period, '
            '3) Use assess_sustainability to determine if revenue covers '
            'infrastructure, '
            '4) Use manage_invite_participation to review private core access '
            'agreements.')
        assert 'finance' in d(desc), (
            "the seeded bootstrap_finance_agent description names all four "
            "finance tools and still produces no 'finance' tag -- the "
            "underscore forms are not matched by the space-form keywords")


class TestBroadKeywordsStayOut:
    """The three measured-too-broad terms must never be re-introduced.

    Each was counted against the real 884-goal corpus; re-adding one
    silently re-tags a fifth of every agent on the box.
    """

    @pytest.mark.parametrize("kw,pct", [("'budget'", "20.4%"),
                                        ("'sustainab'", "12.1%")])
    def test_rejected_keyword_absent(self, kw, pct):
        src = _src("integrations/agent_engine/marketing_tools.py")
        # only inspect the detector, not the whole module
        body = src[src.index("def detect_goal_tags("):]
        body = body[:body.index("def resolve_goal_tags(")]
        # COMMENTS MUST BE STRIPPED.  The rejection rationale is written in a
        # comment that necessarily NAMES 'budget' and 'sustainab', so a raw
        # substring scan fails on the very text explaining the rejection
        # (it did, first run).  What matters is whether the term is in a
        # live keyword LIST -- code, not prose.
        code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
        assert kw not in code, (
            f"{kw} was measured to tag {pct} of real goals and is rejected; "
            f"it is present in live detector code, not merely in a comment")

    def test_bare_revenue_keyword_is_not_used_alone(self):
        """Bare 'revenue' is an owner call, deliberately not taken here.

        It would tag 16.5% of agents and hand them adjust_pricing, which
        mutates pricing.  'revenue split' / 'api revenue' are the narrow
        forms used instead.
        """
        d = _detect()
        assert 'revenue' not in d("our revenue is important to the team"), (
            "bare 'revenue' matching was not part of this change -- if it "
            "is added deliberately, update this test and say why")


class TestDispatchParity:
    """finance must be dispatched wherever revenue is."""

    @pytest.mark.parametrize("leg", ["hartos/reuse_recipe.py",
                                     "hartos/create_recipe.py"])
    def test_finance_branch_exists(self, leg):
        src = _src(leg)
        assert re.search(r"if 'finance' in goal_tags:", src), (
            f"{leg} dispatches revenue but not finance, so "
            f"register_finance_tools keeps its zero production callers")

    @pytest.mark.parametrize("leg", ["hartos/reuse_recipe.py",
                                     "hartos/create_recipe.py"])
    def test_finance_registrar_is_called(self, leg):
        assert "register_finance_tools(" in _src(leg)
