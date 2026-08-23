"""#689 / P3 — the classifier's not-casual verdict must govern tool access.

get_ans's own comment (hie:7369) declares "casual_conv is the draft
classifier's `is_casual` flag propagated" — but the route actually sends
a STRUCTURAL guess: chatbot_routes.py:486 `not bool(prompt_id or
create_agent)`, i.e. every default-agent turn arrives casual_conv=True.
casual_conv=True strips ALL langchain tools AND routes CustomGPT to the
tool-less 0.8B draft.

Live 2026-08-24 00:28 (installed build, val-p1v3): "What agents do you
have available?" dispatched casual=True; three seconds later the draft
classifier evaluated the SAME text as is_casual=False — and the stale
route flag won.  List_Agents (registered, byte-verified in the bundle)
was never attached, and the model fabricated "I don't have specific
agents".  The same mechanism starved P7's five live-data turns of
google_search (#689): delegate='hive' turns answered by the 0.8B with
zero tools.

Fix under test: in the classifier's is_casual=False fall-through (the
branch that routes to get_ans), a ONE-WAY override sets casual_conv =
False.  One decision point, restoring the declared contract; a prompt_id
agent chat (route sends casual_conv=False) is never upgraded to casual.

Source pins (importing hart_intelligence_entry needs the full app env).

    python -m pytest tests/unit/test_casual_conv_classifier_override.py --noconftest -q
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC = open(os.path.join(ROOT, 'hart_intelligence_entry.py'),
            encoding='utf-8').read()


def _noncasual_fallthrough_region():
    """The branch that logs 'is_casual=False ... routing to the langchain
    chat (get_ans)' — from that log call to the next `else:` at the same
    nesting (the draft-reply return branch)."""
    m = re.search(
        r'is_casual=False, is_create_agent=False — routing to "\n(.*?)\n\s+else:',
        _SRC, re.DOTALL)
    assert m, 'non-casual fall-through branch not found'
    return m.group(1)


def test_noncasual_fallthrough_overrides_casual_conv():
    region = _noncasual_fallthrough_region()
    assert re.search(r'casual_conv\s*=\s*False', region), (
        "the classifier just proved this turn is NOT casual, but the route's "
        "structural casual_conv=True still reaches get_ans — tools stripped, "
        "0.8B answers a work turn (live: fabricated 'I don't have agents')")
    assert 'if casual_conv' in region, (
        'override must be one-way: only flip True->False; never upgrade a '
        'prompt_id agent chat to casual')


def test_get_ans_contract_comment_still_declares_classifier_flag():
    """hie:7369's declared contract is the spec this fix restores — keep it."""
    assert "casual_conv is the draft classifier's `is_casual` flag" in _SRC


def test_route_hint_unchanged_in_nunba():
    """chatbot_routes:486 stays the pre-classification HINT (one producer);
    the reconciliation lives at hie's single decision point, not a second
    heuristic in the route."""
    nunba = open(os.path.join(
        os.path.dirname(ROOT), 'Nunba-HART-Companion', 'routes',
        'chatbot_routes.py'), encoding='utf-8').read()
    assert '"casual_conv": not bool(prompt_id or create_agent)' in nunba
