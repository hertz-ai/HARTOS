"""A call the executor refused as broken JSON is marked refused in the
conversation, never shown to the model as the split dict json_repair made.

Log RCA defect 14, leftover after 68377afd2: the patched executor refuses
``{"text": Financial Dashboard ... - Consulting: $5,000 ...}`` (a string
value left unquoted) because json_repair splits it into
``{"text": "...$10", "Consulting": "5,000", ...}``, which does not bind to
the tool.  But the TOOL-ARGS-GUARD (ensure_tool_call_arguments_json), which
rebuilds every later request's history, repaired the same text on its own and
wrote that split dict back as the call's arguments.  So the model was shown
its broken call as if it had been a well-formed one, next to a reply saying
its arguments were not valid JSON.

The executor is the one place that knows whether the call ran (it has the
tool's signature; the guard does not).  autogen hands it the very function
dict stored in the conversation (generate_tool_calls_reply:
``tool_call.get("function")`` of ``messages[-1]``), so a refusal of
arguments that were not valid JSON now writes the refused stand-in
(refused_arguments_json: what the model wrote, marked refused, and why) into
that dict.  The guard keeps a strict JSON object as it is, so every later
request carries the stand-in.

Real autogen ConversableAgents; the tool is wrapped the way live tools are
(core.tool_logging.log_tool_execution).  Boundaries mocked: the credential
vault only.
"""
import asyncio
import copy
import json
import unittest
from unittest import mock

from autogen.agentchat.conversable_agent import ConversableAgent
from flask import Flask

from core.tool_logging import log_tool_execution
from hartos.helper import (REFUSED_ARGUMENTS_KEY, REFUSED_BECAUSE_KEY,
                           ToolMessageHandler, force_apply_autogen_json_fix)

UNQUOTED = ('{"text": Financial Dashboard Revenue (Monthly): - Trading: '
            '$10,000\n- Consulting: $5,000\n- Education: $2,500\n- TOTAL: '
            '$17,500 Net Profit Margin: 28.6%}')


class _Chat(unittest.TestCase):

    def setUp(self):
        orig = (ConversableAgent.execute_function,
                ConversableAgent.a_execute_function)

        def restore():
            (ConversableAgent.execute_function,
             ConversableAgent.a_execute_function) = orig
        self.addCleanup(restore)
        self.assertTrue(force_apply_autogen_json_fix())
        vault = mock.patch('core.tool_logging.credential_vault',
                           return_value=None)
        vault.start()
        self.addCleanup(vault.stop)

        self.calls = []

        @log_tool_execution
        def send_message_to_user(text: str, avatar_id: str = '',
                                 response_type: str = 'Neutral') -> str:
            self.calls.append(text)
            return 'sent'

        @log_tool_execution
        async def text_2_image(text: str) -> str:
            self.calls.append(text)
            return 'drawn'

        self.assistant = ConversableAgent('assistant', llm_config=False,
                                          human_input_mode='NEVER')
        self.executor = ConversableAgent('executor', llm_config=False,
                                         human_input_mode='NEVER')
        self.executor.register_function({
            'send_message_to_user':
                self.executor._wrap_function(send_message_to_user),
            'text_2_image': self.executor._wrap_function(text_2_image),
        })

    def call(self, name, arguments, run_async=False):
        """The assistant sends one tool call; the executor answers it.
        Returns the executor's reply."""
        self.assistant.send(
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'call_1', 'type': 'function',
                             'function': {'name': name,
                                          'arguments': arguments}}]},
            self.executor, request_reply=False, silent=True)
        if run_async:
            ok, reply = asyncio.run(self.executor.a_generate_tool_calls_reply(
                sender=self.assistant))
        else:
            ok, reply = self.executor.generate_tool_calls_reply(
                sender=self.assistant)
        self.assertTrue(ok)
        return reply

    def next_request_arguments(self):
        """The call's arguments as the assistant's next request sends them:
        its own history through the ToolMessageHandler guard."""
        history = copy.deepcopy(self.assistant._oai_messages[self.executor])
        with Flask(__name__).app_context():
            out = ToolMessageHandler().validate_messages(history)
        sent = [tc['function']['arguments'] for m in out
                for tc in (m.get('tool_calls') or [])]
        self.assertEqual(len(sent), 1)
        return sent[0]


