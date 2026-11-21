"""
Tests for security/immutable_audit_log.py — tamper-evident hash chain.

Run: pytest tests/unit/test_immutable_audit_log.py -v --noconftest
"""
import os
import sys
import types
import json
import unittest
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from security.immutable_audit_log import (
    ImmutableAuditLog, _compute_hash, _redact_sensitive, get_audit_log,
)


class TestImmutableAuditLog(unittest.TestCase):
    """Core audit log functionality."""

    def setUp(self):
        # Fresh in-memory instance for each test (no DB)
        self.log = ImmutableAuditLog()
        self.log._use_db = False
        self.log._memory_log = []

    def test_log_event_returns_id_and_hash(self):
        entry_id, entry_hash = self.log.log_event(
            'state_change', actor_id='user_1', action='completed task 5')
        self.assertEqual(entry_id, 1)
        self.assertIsInstance(entry_hash, str)
        self.assertEqual(len(entry_hash), 64)  # SHA-256 hex

    def test_chain_integrity_multiple_entries(self):
        """Multiple entries form a valid chain."""
        for i in range(10):
            self.log.log_event('test', actor_id=f'user_{i}', action=f'action_{i}')

        ok, reason = self.log.verify_chain()
        self.assertTrue(ok, f"Chain should be valid: {reason}")
        self.assertIn('10 entries', reason)

    def test_tamper_detection(self):
        """Modifying an entry breaks the chain."""
        self.log.log_event('auth', actor_id='user_1', action='login')
        self.log.log_event('state_change', actor_id='user_1', action='update')
        self.log.log_event('auth', actor_id='user_1', action='logout')

        # Tamper with middle entry
        self.log._memory_log[1]['action'] = 'TAMPERED'

        ok, reason = self.log.verify_chain()
        self.assertFalse(ok, "Chain should be broken after tamper")
        self.assertIn('Chain broken', reason)

    def test_empty_chain_valid(self):
        ok, reason = self.log.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(reason, 'Empty log')

    def test_sensitive_redaction(self):
        """Sensitive fields in detail are redacted."""
        self.log.log_event(
            'auth', actor_id='user_1', action='login',
            detail={'username': 'john', 'password': 'secret123', 'token': 'abc'})

        entry = self.log._memory_log[0]
        self.assertIn('[REDACTED]', entry['detail_json'])
        self.assertNotIn('secret123', entry['detail_json'])
        self.assertIn('john', entry['detail_json'])  # username not redacted

    def test_get_trail_filters(self):
        """Trail filtering by actor and event type."""
        self.log.log_event('auth', actor_id='user_1', action='login')
        self.log.log_event('state_change', actor_id='user_2', action='update')
        self.log.log_event('auth', actor_id='user_1', action='logout')
        self.log.log_event('state_change', actor_id='user_1', action='delete')

        # Filter by actor
        trail = self.log.get_trail(actor_id='user_1')
        self.assertEqual(len(trail), 3)

        # Filter by event type
        trail = self.log.get_trail(event_type='auth')
        self.assertEqual(len(trail), 2)

        # Both filters
        trail = self.log.get_trail(actor_id='user_1', event_type='auth')
        self.assertEqual(len(trail), 2)

    def test_concurrent_writes(self):
        """Concurrent writes must not corrupt the chain."""
        errors = []

        def write_batch(prefix, count):
            try:
                for i in range(count):
                    self.log.log_event('test', actor_id=prefix, action=f'{prefix}_{i}')
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=write_batch, args=(f't{i}', 20))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent write errors: {errors}")
        self.assertEqual(len(self.log._memory_log), 100)

        ok, reason = self.log.verify_chain()
        self.assertTrue(ok, f"Chain should be valid after concurrent writes: {reason}")


class TestHashFunction(unittest.TestCase):
    """Verify hash computation is deterministic."""

    def test_deterministic_hash(self):
        h1 = _compute_hash('prev', 'type', 'actor', 'action', '2026-01-01', None)
        h2 = _compute_hash('prev', 'type', 'actor', 'action', '2026-01-01', None)
        self.assertEqual(h1, h2)

    def test_different_inputs_different_hash(self):
        h1 = _compute_hash('prev', 'type', 'actor', 'action_A', '2026-01-01', None)
        h2 = _compute_hash('prev', 'type', 'actor', 'action_B', '2026-01-01', None)
        self.assertNotEqual(h1, h2)


