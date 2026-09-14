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
        # RE-POINTED 2026-09-08.  This guard searched reuse_recipe.py for a
        # local `_MAIN_LEG_CORE = {...}`, but that set was deliberately MOVED to
        # core/agent_tools.py as MAIN_LEG_CORE_TOOLS, "beside
        # build_core_tool_closures() that produces the closures, so the two legs
        # agree by construction" (its own comment).  reuse_recipe.py has ZERO
        # mentions of _MAIN_LEG_CORE at HEAD and in the working tree, so the
        # guard could only ever fail — it was watching a symbol that no longer
        # exists, not detecting drift.  Assert the invariant at its real home,
        # and that the main leg still registers the FILTERED slice.
        from core.agent_tools import MAIN_LEG_CORE_TOOLS
        # SUBSET, not equality (relaxed 2026-09-10).  The invariant this guard
        # exists for is that the _MAIN_LEG_CORE -> MAIN_LEG_CORE_TOOLS migration
        # LOST NOTHING; an assertEqual also forbids ever ADDING a main-leg tool,
        # which is a feature, not drift.  It fired on the book-navigation tools
        # (integrations/learning/book_tools.py) — a legitimate new capability,
        # not a regression of the migration.  Dropping a migrated name still
        # fails here, which is the case that actually breaks REUSE.
        self.assertLessEqual(MIGRATED, set(MAIN_LEG_CORE_TOOLS),
                             "a MIGRATED tool was dropped from "
                             "MAIN_LEG_CORE_TOOLS — the main leg would silently "
                             "stop registering it")
        src = _REUSE.read_text(encoding='utf-8')
        self.assertIn("register_core_tools(main_leg_core_tools(core_tools), "
                      "helper, assistant", src,
                      "main leg must register the FILTERED factory slice — "
                      "registering the full list would put every closure, "
                      "including ones no action asked for, on every agent")

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
                       helper_fun=None, send1=None, agent_data=None):
        from unittest import mock as _m
        from core.agent_tools import build_core_tool_closures
        ctx = {k: None for k in (
            'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
            'request_id_list', 'recent_file_id', 'scheduler',
            'send_message_to_user1', 'retrieve_json', 'strip_json_values',
            'save_conversation_db')}
        ctx.update(user_id=1, prompt_id='p1',
                   agent_data=agent_data if agent_data is not None else {},
                   user_prompt='s1',
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

    def test_get_through_a_non_dict_is_a_miss_not_an_exception(self):
        """#98, central 2026-09-13: a hive reuse turn asked for a nested key
        under a None value, and get_data_by_key raised TypeError out of the
        tool instead of falling back the way a missing key does."""
        from unittest import mock
        stored = {'p1': {'user': None, 'name': 'Ada', 'tags': ['a', 'b']}}
        bare = self._factory_tools(memory_graph=None, agent_data=stored)
        for path in ('user.color', 'name.first', 'tags.first'):
            self.assertEqual(bare['get_data_by_key'](path),
                             'Key not found in stored data.', path)
        graph = mock.Mock()
        graph.recall.return_value = [SimpleNamespace(content='teal-from-graph')]
        tools = self._factory_tools(memory_graph=graph, agent_data=stored)
        self.assertEqual(tools['get_data_by_key']('user.color'),
                         'teal-from-graph')
        graph.recall.assert_called_with('[KV] user.color', mode='text', top_k=1)

    # ── #104: a tool result cannot carry a whole store ────────────────
    # Live on central 2026-09-14: save_data_in_memory returned the agent's
    # whole data store on every call, the group chat wrote each return back
    # as a memory, and search_long_term_memory joined such rows into one
    # 3,386,616-char result. Every call to the hosted model was then a bare
    # 400 until the loop-break marked the action done.

    def _big_store(self):
        return {'p1': {'hive': {'history': ['cycle %d steady' % i
                                            for i in range(20000)]}}}

    def test_save_reports_the_save_not_the_store(self):
        from unittest import mock
        from core.constants import TOOL_OBSERVATION_MAX_CHARS
        helper = mock.Mock(**{'save_agent_data_to_file.return_value': True})
        tools = self._factory_tools(helper_fun=helper,
                                    agent_data=self._big_store())
        self.assertEqual(tools['save_data_in_memory']('user.color', 'teal'),
                         'Saved at user.color: "teal"')
        big_value = tools['save_data_in_memory']('hive.note', 'x' * 50000)
        # The save check reads the value back whole; a paged read-back would
        # make every long save report a failure.
        self.assertTrue(big_value.startswith('Saved at hive.note'), big_value[:80])
        self.assertLessEqual(len(big_value), TOOL_OBSERVATION_MAX_CHARS + 100)

    def test_a_long_value_is_read_a_page_at_a_time(self):
        """#104 review: a long saved string has no narrower key, so the reply
        is one page and names the offset of the next; the pages add up to the
        whole value."""
        from core.constants import TOOL_OBSERVATION_MAX_CHARS as page
        value = ''.join(chr(97 + i % 26) for i in range(2 * page + 500))
        tools = self._factory_tools(
            memory_graph=None, agent_data={'p1': {'notes': {'long': value}}})
        read = tools['get_data_by_key']
        first = read('notes.long')
        self.assertIn(f'offset={page}', first)
        second = read('notes.long', offset=page)
        self.assertIn(f'offset={2 * page}', second)
        last = read('notes.long', offset=2 * page)
        self.assertNotIn('offset=', last)
        pages = [first.split('\n...[')[0], second.split('\n...[')[0], last]
        self.assertEqual(''.join(pages), value)
        small = self._factory_tools(
            memory_graph=None, agent_data={'p1': {'user': {'color': 'teal'}}})
        self.assertEqual(small['get_data_by_key']('user.color'), 'teal')

    def test_graph_recall_is_bounded_and_skips_rows_from_before_the_cap(self):
        from unittest import mock
        from core.constants import (MEMORY_ITEM_MAX_CHARS,
                                    TOOL_OBSERVATION_MAX_CHARS)
        legacy = "{'hive': {'scheduler': " + 'x' * MEMORY_ITEM_MAX_CHARS
        graph = mock.Mock()
        graph.recall.return_value = [
            SimpleNamespace(content=legacy),
            SimpleNamespace(content='threat pattern A'),
            SimpleNamespace(content='y' * (2 * TOOL_OBSERVATION_MAX_CHARS))]
        tools = self._factory_tools(memory_graph=graph)
        out = tools['search_long_term_memory']('prior threat patterns')
        self.assertTrue(out.startswith('threat pattern A'), out[:80])
        self.assertNotIn("{'hive'", out)
        self.assertLessEqual(len(out), TOOL_OBSERVATION_MAX_CHARS + 50)

    def test_simplemem_recall_cuts_its_answer_rather_than_skipping_it(self):
        """SimpleMem returns an answer, not a stored row, so a long one is cut
        to the observation budget; skipping it made the tool report 'No
        relevant memories found' (#104 review)."""
        from core.constants import MEMORY_ITEM_MAX_CHARS, TOOL_OBSERVATION_MAX_CHARS
        answer = 'The prior threat patterns were ' + 'y' * MEMORY_ITEM_MAX_CHARS

        async def _search(query):
            return [SimpleNamespace(content=answer),
                    SimpleNamespace(content='fact B')]

        async def _add(content, meta):
            return None
        tools = self._factory_tools(
            simplemem_store=SimpleNamespace(search=_search, add=_add))
        out = tools['search_long_term_memory']('q')
        self.assertTrue(out.startswith('The prior threat patterns were'), out[:60])
        self.assertLessEqual(len(out), TOOL_OBSERVATION_MAX_CHARS)

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
