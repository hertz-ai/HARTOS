"""
Tests for security/action_classifier.py — destructive action detection + preview.

Run: pytest tests/unit/test_action_classifier.py -v --noconftest
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from security.action_classifier import classify_action, should_preview


class TestClassifyAction(unittest.TestCase):
    """Action classification tests."""

    def test_delete_is_destructive(self):
        self.assertEqual(classify_action("DELETE FROM users WHERE id=5"), 'destructive')

    def test_drop_table_is_destructive(self):
        self.assertEqual(classify_action("DROP TABLE sessions"), 'destructive')

    def test_rm_rf_is_destructive(self):
        self.assertEqual(classify_action("rm -rf /var/log/app"), 'destructive')

    def test_truncate_is_destructive(self):
        self.assertEqual(classify_action("TRUNCATE TABLE logs"), 'destructive')

    def test_git_force_push_is_destructive(self):
        self.assertEqual(classify_action("git push --force origin main"), 'destructive')

    def test_git_reset_hard_is_destructive(self):
        self.assertEqual(classify_action("git reset --hard HEAD~5"), 'destructive')

    def test_shutdown_is_destructive(self):
        self.assertEqual(classify_action("shutdown -h now"), 'destructive')

    def test_read_file_is_safe(self):
        self.assertEqual(classify_action("read the config file"), 'safe')

    def test_select_query_is_safe(self):
        self.assertEqual(classify_action("SELECT * FROM users"), 'safe')

    def test_git_status_is_safe(self):
        self.assertEqual(classify_action("git status"), 'safe')

    def test_list_files_is_safe(self):
        self.assertEqual(classify_action("list all files in directory"), 'safe')

    def test_empty_is_unknown(self):
        self.assertEqual(classify_action(""), 'unknown')
        self.assertEqual(classify_action("   "), 'unknown')

    def test_ambiguous_is_unknown(self):
        self.assertEqual(classify_action("process the data batch"), 'unknown')

    def test_destructive_overrides_safe_keywords(self):
        """If both safe and destructive match, destructive wins."""
        # "show" is safe, "delete" is destructive
        result = classify_action("show me how to delete the database")
        self.assertEqual(result, 'destructive')


class TestShouldPreview(unittest.TestCase):
    """Preview gate tests."""

    def test_preview_disabled_returns_false(self):
        self.assertFalse(should_preview("DELETE FROM users", preview_enabled=False))

    def test_preview_enabled_destructive_returns_true(self):
        self.assertTrue(should_preview("DROP TABLE users", preview_enabled=True))

    def test_preview_enabled_safe_returns_false(self):
        self.assertFalse(should_preview("read config file", preview_enabled=True))

    def test_preview_enabled_unknown_returns_true(self):
        """Unknown actions should also be previewed when enabled."""
        self.assertTrue(should_preview("run the batch job", preview_enabled=True))


class TestPreviewStatesExist(unittest.TestCase):
    """Verify lifecycle_hooks has preview states."""

    def test_preview_states_in_action_state(self):
        from lifecycle_hooks import ActionState
        self.assertTrue(hasattr(ActionState, 'PREVIEW_PENDING'))
        self.assertTrue(hasattr(ActionState, 'PREVIEW_APPROVED'))


if __name__ == '__main__':
    unittest.main()