class TestRedactSensitive(unittest.TestCase):

    def test_redacts_password(self):
        result = _redact_sensitive({'password': 'secret', 'name': 'john'})
        self.assertIn('[REDACTED]', result)
        self.assertNotIn('secret', result)
        self.assertIn('john', result)

    def test_none_detail(self):
        self.assertIsNone(_redact_sensitive(None))

    def test_a_secret_is_named_by_the_one_rule(self):
        """The log's own substring list kept 'passwd', 'pwd', 'cookie' and
        'apikey' (claude-autostart-71's review of 14e8e7907); the one rule
        (security.edge_privacy.is_secret_key) names them."""
        detail = {'passwd': 'p1', 'user-pwd': 'p2', 'sessionCookie': 'c1',
                  'apikey': 'k1', 'x-api-key': 'k2', 'ssn': '123-45-6789',
                  'card_number': '4111', 'name': 'john'}
        result = json.loads(_redact_sensitive(detail))
        for key in ('passwd', 'user-pwd', 'sessionCookie', 'apikey', 'x-api-key',
                    'ssn', 'card_number'):
            self.assertEqual(result[key], '[REDACTED]', key)
        self.assertEqual(result['name'], 'john')

    def test_a_secret_word_anywhere_in_the_key_is_withheld(self):
        """The one rule reads a secret word only as the whole name or its
        last word; the log is hash-chained, so these stay withheld by the
        log's own catch (review of 2f3d2255e, which kept them raw).  A count
        of tokens is withheld with them."""
        names = ('tokens', 'auth_tokens', 'access_tokens', 'refresh_tokens',
                 'token_value', 'secrets', 'client_secrets', 'api_keys',
                 'secret_answer', 'credential_id', 'ssn_last4', 'max_tokens')
        result = json.loads(_redact_sensitive(
            dict({n: 'raw-' + n for n in names}, name='john')))
        for key in names:
            self.assertEqual(result[key], '[REDACTED]', key)
        self.assertEqual(result['name'], 'john')

    def test_the_catch_reads_every_spelling_and_a_number_only_as_a_word(self):
        """Read only lowercased, 'apiKeys' and 'privateKeys' stayed raw while
        'api_keys' was withheld; read as three letters anywhere, 'ssn'
        withheld 'classname' and 'processName' (review of fbae5ea36)."""
        withheld = ('apiKeys', 'API-Keys', 'privateKeys', 'private-key-pem',
                    'ssn_last4', 'ssnLast4', 'mySSN', 'user_ssn', 'SSN')
        kept = ('classname', 'className', 'processName', 'businessName',
                'addressName', 'witnessNode')
        result = json.loads(_redact_sensitive(
            {n: 'raw-' + n for n in withheld + kept}))
        for key in withheld:
            self.assertEqual(result[key], '[REDACTED]', key)
        for key in kept:
            self.assertEqual(result[key], 'raw-' + key, key)

    def test_a_number_glued_or_plural_and_a_glued_plural_secret_are_withheld(self):
        """Read only as a word of its own, 'ssn' let through raw ten spellings
        the log had withheld ('ssns', 'SSNumber', 'userssn'), and the glued
        plurals of the two-word secrets were never caught (review of
        d2a2f2ad4, M-1 and m-1).  Everyday names that hold the same letters
        stay readable."""
        withheld = ('ssns', 'SSNs', 'SSNumber', 'ssNumber', 'ssnumber',
                    'ssnlast4', 'ssnhash', 'userssn', 'customerssn',
                    'encryptedssn', 'mySsnValue', 'apikeys', 'privatekeys',
                    'creditcards', 'cardnumbers')
        kept = ('accessNumber', 'accessnumber', 'unlessNested', 'crossNet',
                'glassNode', 'harnessName', 'fitnessNote', 'chessNotation',
                'missingNodes', 'bossName', 'lessonNumber', 'successNotice',
                'businessNumber', 'progressNote', 'processNode', 'pressNumber',
                'assignee', 'sessionName', 'card_count', 'publicKey')
        result = json.loads(_redact_sensitive(
            {n: 'raw-' + n for n in withheld + kept}))
        for key in withheld:
            self.assertEqual(result[key], '[REDACTED]', key)
        for key in kept:
            self.assertEqual(result[key], 'raw-' + key, key)


