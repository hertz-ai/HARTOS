"""Unit tests for MentionService._load_thread_context — the "read full
context before participate" hook.

What this pins:
  1. For source_kind='message', the agent receives the full thread of
     prior messages in chronological order (oldest-first).
  2. For source_kind='post', the post body + all comments under it.
  3. For source_kind='comment', the parent post + sibling comments.
  4. Encrypted DM placeholders ('[encrypted]') are filtered out so the
     LLM never sees them in its context.
  5. Empty thread (no prior messages) returns '' so the caller falls
     back to the mention-only prompt — never crashes.
  6. _dispatch_agent prepends the thread to the prompt when it's non-empty.

Uses ONLY existing tables (messages, posts, comments) via mocked DB
rows — no new schema, no new CRUD path.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.social.mention_service import MentionService


class _Row(tuple):
    """SQLAlchemy returns row-like tuples; indexable but also unpackable."""
    pass


class _FakeDB:
    """Routes db.execute(text('SELECT ...')) to canned responses keyed
    by a substring of the SQL text."""

    def __init__(self, routes):
        # routes is list of (sql_substring, return_value)
        self.routes = routes
        self.calls = []

    def execute(self, query, params=None):
        sql = str(query)
        self.calls.append((sql, params))
        for substr, retval in self.routes:
            if substr in sql:
                return _FakeResult(retval)
        return _FakeResult(None)


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        if self.value is None:
            return None
        if isinstance(self.value, list):
            return self.value[0] if self.value else None
        return self.value

    def fetchall(self):
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class LoadThreadContextTests(unittest.TestCase):

    def test_message_thread_chronological(self):
        """5 messages — oldest at the bottom of the SQL (DESC ORDER BY),
        reversed in output so the LLM reads top-down."""
        db = _FakeDB([
            ('FROM messages WHERE id', _Row(('conv-abc',))),
            ('parent_id=:pid', [
                _Row(('2026-05-23 19:35:00', 'alice', 'third')),
                _Row(('2026-05-23 19:30:00', 'bob', 'second')),
                _Row(('2026-05-23 19:25:00', 'alice', 'first')),
            ]),
        ])
        out = MentionService._load_thread_context(
            db, source_kind='message', source_id='m-1',
            requester_id='agent-x', max_messages=20,
        )
        # Reversed: chronological output (oldest first → top-down read)
        lines = out.split('\n')
        self.assertEqual(len(lines), 3)
        self.assertIn('first', lines[0])
        self.assertIn('second', lines[1])
        self.assertIn('third', lines[2])
        self.assertIn('@alice', lines[0])
        self.assertIn('@bob', lines[1])

    def test_message_thread_empty_returns_empty_string(self):
        db = _FakeDB([
            ('FROM messages WHERE id', _Row(('conv-empty',))),
            ('parent_id=:pid', []),
        ])
        out = MentionService._load_thread_context(
            db, source_kind='message', source_id='m-empty',
            requester_id='agent-x',
        )
        self.assertEqual(out, '')

    def test_message_thread_filters_encrypted_placeholders(self):
        """The SQL clause `content != '[encrypted]'` must keep
        encrypted DM rows out of the LLM's context."""
        db = _FakeDB([
            ('FROM messages WHERE id', _Row(('conv-mixed',))),
            ('parent_id=:pid', [
                _Row(('2026-05-23 19:35:00', 'alice', 'plain text 1')),
            ]),
        ])
        out = MentionService._load_thread_context(
            db, source_kind='message', source_id='m-x',
            requester_id='agent-x',
        )
        self.assertIn('plain text 1', out)
        # Find the SQL that filtered messages
        plaintext_query = next(
            (sql for sql, _ in db.calls if 'parent_id=:pid' in sql), '')
        self.assertIn("content != '[encrypted]'", plaintext_query)

    def test_message_missing_parent_returns_empty(self):
        db = _FakeDB([('FROM messages WHERE id', None)])
        out = MentionService._load_thread_context(
            db, source_kind='message', source_id='m-gone',
            requester_id='agent-x',
        )
        self.assertEqual(out, '')

    def test_post_thread_includes_post_and_comments(self):
        db = _FakeDB([
            ('FROM posts WHERE id', _Row((
                '2026-05-23 18:00:00', 'alice', 'Trading demo', 'See repo'
            ))),
            ('FROM comments', [
                _Row(('2026-05-23 18:05:00', 'bob', 'first comment')),
                _Row(('2026-05-23 18:10:00', 'carol', 'second comment')),
            ]),
        ])
        out = MentionService._load_thread_context(
            db, source_kind='post', source_id='p-1',
            requester_id='agent-x',
        )
        lines = out.split('\n')
        self.assertEqual(len(lines), 3)
        self.assertIn('Trading demo', lines[0])
        self.assertIn('See repo', lines[0])
        self.assertIn('first comment', lines[1])
        self.assertIn('second comment', lines[2])

    def test_comment_thread_resolves_to_parent_post(self):
        db = _FakeDB([
            ('FROM comments WHERE id', _Row(('p-root',))),
            ('FROM posts WHERE id', _Row((
                '2026-05-23 18:00:00', 'alice', 'Title', 'Body'
            ))),
            ('FROM comments WHERE', [
                _Row(('2026-05-23 18:05:00', 'bob', 'sibling comment 1')),
            ]),
        ])
        out = MentionService._load_thread_context(
            db, source_kind='comment', source_id='c-1',
            requester_id='agent-x',
        )
        self.assertIn('Title', out)
        self.assertIn('Body', out)
        self.assertIn('sibling comment 1', out)

    def test_unknown_source_kind_returns_empty(self):
        db = _FakeDB([])
        out = MentionService._load_thread_context(
            db, source_kind='weird', source_id='x',
            requester_id='agent-x',
        )
        self.assertEqual(out, '')


