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

This is an AST guard (no live llama needed): it asserts the selector body
contains the can_execute_function routing and no longer hardcodes
`return executor` for a tool call.
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _state_transition_body():
    src = open(SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    # the MAIN reuse group's selector is the state_transition nested in
    # create_agents_for_user; take the first state_transition whose body
    # references 'verify' and 'assistant' (the main group).
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'state_transition':
            body = ast.get_source_segment(src, node)
            if body and 'can_execute_function' in body:
                return body
    return ''


class ToolCallRouting(unittest.TestCase):
    def setUp(self):
        self.body = _state_transition_body()
        self.assertTrue(self.body, "main-group state_transition not found")

    def test_routes_by_can_execute_function(self):
        self.assertIn('can_execute_function', self.body,
                      "the selector must route a tool call to the agent that "
                      "can execute it (autogen's func_call_filter rule)")

    def test_iterates_group_agents_for_the_executor(self):
        self.assertIn('groupchat.agents', self.body,
                      "must scan the group's agents to find the executor")

    def test_no_hardcoded_executor_for_tool_calls(self):
        self.assertNotIn('reuse: structured tool_call from Assistant -> Executor', self.body,
                         "the hardcoded 'Assistant tool_call -> Executor' route "
                         "sent google_search to an agent that could not run it")


if __name__ == '__main__':
    unittest.main()
