"""An agent's message to the user reaches the desktop on the local chat topic.

Measured 2026-09-26 in the Nunba gui_app.log: REUSE's send_message_to_user1
POSTed every message to http://aws_rasa.hertzai.com:9890/autogen_response,
including on the desktop, where the connection was refused
(WinError 10061).  The send_message_to_user tool's questions, scheduled-task
results and state-transition messages were lost.  CREATE's copy of the same
function had a bundled branch that published to the local topic
com.hertzai.hevolve.chat.{user_id}; REUSE had none.

Both copies now hand the bundled case to ONE publisher,
core.peer_link.crossbar_publish.publish_agent_message.  These tests call the
real functions.  The boundaries are mocked: the hart_intelligence
publish_async (resolved through sys.modules, as safe_hartos_attr does), the
HTTP pool and the scheduler.

create_recipe cannot be imported in a bare pytest env (its import waits on
live services), so its function is lifted by name with ast and exec'd with
its collaborators injected, the same way test_trace_action_banking does.
"""
import ast
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture()
def publisher(monkeypatch):
    """The hart_intelligence publish_async, as safe_hartos_attr resolves it."""
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


@pytest.fixture()
def no_publisher(monkeypatch):
    monkeypatch.delitem(sys.modules, 'hart_intelligence', raising=False)
    monkeypatch.delitem(sys.modules, 'hart_intelligence_entry', raising=False)


@pytest.fixture()
def bundled(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')


@pytest.fixture()
def central(monkeypatch):
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.delattr(sys, 'frozen', raising=False)


def _published(pub):
    assert pub.call_count == 1, pub.call_args_list
    topic, payload = pub.call_args[0][0], pub.call_args[0][1]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return topic, payload


# ── the canonical publisher ────────────────────────────────────────────

def test_publish_agent_message_goes_to_the_users_chat_topic(publisher):
    from core.peer_link.crossbar_publish import publish_agent_message
    ok = publish_agent_message(text='Which month?', user_id='u7',
                               request_id='r1-intermediate', prompt_id='42',
                               inp='task')
    assert ok is True
    topic, payload = _published(publisher)
    assert topic == 'com.hertzai.hevolve.chat.u7'
    assert payload['text'] == ['Which month?']
    assert payload['request_id'] == 'r1-intermediate'
    assert payload['prompt_id'] == '42'
    assert payload['inp'] == 'task'


def test_publish_agent_message_reports_an_unresolvable_publisher(no_publisher):
    from core.peer_link.crossbar_publish import publish_agent_message
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='1') is False


def test_publish_agent_message_reports_a_raising_publisher(monkeypatch):
    monkeypatch.setitem(sys.modules, 'hart_intelligence', SimpleNamespace(
        publish_async=MagicMock(side_effect=RuntimeError('bus down'))))
    from core.peer_link.crossbar_publish import publish_agent_message
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='1') is False


@pytest.mark.parametrize('text,user_id', [('', 'u7'), ('hi', '')])
def test_publish_agent_message_sends_nothing_without_text_or_user(
        publisher, text, user_id):
    from core.peer_link.crossbar_publish import publish_agent_message
    assert publish_agent_message(text=text, user_id=user_id, request_id='r',
                                 prompt_id='1') is False
    publisher.assert_not_called()


# ── REUSE: hartos.reuse_recipe.send_message_to_user1 ───────────────────

@pytest.fixture()
def rr():
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import reuse_recipe
    return reuse_recipe


def _reuse_send(rr, user_id, text):
    with patch.object(rr, 'pooled_post') as post, \
            patch.object(rr, 'scheduler') as sched:
        sched.get_job.return_value = None
        out = rr.send_message_to_user1(user_id, text, 'inp', 'p-9')
    return out, post


def test_reuse_on_the_desktop_publishes_locally_and_never_posts(
        rr, bundled, publisher):
    out, post = _reuse_send(rr, 'desk-1', 'I need the gradient window.')
    post.assert_not_called()
    topic, payload = _published(publisher)
    assert topic == 'com.hertzai.hevolve.chat.desk-1'
    assert payload['text'] == ['I need the gradient window.']
    assert payload['prompt_id'] == 'p-9'
    assert '-intermediate-' in payload['request_id']
    assert out.startswith('Message sent successfully'), out


def test_reuse_on_the_desktop_reports_a_publish_that_did_not_happen(
        rr, bundled, no_publisher):
    out, post = _reuse_send(rr, 'desk-2', 'hello')
    post.assert_not_called()
    assert out.startswith('Failed to send message'), out


def test_reuse_on_central_still_forwards_over_http(rr, central, publisher):
    out, post = _reuse_send(rr, 'cent-1', 'hello central')
    assert post.called
    publisher.assert_not_called()
    assert out.startswith('Message sent successfully'), out


# ── CREATE: hartos.create_recipe.send_message_to_user1 (lifted) ────────

def _lift_create_send():
    path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
    src = open(path, encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == 'send_message_to_user1')
    post = MagicMock(name='pooled_post')
    from core.config_cache import is_bundled
    ns = {'json': json, 'pooled_post': post, 'request_id_list': {},
          'is_bundled': is_bundled,
          'current_app': SimpleNamespace(logger=MagicMock())}
    exec(ast.get_source_segment(src, fn), ns)
    return ns['send_message_to_user1'], post


def test_create_on_the_desktop_publishes_through_the_same_publisher(
        bundled, publisher):
    send, post = _lift_create_send()
    send('desk-3', 'Created your agent.', 'inp', 'p-1')
    post.assert_not_called()
    topic, payload = _published(publisher)
    assert topic == 'com.hertzai.hevolve.chat.desk-3'
    assert payload['text'] == ['Created your agent.']
    assert payload['request_id'] == 'desk-3_p-1-intermediate'


def test_create_on_central_still_forwards_over_http(central, publisher):
    send, post = _lift_create_send()
    send('cent-2', 'hello', 'inp', 'p-1')
    assert post.called
    publisher.assert_not_called()
