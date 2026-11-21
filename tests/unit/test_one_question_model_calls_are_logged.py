"""core.llm_json.ask_model_json: its model call is on the outbound log.

The call goes out through requests (core.http_pool.pooled_post).  The
outbound hook (core.llm_outbound_logger.install) patches httpx and urllib
only, so these calls were on neither the outbound log nor the audit trail
('llm_call'): the flow-goal judge (core.flow_progress.settle:
flow_goal_checkable, flow_goal_met, flow_weakest) and the credential-ask
checks (hartos.ai_key_vault: credential_ask_check, credential_ask_use).
ask_model_json now logs through the hook's own writer, log_outbound, and
answers exactly as before.

    python -m pytest tests/unit/test_one_question_model_calls_are_logged.py -q
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['met'],
          'properties': {'met': {'type': 'boolean'}}}


class _Reply:
    def __init__(self, status=200, content='{"met": true}'):
        self.status_code = status
        self._content = content

    def json(self):
        return {'choices': [{'message': {'content': self._content}}]}


@pytest.fixture
def wire(monkeypatch):
    import core.http_pool as http_pool
    import core.llm_outbound_logger as outbound
    seen = SimpleNamespace(posts=[], logged=[], reply=_Reply(), error=None, log_error=None)

    def post(url, timeout=None, **kwargs):
        seen.posts.append(SimpleNamespace(url=url, timeout=timeout, kwargs=kwargs))
        if seen.error is not None:
            raise seen.error
        return seen.reply

    def log(body, **fields):
        seen.logged.append((body, fields))
        if seen.log_error is not None:
            raise seen.log_error
    monkeypatch.setattr(http_pool, 'pooled_post', post)
    monkeypatch.setattr(outbound, 'log_outbound', log)
    return seen


def _ask(name='flow_goal_met', **kw):
    from core.llm_json import ask_model_json
    return ask_model_json('Was the goal met?', SCHEMA, name, **kw)


def test_an_answered_call_is_logged_once_with_its_body_and_status(wire):
    assert _ask() == {'met': True}
    assert len(wire.posts) == 1 and len(wire.logged) == 1
    body, fields = wire.logged[0]
    assert body is wire.posts[0].kwargs['json']
    assert fields['response_status'] == 200
    assert fields['source'] == 'llm_json.flow_goal_met'
    assert fields['response_error'] is None
    assert isinstance(fields['latency_ms'], float) and fields['latency_ms'] >= 0


@pytest.mark.parametrize('name', ['flow_goal_checkable', 'flow_goal_met', 'flow_weakest',
                                  'credential_ask_check', 'credential_ask_use'])
def test_every_callers_question_is_logged_under_its_own_name(wire, name):
    _ask(name)
    assert [f['source'] for _b, f in wire.logged] == [f'llm_json.{name}']


def test_a_refused_call_is_logged_with_its_status_and_answers_none(wire):
    wire.reply = _Reply(status=402)
    assert _ask() is None
    assert [(f['response_status'], f['response_error']) for _b, f in wire.logged] == [(402, None)]


def test_a_call_that_never_got_a_reply_is_logged_with_why(wire):
    import requests
    wire.error = requests.exceptions.ReadTimeout('30 s')
    assert _ask() is None
    assert [(f['response_status'], f['response_error']) for _b, f in wire.logged] == [
        (None, 'ReadTimeout')]


def test_a_reply_that_is_not_the_object_is_logged_and_answers_none(wire):
    wire.reply = _Reply(content='not json')
    assert _ask() is None
    assert [f['response_status'] for _b, f in wire.logged] == [200]


def test_a_log_that_fails_never_changes_the_answer(wire):
    wire.log_error = RuntimeError('disk full')
    assert _ask() == {'met': True}
    assert len(wire.logged) == 1


def test_the_request_itself_is_as_it_was(wire):
    from core.autogen_config import llm_http_target
    assert _ask(max_tokens=77, timeout=12.5) == {'met': True}
    url, headers, model = llm_http_target()
    call = wire.posts[0]
    assert call.url == url and call.timeout == 12.5 and call.kwargs['headers'] == headers
    body = call.kwargs['json']
    assert (body['model'], body['temperature'], body['max_tokens']) == (model, 0, 77)
    assert body['messages'] == [{'role': 'user', 'content': 'Was the goal met?'}]
    assert body['response_format'] == {'type': 'json_schema', 'json_schema': {
        'name': 'flow_goal_met', 'strict': True, 'schema': SCHEMA}}
    assert set(body) <= {'model', 'temperature', 'max_tokens', 'messages',
                         'response_format', 'chat_template_kwargs'}
