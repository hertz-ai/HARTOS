"""On central, an agent's message to its person reaches the backend that
delivers it.

Measured on central 2026-11-10 (build f73df8f16), the first 15 minutes of
the owner's earning experiment: the earning goals' REUSE turns called
send_message_to_user 36 times and every one logged "Error sending message to
user: HTTPConnectionPool(host='aws_rasa.hertzai.com', port=9890): Max retries
exceeded".  REUSE's send_message_to_user1 posted to
http://aws_rasa.hertzai.com:9890/autogen_response and CREATE's copy to
https://azurekong.hertzai.com:8443/autogen_response: two URLs for one
backend, and both dead from central (both names are the box's public
address, where neither port answers; Kong publishes its 8443 as 8543 and has
no /autogen_response route).  The backend, the chatbot_pipeline container,
is published on the host's port 8001: from inside HARTOS's container,
GET http://172.17.0.1:8001/autogen_response answered 405 (the POST-only
route exists).

Now both copies post to ONE URL, hartos.helper.autogen_response_url:
AUTOGEN_RESPONSE_URL when set (central's deploy sets the backend's own port;
the e2e compose already set it for its mock), else one default.

The two senders are the real functions, called the way the existing desktop
tests call them (test_agent_message_reaches_the_desktop_locally: REUSE's
imported, CREATE's lifted with ast); the HTTP pool is the boundary.
The deploy half runs the whole deploy script under bash with stub docker
(test_a_deploy_cannot_fill_centrals_disk's harness).

    python -m pytest tests/unit/test_central_agent_messages_reach_the_backend.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.unit.test_agent_message_reaches_the_desktop_locally import (  # noqa: E402,F401
    _lift_create_send, _reuse_send, central, publisher, rr,
)

BACKEND = 'http://172.17.0.1:8001/autogen_response'


def _url(post):
    assert post.called, 'nothing was posted'
    args, kwargs = post.call_args
    return kwargs.get('url', args[0] if args else None)


def test_reuse_posts_to_the_configured_backend(rr, central, publisher, monkeypatch):
    """THE 11-10 SENDS: REUSE's copy went to aws_rasa:9890 whatever was set."""
    monkeypatch.setenv('AUTOGEN_RESPONSE_URL', BACKEND)
    out, post = _reuse_send(rr, 'cent-1', 'Your grocery order is ready to place.')
    assert _url(post) == BACKEND
    assert out.startswith('Message sent successfully'), out


def test_create_posts_to_the_configured_backend(central, publisher, monkeypatch):
    monkeypatch.setenv('AUTOGEN_RESPONSE_URL', BACKEND)
    send, post = _lift_create_send()
    send('cent-2', 'Created your agent.', 'inp', 'p-1')
    assert _url(post) == BACKEND


def test_both_copies_post_to_one_url_when_nothing_is_set(rr, central, publisher,
                                                         monkeypatch):
    """The drift itself: two copies of one sender, two different URLs."""
    monkeypatch.delenv('AUTOGEN_RESPONSE_URL', raising=False)
    from hartos.helper import AUTOGEN_RESPONSE_URL_DEFAULT
    _out, reuse_post = _reuse_send(rr, 'cent-3', 'hello')
    send, create_post = _lift_create_send()
    send('cent-4', 'hello', 'inp', 'p-1')
    assert _url(reuse_post) == _url(create_post) == AUTOGEN_RESPONSE_URL_DEFAULT


def test_a_blank_setting_falls_back_to_the_default(monkeypatch):
    from hartos.helper import AUTOGEN_RESPONSE_URL_DEFAULT, autogen_response_url
    monkeypatch.setenv('AUTOGEN_RESPONSE_URL', '   ')
    assert autogen_response_url() == AUTOGEN_RESPONSE_URL_DEFAULT


def test_centrals_deploy_points_both_starts_at_the_backend():
    """The deploy sets the URL on the HARTOS container's normal start and on
    the rollback start: a rolled-back central must deliver too."""
    from tests.unit.test_a_deploy_cannot_fill_centrals_disk import (
        _BASH, ADeployCannotFillCentralsDisk, _pairs)
    if not _BASH:
        pytest.skip('no bash: the deploy script cannot run here')
    want = ('-e', 'AUTOGEN_RESPONSE_URL=' + BACKEND)
    case = ADeployCannotFillCentralsDisk('test_the_hartos_container_starts_with_a_bounded_log')
    for status_up, n_runs in (('1', 1), ('0', 2)):
        case.setUp()
        try:
            code, out, docker = case._deploy(DEPLOY_TEST_STATUS_UP=status_up)
            runs = case._hartos_runs(docker)
            assert len(runs) == n_runs, out[-3000:]
            for run in runs:
                assert want in _pairs(run), run
        finally:
            case.doCleanups()
