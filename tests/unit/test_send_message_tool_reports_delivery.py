"""The send_message_to_user tool tells the model what happened to its message.

Measured 2026-09-26 in the Nunba gui_app.log: the tool logged
"SENDING DATA 2 user ... I'd like to know if you have an..." at 00:02:10 and
returned "Message sent successfully" at once, because it started
send_message_to_user1 on a thread and returned before that ran.  9.5 s later
the send failed (WinError 10061).  The agent believed it had asked the user a
question and waited for an answer that could not come.

The tool now runs the send and returns ITS result, and CREATE's
send_message_to_user1 reports the outcome the way REUSE's already does
(it returned None, so a tool reading it could only guess).

The tool is built by the real core.agent_tools.build_core_tool_closures; only
send_message_to_user1 (the delivery boundary) is a stand-in.  CREATE's
function is lifted by name with ast, as test_agent_message_reaches_the_desktop
_locally does, because create_recipe cannot be imported in a bare env.
"""
import ast
import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SENT = 'Message sent successfully to user with request_id: r1'
_FAILED = 'Failed to send message to user with request_id: r1'


def _tool(send1, request_id_list=None):
    from core.agent_tools import build_core_tool_closures
    ctx = {
        'user_id': 'u1', 'prompt_id': 'p1', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 'u1_p1',
        'request_id_list': ({'u1_p1': 'r1'} if request_id_list is None
                            else request_id_list),
        'recent_file_id': {}, 'scheduler': MagicMock(),
        'send_message_to_user1': send1,
        'retrieve_json': lambda v: v, 'strip_json_values': lambda v: v,
        'save_conversation_db': MagicMock(),
    }
    return {n: f for n, _, f in build_core_tool_closures(ctx)}[
        'send_message_to_user']


def test_a_failed_send_is_reported_as_failed():
    send1 = MagicMock(return_value=_FAILED)
    out = _tool(send1)('Which month should I use?')
    assert out == _FAILED, out
    send1.assert_called_once_with('u1', 'Which month should I use?', '', 'p1')


def test_a_delivered_message_is_reported_with_the_senders_own_words():
    send1 = MagicMock(return_value=_SENT)
    assert _tool(send1)('hello') == _SENT


def test_the_send_has_finished_before_the_tool_answers():
    """The result is the send's, so the send must have run first -- on this
    thread, not a background one the tool never waits for."""
    ran_on = []

    def send1(*a):
        ran_on.append(threading.current_thread())
        return _SENT

    _tool(send1)('hello')
    assert ran_on == [threading.current_thread()]


def test_no_request_id_on_record_does_not_break_the_tool():
    """The tool used to build its own receipt from request_id_list[user_prompt]
    and raised KeyError when there was none; the sender owns the id."""
    send1 = MagicMock(return_value=_SENT)
    assert _tool(send1, request_id_list={})('hello') == _SENT


@pytest.mark.parametrize('result', [None, '', 42])
def test_a_sender_that_reports_nothing_is_never_read_as_success(result):
    out = _tool(MagicMock(return_value=result))('hello')
    assert 'successfully' not in out.lower(), out
    assert 'not confirmed' in out, out


def test_a_message_for_another_agent_is_still_not_sent():
    send1 = MagicMock(return_value=_SENT)
    out = _tool(send1)('@helper please run it')
    send1.assert_not_called()
    assert 'not sending to user' in out


# -- CREATE's send_message_to_user1 reports its outcome ---------------------

@pytest.fixture()
def bundled(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')


@pytest.fixture()
def central(monkeypatch):
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.delattr(sys, 'frozen', raising=False)


def _publisher(monkeypatch, pub):
    if pub is None:
        monkeypatch.delitem(sys.modules, 'hart_intelligence', raising=False)
        monkeypatch.delitem(sys.modules, 'hart_intelligence_entry',
                            raising=False)
    else:
        monkeypatch.setitem(sys.modules, 'hart_intelligence',
                            SimpleNamespace(publish_async=pub))


def _lift_create_send(post):
    path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
    src = open(path, encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == 'send_message_to_user1')
    from core.config_cache import is_bundled
    ns = {'json': json, 'pooled_post': post,
          'request_id_list': {'c1_p1': 'req9'},
          'is_bundled': is_bundled,
          'current_app': SimpleNamespace(logger=MagicMock())}
    exec(ast.get_source_segment(src, fn), ns)
    return ns['send_message_to_user1']


def test_create_desktop_publish_is_reported_sent(bundled, monkeypatch):
    pub = MagicMock()
    _publisher(monkeypatch, pub)
    out = _lift_create_send(MagicMock())('c1', 'hi', '', 'p1')
    assert pub.call_count == 1
    assert out == 'Message sent successfully to user with request_id: req9-intermediate'


def test_create_desktop_publish_that_did_not_happen_is_reported_failed(
        bundled, monkeypatch):
    _publisher(monkeypatch, None)
    out = _lift_create_send(MagicMock())('c1', 'hi', '', 'p1')
    assert out == 'Failed to send message to user with request_id: req9-intermediate'


def test_create_central_post_is_reported_sent(central, monkeypatch):
    _publisher(monkeypatch, MagicMock())
    post = MagicMock()
    out = _lift_create_send(post)('c1', 'hi', '', 'p1')
    assert post.call_count == 1
    assert out == 'Message sent successfully to user with request_id: req9-intermediate'


def test_create_central_post_that_raised_is_reported_failed(
        central, monkeypatch):
    _publisher(monkeypatch, MagicMock())
    post = MagicMock(side_effect=OSError('refused'))
    out = _lift_create_send(post)('c1', 'hi', '', 'p1')
    assert out == 'Failed to send message to user with request_id: req9-intermediate'
