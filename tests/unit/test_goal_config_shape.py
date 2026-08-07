"""#624 — AgentGoal.to_dict() flattens config_json; prompt builders must cope.

THE BUG THESE PIN (observed live 2026-08-05, gui_app.log):

    AgentGoal.to_dict()  (_models_local.py:2150-2171) does
        result = {<column fields>}; result.update(self.config_json or {})
    so the returned dict has NO 'config' key and NO 'config_json' key — the
    config's keys become TOP-LEVEL keys.

    Every prompt builder read
        goal_dict.get('config', goal_dict.get('config_json', {})) or {}
    i.e. a NESTED dict that flattening had dissolved.  Result: {} every time,
    and every config.get(k, default) returned the DEFAULT.

    A self_heal goal carrying a real ModuleNotFoundError rendered
        Exception: Unknown / Module: unknown / Occurrences: 0 /
        Sample traceback: N/A
    while title and description rendered correctly — they are real columns.
    That asymmetry is the fingerprint of this bug.

    Worse than blank text: _build_self_heal_prompt branches on
        category in _BACKEND_REPAIR_CATEGORIES and backend
    so with both empty, the repair_backend_venv route was UNREACHABLE for
    every category it exists to serve, and those goals fell through to a path
    whose own docstring says it cannot repair a live broken venv.

Both real dispatch paths feed the flattened shape (agent_daemon.py:1109 and
coding_daemon.py:230 each call build_prompt(goal.to_dict())), so this
affected every goal type, not just self_heal.

DISCRIMINATION: test_self_heal_prompt_sees_flattened_config FAILS against the
pre-fix builder — it renders 'Unknown' and 0 instead of the real values.
"""
import unittest
from unittest.mock import MagicMock, patch


_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()


class GoalConfigReaderTests(unittest.TestCase):
    """_goal_config must handle all three dict shapes in this codebase."""

    def setUp(self):
        self._patcher = patch(
            'integrations.agent_engine.goal_manager.current_app',
            _fake_current_app, create=True)
        try:
            self._patcher.start()
        except AttributeError:
            self._patcher = None  # module may not import current_app

    def tearDown(self):
        if self._patcher is not None:
            try:
                self._patcher.stop()
            except RuntimeError:
                pass

    def test_nested_under_config(self):
        from integrations.agent_engine.goal_manager import _goal_config
        got = _goal_config({'title': 't', 'config': {'exc_type': 'ValueError'}})
        self.assertEqual(got, {'exc_type': 'ValueError'})

    def test_nested_under_config_json(self):
        from integrations.agent_engine.goal_manager import _goal_config
        got = _goal_config({'title': 't', 'config_json': {'exc_type': 'KeyError'}})
        self.assertEqual(got, {'exc_type': 'KeyError'})

    def test_flattened_by_to_dict(self):
        """The shape AgentGoal.to_dict() actually produces.  FAILS PRE-FIX."""
        from integrations.agent_engine.goal_manager import _goal_config
        flat = {
            'id': 7, 'goal_type': 'self_heal', 'title': 'Self-heal: x',
            'description': 'd', 'status': 'active', 'priority': 1,
            'exc_type': 'ModuleNotFoundError', 'occurrence_count': 3,
            'category': 'subprocess.tool_load',
        }
        got = _goal_config(flat)
        self.assertEqual(got['exc_type'], 'ModuleNotFoundError')
        self.assertEqual(got['occurrence_count'], 3)
        self.assertEqual(got['category'], 'subprocess.tool_load')

    def test_column_fields_never_leak_into_config(self):
        """The reason a blanket `or goal_dict` fallback is wrong: it would
        make config.get('title') return the GOAL title, silently changing
        prompts that legitimately expect a default."""
        from integrations.agent_engine.goal_manager import _goal_config
        got = _goal_config({
            'id': 1, 'title': 'GOAL TITLE', 'description': 'GOAL DESC',
            'status': 'active', 'goal_type': 'coding', 'owner_id': 2,
            'priority': 5, 'product_id': None, 'spark_budget': 0,
            'spark_spent': 0, 'created_by': 'x', 'prompt_id': None,
            'last_dispatched_at': None, 'created_at': None, 'updated_at': None,
        })
        self.assertEqual(got, {}, f"column fields leaked into config: {got}")

    def test_empty_goal_dict_is_safe(self):
        from integrations.agent_engine.goal_manager import _goal_config
        self.assertEqual(_goal_config({}), {})


