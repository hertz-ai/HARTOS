"""A goal's memory recall reads what was saved, never the goal's own talk.

WHAT WENT WRONG (central, 20 Nov 01:07-01:16Z, build 70ece55bd)
  Daemon goal 74416785 (rice buyer growth), CREATE action 2: 'search_long_term_
  memory: recall saved facts on the HertzAI commission agent trade model,
  approval_required rules, lane cost checklist and prior importer replies'.
  search_long_term_memory ran 28 times.  Every result was rows of the goal's
  own MemoryGraph conversation: its goal prompt ('You are the HARTOS Sales &
  Marketing Agent...', stored as the 'user' turn of each /chat), its own past
  replies ('HertzAI commission agent model: not found ...') and a loop steer
  ('[retry:nojson-2] Finish what you started ...').  The StatusVerifier said
  each time that these were not the requested facts, and the goal paused
  needs_help: 'it did not complete after 3 attempts'.

  The graph is one store per user and prompt, and every group message and
  every /chat turn goes into it as a 'conversation' row.  On a goal turn
  nobody is speaking: those rows are the goal's own prompt and its own
  replies.  Since 7b7659c51 a free-text recall matches rows holding two of its
  words, so each 'not found' reply naming the facts became the top hit of the
  next search, and the recall could never come back empty (the 'nothing
  found, carry on' answer that lets the step move on).

THE RULE (search_long_term_memory, MemoryGraph leg)
  - on a turn that is not a person's (is_a_persons_request), conversation rows
    are left out; saved facts, insights and observations are recalled;
  - with nothing saved, the goal gets nothing_recalled, so the step carries on;
  - a person's turn recalls their conversation as before.

    python -m pytest tests/unit/test_a_goal_recall_reads_saved_facts_not_its_own_talk.py -q --noconftest
"""
import os
import tempfile
import unittest
from unittest import mock

GOAL_PROMPT = ("You are the HARTOS Sales & Marketing Agent. You run CONTINUOUSLY "
               "inside the HARTOS daemon. Grow rice buyers for the HertzAI "
               "commission agent trade model; approval_required rules apply.")
OWN_REPLY = ("Action 2 is still open. HertzAI commission agent model: not found. "
             "approval_required rules: not found. Lane cost checklist: not found.")
STEER = ("[retry:nojson-2] Finish what you started, Continue with action 2: "
         "search_long_term_memory: recall saved facts on the HertzAI commission "
         "agent trade model")
FACT = ("HertzAI commission agent trade model: 2% of FOB, minimum US$300 per "
        "20 ft container.")
QUERY = "HertzAI commission agent trade model"
SESSION = 'system_daemon_74416785'


class GoalRecallReadsSavedFacts(unittest.TestCase):
    def setUp(self):
        from integrations.channels.memory.memory_graph import MemoryGraph
        self.dir = tempfile.mkdtemp()
        self.graph = MemoryGraph(db_path=self.dir, user_id='system')
        # What the write-back stores for a goal's turns (hart_intelligence_
        # entry prep_outputs and graph_conversation_sink).
        self.graph.register_conversation('user', GOAL_PROMPT, SESSION)
        self.graph.register_conversation('langchain', OWN_REPLY, SESSION)
        self.graph.register_conversation('Assistant', OWN_REPLY + ' Pending.', SESSION)
        self.graph.register_conversation('Helper', STEER, SESSION)

    def tearDown(self):
        # The graph's few files one by one: shutil.rmtree crashes in the
        # test venv (os._walk_symlinks_as_files missing).
        self.graph.close()
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _recall(self, request_id, query=QUERY):
        from core.agent_tools import build_core_tool_closures
        from core.llm_outbound_logger import request_id_context
        ctx = {k: None for k in (
            'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
            'request_id_list', 'recent_file_id', 'scheduler',
            'send_message_to_user1', 'retrieve_json', 'strip_json_values',
            'save_conversation_db')}
        ctx.update(user_id='system', prompt_id='daemon_74416785',
                   agent_data={}, user_prompt=SESSION,
                   request_id_list={SESSION: request_id},
                   memory_graph=self.graph, simplemem_store=None,
                   helper_fun=mock.Mock(), send_message_to_user1=mock.Mock(),
                   retrieve_json=lambda v: v)
        tools = {n: f for n, _, f in build_core_tool_closures(ctx)}
        with request_id_context(request_id):
            return tools['search_long_term_memory'](query)

    def test_a_goal_turn_recalls_the_saved_fact_not_its_own_talk(self):
        self.graph.register(FACT, {'memory_type': 'fact',
                                   'source_agent': 'System',
                                   'session_id': SESSION})
        out = self._recall('daemon_74416785-aaaa')
        self.assertIn('2% of FOB', out)
        for own in ('You are the HARTOS Sales', 'not found', '[retry:'):
            self.assertNotIn(own, out)

    def test_a_goal_turn_with_nothing_saved_is_told_to_carry_on(self):
        from core.tool_refusal import nothing_recalled
        out = self._recall('daemon_74416785-aaaa')
        self.assertEqual(out, nothing_recalled(QUERY))

    def test_a_persons_turn_still_recalls_their_conversation(self):
        self.graph.register_conversation(
            'user', 'My HertzAI commission agent trade model contact is in '
                    'Leeds.', SESSION)
        out = self._recall('nunba_req_42')
        self.assertIn('Leeds', out)


if __name__ == '__main__':
    unittest.main()
