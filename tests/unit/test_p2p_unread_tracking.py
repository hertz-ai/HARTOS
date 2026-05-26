"""#200 — P2P message unread tracking via memberships.last_read_at.

Previously: sync_service._row_message did `is_unread = not is_deleted`,
which meant every non-deleted message stayed forever marked unread.
The v46 migration added memberships.last_read_at; this test pins the
contract that the row mapper now consults it.

Read-state semantics:
  * Self-authored message  → NEVER unread (you wrote it, you read it)
  * Deleted message        → NEVER unread (gone — don't badge it)
  * No membership.last_read_at (legacy / pre-v46) → unread until
    the user opens the conversation and the mark-read endpoint fires
  * msg.created_at > membership.last_read_at AND author != viewer
    → unread
  * otherwise → read
"""
import unittest

from integrations.social.sync_service import _row_message


class P2PUnreadTests(unittest.TestCase):

    def test_self_authored_never_unread(self):
        r = _row_message({
            'id': 'm1',
            'parent_kind': 'conversation',
            'parent_id': 'c1',
            'author_id': 'me',
            '_viewer_id': 'me',
            'created_at': '2026-05-20T10:00:00',
            'membership_last_read_at': None,  # would normally mean unread
            'is_deleted': False,
            'content': 'hi from me',
        })
        self.assertFalse(r['is_unread'])

    def test_deleted_never_unread(self):
        r = _row_message({
            'id': 'm2',
            'author_id': 'other',
            '_viewer_id': 'me',
            'created_at': '2026-05-20T10:00:00',
            'membership_last_read_at': None,
            'is_deleted': True,
            'content': '',
        })
        self.assertFalse(r['is_unread'])

    def test_no_read_cursor_treats_as_unread(self):
        r = _row_message({
            'id': 'm3',
            'author_id': 'other',
            '_viewer_id': 'me',
            'created_at': '2026-05-20T10:00:00',
            'membership_last_read_at': None,
            'is_deleted': False,
            'content': 'hello',
        })
        self.assertTrue(r['is_unread'])

    def test_message_after_last_read_is_unread(self):
        r = _row_message({
            'id': 'm4',
            'author_id': 'other',
            '_viewer_id': 'me',
            'created_at': '2026-05-20T10:00:00',
            'membership_last_read_at': '2026-05-19T18:00:00',  # earlier
            'is_deleted': False,
            'content': 'newer than cursor',
        })
        self.assertTrue(r['is_unread'])

    def test_message_before_last_read_is_read(self):
        r = _row_message({
            'id': 'm5',
            'author_id': 'other',
            '_viewer_id': 'me',
            'created_at': '2026-05-19T09:00:00',
            'membership_last_read_at': '2026-05-19T18:00:00',  # later
            'is_deleted': False,
            'content': 'older than cursor',
        })
        self.assertFalse(r['is_unread'])

    def test_other_authored_after_self_read_cursor_is_unread(self):
        """The canonical Slack/Discord case: someone DM'd me after I
        last opened the chat — must show as unread."""
        r = _row_message({
            'id': 'm6',
            'author_id': 'bob',
            '_viewer_id': 'alice',
            'created_at': '2026-05-20T15:00:00',
            'membership_last_read_at': '2026-05-20T12:00:00',
            'is_deleted': False,
            'content': 'hey alice',
        })
        self.assertTrue(r['is_unread'])

    def test_no_viewer_id_does_not_crash(self):
        """Backwards compat — old call sites that don't pass _viewer_id
        still get a sensible answer (treat as unread, no crash)."""
        r = _row_message({
            'id': 'm7',
            'author_id': 'someone',
            'created_at': '2026-05-20T10:00:00',
            'membership_last_read_at': None,
            'is_deleted': False,
        })
        self.assertIsInstance(r['is_unread'], bool)


if __name__ == '__main__':
    unittest.main()
