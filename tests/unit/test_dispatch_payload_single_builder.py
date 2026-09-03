"""Parallel-path fix #10 (payload half): ``_dispatch_to_model`` and
``_dispatch_expert_langchain`` in ``speculative_dispatcher`` built a
CHAR-IDENTICAL inner-/chat payload inline. Extracted to one
``_build_dispatch_payload`` so the two cannot drift (a payload-field drift would
make the inner /chat route / create agents wrongly for one path only).
"""
from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher


class _FakeModel:
    def to_config_list(self):
        return [{'model': 'x'}]


def _build(prompt_id=None, goal_id=None, goal_type='general'):
    d = object.__new__(SpeculativeDispatcher)  # method uses no self attrs
    return SpeculativeDispatcher._build_dispatch_payload(
        d, _FakeModel(), 'the-prompt', 'user-1', prompt_id, goal_id, goal_type)


def test_base_payload_fields_and_no_reentry():
    p = _build()
    assert p['user_id'] == 'user-1' and p['prompt'] == 'the-prompt'
    # 0ce26b9: create_agent/autonomous follow bool(goal_id). A USER
    # conversational turn (goal_id None, this case) must reach the agent as a
    # CONVERSATION — the old hardcoded True routed a live turn into
    # creation-RESUME, which auto-completed a stale plan with zero LLM calls
    # and fabricated a completion claim (prompt_id 90916249292). This file
    # previously pinned that old behaviour; the deliberate contract lives in
    # test_expert_dispatch_mode.py and this assertion now matches it.
    assert p['create_agent'] is False and p['autonomous'] is False
    assert p['casual_conv'] is False
    assert p['model_config'] == [{'model': 'x'}]
    # hard no-reentry: the inner /chat must skip the dispatcher
    assert p['speculative'] is False and p['draft_first'] is False


def test_goal_driven_dispatch_keeps_creation_flags():
    """The other half of the 0ce26b9 contract: goal-driven daemon dispatch
    (goal_id set) IS autonomous creation work and keeps the flags."""
    p = _build(goal_id='gid')
    assert p['create_agent'] is True and p['autonomous'] is True


def test_optional_fields_only_when_meaningful():
    base = _build()
    assert 'prompt_id' not in base and 'goal_id' not in base and 'goal_type' not in base
    full = _build(prompt_id='pid', goal_id='gid', goal_type='coding')
    assert full['prompt_id'] == 'pid' and full['goal_id'] == 'gid'
    assert full['goal_type'] == 'coding'
    # goal_type 'general' is treated as "no meaningful goal_type"
    assert 'goal_type' not in _build(goal_type='general')


def test_only_the_expert_path_uses_the_chat_shaped_builder():
    """2026-08-19: _dispatch_to_model no longer builds an inner-/chat
    payload at all — it hits the draft model's /v1/chat/completions
    directly (see test_draft_first_dispatch.py::TestLoopPrevention), so it
    has no reason to call _build_dispatch_payload any more. Only
    _dispatch_expert_langchain still routes through /chat (it needs the
    full tool registry for a real answer) and must keep using the shared
    builder so its payload can't drift from this file's pinned fields."""
    import inspect
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher as _SD
    assert 'self._build_dispatch_payload(' not in inspect.getsource(
        _SD._dispatch_to_model), (
        "_dispatch_to_model must not build a /chat-shaped payload — it "
        "should call the model server directly, never round-trip through "
        "/chat (that re-enters the full HARTOS pipeline, see the 2026-08-19 "
        "duplicate-turn fix)")
    assert 'self._build_dispatch_payload(' in inspect.getsource(
        _SD._dispatch_expert_langchain), (
        "_dispatch_expert_langchain must keep using the one shared builder")
