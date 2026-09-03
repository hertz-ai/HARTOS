"""Coding-agent desktop leg routes through the canonical VLM adapter.

Caught during the 2026-09-01 full-caller audit of the VLM selector:
execute_desktop_task(target='local') called run_local_agentic_loop with
ONE positional arg (missing required `tier`) and a str where the loop
expects the message dict — the bare `except Exception` swallowed the
TypeError into {'success': False}, so the leg had NEVER executed.
"""
import sys
import types
import unittest
from unittest.mock import patch


def _executor():
    # port_registry is imported at module level for the default URL —
    # give it a real value without needing the full core boot.
    from integrations.coding_agent.remote_executor import RemoteDesktopExecutor
    return RemoteDesktopExecutor('http://localhost:5000')


class DesktopTaskGoesThroughAdapter(unittest.TestCase):

    def test_local_target_calls_execute_vlm_instruction_with_message_dict(self):
        ex = _executor()
        seen = {}

        def _fake_adapter(message):
            seen.update(message)
            return {'status': 'success', 'exit_reason': 'done',
                    'extracted_responses': [], 'execution_time_seconds': 1.0}

        with patch('integrations.vlm.vlm_adapter.execute_vlm_instruction',
                   _fake_adapter):
            out = ex.execute_desktop_task('open notepad', target='local')

        self.assertTrue(out['success'], out)
        # the loop reads these exact keys (local_loop.py:183-201)
        self.assertEqual(seen.get('instruction_to_vlm_agent'), 'open notepad')
        self.assertIn('user_id', seen)
        self.assertIn('prompt_id', seen)

    def test_incomplete_loop_reports_failure_not_fabricated_success(self):
        ex = _executor()
        with patch('integrations.vlm.vlm_adapter.execute_vlm_instruction',
                   return_value={'status': 'incomplete',
                                 'exit_reason': 'timeout',
                                 'extracted_responses': []}):
            out = ex.execute_desktop_task('open notepad', target='local')
        self.assertFalse(out['success'])

    def test_tier3_none_result_is_an_honest_error(self):
        ex = _executor()
        with patch('integrations.vlm.vlm_adapter.execute_vlm_instruction',
                   return_value=None):
            out = ex.execute_desktop_task('open notepad', target='local')
        self.assertFalse(out['success'])
        self.assertIn('VLM', out['error'])


if __name__ == '__main__':
    unittest.main()
