"""Guard: the reuse main-group speaker selector routes a tool-call message to
the agent that can EXECUTE the named function — autogen's own func_call_filter
rule (groupchat.py _prepare_and_select_agents: the agents whose function_map
holds the function), which a custom speaker_selection_method bypasses.

Live root cause 2026-09-05 01:25 (Auto Research, installed build): the Helper
proposed google_search (it carries the LLM schema); the selector routed the
call to a hardcoded executor whose function_map lacked google_search, so
execute_function returned "Error: Function google_search not found." — the
tool never ran and the turn fell back to a knowledge-cutoff answer.

`google_search` is a _MAIN_LEG_CORE tool registered (helper=llm,
assistant=exec), so its executor is the Assistant, not the Executor: which
agent runs a call is per-tool, and the selector must ask can_execute_function
rather than assume one agent.

The rule lives in reuse_recipe._agent_that_executes (the role group needs it
too), so this guard checks that the main selector delegates to it and, with
no live llama, that the helper picks the agent that holds the function.
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _state_transition_body():
    src = open(SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    # The MAIN reuse group's selector is the state_transition nested in
    # create_agents_for_user.  The role group's selector also calls the
    # helper; only the main one routes @statusverifier to `verify`.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'state_transition':
            body = ast.get_source_segment(src, node)
            if body and '_agent_that_executes(' in body and 'verify' in body:
                return body
    return ''


class _Agent:
    def __init__(self, name, functions):
        self.name = name
        self._functions = set(functions)

    def can_execute_function(self, names):
        names = [names] if isinstance(names, str) else list(names)
        return all(n in self._functions for n in names)


class _Group:
    def __init__(self, agents):
        self.agents = agents


def _call(name):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'c1', 'type': 'function',
         'function': {'name': name, 'arguments': '{}'}}]}


class ToolCallRouting(unittest.TestCase):
    def setUp(self):
        self.body = _state_transition_body()
        self.assertTrue(self.body, "main-group state_transition not found")

    def test_routes_by_can_execute_function(self):
        self.assertIn('_agent_that_executes(', self.body,
                      "the selector must route a tool call to the agent that "
                      "can execute it (autogen's func_call_filter rule)")

    def test_iterates_group_agents_for_the_executor(self):
        from hartos.reuse_recipe import _agent_that_executes
        helper = _Agent('Helper', [])
        executor = _Agent('Executor', ['crawl4ai_crawl'])
        assistant = _Agent('Assistant', ['google_search'])
        group = _Group([helper, executor, assistant])
        agent, funcs = _agent_that_executes(group, _call('google_search'))
        self.assertIs(agent, assistant,
                      "google_search must go to the agent whose function_map "
                      "holds it, not to a fixed executor")
        self.assertEqual(funcs, ['google_search'])
        agent, _ = _agent_that_executes(group, {'role': 'assistant',
                                               'content': 'plain reply'})
        self.assertIsNone(agent, "a message with no call selects nobody")

    def test_no_hardcoded_executor_for_tool_calls(self):
        self.assertNotIn('reuse: structured tool_call from Assistant -> Executor', self.body,
                         "the hardcoded 'Assistant tool_call -> Executor' route "
                         "sent google_search to an agent that could not run it")


if __name__ == '__main__':
    unittest.main()
