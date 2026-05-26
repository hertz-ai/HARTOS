"""Unit tests for reasoning_trace — append-only consensus audit log."""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import reasoning_trace


def test_record_decision_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reasoning_trace, '_resolve_trace_dir', lambda: str(tmp_path),
    )
    ok = reasoning_trace.record_decision(
        action='upgrade_proposal',
        approved=True,
        votes={
            'circuit_breaker': {'passed': True, 'reason': 'ok'},
            'constitutional': {'passed': True, 'reason': 'ok'},
            'local_probe': {'passed': True, 'reason': 'margin=0.05'},
            'peer_probe_quorum': {'passed': True, 'reason': '3 agree'},
        },
        subject={'agent_id': 'atlas', 'goal_type': 'coding'},
        reason='all 4 votes passed',
        event_bus_emit=False,
    )
    assert ok is True
    files = list(tmp_path.glob('*.jsonl'))
    assert len(files) == 1
    lines = files[0].read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry['action'] == 'upgrade_proposal'
    assert entry['approved'] is True
    assert entry['subject']['agent_id'] == 'atlas'


def test_record_decision_is_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reasoning_trace, '_resolve_trace_dir', lambda: str(tmp_path),
    )
    for i in range(3):
        reasoning_trace.record_decision(
            action='upgrade_proposal',
            approved=(i == 1),
            votes={},
            subject={'idx': i},
            event_bus_emit=False,
        )
    files = list(tmp_path.glob('*.jsonl'))
    assert len(files) == 1
    lines = files[0].read_text(encoding='utf-8').splitlines()
    assert len(lines) == 3
    entries = [json.loads(l) for l in lines]
    assert [e['subject']['idx'] for e in entries] == [0, 1, 2]
    assert [e['approved'] for e in entries] == [False, True, False]


def test_read_recent_returns_ordered(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reasoning_trace, '_resolve_trace_dir', lambda: str(tmp_path),
    )
    for i in range(5):
        reasoning_trace.record_decision(
            action='x',
            approved=True,
            votes={},
            subject={'n': i},
            event_bus_emit=False,
        )
    rows = reasoning_trace.read_recent(limit=3)
    assert len(rows) == 3
    # Must be the LAST three (append-only + tail)
    assert [r['subject']['n'] for r in rows] == [2, 3, 4]
