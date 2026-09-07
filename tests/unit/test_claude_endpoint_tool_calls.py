"""The claude-code shim must serve tool turns like any other OpenAI endpoint.

Measured live 2026-09-05 (CREATE walk of agent "Relay" 88601674818):

    503 {'category': 'no_tools',
         'message': 'claude-code tier cannot execute tools; route tool turns
                     to a tool-capable tier'}

Four layers of workaround stacked on one misreading:

  1. claude_code_backend.invoke_claude(mode='inference') passes
     `--allowedTools ''`.  That flag is CORRECT and stays: it stops Claude
     running its OWN tools (file edits, bash, web) inside what is meant to be
     a pure completion engine.
  2. It was read as "this tier cannot do tools at all", so
     claude_code_endpoint grew a guard that 503s ANY request carrying tools[].
  3. autogen's client saw a 503.
  4. agent_lightning/wrapper classified 503 (>=500) as a recoverable
     GENERATION failure, re-sampled the same tier twice — guaranteed to fail,
     the refusal is deterministic — then returned a canned reply.

The premise of (2) is false.  In autogen the LLM NEVER executes a tool: it
emits a tool_call (name + arguments) and HARTOS's executor agent runs the
registered Python closure in-process.  Any model that can name a call can
serve a tool turn.  The shim's own docstring already states the contract —
"autogen uses Claude Code 'like any other LLM' without knowing it is a
subprocess" — and the guard violates it.

So the shim must complete the protocol it claims to speak: carry tools[] into
the prompt, and return the standard tool_calls shape.  Argument repair uses
autogen's OWN helper (ConversableAgent._format_json_str, the one
execute_function already uses) — no parser is written here.

    python -m pytest tests/unit/test_claude_endpoint_tool_calls.py --noconftest -q
"""
import json
import unittest

from flask import Flask

from integrations.providers import claude_code_endpoint as ep


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'google_search',
        'description': 'web search for a query',
        'parameters': {'type': 'object',
                       'properties': {'text': {'type': 'string'}},
                       'required': ['text']}}},
    {'type': 'function', 'function': {
        'name': 'save_to_long_term_memory',
        'description': 'persist a fact',
        'parameters': {'type': 'object',
                       'properties': {'content': {'type': 'string'}},
                       'required': ['content']}}},
]


def _client():
    app = Flask(__name__)
    app.register_blueprint(ep.claude_code_bp)
    return app.test_client()


def _post(client, body):
    return client.post('/api/claude/v1/chat/completions', json=body)


class _Stub:
    """Stands in for invoke_claude: returns whatever stdout we want."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.seen_prompt = None

    def __call__(self, prompt, **kw):
        self.seen_prompt = prompt
        # classify_failure() treats anything without ok=True as a FAILURE, so a
        # success stub must carry it or every call 502s (caught writing this
        # test: the plain-reply case failed for the harness's reason, not the
        # product's).
        return {'ok': True, 'stdout': self.stdout, 'returncode': 0}


class ClaudeEndpointToolCalls(unittest.TestCase):

    def setUp(self):
        self._real = ep.invoke_claude

    def tearDown(self):
        ep.invoke_claude = self._real

    # ---- THE REGRESSION ------------------------------------------------
    def test_tool_turn_is_not_refused(self):
        """A request carrying tools[] must be SERVED, not 503'd.

        Pre-fix this returns 503 no_tools, which is what stalled Relay's
        CREATE at Review Step 2 (3/3 reproducible, ~0.3s each).
        """
        ep.invoke_claude = _Stub('Sure — here is the answer.')
        r = _post(_client(), {'model': 'claude-code', 'messages': [
            {'role': 'user', 'content': 'find the latest release'}],
            'tools': TOOLS})
        self.assertNotEqual(
            503, r.status_code,
            'the shim must serve tool turns: in autogen the LLM only NAMES a '
            'tool, HARTOS executes it — refusing is refusing work it can do')
        self.assertEqual(200, r.status_code)

    def test_tools_reach_the_model(self):
        """The tool names must appear in the prompt, or the model cannot name one."""
        stub = _Stub('ok')
        ep.invoke_claude = stub
        _post(_client(), {'messages': [{'role': 'user', 'content': 'search it'}],
                          'tools': TOOLS})
        self.assertIsNotNone(stub.seen_prompt)
        self.assertIn('google_search', stub.seen_prompt,
                      'tools[] must be carried into the prompt — a model cannot '
                      'call what it was never shown (the 2026-09-05 defect)')

    # ---- the tool_calls shape -----------------------------------------
    def test_named_tool_becomes_openai_tool_calls(self):
        """When the model names a tool, return the STANDARD OpenAI shape."""
        ep.invoke_claude = _Stub(json.dumps({
            'name': 'google_search', 'arguments': {'text': 'llama.cpp release'}}))
        r = _post(_client(), {'messages': [{'role': 'user', 'content': 'go'}],
                              'tools': TOOLS})
        self.assertEqual(200, r.status_code)
        choice = r.get_json()['choices'][0]
        self.assertEqual('tool_calls', choice['finish_reason'])
        calls = choice['message']['tool_calls']
        self.assertEqual(1, len(calls))
        self.assertEqual('function', calls[0]['type'])
        self.assertEqual('google_search', calls[0]['function']['name'])
        self.assertTrue(calls[0].get('id'), 'autogen pairs results by tool_call id')
        args = calls[0]['function']['arguments']
        self.assertIsInstance(args, str,
                              'OpenAI arguments is a JSON STRING; autogen calls '
                              'json.loads on it in execute_function')
        self.assertEqual({'text': 'llama.cpp release'}, json.loads(args))

    def test_only_offered_tools_are_emitted(self):
        """A name the caller never offered must not become a tool_call."""
        ep.invoke_claude = _Stub(json.dumps({
            'name': 'rm_minus_rf', 'arguments': {}}))
        r = _post(_client(), {'messages': [{'role': 'user', 'content': 'go'}],
                              'tools': TOOLS})
        choice = r.get_json()['choices'][0]
        self.assertEqual('stop', choice['finish_reason'],
                         'an unoffered name is not a tool call — it is prose')
        self.assertIsNone(choice['message'].get('tool_calls'))

    # ---- no regression on the path that already worked ------------------
    def test_plain_reply_is_unchanged(self):
        """No tools[]: byte-identical behaviour to before the change."""
        ep.invoke_claude = _Stub('The capital of France is Paris.')
        r = _post(_client(), {'messages': [{'role': 'user', 'content': 'capital?'}]})
        self.assertEqual(200, r.status_code)
        choice = r.get_json()['choices'][0]
        self.assertEqual('stop', choice['finish_reason'])
        self.assertEqual('The capital of France is Paris.',
                         choice['message']['content'])

    def test_prose_answer_to_a_tool_turn_still_answers(self):
        """Tools offered but the model just answers: that is a valid 'stop'."""
        ep.invoke_claude = _Stub('I already know: v1.2.0 shipped Tuesday.')
        r = _post(_client(), {'messages': [{'role': 'user', 'content': 'go'}],
                              'tools': TOOLS})
        choice = r.get_json()['choices'][0]
        self.assertEqual('stop', choice['finish_reason'])
        self.assertIn('v1.2.0', choice['message']['content'])


if __name__ == '__main__':
    unittest.main()