class TestBorrowedSessionContract(unittest.TestCase):
    """log_event(db=...) rides the CALLER'S transaction.

    Why this exists (measured, not theorized): settle_metered_api_costs was
    mid-transaction — wallet credit + spark transaction flushed, uncommitted —
    when it wrote its audit entry. log_event opened its OWN session; on a
    shared-connection pool (sqlite:// StaticPool, i.e. every in-memory test
    env including CI) checking that second session's connection out RESET it,
    which ROLLED BACK the caller's writes. The SQL trace read:

        INSERT resonance_wallets / INSERT resonance_transactions /
        UPDATE metered_api_usage ... ROLLBACK ... INSERT audit_log_entries /
        COMMIT

    — the settlement was destroyed, its audit row survived, and settle
    reported success. On a real database the same design commits the entry
    even when the caller's transaction later rolls back: the immutable trail
    then attests to work that never happened.
    """

    def _fresh_log(self):
        import security.immutable_audit_log as mod
        log = ImmutableAuditLog()
        log._use_db = True
        return log

    def test_borrowed_session_is_never_committed_rolled_back_or_closed(self):
        from unittest.mock import MagicMock
        log = self._fresh_log()
        db = MagicMock()
        # _get_last_hash path: no prior entries.
        db.query.return_value.order_by.return_value.first.return_value = None

        log.log_event('security', 'tester', 'borrowed-session probe', db=db)

        db.add.assert_called_once()
        db.flush.assert_called_once()      # id materialised on the caller's txn
        db.commit.assert_not_called()
        db.rollback.assert_not_called()
        db.close.assert_not_called()

    def test_borrowed_session_entry_carries_the_chain_hash(self):
        """The entry added to the caller's session is a REAL chain link:
        prev_hash from the same session's view, entry_hash recomputable."""
        from unittest.mock import MagicMock
        from security.immutable_audit_log import _compute_hash
        log = self._fresh_log()
        db = MagicMock()
        prior = MagicMock()
        prior.entry_hash = 'abc123'
        db.query.return_value.order_by.return_value.first.return_value = prior

        log.log_event('security', 'tester', 'chained', db=db)

        entry = db.add.call_args[0][0]
        self.assertEqual(entry.prev_hash, 'abc123')
        recomputed = _compute_hash(
            entry.prev_hash, entry.event_type, entry.actor_id, entry.action,
            entry.created_at.isoformat(), entry.detail_json)
        self.assertEqual(entry.entry_hash, recomputed)

    def test_without_db_the_owned_session_still_commits(self):
        """The pre-existing contract for every caller that does NOT pass a
        session is untouched: own session, own commit, closed after."""
        from unittest.mock import MagicMock, patch
        log = self._fresh_log()
        own = MagicMock()
        own.query.return_value.order_by.return_value.first.return_value = None
        fake_models = MagicMock()
        fake_models.get_db.return_value = own
        with patch.dict('sys.modules',
                        {'integrations.social.models': fake_models}):
            log.log_event('security', 'tester', 'own-session probe')
        own.add.assert_called_once()
        own.commit.assert_called_once()
        # Two owned sessions run through get_db here — _get_last_hash's read
        # and the insert — and BOTH must be closed. The count is exactly 2;
        # a third would mean a leaked acquisition somewhere new.
        self.assertEqual(own.close.call_count, 2)


class TestSingleton(unittest.TestCase):

    def test_singleton_returns_same_instance(self):
        import security.immutable_audit_log as mod
        mod._audit_log = None
        a = get_audit_log()
        b = get_audit_log()
        self.assertIs(a, b)