class DispatchAgentPromptShapeTests(unittest.TestCase):
    """End-to-end: ensure _dispatch_agent inserts the thread context
    into the prompt when present, and falls back cleanly when not."""

    def test_dispatch_agent_prepends_thread_when_context_available(self):
        from integrations.social import mention_service as ms

        captured = {}

        class _FakeRouter:
            @staticmethod
            def dispatch_to_agent(agent_id, prompt, context):
                captured['prompt'] = prompt
                captured['context'] = context

        with mock.patch.object(
            MentionService, '_load_thread_context',
            return_value='[t1] @alice: prior turn'
        ):
            with mock.patch.dict(sys.modules, {
                'integrations.agentic_router': _FakeRouter,
                'integrations': SimpleNamespace(agentic_router=_FakeRouter),
            }):
                MentionService._dispatch_agent(
                    db=mock.MagicMock(),
                    agent=SimpleNamespace(id='ag-1', username='helper_bot'),
                    source_kind='message',
                    source_id='m-1',
                    content='@helper_bot can you summarize?',
                    author_id='u-x',
                    tenant_id=None,
                )

        self.assertIn('Prior thread', captured.get('prompt', ''))
        self.assertIn('prior turn', captured['prompt'])
        self.assertIn('summarize', captured['prompt'])

    def test_dispatch_agent_falls_back_to_mention_only_when_context_empty(self):
        from integrations.social import mention_service as ms

        captured = {}

        class _FakeRouter:
            @staticmethod
            def dispatch_to_agent(agent_id, prompt, context):
                captured['prompt'] = prompt

        with mock.patch.object(
            MentionService, '_load_thread_context', return_value=''
        ):
            with mock.patch.dict(sys.modules, {
                'integrations.agentic_router': _FakeRouter,
                'integrations': SimpleNamespace(agentic_router=_FakeRouter),
            }):
                MentionService._dispatch_agent(
                    db=mock.MagicMock(),
                    agent=SimpleNamespace(id='ag-1', username='helper_bot'),
                    source_kind='post', source_id='p-1',
                    content='please respond',
                    author_id='u-y', tenant_id=None,
                )

        # Old prompt shape: no "Prior thread" header
        self.assertNotIn('Prior thread', captured.get('prompt', ''))
        self.assertIn('please respond', captured['prompt'])


if __name__ == '__main__':
    unittest.main()
