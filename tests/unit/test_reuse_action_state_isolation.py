"""REUSE must not inherit the terminal ActionStates that CREATE just wrote.

``hartos.lifecycle_hooks.action_states`` is a process-global dict keyed ONLY by
``user_prompt`` ("{user_id}_{prompt_id}").  It carries no phase dimension and no
run id, so the CREATE pipeline and the REUSE pipeline address the SAME cells.

CREATE force-terminates every action at its flow boundary
(``create_recipe.py`` ~4916-4928, "[FLOW-COMPLETE] Forcing action N ...").  When
REUSE for that agent then runs IN THE SAME PROCESS, every action already reads
TERMINATED, so ``get_agent_response``'s ``[AUTO-ADVANCE] action N already
terminated, advancing`` walks the whole flow without executing anything.

Measured live 2026-09-05 on agent 90210554431 ("Beacon", 4 actions), same
agent / same recipe / same request, the only variable being a process restart:

    same process as CREATE   ->  4x [AUTO-ADVANCE], action id 1 -> 5 in 14 ms,
                                 0 tool calls, 0 tool results
    fresh process            ->  0x [AUTO-ADVANCE], action id stays 1,
                                 google_search executed and returned real
                                 results from github.com/ggml-org/llama.cpp

A restart is not a fix -- the daemon flywheel dispatches CREATE and REUSE in one
long-lived process, which is exactly the broken case.  REUSE must start its own
run from a clean slate.

    python -m pytest tests/unit/test_reuse_action_state_isolation.py --noconftest -q
"""
import ast
import os

import pytest

from hartos.lifecycle_hooks import (
    ActionState,
    action_states,
    force_state_through_valid_path,
    get_action_state,
)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')

_UP = 'testuser_90210554431'
_OTHER = 'testuser_11111111111'


@pytest.fixture(autouse=True)
def _clean_state():
    """Never leak test state into the process-global dict."""
    action_states.pop(_UP, None)
    action_states.pop(_OTHER, None)
    yield
    action_states.pop(_UP, None)
    action_states.pop(_OTHER, None)


def _terminate_all(user_prompt, n):
    """Reproduce what CREATE's flow-complete loop leaves behind."""
    for aid in range(1, n + 1):
        force_state_through_valid_path(
            user_prompt, aid, ActionState.TERMINATED, 'test: create flow complete')


class TestClearActionStates:

    def test_helper_exists_on_the_module_that_owns_the_dict(self):
        """The clear must live beside `action_states`, not in a caller.

        `set_action_state` is the dict's only writer and `get_action_state` its
        only reader; a second module reaching in would be a third accessor and a
        parallel path (Gate 4).
        """
        from hartos import lifecycle_hooks
        assert hasattr(lifecycle_hooks, 'clear_action_states'), (
            'lifecycle_hooks must expose clear_action_states(user_prompt) — the '
            'module that owns action_states is the only correct home for it.')

    def test_clearing_restores_the_default_assigned_state(self):
        """This is the defect, stated as an assertion.

        After CREATE terminates every action, REUSE reads TERMINATED and
        auto-advances past work it never did.  Clearing must put the session
        back to the ASSIGNED default `get_action_state` returns on a miss.
        """
        from hartos.lifecycle_hooks import clear_action_states
        _terminate_all(_UP, 4)
        assert get_action_state(_UP, 1) is ActionState.TERMINATED
        assert get_action_state(_UP, 4) is ActionState.TERMINATED

        clear_action_states(_UP)

        for aid in range(1, 5):
            assert get_action_state(_UP, aid) is ActionState.ASSIGNED, (
                f'action {aid} still reads a stale terminal state — REUSE will '
                f'[AUTO-ADVANCE] past it without executing its tool.')

    def test_clearing_is_scoped_to_one_session(self):
        """Never touch another agent's or another user's run."""
        from hartos.lifecycle_hooks import clear_action_states
        _terminate_all(_UP, 2)
        _terminate_all(_OTHER, 2)

        clear_action_states(_UP)

        assert get_action_state(_OTHER, 1) is ActionState.TERMINATED
        assert get_action_state(_OTHER, 2) is ActionState.TERMINATED

    def test_clearing_an_unknown_session_is_a_no_op(self):
        """First-ever REUSE for an agent has no entry — must not raise."""
        from hartos.lifecycle_hooks import clear_action_states
        clear_action_states('nobody_00000000000')  # must not raise


class TestReuseWiring:
    """The helper is worthless unless REUSE actually calls it."""

    def _reuse_tree(self):
        return ast.parse(open(_REUSE_SRC, encoding='utf-8').read())

    def _create_agents_for_user(self):
        for node in ast.walk(self._reuse_tree()):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == 'create_agents_for_user'):
                return node
        pytest.fail('create_agents_for_user not found — re-point this guard')

    def test_reuse_clears_state_when_it_builds_its_action_object(self):
        """`create_agents_for_user` is the ONE place REUSE builds its Action
        (`user_tasks[user_prompt] = Action(role_actions)`), so it is the one
        place that must start the run clean."""
        fn = self._create_agents_for_user()
        called = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, 'attr', '')
            for n in ast.walk(fn) if isinstance(n, ast.Call)
        }
        assert 'clear_action_states' in called, (
            'create_agents_for_user must clear this session\'s action states '
            'before REUSE starts, or it inherits CREATE\'s TERMINATED states '
            'and auto-advances past every action (live 2026-09-05, Beacon '
            '90210554431: 4 actions skipped in 14 ms, 0 tools executed).')

    def test_reuse_does_not_touch_the_dict_directly(self):
        """Gate 4: one owner.  REUSE must go through the helper, never reach
        into `action_states` itself."""
        src = open(_REUSE_SRC, encoding='utf-8').read()
        assert 'action_states[' not in src, (
            'reuse_recipe must not index action_states directly — '
            'lifecycle_hooks owns that dict.')