class TestGetEntriesDBPath(unittest.TestCase):
    """The DB-backed read path (_use_db=True). The in-memory path and the
    DB-EXCEPT fallback are covered elsewhere; this pins the happy DB query ->
    dict mapping, which for an AUDIT LOG must be faithful (a mapping bug
    silently corrupts the trail an auditor reads) and must not leak the
    connection (the `finally: db.close()`)."""

    @staticmethod
    def _row(**kw):
        r = MagicMock()
        for k, v in kw.items():
            setattr(r, k, v)
        return r

    def _run_with_fake_db(self, db, limit=10):
        log = ImmutableAuditLog()
        log._use_db = True
        fake_models = types.ModuleType('integrations.social.models')
        fake_models.get_db = lambda: db
        fake_models.AuditLogEntry = MagicMock(name='AuditLogEntry')
        # Inject the whole dotted path so the real (heavy / non-importable here)
        # integrations.social.models is never touched.
        with patch.dict(sys.modules, {
            'integrations': types.ModuleType('integrations'),
            'integrations.social': types.ModuleType('integrations.social'),
            'integrations.social.models': fake_models,
        }):
            return log._get_entries(limit=limit)

    def test_db_rows_are_mapped_and_connection_closed(self):
        created = MagicMock()
        created.isoformat.return_value = '2026-09-01T00:00:00'
        row = self._row(
            id=1, event_type='auth', actor_id='user_1', target_id='t',
            action='login', detail_json='{}', prev_hash='GENESIS',
            entry_hash='abc123', created_at=created,
        )
        db = MagicMock()
        db.query.return_value.order_by.return_value.limit.return_value.all.return_value = [row]

        entries = self._run_with_fake_db(db)

        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['id'], 1)
        self.assertEqual(e['event_type'], 'auth')
        self.assertEqual(e['actor_id'], 'user_1')
        self.assertEqual(e['target_id'], 't')
        self.assertEqual(e['action'], 'login')
        self.assertEqual(e['detail_json'], '{}')
        self.assertEqual(e['prev_hash'], 'GENESIS')
        self.assertEqual(e['entry_hash'], 'abc123')
        # datetime-like created_at is serialized via isoformat()
        self.assertEqual(e['created_at'], '2026-09-01T00:00:00')
        # the connection was returned in the finally block (no leak)
        db.close.assert_called_once()

    def test_created_at_without_isoformat_is_passed_through(self):
        # A plain-string created_at (no .isoformat) must pass through verbatim,
        # exercising the else side of the serialization ternary.
        row = self._row(
            id=2, event_type='state_change', actor_id='u2', target_id=None,
            action='x', detail_json=None, prev_hash='abc123',
            entry_hash='def456', created_at='2026-09-01 raw',
        )
        db = MagicMock()
        db.query.return_value.order_by.return_value.limit.return_value.all.return_value = [row]

        entries = self._run_with_fake_db(db)
        self.assertEqual(entries[0]['created_at'], '2026-09-01 raw')
        db.close.assert_called_once()