class GoalColumnDriftGuard(unittest.TestCase):
    """_GOAL_COLUMNS is a hand-maintained mirror of the column list in
    AgentGoal.to_dict().  Two copies of one list is the same class of bug
    this task is about, one level up — so pin them together."""

    def test_goal_columns_match_to_dict_output(self):
        """Read AgentGoal.to_dict()'s literal dict via AST.

        Deliberately does NOT import the ORM: _models_local defines
        SQLAlchemy tables against a shared MetaData, so importing it inside a
        pytest process that already registered those models raises
        "Table 'users' is already defined for this MetaData instance".
        Parsing the source is also a stronger guard — it pins the literal
        that to_dict actually returns, with no dependence on ORM state.
        """
        import ast
        import os
        from integrations.agent_engine.goal_manager import _GOAL_COLUMNS

        here = os.path.dirname(os.path.abspath(__file__))
        models = os.path.join(
            here, '..', '..', 'integrations', 'social', '_models_local.py')
        tree = ast.parse(open(models, encoding='utf-8').read())

        cls = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == 'AgentGoal'),
                   None)
        self.assertIsNotNone(cls, "AgentGoal class not found in _models_local")

        fn = next((n for n in cls.body
                   if isinstance(n, ast.FunctionDef) and n.name == 'to_dict'),
                  None)
        self.assertIsNotNone(fn, "AgentGoal.to_dict() not found")

        literal = next((n.value for n in ast.walk(fn)
                        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)),
                       None)
        self.assertIsNotNone(literal, "no dict literal inside to_dict()")

        emitted = {k.value for k in literal.keys
                   if isinstance(k, ast.Constant) and isinstance(k.value, str)}

        self.assertEqual(
            emitted, set(_GOAL_COLUMNS),
            "AgentGoal.to_dict() column set drifted from _GOAL_COLUMNS.\n"
            f"  only in to_dict():      {sorted(emitted - set(_GOAL_COLUMNS))}\n"
            f"  only in _GOAL_COLUMNS:  {sorted(set(_GOAL_COLUMNS) - emitted)}\n"
            "Update goal_manager._GOAL_COLUMNS to match, or _goal_config will "
            "either drop real config keys or leak column fields into config.")


class SelfHealPromptTests(unittest.TestCase):
    """End-to-end on the shape the daemons actually pass."""

    def test_self_heal_prompt_sees_flattened_config(self):
        """FAILS PRE-FIX: renders 'Unknown' / 0 / 'N/A' instead of the real
        exception, because the builder looked for a nested config."""
        from integrations.agent_engine.goal_manager import (
            _build_self_heal_prompt)

        flat = {
            'id': 42, 'goal_type': 'self_heal',
            'title': 'Self-heal: subprocess.tool_load (ModuleNotFoundError)',
            'description': 'A high-severity failure escaped recovery.',
            'status': 'active', 'priority': 1,
            'exc_type': 'ModuleNotFoundError',
            'source_module': 'gpu_worker.py',
            'source_function': 'run_worker',
            'occurrence_count': 4,
            'sample_traceback': '  File "gpu_worker.py", line 1\n',
            'category': 'tts.probe',
            'context': {'backend': 'chatterbox'},
        }
        prompt = _build_self_heal_prompt(flat)

        self.assertIn('ModuleNotFoundError', prompt)
        self.assertIn('gpu_worker.py', prompt)
        self.assertIn('Occurrences: 4', prompt)
        self.assertNotIn('Exception: Unknown', prompt)
        self.assertNotIn('Occurrences: 0', prompt)

    def test_backend_repair_branch_is_now_reachable(self):
        """The real harm: with config empty, `category in
        _BACKEND_REPAIR_CATEGORIES and backend` could never be true, so
        venv failures were routed to a path that cannot fix them.
        FAILS PRE-FIX — the prompt took the generic branch."""
        from integrations.agent_engine.goal_manager import (
            _build_self_heal_prompt, _BACKEND_REPAIR_CATEGORIES)

        self.assertIn('tts.probe', _BACKEND_REPAIR_CATEGORIES)
        prompt = _build_self_heal_prompt({
            'id': 1, 'goal_type': 'self_heal', 'title': 't',
            'description': 'd', 'status': 'active',
            'exc_type': 'RuntimeError',
            'category': 'tts.probe',
            'context': {'backend': 'chatterbox'},
        })
        self.assertIn('repair_backend_venv', prompt)
        self.assertIn('chatterbox', prompt)


if __name__ == '__main__':
    unittest.main()
