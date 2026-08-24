"""casual_conv is a SESSION-SHAPE flag, not a per-turn classifier verdict.

Owner's design (stated 2026-08-24, reverting a same-day change): the
route sends `not bool(prompt_id or create_agent)` — anything WITHOUT a
prompt_id behaves like a casual companion conversation (draft-first,
light get_ans path), and any session WITH a prompt_id is agent-bound.
A 2026-08-24 override flipped casual_conv to False in the /chat handler
whenever the draft classifier said is_casual=False; that pushed
default-agent turns onto the heavy tool path (and, on builds 4-6, into
a get_ans crash -> bare direct-tier fallback).  The owner called it a
regression and it was reverted.

These pins keep the design from silently flipping again in either repo.

    python -m pytest tests/unit/test_casual_conv_classifier_override.py --noconftest -q
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC = open(os.path.join(ROOT, 'hart_intelligence_entry.py'),
            encoding='utf-8').read()


def test_no_classifier_override_of_casual_conv():
    """The /chat handler must not overwrite the route's casual_conv with
    the classifier verdict — session shape is decided by prompt_id."""
    m = re.search(
        r'is_casual=False, is_create_agent=False — routing to "\n(.*?)\n\s+else:',
        _SRC, re.DOTALL)
    assert m, 'non-casual fall-through branch not found'
    region = m.group(1)
    assert not re.search(r'^\s*casual_conv\s*=\s*False\s*$', region,
                         re.MULTILINE), (
        'classifier override of casual_conv reintroduced — default-agent '
        '(no-prompt_id) sessions must KEEP the casual shape; only '
        'prompt_id sessions run agent-bound (owner design, 2026-08-24)')


def test_route_hint_is_the_authority_in_nunba():
    """Pins BOTH producers of the session-shape flag (review finding #3:
    the earlier version pinned only the legacy gpt_lang helper while the
    LIVE producer is chat_route's `_needs_tools`).

    Legacy helper (gpt_lang, :486): `not bool(prompt_id or create_agent)`.
    Live producer (chat_route, ~:2979): `_needs_tools = bool(
    langchain_prompt_id or create_agent or agentic_execute or
    agentic_plan or autonomous_creation)` — where langchain_prompt_id is
    digit-gated.  The two formulas DIVERGE for non-numeric prompt_ids
    (iq_* recipes): those sessions route through the autogen reuse path
    before get_ans matters, but the divergence is an owner design
    question, tracked separately — this pin documents what IS."""
    nunba = open(os.path.join(
        os.path.dirname(ROOT), 'Nunba-HART-Companion', 'routes',
        'chatbot_routes.py'), encoding='utf-8').read()
    assert '"casual_conv": not bool(prompt_id or create_agent)' in nunba
    assert '_needs_tools = bool(langchain_prompt_id or create_agent' in nunba, (
        'live session-shape producer (chat_route _needs_tools) changed — '
        're-audit casual_conv derivation end to end')