class TestGetTrailReadsTheNewestEntries(unittest.TestCase):
    """get_trail read the OLDEST rows (id ascending, LIMIT limit*5) and
    filtered after, so on a node with a long history it never saw a recent
    entry.  It reads the newest matching rows now and still hands them back
    oldest first.  In-memory store here; the database below."""

    def setUp(self):
        self.log = ImmutableAuditLog()
        self.log._use_db = False
        self.log._memory_log = []

    def test_a_recent_entry_behind_many_older_ones_is_found(self):
        for i in range(30):
            self.log.log_event('test', actor_id='other', action=f'old {i}')
        self.log.log_event('a2ui_push', actor_id='agent-54', action='push card')
        trail = self.log.get_trail(actor_id='agent-54', limit=2)
        self.assertEqual([e['action'] for e in trail], ['push card'])

    def test_the_newest_entries_come_back_oldest_first(self):
        for i in range(5):
            self.log.log_event('test', actor_id='u', action=f'a{i}')
        self.assertEqual([e['action'] for e in self.log.get_trail(limit=3)],
                         ['a2', 'a3', 'a4'])

    def test_related_to_matches_the_actor_or_the_target(self):
        self.log.log_event('a2ui_push', actor_id='54', action='by the agent')
        self.log.log_event('goal_dispatched', actor_id='owner', action='to the goal',
                           target_id='goal-1')
        self.log.log_event('agent_steered', actor_id='owner', action='someone else',
                           target_id='999')
        got = self.log.get_trail(related_to={'54', 'goal-1'})
        self.assertEqual([e['action'] for e in got], ['by the agent', 'to the goal'])

    def test_since_and_until_bound_the_entries_both_ends_included(self):
        stamps = ['2026-11-20T23:59:59', '2026-11-21T08:00:00',
                  '2026-11-21T09:30:00.250000', '2026-11-22T00:00:01']
        for i, stamp in enumerate(stamps):
            self.log.log_event('test', actor_id='54', action=f'e{i}')
            self.log._memory_log[-1]['created_at'] = stamp
        got = self.log.get_trail(related_to=['54'], since='2026-11-21',
                                 until='2026-11-21T09:30:00.250+00:00')
        self.assertEqual([e['action'] for e in got], ['e1', 'e2'])
        # Another zone is read as the moment it names: 13:30 IST is 08:00 UTC.
        got = self.log.get_trail(related_to=['54'], since='2026-11-21T13:30:00+05:30')
        self.assertEqual([e['action'] for e in got], ['e1', 'e2', 'e3'])

    def test_a_window_that_is_not_a_date_is_refused_not_ignored(self):
        with self.assertRaises(ValueError) as caught:
            self.log.get_trail(since='yesterday')
        self.assertIn('2026-11-21', str(caught.exception))  # it says what to send


class TestGetTrailOnTheDatabase(unittest.TestCase):
    """The same reads on a real SQLite audit table: the filters and the
    newest-rows window run in the query."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from integrations.social import models
        self.models = models
        engine = create_engine('sqlite://', poolclass=StaticPool,
                               connect_args={'check_same_thread': False})
        models.Base.metadata.create_all(engine, tables=[models.AuditLogEntry.__table__])
        self._saved = (models._engine, models._SessionLocal)
        models._engine = engine
        models._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        self.log = ImmutableAuditLog()
        self.log._use_db = True

    def tearDown(self):
        self.models._engine, self.models._SessionLocal = self._saved

    def test_a_recent_entry_is_read_from_the_newest_rows(self):
        for i in range(12):
            self.log.log_event('test', actor_id='other', action=f'old {i}')
        self.log.log_event('goal_dispatched', actor_id='owner', action='dispatch goal-1',
                           target_id='goal-1')
        self.log.log_event('test', actor_id='other', action='newest noise')
        self.assertEqual([e['action'] for e in self.log.get_trail(actor_id='owner', limit=1)],
                         ['dispatch goal-1'])
        self.assertEqual([e['action'] for e in self.log.get_trail(related_to={'goal-1'},
                                                                   limit=1)],
                         ['dispatch goal-1'])
        self.assertEqual([e['action'] for e in self.log.get_trail(limit=2)],
                         ['dispatch goal-1', 'newest noise'])
        ok, reason = self.log.verify_chain()  # still walked from the first entry
        self.assertTrue(ok, reason)

    def test_the_window_runs_in_the_query(self):
        from datetime import datetime as dt
        db = self.models.get_db()
        for i, stamp in enumerate([dt(2026, 11, 20, 23, 59, 59), dt(2026, 11, 21, 8),
                                   dt(2026, 11, 22, 0, 0, 1)]):
            db.add(self.models.AuditLogEntry(
                event_type='test', actor_id='54', action=f'e{i}', prev_hash='p',
                entry_hash=f'h{i}', created_at=stamp))
        db.commit()
        db.close()
        got = self.log.get_trail(related_to=['54'], since='2026-11-21',
                                 until='2026-11-21T23:59:59+00:00')
        self.assertEqual([e['action'] for e in got], ['e1'])


if __name__ == '__main__':
    unittest.main()
