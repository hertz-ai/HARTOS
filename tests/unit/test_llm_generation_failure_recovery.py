"""Guard: an engine-side generation failure never ends a turn on the first
bad sample — the AgentLightningWrapper re-samples, then re-samples once
tool-less so the model can write prose from the tool results already in
history, then returns an honest fallback reply.

The trigger is the OpenAI-compatible exception CLASS every serving engine
speaks — 5xx status, or a connection that dropped/timed out — never an
engine's message text.  llama.cpp --jinja answers 500 when the model's
tool-call text will not parse; vLLM/TGI/hosted endpoints fail their own
way; a dropped socket looks the same from here.  4xx is the caller's
request (context overflow, auth) and must propagate to its own handlers.

Live root cause 2026-09-03 (Auto Research agent, installed build): the
Assistant's google_search executed and returned real 2024 articles, then
the synthesis turn failed — first a connection error, then 500s on its own
malformed tool call.  A message-text match on the 500 caught the second
class only.

The real AgentLightningWrapper.__init__ builds a tracer + reward calculator
(seconds of setup), so these guards drive the recovery closure
(_wrap_generate_reply) on a minimally-constructed instance — the exact code
under test, none of the instrumentation.
"""
import unittest

import httpx
import openai

_REQ = httpx.Request('POST', 'http://127.0.0.1:8080/v1/chat/completions')


def _server_500():
    return openai.InternalServerError(
        "Error code: 500 - {'error': {'code': 500, 'message': 'Failed to "
        "parse tool call arguments as JSON: [json.exception.parse_error.101]'}}",
        response=httpx.Response(500, request=_REQ), body=None)


def _connection_dropped():
    return openai.APIConnectionError(request=_REQ)


def _bad_request_400():
    return openai.BadRequestError(
        'context size exceeded', response=httpx.Response(400, request=_REQ),
        body=None)


class _FakeOpenAIWrapper:
    def __init__(self, **cfg):
        self.cfg = cfg


class _StubAgent:
    """Autogen-shaped stub: raises `failure()` while llm_config has tools,
    returns prose once tools are stripped."""

    PROSE = ('Based on the results: the top 3 AI advances of 2024 are '
             'agentic systems, multimodal foundation models, and small '
             'efficient models.')

    def __init__(self, failure):
        self.name = 'Assistant'
        self.llm_config = {'config_list': [{'model': 'local'}],
                           'tools': [{'type': 'function',
                                      'function': {'name': 'google_search'}}]}
        self.client = _FakeOpenAIWrapper(**self.llm_config)
        self.calls = []
        self._failure = failure

    def generate_reply(self, *a, **k):
        has_tools = bool((self.llm_config or {}).get('tools'))
        self.calls.append(has_tools)
        if has_tools:
            raise self._failure()
        return self.PROSE


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


class Classifier(unittest.TestCase):
    def test_classes_not_message_text(self):
        from integrations.agent_lightning.wrapper import (
            _is_recoverable_generation_failure as rec)
        self.assertTrue(rec(_server_500()))
        self.assertTrue(rec(_connection_dropped()))
        self.assertTrue(rec(openai.APITimeoutError(request=_REQ)))
        self.assertFalse(rec(_bad_request_400()))
        # llama's message text on a plain exception is NOT the trigger.
        self.assertFalse(rec(RuntimeError(str(_server_500()))))


class GenerationFailureRecovery(unittest.TestCase):
    def _assert_recovered(self, agent):
        reply = _make_wrapped(agent)([{'role': 'user', 'content': 'go'}])
        self.assertIn('top 3 AI advances of 2024', reply)
        # 1 initial + 2 retries with tools all failed; the tool-less call
        # then succeeded; config restored to its tools-bearing form.
        self.assertEqual(agent.calls, [True, True, True, False])
        self.assertTrue(agent.llm_config.get('tools'))

    def test_5xx_recovers_via_tool_less_synthesis(self):
        self._assert_recovered(_StubAgent(_server_500))

    def test_connection_drop_recovers_via_tool_less_synthesis(self):
        # The first failure in the 2026-09-03 live trace was this class; a
        # message-text match on the 500 never reached it.
        self._assert_recovered(_StubAgent(_connection_dropped))

    def test_fallback_reply_when_tool_less_also_fails(self):
        agent = _StubAgent(_server_500)
        agent.llm_config = {'config_list': [{'model': 'local'}]}  # no tools

        def _always_fail(*a, **k):
            raise _server_500()
        agent.generate_reply = _always_fail
        reply = _make_wrapped(agent)([{'role': 'user', 'content': 'go'}])
        self.assertIn('rephrase', reply)
        self.assertNotIn('500', reply)  # engine internals never reach the user

    def test_4xx_propagates_to_its_own_handlers(self):
        agent = _StubAgent(_bad_request_400)
        with self.assertRaises(openai.BadRequestError):
            _make_wrapped(agent)([{'role': 'user', 'content': 'go'}])
        self.assertEqual(agent.calls, [True])  # ladder never entered

    def test_unrelated_exception_propagates(self):
        agent = _StubAgent(lambda: RuntimeError('some other failure'))
        with self.assertRaises(RuntimeError):
            _make_wrapped(agent)([{'role': 'user', 'content': 'go'}])
        self.assertEqual(agent.calls, [True])


if __name__ == '__main__':
    unittest.main()
