"""Guard: after tools-bearing retries exhaust on a malformed-tool-call 500,
the AgentLightningWrapper does ONE tool-less resample so the model can
synthesise prose from the tool results already in history.

Live root cause 2026-09-03 (Auto Research agent, installed build): the
Assistant's mirrored google_search executed and returned real 2024 articles
into the conversation, then the synthesis turn emitted its OWN malformed
tool-call JSON; llama --jinja rejected it 500 ("Failed to parse tool call
arguments as JSON"); the wrapper re-sampled with tools present (same bad
call), exhausted, and returned a canned apology; the reuse turn then fell
back to a direct-4B knowledge answer.  The fix strips tools for a final
resample; here the stub agent raises the exact 500 while tools are present
and returns prose once tools are removed.

The real AgentLightningWrapper.__init__ builds a tracer + reward calculator
(seconds of setup, network-y in some envs), so these guards drive the
recovery closure (_wrap_generate_reply) on a minimally-constructed instance
— the exact code under test, none of the instrumentation.
"""
import unittest

TOOLCALL_500 = ("Error code: 500 - {'error': {'code': 500, 'message': "
                "'Failed to parse tool call arguments as JSON: "
                "[json.exception.parse_error.101] parse error'}}")


class _FakeOpenAIWrapper:
    def __init__(self, **cfg):
        self.cfg = cfg


class _StubAgent:
    """Autogen-shaped stub: raises the toolcall 500 while llm_config has
    tools, returns prose once tools are stripped."""

    def __init__(self):
        self.name = 'Assistant'
        self.llm_config = {'config_list': [{'model': 'local'}],
                           'tools': [{'type': 'function',
                                      'function': {'name': 'google_search'}}]}
        self.client = _FakeOpenAIWrapper(**self.llm_config)
        self.calls = []

    def generate_reply(self, *a, **k):
        has_tools = bool((self.llm_config or {}).get('tools'))
        self.calls.append(has_tools)
        if has_tools:
            raise RuntimeError(TOOLCALL_500)
        return ('Based on the results: the top 3 AI advances of 2024 are '
                'agentic systems, multimodal foundation models, and small '
                'efficient models.')


def _make_wrapped(agent):
    """Build the recovery closure on a bare wrapper (no heavy __init__)."""
    import autogen
    from integrations.agent_lightning.wrapper import AgentLightningWrapper
    autogen.OpenAIWrapper = _FakeOpenAIWrapper  # avoid a real network client
    w = object.__new__(AgentLightningWrapper)
    w.agent = agent
    w.agent_id = 'u_p'
    w.tracer = None
    w.reward_calculator = None
    w.current_span_id = None
    w.execution_count = 0
    return w._wrap_generate_reply(agent.generate_reply)


class ToollessRecovery(unittest.TestCase):
    def test_recovers_via_tool_less_synthesis(self):
        agent = _StubAgent()
        wrapped = _make_wrapped(agent)
        reply = wrapped([{'role': 'user', 'content': 'go'}])
        self.assertIn('top 3 AI advances of 2024', reply)
        # tools-bearing calls (1 initial + 2 retries) all 500'd; a final
        # tool-less call then succeeded.
        self.assertGreaterEqual(agent.calls.count(True), 1)
        self.assertIn(False, agent.calls)
        # config restored to its tools-bearing form after recovery.
        self.assertTrue(agent.llm_config.get('tools'))

    def test_apology_when_tool_less_also_fails(self):
        # No tools at all -> nothing to strip -> reach the apology string
        # (the tool-less resample only runs when tools were present).
        agent = _StubAgent()
        agent.llm_config = {'config_list': [{'model': 'local'}]}  # no tools

        def _always_500(*a, **k):
            raise RuntimeError(TOOLCALL_500)
        agent.generate_reply = _always_500
        wrapped = _make_wrapped(agent)
        reply = wrapped([{'role': 'user', 'content': 'go'}])
        self.assertIn("couldn't be parsed", reply)

    def test_non_toolcall_500_still_raises(self):
        agent = _StubAgent()

        def _other(*a, **k):
            raise RuntimeError('Error code: 500 - some other failure')
        agent.generate_reply = _other
        wrapped = _make_wrapped(agent)
        with self.assertRaises(RuntimeError):
            wrapped([{'role': 'user', 'content': 'go'}])


if __name__ == '__main__':
    unittest.main()