class RefusedBrokenJsonIsMarked(_Chat):

    def _assert_marked(self, arguments, original):
        parsed = json.loads(arguments)
        self.assertEqual(set(parsed), {REFUSED_ARGUMENTS_KEY,
                                       REFUSED_BECAUSE_KEY})
        self.assertEqual(parsed[REFUSED_ARGUMENTS_KEY], original)
        self.assertIn('not run', parsed[REFUSED_BECAUSE_KEY])
        self.assertIn('not valid JSON', parsed[REFUSED_BECAUSE_KEY])

    def test_split_call_is_sent_back_marked_refused(self):
        reply = self.call('send_message_to_user', UNQUOTED)
        self.assertEqual(self.calls, [])
        self.assertIn('not valid JSON', reply['content'])
        sent = self.next_request_arguments()
        self.assertNotIn('Consulting', json.loads(sent))
        self._assert_marked(sent, UNQUOTED)

    def test_split_call_async_is_marked(self):
        self.call('text_2_image', UNQUOTED, run_async=True)
        self.assertEqual(self.calls, [])
        self._assert_marked(self.next_request_arguments(), UNQUOTED)

    def test_emptied_required_value_is_marked(self):
        self.call('send_message_to_user', '{"text":')
        self.assertEqual(self.calls, [])
        self._assert_marked(self.next_request_arguments(), '{"text":')

    def test_arguments_nothing_could_parse_are_marked(self):
        # When even the fallback parse raises, the call is refused as not
        # JSON; it is marked the same way.
        with mock.patch('hartos.helper.retrieve_json',
                        side_effect=ValueError('unparseable')):
            reply = self.call('send_message_to_user', '{"text": <<>>')
        self.assertEqual(self.calls, [])
        self.assertIn('must be in JSON format', reply['content'])
        self._assert_marked(self.next_request_arguments(), '{"text": <<>>')

    def test_the_marked_call_is_never_run_again(self):
        self.call('send_message_to_user', UNQUOTED)
        marked = self.assistant._oai_messages[self.executor][-1][
            'tool_calls'][0]['function']['arguments']
        reply = self.call('send_message_to_user', marked)
        self.assertEqual(self.calls, [])
        self.assertIn('refused earlier', reply['content'])


class CallsThatWerePossibleStayAsWritten(_Chat):

    def test_a_repaired_call_that_ran_is_not_marked(self):
        # Trailing comma: repaired, bound, run.  Nothing to mark.
        self.call('send_message_to_user', '{"text": "hi",}')
        self.assertEqual(self.calls, ['hi'])
        stored = self.assistant._oai_messages[self.executor][-1][
            'tool_calls'][0]['function']['arguments']
        self.assertEqual(stored, '{"text": "hi",}')
        self.assertEqual(json.loads(self.next_request_arguments()),
                         {'text': 'hi'})

    def test_valid_json_with_a_wrong_name_stays_as_written(self):
        # Well-formed JSON: the model's own words, refused by name.  The
        # history shows exactly what it sent, which the reply names.
        text = '{"text": "hi", "status": "done"}'
        reply = self.call('send_message_to_user', text)
        self.assertEqual(self.calls, [])
        self.assertIn('Unknown argument(s): status', reply['content'])
        self.assertEqual(self.next_request_arguments(), text)



class GroupChatSeesTheMark(unittest.TestCase):
    """The mark reaches every seat of a real autogen GroupChat: the speaker
    that wrote the call, the executor, and groupchat.messages (speaker
    selection), because the manager broadcasts the one message object."""

    def test_every_seat_holds_the_marked_call(self):
        from autogen import GroupChat, GroupChatManager
        orig = (ConversableAgent.execute_function,
                ConversableAgent.a_execute_function)

        def restore():
            (ConversableAgent.execute_function,
             ConversableAgent.a_execute_function) = orig
        self.addCleanup(restore)
        self.assertTrue(force_apply_autogen_json_fix())
        calls = []

        def send_message_to_user(text: str) -> str:
            calls.append(text)
            return 'sent'

        user = ConversableAgent('user', llm_config=False,
                                human_input_mode='NEVER', default_auto_reply='')
        assistant = ConversableAgent('assistant', llm_config=False,
                                     human_input_mode='NEVER')
        turns = []

        def reply(recipient, messages=None, sender=None, config=None):
            turns.append(1)
            if len(turns) == 1:
                return True, {'role': 'assistant', 'content': None,
                              'tool_calls': [{
                                  'id': 'call_1', 'type': 'function',
                                  'function': {'name': 'send_message_to_user',
                                               'arguments': UNQUOTED}}]}
            return True, 'TERMINATE'
        assistant.register_reply([ConversableAgent, None], reply, position=0)
        executor = ConversableAgent('executor', llm_config=False,
                                    human_input_mode='NEVER')
        executor.register_function({'send_message_to_user':
                                    executor._wrap_function(send_message_to_user)})
        chat = GroupChat(agents=[user, assistant, executor], messages=[],
                         max_round=4, speaker_selection_method='round_robin')
        manager = GroupChatManager(chat, llm_config=False)
        user.initiate_chat(manager, message='report', silent=True)

        self.assertEqual(calls, [])
        views = {'assistant': assistant._oai_messages[manager],
                 'executor': executor._oai_messages[manager],
                 'groupchat': chat.messages}
        for seat, history in views.items():
            sent = [tc['function']['arguments'] for m in history
                    for tc in (m.get('tool_calls') or [])]
            self.assertEqual(len(sent), 1, seat)
            self.assertEqual(json.loads(sent[0])[REFUSED_ARGUMENTS_KEY],
                             UNQUOTED, seat)


if __name__ == '__main__':
    unittest.main()
