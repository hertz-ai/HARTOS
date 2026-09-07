"""``create_ledger_from_actions`` must be called BY KEYWORD in production.

The signature is::

    create_ledger_from_actions(agent_id=None, session_id=None, actions=None,
                               backend=None, user_id=None, prompt_id=None, ...)

so the positional form both production call sites used::

    create_ledger_from_actions(user_id, prompt_id, actions, ...)

bound ``user_id -> agent_id`` and ``prompt_id -> session_id``.  Two defects
followed, both measured live 2026-09-05 on Scout2 (prompt 77712340019, a
2-action recipe):

1. ``session_id`` was never ``None``, so the session-resolution block
   (core.py:3884 — resume an in-flight session, else mint a fresh
   ``f"{user_id}_{prompt_id}_{ts_ms}"``) could not run, and neither could
   ``resume_if_unfinished`` in either direction.  The feature was dead code
   in production and exercised only by tests, which pass keywords.

2. create_recipe.py:3938 and reuse_recipe.py:1112 used the identical
   positional form, so CREATE and REUSE both built ``SmartLedger(user_id,
   prompt_id)`` — the SAME ledger — accumulating every run's tasks forever.

Live evidence, the ``[REUSE-LEDGER]`` marker at 19:45:02::

    [REUSE-LEDGER] session=77712340019 tasks=77 actions=2

``session`` is the bare prompt_id and the "new" ledger already held 77 tasks
for a 2-action recipe.  Their terminal statuses then blocked every transition
("Cannot transition from terminal state COMPLETED", core.py:624 — a correct
invariant), so the action never advanced; daemon 43104584497 read
``current_action_id: 1`` fifty-two times across an hour.

Passing ``resume_if_unfinished=False`` is NOT the fix and is guarded against
below: with keywords it selects the legacy deterministic
``f"{user_id}_{prompt_id}"``, which is the accumulate-forever behaviour again.

    python -m pytest tests/unit/test_reuse_ledger_session_isolation.py --noconftest -q
"""
import ast
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')
_CREATE_SRC = os.path.join(_ROOT, 'hartos', 'create_recipe.py')

# (source file, enclosing function) for every production ledger construction.
_PRODUCTION_SITES = [
    (_REUSE_SRC, 'create_agents_for_user'),
    (_CREATE_SRC, 'create_action_with_ledger'),
]


def _find_func(path, name):
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
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


@pytest.mark.parametrize('path,func', _PRODUCTION_SITES,
                         ids=[f for _, f in _PRODUCTION_SITES])
class TestProductionCallsUseKeywords:

    def test_no_positional_arguments(self, path, func):
        """The defect, stated as an assertion.

        A positional call silently binds user_id to agent_id and prompt_id to
        session_id — pinning one permanent ledger per (user, prompt) and
        disabling session resolution entirely.
        """
        calls = _ledger_calls(_find_func(path, func))
        assert calls, (f'{func} no longer calls create_ledger_from_actions '
                       '— re-point this guard')
        for call in calls:
            assert not call.args, (
                f'{os.path.basename(path)}:{call.lineno} passes '
                f'{len(call.args)} positional arg(s) to '
                'create_ledger_from_actions. The signature starts '
                '(agent_id, session_id, actions), so positionally user_id '
                'becomes agent_id and prompt_id becomes SESSION_ID. Live '
                '2026-09-05: session=77712340019 tasks=77 actions=2.')

    def test_passes_user_id_and_prompt_id_by_name(self, path, func):
        for call in _ledger_calls(_find_func(path, func)):
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            missing = {'user_id', 'prompt_id', 'actions'} - kwargs
            assert not missing, (
                f'{os.path.basename(path)}:{call.lineno} must pass '
                f'{sorted(missing)} by keyword so the backwards-compat block '
                'at core.py:3884 resolves agent_id/session_id.')

    def test_does_not_pin_the_legacy_deterministic_session(self, path, func):
        """resume_if_unfinished=False re-creates the accumulate-forever bug."""
        for call in _ledger_calls(_find_func(path, func)):
            for kw in call.keywords:
                if kw.arg != 'resume_if_unfinished':
                    continue
                assert not (isinstance(kw.value, ast.Constant)
                            and kw.value.value is False), (
                    f'{os.path.basename(path)}:{call.lineno} pins '
                    'resume_if_unfinished=False, which selects the legacy '
                    'deterministic f"{user_id}_{prompt_id}" session — one '
                    'permanent ledger that accumulates every run. Leave the '
                    'default (True): resume a real in-flight session, else '
                    'mint a fresh timestamped one.')


class TestKeywordCallMintsARealSession:
    """The primitive must actually behave as the call sites now depend on."""

    def test_session_id_is_not_the_bare_prompt_id(self, tmp_path, monkeypatch):
        core = pytest.importorskip('agent_ledger.core')
        backend_mod = pytest.importorskip('agent_ledger.backends')
        InMemory = getattr(backend_mod, 'InMemoryBackend', None)
        if InMemory is None:
            pytest.skip('InMemoryBackend unavailable')

        # Isolate the ledger directory so a resumable session from the real
        # agent_data/ cannot influence this assertion either way.
        monkeypatch.chdir(tmp_path)

        user_id, prompt_id = 4242, 99887766
        actions = [{'action_id': 1, 'action': 'search', 'description': 'search'}]
        ledger = core.create_ledger_from_actions(
            user_id=user_id, prompt_id=prompt_id, actions=list(actions),
            backend=InMemory())

        assert str(ledger.session_id) != str(prompt_id), (
            'session_id is the bare prompt_id — the exact pre-fix signature '
            'logged live as "[REUSE-LEDGER] session=77712340019".')
        assert str(ledger.session_id).startswith(f'{user_id}_{prompt_id}'), (
            f'expected a minted f"{{user_id}}_{{prompt_id}}_{{ts_ms}}" session, '
            f'got {ledger.session_id!r}')
        assert str(ledger.agent_id) == str(prompt_id), (
            'agent_id must be the prompt_id — the documented convention the '
            "dashboard's prompt -> session -> flow -> action grouping reads.")
        assert len(ledger.tasks) == len(actions), (
            f'a fresh session must hold exactly one task per recipe action; '
            f'got {len(ledger.tasks)} for {len(actions)}')
