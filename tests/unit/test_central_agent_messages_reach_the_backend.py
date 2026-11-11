"""On central, an agent's message to its person reaches the backend that
delivers it, once, and is reported delivered only when the backend took it.

Measured on central 2026-11-10 (build f73df8f16), the first 15 minutes of
the owner's earning experiment: the earning goals' REUSE turns called
send_message_to_user 36 times and every one logged "Error sending message to
user: HTTPConnectionPool(host='aws_rasa.hertzai.com', port=9890): Max retries
exceeded".  REUSE's send_message_to_user1 posted to aws_rasa:9890 and CREATE's
copy to port 8443 of the same box: two URLs for one backend, both dead from
central.  The backend, the chatbot_pipeline container, is published on the
host's port 8001 (GET from inside HARTOS's container answered 405: the
POST-only route exists).

The review of 983c98e14 (which pointed both copies at one URL) ran the
senders against a local server and found three more defects:
- both copies answered "Message sent successfully" whatever the backend
  answered, and the backend answers 500 when the person has no session there;
- the shared pool re-sends a POST on a read timeout or a 503, so one slow
  send reached the backend three times;
- a default URL (port 8443) that is known dead.

Now both copies send through hartos.helper.post_agent_message: the URL from
core.config_cache.get_autogen_response_url (AUTOGEN_RESPONSE_URL, no
default), core.http_pool.pooled_post_once (never re-sent), delivered only on
a 2xx, and a failure says why.

The senders are the real functions (REUSE's imported, CREATE's lifted with
ast, as test_agent_message_reaches_the_desktop_locally does).  The wire is
core.http_pool.pooled_post_once, except in the sent-once test, which runs the
real transport against a real local HTTP server.  The deploy half runs the
whole deploy script under bash with stub docker
(test_a_deploy_cannot_fill_centrals_disk's harness).

    python -m pytest tests/unit/test_central_agent_messages_reach_the_backend.py -q
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.unit.test_agent_message_reaches_the_desktop_locally import (  # noqa: E402,F401
    BACKEND, _answered, _lift_create_send, _reuse_send, bundled, central,
    publisher, rr,
)


def _url(post):
    assert post.called, 'nothing was posted'
    args, kwargs = post.call_args
    return kwargs.get('url', args[0] if args else None)


def _send(which, rr, post, text='Your grocery order is ready to place.'):
    """One message through REUSE's or CREATE's real sender; its reply."""
    if which == 'reuse':
        out, _post = _reuse_send(rr, 'cent-1', text, post=post)
        return out
    send, _post = _lift_create_send(post)
    return send('cent-2', text, 'inp', 'p-1')


@pytest.mark.parametrize('which', ['reuse', 'create'])
def test_a_message_goes_to_the_configured_backend(which, rr, central, publisher):
    """THE 11-10 SENDS: REUSE's copy went to aws_rasa:9890, CREATE's to
    :8443, whatever was set."""
    post = _answered(200)
    out = _send(which, rr, post)
    assert _url(post) == BACKEND
    assert post.call_count == 1
    assert out.startswith('Message sent successfully'), out
    publisher.assert_not_called()


@pytest.mark.parametrize('status', [500, 404])
@pytest.mark.parametrize('which', ['reuse', 'create'])
def test_a_refused_message_is_not_reported_delivered(which, status, rr, central,
                                                     publisher):
    """Review of 983c98e14: the backend answers 500 when the person has no
    session there, and both copies said "sent".  The reply now says it was
    not, and why, and the step is not counted as delivered."""
    from hartos.lifecycle_hooks import send_delivered
    out = _send(which, rr, _answered(status))
    assert out.startswith('Failed to send message'), out
    assert f'HTTP {status}' in out, out
    assert send_delivered(out) is False


@pytest.mark.parametrize('which', ['reuse', 'create'])
def test_no_backend_set_sends_nothing_and_names_the_setting(which, rr, central,
                                                           publisher, monkeypatch):
    """No default: a guessed URL fails the same quiet way the two dead ones
    did.  Nothing is posted, and the agent is told which setting is
    missing."""
    monkeypatch.delenv('AUTOGEN_RESPONSE_URL', raising=False)
    from core.config_cache import get_autogen_response_url
    assert get_autogen_response_url() == ''
    post = _answered(200)
    out = _send(which, rr, post)
    post.assert_not_called()
    assert out.startswith('Failed to send message'), out
    assert 'AUTOGEN_RESPONSE_URL' in out, out


def test_a_blank_setting_is_no_setting(monkeypatch):
    from core.config_cache import get_autogen_response_url
    monkeypatch.setenv('AUTOGEN_RESPONSE_URL', '   ')
    assert get_autogen_response_url() == ''


def _free_loopback_server(status, delay=0.0):
    """A real HTTP server on 127.0.0.2 (not 'localhost'/'127.0.0.1', which
    the shared pool mounts with no retries, so the shared pool's remote
    retries would apply to it as they do to central's 172.17.0.1).  It
    records each body as it arrives, then waits ``delay`` seconds before it
    answers, as chatbot_pipeline does while it makes the video.  Returns
    (server, received bodies) or None when 127.0.0.2 cannot be bound."""
    received = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get('Content-Length') or 0)
            received.append(json.loads(self.rfile.read(n) or b'{}'))
            time.sleep(delay)
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_a):
            pass

    try:
        server = http.server.HTTPServer(('127.0.0.2', 0), _Handler)
    except (OSError, socket.error):
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def test_one_send_reaches_a_struggling_backend_once(rr, central, publisher,
                                                     monkeypatch):
    """Review of 983c98e14: one send to a slow backend arrived three times
    (the shared pool re-sends a POST twice).  A 503 makes the shared pool
    re-send too, without waiting out a timeout.  The real transport, a real
    server: one send, one POST, reported not delivered."""
    up = _free_loopback_server(503)
    if up is None:
        pytest.skip('127.0.0.2 cannot be bound here (macOS has only 127.0.0.1)')
    server, received = up
    try:
        monkeypatch.setenv('AUTOGEN_RESPONSE_URL',
                           f'http://127.0.0.2:{server.server_port}/autogen_response')
        from unittest.mock import patch
        with patch.object(rr, 'scheduler') as sched:
            sched.get_job.return_value = None
            out = rr.send_message_to_user1('cent-9', 'one message', 'inp', 'p-9')
    finally:
        server.shutdown()
        server.server_close()
    assert [r.get('message') for r in received] == ['one message']
    assert out.startswith('Failed to send message'), out
    assert 'HTTP 503' in out, out


def _same_turn(rr, monkeypatch, user_id):
    """Pin the turn's request id, as /chat does: REUSE keys its sent mark by
    it, so two sends of one text in one turn share the mark."""
    monkeypatch.setitem(rr.request_id_list, f'{user_id}_p-9', 'turn-1')


def _answers(*statuses):
    return MagicMock(name='pooled_post_once', side_effect=[
        SimpleNamespace(status_code=s, text='') for s in statuses])


def test_a_resend_after_a_refusal_goes_out(rr, central, publisher, monkeypatch):
    """Review of f1d599dd9: the first send got a 500 and the agent was told;
    its resend of the same text in the same turn made no POST and was told
    "Message already sent successfully", with nothing delivered.  A refusal
    now drops the sent mark."""
    _same_turn(rr, monkeypatch, 'cent-7')
    post = _answers(500, 200)
    first, _ = _reuse_send(rr, 'cent-7', 'Your order is ready.', post=post)
    again, _ = _reuse_send(rr, 'cent-7', 'Your order is ready.', post=post)
    assert first.startswith('Failed to send message'), first
    assert post.call_count == 2
    assert again.startswith('Message sent successfully'), again


def test_a_delivered_message_is_not_sent_twice(rr, central, publisher,
                                               monkeypatch):
    """The mark still does its job: a delivered text sent again in the same
    turn is not posted a second time."""
    _same_turn(rr, monkeypatch, 'cent-6')
    post = _answers(200, 200)
    _reuse_send(rr, 'cent-6', 'Your order is ready.', post=post)
    again, _ = _reuse_send(rr, 'cent-6', 'Your order is ready.', post=post)
    assert post.call_count == 1
    assert again.startswith('Message already sent'), again


def test_a_resend_after_a_failed_local_publish_goes_out(rr, bundled, monkeypatch):
    """The desktop branch had the same trap: a publish that failed kept the
    sent mark, so the agent's resend was answered "already sent"."""
    from core.peer_link import crossbar_publish
    _same_turn(rr, monkeypatch, 'desk-7')
    pub = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(crossbar_publish, 'publish_agent_message', pub)
    first, _ = _reuse_send(rr, 'desk-7', 'Which month?')
    again, _ = _reuse_send(rr, 'desk-7', 'Which month?')
    assert first.startswith('Failed to send message'), first
    assert pub.call_count == 2
    assert again.startswith('Message sent successfully'), again


@pytest.mark.parametrize('which', ['reuse', 'create'])
def test_no_answer_in_time_tells_the_agent_not_to_resend(which, rr, central,
                                                        publisher):
    """Review of f1d599dd9: a read timeout was reported as "could not be
    reached", but the request had gone out whole and chatbot_pipeline
    publishes the text before it makes the video, so the resend it invited
    reached the person twice."""
    post = MagicMock(name='pooled_post_once',
                     side_effect=requests.exceptions.ReadTimeout('slow'))
    out = _send(which, rr, post)
    assert out.startswith('Failed to send message'), out
    assert 'do not send it again' in out, out
    assert 'could not be reached' not in out, out


def test_unreachable_is_still_safe_to_resend(rr, central, publisher):
    """A refused connection never reached the backend: that reply must not
    tell the agent to hold back."""
    post = MagicMock(name='pooled_post_once',
                     side_effect=requests.exceptions.ConnectionError('refused'))
    out = _send('reuse', rr, post)
    assert 'could not be reached' in out, out
    assert 'do not send it again' not in out, out


def test_a_slow_backend_gets_one_copy_across_a_resend(rr, central, publisher,
                                                      monkeypatch):
    """The real transport against a real server that takes the message and
    answers after the read budget: one POST, the agent is told not to send
    it again, and its resend in the same turn posts nothing."""
    from core import http_pool
    up = _free_loopback_server(200, delay=2.0)
    if up is None:
        pytest.skip('127.0.0.2 cannot be bound here (macOS has only 127.0.0.1)')
    server, received = up
    _same_turn(rr, monkeypatch, 'cent-8')
    monkeypatch.setattr(http_pool, 'LLM_COMPLETION_TIMEOUT', (3, 0.5))
    try:
        monkeypatch.setenv('AUTOGEN_RESPONSE_URL',
                           f'http://127.0.0.2:{server.server_port}/autogen_response')
        with patch.object(rr, 'scheduler') as sched:
            sched.get_job.return_value = None
            first = rr.send_message_to_user1('cent-8', 'one message', 'inp', 'p-9')
            again = rr.send_message_to_user1('cent-8', 'one message', 'inp', 'p-9')
        time.sleep(2.5)  # let the server finish answering the one it took
    finally:
        server.shutdown()
        server.server_close()
    assert [r.get('message') for r in received] == ['one message']
    assert 'do not send it again' in first, first
    assert again.startswith('Message already sent'), again


def test_centrals_deploy_points_both_starts_at_the_backend():
    """The deploy sets the URL on the HARTOS container's normal start and on
    the rollback start (a rolled-back central must deliver too), and an
    operator can point it elsewhere."""
    from tests.unit.test_a_deploy_cannot_fill_centrals_disk import (
        _BASH, ADeployCannotFillCentralsDisk, _pairs)
    if not _BASH:
        pytest.skip('no bash: the deploy script cannot run here')
    case = ADeployCannotFillCentralsDisk('test_the_hartos_container_starts_with_a_bounded_log')
    for status_up, n_runs, extra, url in (
            ('1', 1, {}, 'http://172.17.0.1:8001/autogen_response'),
            ('0', 2, {}, 'http://172.17.0.1:8001/autogen_response'),
            ('1', 1, {'DEPLOY_AUTOGEN_RESPONSE_URL': 'http://10.0.0.5:8001/autogen_response'},
             'http://10.0.0.5:8001/autogen_response')):
        case.setUp()
        try:
            code, out, docker = case._deploy(DEPLOY_TEST_STATUS_UP=status_up, **extra)
            runs = case._hartos_runs(docker)
            assert len(runs) == n_runs, out[-3000:]
            for run in runs:
                assert ('-e', 'AUTOGEN_RESPONSE_URL=' + url) in _pairs(run), run
        finally:
            case.doCleanups()
