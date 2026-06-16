"""#140: JSONBackend.save is atomic — a failed write preserves the prior file.

The agent-ledger lib is a standalone open-source package (it can't import
HARTOS's core.file_cache.atomic_json_write — different package boundary), so it
carries its OWN tmp + fsync + os.replace atomic write (backends.py:111). That
load-bearing persistence (every coordinator/agent ledger) had no test for the
FAILURE boundary: when the tmp write fails (e.g. ENOSPC disk-full), the EXISTING
ledger file must survive intact (os.replace never runs), save() returns False
(not raises), and no .tmp is left behind.

Behavioral: real JSONBackend, mock the json.dump boundary, assert observable
disk state. Distinct from tests/unit/test_atomic_json.py (that covers
core.file_cache, the HARTOS-side atomic writer — not this backend).
"""
import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ledger.backends import JSONBackend  # noqa: E402


def test_save_roundtrips(tmp_path):
    b = JSONBackend(str(tmp_path))
    assert b.save('led', {'tasks': {'t1': 1}}) is True
    assert b.load('led') == {'tasks': {'t1': 1}}


def test_failed_write_preserves_existing_file_and_returns_false(tmp_path):
    b = JSONBackend(str(tmp_path))
    assert b.save('led', {'v': 1}) is True  # seed the durable copy
    # tmp write fails mid-save (disk full) — original must survive untouched.
    with patch('json.dump', side_effect=OSError(28, 'No space left on device')):
        ok = b.save('led', {'v': 2})
    assert ok is False                       # failure reported, never raised
    assert b.load('led') == {'v': 1}         # atomic: os.replace never ran
    assert [f for f in os.listdir(str(tmp_path))
            if f.endswith('.tmp')] == []     # tmp cleaned up


def test_failed_write_on_fresh_key_leaves_no_partial_file(tmp_path):
    b = JSONBackend(str(tmp_path))
    with patch('json.dump', side_effect=OSError(28, 'No space left on device')):
        ok = b.save('fresh', {'v': 1})
    assert ok is False
    assert b.load('fresh') is None           # no partial/corrupt file surfaced
    assert [f for f in os.listdir(str(tmp_path))
            if f.endswith('.tmp')] == []
