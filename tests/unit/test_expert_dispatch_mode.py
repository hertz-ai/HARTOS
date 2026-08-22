"""#684 — the expert leg must CONVERSE with an agent, not re-CREATE it.

Live 2026-08-22 13:30 (user a1dd9b6c, prompt_id 90916249292, completed
agent): the draft fabricated "BLUEFIN is saved and confirmed"; the expert
background task then posted the inner /chat with the hardcoded
``create_agent: True, autonomous: True`` payload, which routed the turn
into creation-RESUME — the machinery auto-completed the agent's OLD plan
("resumed - action complete" on 4 stale actions) without one LLM call,
returned a 34-char stub, and the user's request was never executed
(recall("BLUEFIN") → count 0).

Rule under test: ``_build_dispatch_payload`` already receives ``goal_id``
— the semantic discriminator.  Goal-driven daemon dispatch (goal_id set)
IS autonomous creation work and keeps the flags.  A user conversational
turn (goal_id None) must go to the agent as a conversation.

Second seam: when the turn is agent-bound and delegated, the draft's
in-band reply is a verified-signal anti-pattern (it claims completion
while the expert has not run).  The ACTIONABLE_INTENT branch already
swaps in ``_REFUSAL_STANDBY_REPLY``; the agent-bound delegation must do
the same.  Source-level guard below pins that assignment.

    python -m pytest tests/unit/test_expert_dispatch_mode.py --noconftest -q
"""
import re
from pathlib import Path

from integrations.agent_engine.speculative_dispatcher import (
    SpeculativeDispatcher,
)

_SRC = (Path(__file__).resolve().parents[2] /
        'integrations' / 'agent_engine' /
        'speculative_dispatcher.py').read_text(encoding='utf-8')


class _ModelStub:
    model_id = 'stub-4b'

    def to_config_list(self):
        return [{'model': 'stub-4b'}]


def _payload(goal_id):
    # _build_dispatch_payload reads only its arguments — no self state —
    # so it is called unbound with a None receiver on purpose.
    return SpeculativeDispatcher._build_dispatch_payload(
        None, _ModelStub(), 'save codename BLUEFIN to memory',
        'user-1', '90916249292', goal_id, 'general')


def test_user_turn_payload_is_conversational():
    p = _payload(goal_id=None)
    assert p['create_agent'] is False, (
        "a user conversational turn must not re-enter agent CREATION — "
        "create_agent:True routed the 13:30 live turn into creation-RESUME "
        "which auto-completed stale actions and dropped the request")
    assert p['autonomous'] is False


def test_goal_turn_payload_stays_autonomous():
    p = _payload(goal_id='goal-abc')
    assert p['create_agent'] is True, (
        "goal-driven daemon dispatch is autonomous creation work by design")
    assert p['autonomous'] is True
    assert p['goal_id'] == 'goal-abc'


def test_no_reentry_flags_unchanged():
    for gid in (None, 'goal-abc'):
        p = _payload(gid)
        assert p['speculative'] is False
        assert p['draft_first'] is False
        assert p['casual_conv'] is False


def test_agent_bound_delegation_swaps_standby_reply():
    """The agent-bound escalation block must replace the draft's reply
    with the standby, exactly as the ACTIONABLE_INTENT block does.
    Pinned at source level: within the ``agent_bound`` escalation branch
    (the one assigning ``EscalationReason.AGENT_BOUND``), a
    ``draft_reply = _REFUSAL_STANDBY_REPLY`` assignment must appear."""
    m = re.search(
        r"if delegate == 'none' and agent_bound:.*?"
        r"escalation_reason = EscalationReason\.AGENT_BOUND",
        _SRC, re.DOTALL)
    assert m, "agent_bound escalation branch not found"
    assert 'draft_reply = _REFUSAL_STANDBY_REPLY' in m.group(0), (
        "agent-bound escalation keeps the draft's in-band reply — the "
        "live 13:30 turn shipped 'saved and confirmed' as the visible "
        "answer while nothing had run; swap the standby like "
        "ACTIONABLE_INTENT does")
