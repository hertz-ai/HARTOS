"""FCM token cache: a bare-string registry hit is parsed, and a token FCM says
is stale is dropped so the next push re-syncs it.

Hevolve_Database.get_fcm_token answers a bare token string; the parser used
to accept only a dict, so fetch_central_fcm_token returned None on every hit
and the local cache was never filled.
"""
from unittest.mock import MagicMock, patch

from core import fcm_sync


def _resp(status, body, text=''):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = text
    return r


# ── a bare-string hit ───────────────────────────────────────────────

def test_a_bare_string_hit_is_a_token():
    with patch('requests.get', return_value=_resp(200, 'tok-bare')):
        assert fcm_sync.fetch_central_fcm_token('9003054371') == 'tok-bare'


def test_a_miss_dict_is_still_none():
    with patch('requests.get', return_value=_resp(200, {'detail': 'user not Found'})):
        assert fcm_sync.fetch_central_fcm_token('9003054371') is None


def test_the_node_variant_uses_the_same_parser():
    with patch('requests.get', return_value=_resp(200, 'tok-node')):
        assert fcm_sync.fetch_central_fcm_token_by_node('nodeX') == 'tok-node'
    with patch('requests.get', return_value=_resp(200, {'token': 'tok-wrapped'})):
        assert fcm_sync.fetch_central_fcm_token_by_node('nodeX') == 'tok-wrapped'


# ── stale detection ─────────────────────────────────────────────────

def test_what_counts_as_a_stale_token():
    assert fcm_sync._fcm_token_is_stale(404, '') is True
    assert fcm_sync._fcm_token_is_stale(400, 'The registration token is not a valid FCM registration token') is True
    assert fcm_sync._fcm_token_is_stale(200, 'UNREGISTERED') is True
    assert fcm_sync._fcm_token_is_stale(500, 'backend error') is False
    assert fcm_sync._fcm_token_is_stale(401, 'bad credential') is False
    assert fcm_sync._fcm_token_is_stale(400, 'payload too large') is False


def _post(status, text, on_stale):
    with patch('requests.post', return_value=_resp(status, {}, text)):
        return fcm_sync._post_fcm_message('acc', 'proj', 'tok', 't', 'b', None, 8,
                                          on_stale=on_stale)


def test_a_stale_token_calls_the_handler_and_the_push_fails():
    on_stale = MagicMock()
    assert _post(404, '{"error":{"status":"NOT_FOUND","details":[{"errorCode":"UNREGISTERED"}]}}',
                 on_stale) is False
    on_stale.assert_called_once()


def test_a_server_error_does_not_drop_the_token():
    on_stale = MagicMock()
    assert _post(500, 'oops', on_stale) is False
    on_stale.assert_not_called()


def test_a_200_does_not_call_the_handler():
    on_stale = MagicMock()
    assert _post(200, '', on_stale) is True
    on_stale.assert_not_called()


def test_a_failing_handler_never_breaks_the_push_result():
    assert _post(404, '', MagicMock(side_effect=RuntimeError('x'))) is False


def test_a_failed_push_is_logged_at_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger='hevolve.fcm_sync'):
        _post(500, 'oops', None)
    assert 'FCM send failed' in caplog.text


# ── send_fcm_push wires it to the cache ─────────────────────────────

def test_send_fcm_push_forgets_the_cached_token_when_fcm_says_it_is_stale():
    def post(access, project, token, title, body, data, timeout, on_stale=None):
        on_stale()
        return False
    with patch.object(fcm_sync, '_fcm_credential', return_value=('acc', 'proj')), \
            patch.object(fcm_sync, 'get_local_fcm_token', return_value='old'), \
            patch.object(fcm_sync, '_post_fcm_message', side_effect=post), \
            patch.object(fcm_sync, 'forget_local_fcm_token') as forget:
        assert fcm_sync.send_fcm_push('u1', 't', 'b') is False
    forget.assert_called_once_with('u1')


def test_forget_deletes_that_users_row():
    db = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    with patch('integrations.social.models.db_session', return_value=ctx):
        assert fcm_sync.forget_local_fcm_token('u1') is True
    sql = [str(c.args[0]) for c in db.execute.call_args_list]
    assert any('DELETE FROM fcm_tokens' in q for q in sql)
    assert db.execute.call_args_list[-1].args[1] == {'u': 'u1'}


def test_forget_with_no_user_is_a_noop():
    assert fcm_sync.forget_local_fcm_token('') is False
