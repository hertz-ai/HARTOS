"""L5: Unit tests for hive_task_protocol.load_user_ledger().

Asserts the canonical ledger reader:
  * returns LedgerEntry instances in file order
  * defaults to open-only (closed rows excluded unless requested)
  * include_closed=True returns every parseable row
  * missing ledger file → empty list (no exception)
  * explicit path arg overrides HIVE_LEDGER_PATH env

Spec: memory/project_hive_test_ledger.md (L5).
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.coding_agent.hive_task_protocol import (  # noqa: E402
    LedgerEntry,
    load_user_ledger,
)


_LEDGER_FIXTURE = """\
# Hive Test Ledger

Some preamble that should be ignored.

## Session 2026-04-24

| # | Timestamp | Verbatim ask | Success criterion | Acceptance test | Status |
|---|-----------|--------------|-------------------|------------------|--------|
| L1 | 2026-04-24 | "First ask" | crit-1 | test-1 | open |
| L2 | 2026-04-24 | "Second ask" | crit-2 | test-2 | done_by_claude_code:abc123:pass |
| L3 | 2026-04-25 | "Third ask"  | crit-3 | test-3 | open |

Trailing prose that should be ignored.
"""


@pytest.fixture
def ledger_file(tmp_path):
    f = tmp_path / 'project_hive_test_ledger.md'
    f.write_text(_LEDGER_FIXTURE, encoding='utf-8')
    return str(f)


def test_returns_only_open_by_default(ledger_file):
    rows = load_user_ledger(path=ledger_file)
    assert [r.id for r in rows] == ['L1', 'L3']
    assert all(r.status == 'open' for r in rows)


def test_include_closed_returns_all(ledger_file):
    rows = load_user_ledger(path=ledger_file, include_closed=True)
    assert [r.id for r in rows] == ['L1', 'L2', 'L3']
    statuses = [r.status for r in rows]
    assert statuses[0] == 'open'
    assert statuses[1].startswith('done_by_')
    assert statuses[2] == 'open'


def test_returns_ordered_ledger_entry_instances(ledger_file):
    rows = load_user_ledger(path=ledger_file)
    assert all(isinstance(r, LedgerEntry) for r in rows)
    first = rows[0]
    assert first.id == 'L1'
    assert first.timestamp == '2026-04-24'
    assert first.ask == 'First ask'             # quotes stripped
    assert first.success_criterion == 'crit-1'
    assert first.acceptance_test == 'test-1'
    assert first.status == 'open'


def test_missing_file_returns_empty_list(tmp_path):
    bogus = str(tmp_path / 'does_not_exist.md')
    assert load_user_ledger(path=bogus) == []


def test_explicit_path_overrides_env(monkeypatch, ledger_file, tmp_path):
    other = tmp_path / 'env_ledger.md'
    other.write_text("(no rows)\n", encoding='utf-8')
    monkeypatch.setenv('HIVE_LEDGER_PATH', str(other))
    rows = load_user_ledger(path=ledger_file)
    assert [r.id for r in rows] == ['L1', 'L3']  # ledger_file wins


def test_env_used_when_no_explicit_path(monkeypatch, ledger_file):
    monkeypatch.setenv('HIVE_LEDGER_PATH', ledger_file)
    rows = load_user_ledger()
    assert [r.id for r in rows] == ['L1', 'L3']


def test_header_and_separator_rows_skipped(tmp_path):
    f = tmp_path / 'edge.md'
    f.write_text(
        "| # | Timestamp | Verbatim ask | Success criterion | Acceptance test | Status |\n"
        "|---|-----------|--------------|-------------------|------------------|--------|\n"
        "| L1 | 2026-04-25 | \"only row\" | c | t | open |\n",
        encoding='utf-8',
    )
    rows = load_user_ledger(path=str(f))
    assert [r.id for r in rows] == ['L1']


def test_malformed_rows_skipped(tmp_path):
    f = tmp_path / 'malformed.md'
    f.write_text(
        "| L1 | only-three | columns |\n"             # too few cols
        "not a table row at all\n"                     # prose
        "| L2 | 2026-04-25 | \"ok\" | c | t | open |\n",  # valid
        encoding='utf-8',
    )
    rows = load_user_ledger(path=str(f), include_closed=True)
    assert [r.id for r in rows] == ['L2']
