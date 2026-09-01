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


def test_completed_agent_classifier_flags_conversation():
    """Live 2026-08-23 09:42 (user validate-0823, agent 90916249292,
    status 'completed'): the classifier's all-recipes-exist branch called
    set_flags_to_enter_review_mode, which — despite logging "Going to
    reuse" — set review=True/convo=False.  Phase 2 then routed the turn
    into recipe(), which short-circuited 'Agent Already Created
    Successfully' (34 chars): raw stub shipped as the reply, TTS'd 3x,
    'resumed - action complete' churned 16x, and the user's message was
    never executed.  A completed agent's turn must be flagged as
    CONVERSATION.  The function is renamed to say what it does."""
    import hart_intelligence_entry as hie
    assert not hasattr(hie, 'set_flags_to_enter_review_mode'), (
        "old misnomer still exists — rename, don't fork (parallel path)")
    create_agent = hie.set_flags_to_enter_reuse_mode('0', 'val-user', 'val-prompt')
    assert create_agent is False
    assert hie.review_agents['val-user_val-prompt'] is False, (
        "review=True on a completed agent routes Phase 2 into recipe() "
        "and eats the user's message (live 09:42 turn)")
    assert hie.conversation_agent['val-user_val-prompt'] is True


def test_review_phase_wraps_already_created():
    """Defense in depth at the Phase-2 recipe() handler: a genuine
    mid-creation resume that turns out already complete must be treated
    like completion — not fall through to the generic return that ships
    the raw 34-char stub with Agent_status='Review Mode' forever."""
    hie_src = (Path(__file__).resolve().parents[2] /
               'hart_intelligence_entry.py').read_text(encoding='utf-8')
    m = re.search(r'# Phase 2: Review Phase.*?# Phase 3: Evaluation Phase',
                  hie_src, re.DOTALL)
    assert m, "Phase 2 block not found"
    assert "'Agent Already Created Successfully'" in m.group(0), (
        "Phase-2 handler special-cases only 'Agent Created Successfully' — "
        "the Already variant falls through and ships the raw stub")


def test_classifier_prompt_bans_live_data_none():
    """Live 2026-08-23 09:41 weather turn: draft answered 'hot and humid,
    monsoon clouds' with delegate=none @ confidence 0.95 — a fabricated
    forecast as the FINAL answer (no expert leg).  The prompt's criteria
    block bans live data, but the trailing delegate summary says 'factual
    questions you can fully answer yourself' — the door the 0.8B took.
    The delegate summary must name live/current data as never-none."""
    assert 'Never \\"none\\" for live/current data' in _SRC, (
        "delegate summary lacks the live-data ban — the criteria block "
        "and the summary disagree, and the draft follows the summary")


def test_payload_carries_originating_rid_from_thread_local():
    """#750: the inner /chat runs on a FRESH handler thread — only the
    payload can carry the rid (hie:9099 reads it).  Without it every
    inner reuse send ran request_id='' -> background -> the CLOSABLE bg
    client -> closed by the turn's own foreground edge -> RuntimeError
    'client has been closed' (3x live 2026-09-01)."""
    from hartos.threadlocal import thread_local_data as tl
    tl.set_request_id(request_id='user-rid-750')
    try:
        p = _payload(goal_id=None)
        assert p.get('request_id') == 'user-rid-750'
    finally:
        tl.set_request_id(request_id='')


def test_payload_omits_rid_when_thread_local_empty():
    from hartos.threadlocal import thread_local_data as tl
    tl.set_request_id(request_id='')
    p = _payload(goal_id=None)
    assert 'request_id' not in p
