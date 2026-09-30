"""Guard: goal tags must not be assigned from mid-word substring hits.

``detect_goal_tags`` decides which Tier-2 tool packs a new agent gets
(``create_recipe.py:2047-2084``: marketing / coding / ip_protection /
self_build / outreach / sales / news).  It matched with a plain
``keyword in prompt.lower()``, so a keyword fired anywhere inside a
longer word.

Measured 2026-09-07 over the 705 saved agent goals in
``~/Documents/Nunba/data/prompts/*.json``:

  * ``'ad '``  matched inside "re[ad ]engagement", "lo[ad ]", "spre[ad ]"
    -> 282 of 705 goals tagged **marketing** that are not marketing.
  * ``'sing'`` matched inside "u[sing]", "proces[sing]"
    -> 46 goals tagged **media**.
  * ``'repo'`` matched inside "re[po]rt"
    -> 47 goals tagged **coding**.

361 spurious tag assignments in total.  The cost is not cosmetic: the
tag set decides which tools the create-phase agent is even offered, so
a mis-tagged goal is authored against the wrong toolset.  Live case:
agent 60834540771, goal "Generate and post the weekly Spark economy
recap report to the user's feed", was tagged ``['coding']`` because
"report" contains "repo" -- so ``register_marketing_tools`` never ran
and ``create_social_post`` (the HART-feed posting tool,
``marketing_tools.py:141``) was never offered.  Its saved recipe binds
the "post it to the user's personal feed" action to
``execute_windows_or_android_command`` instead, and no such post exists
in the feed.

The fix anchors matching at a WORD START only.  It deliberately does
NOT anchor the word end, because several keywords are intentional
stems -- 'market' must still match "marketing", 'advertis' must still
match "advertising".  Keywords are used verbatim, so the trailing space
the author already relies on in 'ad ' / 'ads ' / 'contact ' keeps
acting as their word end.  ``'repo'`` gains the same trailing space,
which keeps "clone the repo and" while rejecting "report";
``'repository'`` is separately listed and still matches.

These are functional tests: they call the real ``detect_goal_tags`` and
assert on the tags it returns.  Tests A-C are the defect (red before the
fix); D-H are the non-regression half -- they pin that anchoring the
start must not break the stems or the deliberately-spaced keywords, and
they pass both before and after.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.agent_engine.marketing_tools import detect_goal_tags  # noqa: E402


# --------------------------------------------------------------------
# A-C: the defect.  Each of these is a real in-word substring hit.
# --------------------------------------------------------------------

def test_a_report_does_not_make_a_goal_a_coding_goal():
    """The load-bearing case: live agent 60834540771's actual goal."""
    goal = ("Generate and post the weekly Spark economy recap report "
            "to the user's feed")
    tags = detect_goal_tags(goal)
    assert 'coding' not in tags, (
        "'repo' matched inside the word 'report', so a feed-posting goal "
        "was handed the CODING tool pack and never the marketing pack that "
        "carries create_social_post.  got %r" % (tags,))


def test_b_read_does_not_make_a_goal_a_marketing_goal():
    """'ad ' fired inside 're[ad ]' -- 282 of 705 saved goals.

    The sentence is chosen to contain NO genuine marketing keyword.  An
    earlier draft used "track read engagement", which legitimately
    matches the real keyword 'engagement' and so would have failed for a
    correct reason -- a test asserting the fix should do something it
    must not do.
    """
    tags = detect_goal_tags("Track how many people read the docs")
    assert 'marketing' not in tags, (
        "'ad ' matched inside 'read ', tagging a docs-metrics goal as "
        "marketing.  got %r" % (tags,))


def test_c_using_does_not_make_a_goal_a_media_goal():
    """'sing' fired inside 'u[sing]' -- 46 of 705 saved goals."""
    tags = detect_goal_tags("Summarize the transcript using an open model")
    assert 'media' not in tags, (
        "'sing' matched inside 'using', tagging a summarization goal as "
        "media.  got %r" % (tags,))


# --------------------------------------------------------------------
# D-H: non-regression.  Anchoring the START must keep every intended
# match working.  These pass before AND after the fix by design: they
# exist so the fix cannot be "make everything stop matching".
# --------------------------------------------------------------------

def test_d_marketing_stem_still_matches():
    """'market' is a deliberate stem for 'marketing' / 'markets'."""
    tags = detect_goal_tags("Plan a marketing campaign for the launch")
    assert 'marketing' in tags, (
        'anchoring the word start must not break the intended stems.  '
        'got %r' % (tags,))


def test_e_advertising_stem_still_matches():
    """'advertis' is a deliberate stem for 'advertising'."""
    tags = detect_goal_tags("Review the advertising spend this quarter")
    assert 'marketing' in tags, (
        "the 'advertis' stem must still match 'advertising'.  got %r"
        % (tags,))


def test_f_a_real_ad_still_matches():
    """'ad ' at a word start is the case the keyword was added for."""
    tags = detect_goal_tags("Buy an ad slot for the launch")
    assert 'marketing' in tags, (
        "'ad ' must still match a real standalone 'ad'.  got %r" % (tags,))


def test_g_bare_repo_still_matches():
    """'clone the repo and ...' must still read as a coding goal."""
    tags = detect_goal_tags("Clone the repo and run the tests")
    assert 'coding' in tags, (
        "giving 'repo' a trailing space must keep the bare word working.  "
        'got %r' % (tags,))


def test_h_repository_still_matches():
    tags = detect_goal_tags("Check the repository for open pull requests")
    assert 'coding' in tags, (
        "'repository' is separately listed and must still match.  got %r"
        % (tags,))
