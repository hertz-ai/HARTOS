"""Node-keyed FCM reach (pre-login) — core.fcm_sync.

send_fcm_push_to_node reaches a device by its peer_link node_id (the token was
registered centrally against the ed25519 identity via POST /register_node_token),
reusing the SAME credential gate + FCM-post as send_fcm_push — no parallel send.
Also asserts the user-keyed path still works after the extract-refactor.

requests is imported inside the functions, so the network is stubbed at
'requests.get' / patched helpers rather than a module attribute.
"""
import unittest
from unittest.mock import patch, MagicMock

from core import fcm_sync


def _resp(status, json_body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body
    return r


class FetchTokenByNode(unittest.TestCase):

    def test_bare_string_body(self):
        # /get_node_token returns the raw token string (mirrors get_fcm_token).
        with patch('requests.get', return_value=_resp(200, 'tok-bare')):
            self.assertEqual(fcm_sync.fetch_central_fcm_token_by_node('nodeX'), 'tok-bare')

    def test_dict_wrapped_body(self):
        # A mailer that wraps as {"token": ...} still parses (via canonical parser).
        with patch('requests.get', return_value=_resp(200, {'token': 'tok-d'})):
            self.assertEqual(fcm_sync.fetch_central_fcm_token_by_node('nodeX'), 'tok-d')

    def test_unregistered_404_is_none(self):
        with patch('requests.get', return_value=_resp(404, {'detail': 'node not Found'})):
            self.assertIsNone(fcm_sync.fetch_central_fcm_token_by_node('nodeX'))

    def test_empty_node_is_none(self):
        self.assertIsNone(fcm_sync.fetch_central_fcm_token_by_node(''))

    def test_network_error_never_raises(self):
        with patch('requests.get', side_effect=OSError('down')):
            self.assertIsNone(fcm_sync.fetch_central_fcm_token_by_node('nodeX'))


class SendPushToNode(unittest.TestCase):

    def test_no_credential_is_noop(self):
        with patch('core.fcm_sync._fcm_credential', return_value=(None, None)):
            self.assertFalse(fcm_sync.send_fcm_push_to_node('nodeX', 't', 'b'))

    def test_no_token_is_noop(self):
        with patch('core.fcm_sync._fcm_credential', return_value=('acc', 'proj')), \
             patch('core.fcm_sync.fetch_central_fcm_token_by_node', return_value=None):
            self.assertFalse(fcm_sync.send_fcm_push_to_node('nodeX', 't', 'b'))

    def test_happy_path_reuses_shared_post(self):
        with patch('core.fcm_sync._fcm_credential', return_value=('acc', 'proj')), \
             patch('core.fcm_sync.fetch_central_fcm_token_by_node', return_value='tok'), \
             patch('core.fcm_sync._post_fcm_message', return_value=True) as post:
            self.assertTrue(fcm_sync.send_fcm_push_to_node('nodeX', 't', 'b', {'k': 'v'}))
            post.assert_called_once()
            # (access, project, token, title, body, data, timeout)
            a = post.call_args[0]
            self.assertEqual((a[0], a[1], a[2]), ('acc', 'proj', 'tok'))

    def test_empty_node_is_noop(self):
        self.assertFalse(fcm_sync.send_fcm_push_to_node('', 't', 'b'))


class UserPathStillWorks(unittest.TestCase):
    """The extract-refactor must not change send_fcm_push behavior."""

    def test_user_path_reuses_the_same_shared_post(self):
        with patch('core.fcm_sync._fcm_credential', return_value=('acc', 'proj')), \
             patch('core.fcm_sync.get_local_fcm_token', return_value='utok'), \
             patch('core.fcm_sync._post_fcm_message', return_value=True) as post:
            self.assertTrue(fcm_sync.send_fcm_push(10202, 't', 'b'))
            post.assert_called_once()
            self.assertEqual(post.call_args[0][2], 'utok')

    def test_user_no_credential_is_noop(self):
        with patch('core.fcm_sync._fcm_credential', return_value=(None, None)):
            self.assertFalse(fcm_sync.send_fcm_push(10202, 't', 'b'))


if __name__ == '__main__':
    unittest.main()
