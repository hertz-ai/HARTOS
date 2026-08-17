"""
Reading a post must not depend on the write that counts the read.

Live on 2026-08-17: every link to a post returned 500 while the News list
happily showed the same posts. The post existed and was authorised. What
failed was the view counter:

    sqlalchemy.exc.OperationalError: (sqlite3.OperationalError)
    database is locked
    [SQL: UPDATE posts SET view_count=?, updated_at=CURRENT_TIMESTAMP
     WHERE posts.id = ?]

SQLite permits one writer at a time and this node has agents writing
continuously, so a lock is the ordinary case, not an exceptional one. The
counter ran FIRST and unguarded, so its failure took the whole response with
it, and the UI rendered that as "post not found". We went looking for a
missing row that was never missing.

These are deliberately SOURCE-LEVEL, for two reasons.

First, ordering is the fix. A future edit that moves the counter back above
the response build restores the outage without failing any behavioural test,
because the counter still works whenever the database happens to be unlocked.
The bug only appears under write contention, which a unit test does not have.

Second, importing integrations.social.services here binds the DB engine before
tests/test_phase7c5_post_privacy.py can configure its own, and 40 of its 45
tests then fail. Verified: privacy alone 45 pass, this file alone passes, the
two together failed 40, and an unrelated file before privacy passes 61. So
this file stays import-free on purpose.
"""
import io
import os
import unittest


def _api_source():
    # tests/unit/<file> -> up THREE levels to the repo root. Two lands on
    # tests/ and silently reads a path that does not exist, which reads as a
    # test failure rather than a missing file.
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return io.open(os.path.join(root, 'integrations', 'social', 'api.py'),
                   encoding='utf-8').read()


def _get_post_body():
    src = _api_source()
    start = src.index('def get_post(post_id):')
    end = src.index("@social_bp.route('/posts/<post_id>', methods=['PATCH'])")
    assert end > start, 'handler markers moved; this slice is wrong'
    return src[start:end]


class TestGetPostOrdering(unittest.TestCase):

    def test_the_response_is_built_before_the_counter_runs(self):
        """The fix itself. Build the payload first, so a failing counter can
        never cost the reader the post."""
        body = _get_post_body()
        self.assertLess(
            body.index('post.to_dict('), body.index('increment_view'),
            'the view counter runs before the response is built; a locked '
            'database would take the post down with it again',
        )

    def test_the_counter_is_wrapped_in_a_savepoint(self):
        """A bare try/except is not enough: a failed flush poisons the
        surrounding session, so the already-built response could not be
        returned either. The savepoint contains the rollback."""
        body = _get_post_body()
        self.assertIn('begin_nested', body)
        self.assertIn('try:', body)
        self.assertIn('except', body)

    def test_the_privacy_gate_still_precedes_the_counter(self):
        """Reordering must not have lifted the counter above the 404 gate.
        That would increment views on posts the caller cannot see, and leak
        their existence through the count."""
        body = _get_post_body()
        self.assertLess(body.index('can_view_post'), body.index('increment_view'))

    def test_a_counter_failure_is_logged_rather_than_swallowed_silently(self):
        """Best-effort must not mean invisible. If view counts quietly stop
        recording, someone has to be able to find out why."""
        body = _get_post_body()
        self.assertIn('logger.warning', body)


if __name__ == '__main__':
    unittest.main()
