"""send_fcm_push's direct leg -- core.fcm_sync.

With a node FCM credential the push goes out through the shared FCM post with
the person's locally cached token; with none, this node never calls Google.
"""
import unittest
from unittest.mock import patch

from core import fcm_sync


class UserPathStillWorks(unittest.TestCase):
    """The direct leg of send_fcm_push: credential gate, cached token, shared post."""

    def test_user_path_reuses_the_same_shared_post(self):
        with patch('core.fcm_sync._fcm_credential', return_value=('acc', 'proj')), \
             patch('core.fcm_sync.get_local_fcm_token', return_value='utok'), \
             patch('core.fcm_sync._post_fcm_message', return_value=True) as post:
            self.assertTrue(fcm_sync.send_fcm_push(10202, 't', 'b'))
            post.assert_called_once()
            self.assertEqual(post.call_args[0][2], 'utok')

    def test_user_no_credential_never_posts_to_fcm(self):
        # No credential: this node must not call Google.  It hands the push to
        # central's relay instead (test_fcm_central_handoff.py), and does
        # nothing at all when the caller opts out (relay=False).
        with patch('core.fcm_sync._fcm_credential', return_value=(None, None)), \
             patch('core.fcm_sync._post_fcm_message') as post, \
             patch('core.fcm_sync.hand_push_to_central', return_value=True) as hand:
            self.assertFalse(fcm_sync.send_fcm_push(10202, 't', 'b', relay=False))
            hand.assert_not_called()
            self.assertTrue(fcm_sync.send_fcm_push(10202, 't', 'b'))
            hand.assert_called_once()
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
