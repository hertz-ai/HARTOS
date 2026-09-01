"""#743 Tier-0: reuse-MAIN core tools come from the ONE factory.

Before this migration create_agents_for_user carried 19 inline
decorator-stack registrations whose bodies had drifted from the
canonical build_core_tool_closures twins (current_app.logger in thread
contexts, mandatory start/end on get_chat_history, a sovereignty-
violating direct-minicpm get_user_camera_inp).  The time (:2383) and
visual legs already consumed the factory; the main leg now does too,
name-filtered to exactly the set it registered before (zero schema
growth).  These guards fail if an inline twin is re-introduced or the
filter drifts from the factory's names.

    python -m pytest tests/unit/test_reuse_main_core_factory.py --noconftest -q
"""
import ast
from pathlib import Path
import re
import unittest

_ROOT = Path(__file__).resolve().parents[2]
_REUSE = _ROOT / 'hartos' / 'reuse_recipe.py'

# The 19 names reuse-main registered inline before the migration.
MIGRATED = {
    'txt2img', 'img2txt', 'save_data_in_memory', 'get_saved_metadata',
    'get_data_by_key', 'get_user_id', 'get_prompt_id', 'Generate_video',
    'get_user_uploaded_file', 'get_user_camera_inp', 'get_chat_history',
    'search_visual_history', 'search_long_term_memory',
    'save_to_long_term_memory', 'create_scheduled_jobs',
    'send_message_to_user', 'send_presynthesized_video_to_user',
    'send_message_in_seconds', 'google_search',
}
# Reuse-specific tools that stay inline by design.
KEEP_INLINE = {
    'update_persona', 'send_message_to_roles', 'register_visual_watcher',
    'consult_expert', 'get_user_camera_inp_by_mins',
    'execute_windows_or_android_command', 'create_new_agent',
}


class ReuseMainCoreFactory(unittest.TestCase):

    def _defs_in_create_agents(self):
        tree = ast.parse(_REUSE.read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'create_agents_for_user')
        return {n.name for n in ast.walk(fn)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n is not fn}

    def _defs_in_module(self):
        tree = ast.parse(_REUSE.read_text(encoding='utf-8'))
        return {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_no_inline_twins_of_factory_tools(self):
        inline = self._defs_in_create_agents() & MIGRATED
        self.assertFalse(
            inline,
            f"inline twin(s) of factory core tools re-introduced: {inline}")

    def test_reuse_specific_tools_still_inline(self):
        # update_persona lives in a sibling function and
        # execute_windows_or_android_command is an async def — check the
        # whole module, their exact scope is not this guard's concern.
        missing = KEEP_INLINE - self._defs_in_module()
        self.assertFalse(missing, f"reuse-specific tools vanished: {missing}")

    def test_main_leg_registers_filtered_factory_set(self):
        src = _REUSE.read_text(encoding='utf-8')
        m = re.search(r'_MAIN_LEG_CORE\s*=\s*\{([^}]*)\}', src)
        self.assertIsNotNone(m, "_MAIN_LEG_CORE filter set missing")
        names = set(re.findall(r"'([^']+)'", m.group(1)))
        self.assertEqual(names, MIGRATED)
        self.assertIn("in _MAIN_LEG_CORE], helper, assistant)", src)

    def test_factory_send_message_to_user_keeps_agent_mention_guard(self):
        """The reuse inline version guarded against internal '@helper...'
        chatter being sent to the USER; the factory absorbs that guard in
        this migration (canonical home, both legs benefit)."""
        src = (_ROOT / 'core' / 'agent_tools.py').read_text(encoding='utf-8')
        self.assertIn('_AGENT_MENTIONS', src)
        self.assertIn('not sending to user', src)


if __name__ == '__main__':
    unittest.main()
