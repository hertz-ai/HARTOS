"""Security-critical failures must be VISIBLE, not swallowed.

THE CLASS
─────────
1,533 `except ...: pass` across 450 files means nobody — including the operators —
can distinguish "working" from "failing quietly". It has already cost this project
real time: a false "STT engine NOT installed" message sent successive sessions
chasing torn ctranslate2/numpy/torch that was never the cause.

68 of those live under security/, where silence is worst: a control that fails
without saying so still reports green.

WHAT THIS PINS (the four highest-consequence ones)
──────────────────────────────────────────────────
  immutable_audit_log._get_last_hash  the audit chain's hash link. Swallowing means
                                      the next entry chains onto a different
                                      predecessor and tamper-evidence silently stops.
  immutable_audit_log._get_entries    on failure returns the in-memory tail — EMPTY
                                      in a fresh process. An auditor cannot tell
                                      "nothing happened" from "store unreachable".
  node_integrity._collect_py_files    an unreadable dir silently EXCLUDES .py files
                                      from the code hash; verification then covers
                                      less of the tree than it claims to.
  node_integrity._hash_file           an unreadable file hashes as EMPTY — a
                                      real-looking sha256 that says nothing about
                                      the file, and identical for every such file.

These are BEHAVIOURAL: each drives the real function with the failure injected and
asserts a record was actually emitted. They do not grep for logger calls.

DELIBERATELY NOT COVERED: security/hive_guardrails.py has 4 swallows. CLAUDE.md
forbids modifying the circuit breaker or the structural-immutability machinery, so
they are reported rather than edited.
"""
import logging
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


class _Cap:
    """Attach a capturing handler to the security logger for one block."""
    def __enter__(self):
        self.h = _Capture()
        self.log = logging.getLogger('hevolve_security')
        self.prev = self.log.level
        self.log.addHandler(self.h)
        self.log.setLevel(logging.DEBUG)
        return self.h

    def __exit__(self, *a):
        self.log.removeHandler(self.h)
        self.log.setLevel(self.prev)
        return False


class TheIntegrityHashReportsWhatItCouldNotRead(unittest.TestCase):

    def test_an_unreadable_file_is_reported_not_hashed_as_empty_in_silence(self):
        from security import node_integrity
        with _Cap() as cap:
            with patch('builtins.open', side_effect=OSError('permission denied')):
                digest = node_integrity._hash_file(  # noqa: SLF001 - the unit under test
                    os.path.join(REPO, 'setup.py'))
        self.assertTrue(digest, "must still return a digest; callers expect a string")
        self.assertTrue(
            cap.records,
            "an unreadable file was hashed as EMPTY with NO log record. That digest "
            "is a real-looking sha256 that says nothing about the file, and every "
            "unreadable file produces the SAME one — integrity verification silently "
            "stops meaning anything.")
        self.assertIn('node_integrity', cap.text())

    def test_an_unreadable_directory_is_reported(self):
        from pathlib import Path
        from security import node_integrity
        with _Cap() as cap:
            with patch.object(Path, 'iterdir', side_effect=PermissionError('nope')):
                out = list(node_integrity._collect_py_files(  # noqa: SLF001
                    Path(REPO), Path(REPO)))
        self.assertEqual([], out)
        self.assertTrue(
            cap.records,
            "a directory that could not be read was skipped SILENTLY, so its .py "
            "files are absent from the code hash and nothing says the scan covered "
            "less than the whole tree.")


class TheAuditChainReportsWhenItCannotRead(unittest.TestCase):

    def _log(self):
        from security import immutable_audit_log
        return immutable_audit_log

    def test_a_failed_chain_head_read_is_reported(self):
        m = self._log()
        inst = m.ImmutableAuditLog() if hasattr(m, 'ImmutableAuditLog') else None
        if inst is None:
            self.skipTest('ImmutableAuditLog class not found under that name')
        inst._use_db = True                                  # force the DB branch
        with _Cap() as cap:
            with patch.dict('sys.modules', {'integrations.social.models': None}):
                head = inst._get_last_hash()                 # noqa: SLF001
        self.assertTrue(head, "must still yield a chain head so a write is not lost")
        self.assertTrue(
            cap.records,
            "the chain head could not be read and NOTHING was logged. The next "
            "entry will chain onto a different predecessor than the DB holds, so "
            "the log silently stops being tamper-evident.")

    def test_a_failed_entries_read_is_not_mistaken_for_an_empty_history(self):
        m = self._log()
        inst = m.ImmutableAuditLog() if hasattr(m, 'ImmutableAuditLog') else None
        if inst is None:
            self.skipTest('ImmutableAuditLog class not found under that name')
        inst._use_db = True
        with _Cap() as cap:
            with patch.dict('sys.modules', {'integrations.social.models': None}):
                rows = inst._get_entries(10)                  # noqa: SLF001
        self.assertEqual([], rows)
        self.assertTrue(
            cap.records,
            "the audit store was unreachable and the call returned [] with no log. "
            "An auditor reading that cannot tell 'nothing happened' from 'the store "
            "is down' — which is the entire difference between a clean record and a "
            "missing one.")


if __name__ == '__main__':
    unittest.main()
