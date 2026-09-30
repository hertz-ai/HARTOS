"""execute_coding_task logs why it failed, with the traceback.

Measured live (gui_app.log.2, 2026-09-25 21:05:52): the tool returned
"Coding task execution error: attempt to write a readonly database" from a
bare `except Exception` and logged nothing, so the fault was visible only as
the model's prose ("a database error").  The returned string stays the same;
the failure now also reaches the log.
"""
import asyncio
import logging
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestExecuteCodingTaskLogsFailure:
    def _tool(self):
        from core.agent_tools import build_core_tool_closures
        ctx = {
            'user_id': '999', 'prompt_id': '8888', 'agent_data': {},
            'helper_fun': MagicMock(), 'user_prompt': '999_8888',
            'request_id_list': {'999_8888': 'req1'}, 'recent_file_id': {},
            'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
            'retrieve_json': MagicMock(return_value={}),
            'strip_json_values': MagicMock(return_value=''),
            'save_conversation_db': MagicMock(return_value='1'),
        }
        tools = {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
        return tools['execute_coding_task']

    def test_failure_is_logged_with_traceback(self, caplog):
        fn = self._tool()
        orch = MagicMock()
        orch.execute.side_effect = RuntimeError('boom from orchestrator')
        with patch('integrations.coding_agent.orchestrator.get_coding_orchestrator',
                   return_value=orch), \
             caplog.at_level(logging.DEBUG):
            out = asyncio.run(fn('do a thing'))
        assert out == 'Coding task execution error: boom from orchestrator'
        logged = [r for r in caplog.records
                  if r.levelno >= logging.ERROR and r.exc_info
                  and 'boom from orchestrator' in str(r.exc_info[1])]
        assert logged, [(r.levelname, r.getMessage()) for r in caplog.records]
