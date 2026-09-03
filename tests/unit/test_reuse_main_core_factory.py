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
from types import SimpleNamespace
import unittest

_ROOT = Path(__file__).resolve().parents[2]
_REUSE = _ROOT / 'hartos' / 'reuse_recipe.py'

# The 18 names reuse-main registered inline before the migration and now
# takes from the factory.  create_scheduled_jobs is NOT here: the
# factory's same-named tool is a create-flow STUB while reuse needs live
# scheduling — a #511 name collision; it stays inline (owner audit
# 2026-09-01 caught the swap silently stubbing real scheduling).
MIGRATED = {
    'txt2img', 'img2txt', 'save_data_in_memory', 'get_saved_metadata',
    'get_data_by_key', 'get_user_id', 'get_prompt_id', 'Generate_video',
    'get_user_uploaded_file', 'get_user_camera_inp', 'get_chat_history',
    'search_visual_history', 'search_long_term_memory',
    'save_to_long_term_memory',
    'send_message_to_user', 'send_presynthesized_video_to_user',
    'send_message_in_seconds', 'google_search',
}
# Reuse-specific tools that stay inline by design.
KEEP_INLINE = {
    'update_persona', 'send_message_to_roles', 'register_visual_watcher',
    'consult_expert', 'get_user_camera_inp_by_mins',
    'execute_windows_or_android_command', 'create_new_agent',
    'create_scheduled_jobs',
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

    # ── behavioral effect guards ──────────────────────────────────────
    # These CALL the factory closures and observe the effect; a source
    # string-match would pass on dead or broken code (owner 2026-09-01:
    # "why are we creating src based tests").  Structure guards above
    # stay AST-based because source structure IS their subject.

    class _InlineThread:
        """threading.Thread stand-in that runs target synchronously."""
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}
        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    def _factory_tools(self, memory_graph=None, simplemem_store=None,
                       helper_fun=None, send1=None):
        from unittest import mock as _m
        from core.agent_tools import build_core_tool_closures
        ctx = {k: None for k in (
            'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
            'request_id_list', 'recent_file_id', 'scheduler',
            'send_message_to_user1', 'retrieve_json', 'strip_json_values',
            'save_conversation_db')}
        ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='s1',
                   request_id_list={'s1': 'r1'},
                   memory_graph=memory_graph, simplemem_store=simplemem_store,
                   helper_fun=helper_fun or _m.Mock(),
                   send_message_to_user1=send1 or _m.Mock(),
                   retrieve_json=lambda v: v)
        return {n: f for n, _, f in build_core_tool_closures(ctx)}

    def test_ltm_save_dual_writes_to_memory_graph(self):
        """Loss 1 of the owner audit: reuse's inline
        save_to_long_term_memory dual-wrote to MemoryGraph; the factory
        twin silently dropped it.  Restored — proven by CALLING it."""
        from unittest import mock
        graph = mock.Mock()

        async def _add(content, meta):
            return None
        store = SimpleNamespace(add=_add)
        tools = self._factory_tools(memory_graph=graph, simplemem_store=store)
        with mock.patch('core.agent_tools.threading.Thread', self._InlineThread):
            out = tools['save_to_long_term_memory']('the sky is teal')
        self.assertEqual(out, 'Saved to long-term memory.')
        graph.register.assert_called_once()
        content, meta = graph.register.call_args[0]
        self.assertEqual(content, 'the sky is teal')
        self.assertEqual(meta['source'], 'simplemem')

    def test_kv_save_dual_writes_and_get_recalls_on_miss(self):
        """Losses 2+3: '[KV]' dual-write on save_data_in_memory and the
        MemoryGraph recall fallback on a get_data_by_key dict miss."""
        from unittest import mock
        graph = mock.Mock()
        graph.recall.return_value = [SimpleNamespace(content='teal-from-graph')]
        helper = mock.Mock()
        helper.save_agent_data_to_file.return_value = True
        tools = self._factory_tools(memory_graph=graph, helper_fun=helper)
        with mock.patch('core.agent_tools.threading.Thread', self._InlineThread):
            tools['save_data_in_memory']('user.color', 'teal')
        graph.register.assert_called_once()
        content, meta = graph.register.call_args[0]
        self.assertTrue(content.startswith('[KV] user.color'))
        self.assertEqual(meta['kv_key'], 'user.color')
        # dict miss -> graph fallback answers
        self.assertEqual(tools['get_data_by_key']('never.stored'),
                         'teal-from-graph')
        graph.recall.assert_called_with('[KV] never.stored', mode='text', top_k=1)
        # and with NO graph in ctx the miss string is unchanged (create/
        # time/visual legs that pass memory_graph=None keep old behavior)
        bare = self._factory_tools(memory_graph=None,
                                   helper_fun=mock.Mock(**{'save_agent_data_to_file.return_value': True}))
        self.assertEqual(bare['get_data_by_key']('never.stored'),
                         'Key not found in stored data.')

    def test_send_message_to_user_blocks_agent_mentions(self):
        """The absorbed reuse guard, proven by calling: '@helper' text
        never reaches send_message_to_user1; normal text does."""
        from unittest import mock
        send1 = mock.Mock()
        tools = self._factory_tools(send1=send1)
        blocked = tools['send_message_to_user']('please ask @Helper to run it')
        self.assertIn('not sending to user', blocked)
        send1.assert_not_called()
        with mock.patch('core.agent_tools.threading.Thread', self._InlineThread):
            tools['send_message_to_user']('hello there')
        send1.assert_called_once()

    def test_reuse_inline_scheduler_is_real_not_stub(self):
        """Loss 4: the factory create_scheduled_jobs returns a deferral
        message and schedules NOTHING (create-flow semantics); reuse's
        inline version must keep doing real scheduler.add_job.  Source
        check by exception: the closure lives inside
        create_agents_for_user, whose construction needs live autogen
        agents — behavioral coverage is the live-turn gate, this pins
        the body until then."""
        src = _REUSE.read_text(encoding='utf-8')
        i = src.index('def create_scheduled_jobs')
        body = src[i:i + 1500]
        self.assertIn('scheduler.add_job', body)
        self.assertIn('CronTrigger.from_crontab', body)


if __name__ == '__main__':
    unittest.main()


class InstructionCoercion(unittest.TestCase):
    """Live 2026-09-01 15:14:35: the hive-training agent called
    execute_windows_or_android_command with a DICT instructions arg and
    the tool crashed on .lower() at the recipe matcher — before the VLM
    loop.  The coercion helper is the single normalization point."""

    def test_str_passes_through_untouched(self):
        from hartos.reuse_recipe import _coerce_instruction_text
        self.assertEqual(_coerce_instruction_text('open notepad'), 'open notepad')

    def test_nested_dict_keeps_its_text_field(self):
        from hartos.reuse_recipe import _coerce_instruction_text
        self.assertEqual(
            _coerce_instruction_text({'instructions': 'run benchmarks',
                                      'os_to_control': 'windows'}),
            'run benchmarks')
        self.assertEqual(_coerce_instruction_text({'command': 'notepad'}),
                         'notepad')

    def test_textless_dict_and_non_str_stringify_not_raise(self):
        from hartos.reuse_recipe import _coerce_instruction_text
        out = _coerce_instruction_text({'foo': 1})
        self.assertIn('"foo"', out)
        # every output survives the crash site's exact expression
        for v in ({'foo': 1}, 42, None, {'instructions': 'x'}):
            coerced = _coerce_instruction_text(v)
            ' '.join(coerced.lower().strip().split())
