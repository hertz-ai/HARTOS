"""A REUSE run must not inherit the ledger session CREATE just finished.

Sibling of ``test_reuse_action_state_isolation.py``.  That file fixed ONE of
two mirrored stores; this one fixes the other, and the pair is the whole bug.

``action_states`` (in-memory) and the ``agent_ledger`` (persisted) both hold a
per-action status, and CREATE leaves BOTH terminal when it finishes a flow.
``clear_action_states`` resets the first, so a REUSE run correctly *starts*
action 1 — but nothing reset the second, so the run could never *finish* it:

    agent_ledger/core.py:624
        if TaskStatus.is_terminal_state(self.status):
            logger.warning(f"Cannot transition from terminal state ...")
            return False

Measured live 2026-09-05, Scout2 (prompt 77712340019, 2 actions), 18:47:01:

    Added subtask 1.1 / 1.2 / 1.3
    WARNING - Cannot transition from terminal state
              TaskStatus.COMPLETED to TaskStatus.BLOCKED
    Saved 77 tasks to ledger          <- 77, for a 2-action recipe

77 tasks for a 2-action recipe is the tell: the "new" reuse ledger was not new.
``create_ledger_from_actions`` defaults to ``resume_if_unfinished=True``, which
scans agent_data/ and ATTACHES to any session for this (user_id, prompt_id)
still holding a non-terminal task — dragging CREATE's already-COMPLETED
action 1 into the reuse run.  The action then never advances: the daemon agent
43104584497 read ``current_action_id: 1`` fifty-two times across an hour and
never reached action 2.

The parameter already exists for exactly this case, so the fix is to pass it,
not to weaken the terminal-state invariant (which is correct: terminal means
terminal).  CREATE keeps the default — resuming its own in-flight build is a
real feature there ("[RESUME] Resumed at Flow 3, Action 6").

    python -m pytest tests/unit/test_reuse_ledger_session_isolation.py --noconftest -q
"""
import ast
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')
_CREATE_SRC = os.path.join(_ROOT, 'hartos', 'create_recipe.py')


def _tree(path):
    with open(path, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _find_func(path, name):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f'{name} not found in {path} — re-point this guard')


def _ledger_calls(fn):
    """Every create_ledger_from_actions() call inside *fn*."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        fname = (node.func.id if isinstance(node.func, ast.Name)
                 else getattr(node.func, 'attr', ''))
        if fname == 'create_ledger_from_actions':
            out.append(node)
    return out


class TestReuseMintsItsOwnSession:

    def test_reuse_does_not_resume_an_unfinished_session(self):
        """The defect, stated as an assertion.

        Without ``resume_if_unfinished=False`` the reuse run attaches to
        CREATE's session and inherits its terminal action states, so action 1
        can never leave COMPLETED and the agent wedges on action 1 forever.
        """
        fn = _find_func(_REUSE_SRC, 'create_agents_for_user')
        calls = _ledger_calls(fn)
        assert calls, ('create_agents_for_user no longer calls '
                       'create_ledger_from_actions — re-point this guard')

        for call in calls:
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            assert 'resume_if_unfinished' in kwargs, (
                'REUSE must pass resume_if_unfinished=False so it mints its own '
                'session instead of attaching to the one CREATE just finished. '
                'Live 2026-09-05 Scout2: the "new" reuse ledger came back with '
                '77 tasks and action 1 already COMPLETED, so the ledger refused '
                'every transition and the action never advanced.')
            node = kwargs['resume_if_unfinished']
            assert isinstance(node, ast.Constant) and node.value is False, (
                'resume_if_unfinished must be the literal False for REUSE')

    def test_create_still_resumes_its_own_inflight_build(self):
        """Guard the other half: CREATE must NOT be changed.

        CREATE resuming its own unfinished flow is a real feature
        ("[RESUME] Resumed at Flow 3, Action 6"); only REUSE must start fresh.
        Passing resume_if_unfinished=False there would break mid-build resume.
        """
        fn = _find_func(_CREATE_SRC, 'create_action_with_ledger')
        for call in _ledger_calls(fn):
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            node = kwargs.get('resume_if_unfinished')
            if node is not None:
                assert not (isinstance(node, ast.Constant)
                            and node.value is False), (
                    'CREATE must keep resume_if_unfinished=True (the default) — '
                    'it legitimately resumes its own in-flight build.')


class TestLedgerPrimitiveHonoursTheFlag:
    """The flag must actually do what REUSE will now depend on."""

    def test_resume_disabled_mints_a_distinct_session(self, tmp_path):
        core = pytest.importorskip('agent_ledger.core')
        create = core.create_ledger_from_actions
        backend_mod = pytest.importorskip('agent_ledger.backends')
        InMemory = getattr(backend_mod, 'InMemoryBackend', None)
        if InMemory is None:
            pytest.skip('InMemoryBackend unavailable')

        actions = [{'action_id': 1, 'action': 'search', 'description': 'search'}]
        a = create(user_id=4242, prompt_id=99, actions=list(actions),
                   backend=InMemory(), resume_if_unfinished=False)
        b = create(user_id=4242, prompt_id=99, actions=list(actions),
                   backend=InMemory(), resume_if_unfinished=False)

        assert a.session_id != b.session_id, (
            'resume_if_unfinished=False must mint a fresh session each run; '
            'two runs sharing a session_id is exactly how a reuse run inherits '
            "the previous run's terminal task states.")
