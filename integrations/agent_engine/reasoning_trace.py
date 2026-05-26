"""
ReasoningTrace — append-only decision log for hive_consensus.

The ml_intern brief §5 package C calls for a
`monitoring/reasoning_trace.py` so every promotion (or rejection) is
"auditable forever".  This is the implementation.

Storage layout:
    agent_data/reasoning_traces/{YYYY-MM-DD}.jsonl

One line per decision.  JSONL so a tail -f can stream decisions live
to an operator.  No deletion API — audit log is append-only by
contract.  Rotation is by day; the file grows for a calendar day then
a new file is started.

Related:
- security/immutable_audit_log.py already exists for security-sensitive
  events; we do NOT reuse it here because consensus decisions are
  operational audit, not security audit.  Mixing the two would
  confuse the hash-chain analysis tool.  Single-responsibility: this
  module records consensus decisions and ONLY consensus decisions.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_social')

_TRACE_LOCK = threading.Lock()


def _resolve_trace_dir() -> str:
    try:
        from core.platform_paths import get_agent_data_dir
        return os.path.join(get_agent_data_dir(), 'reasoning_traces')
    except Exception:
        pass
    if os.name == 'nt':
        return os.path.join(
            os.path.expanduser('~'),
            'Documents', 'Nunba', 'data', 'agent_data', 'reasoning_traces',
        )
    return os.path.join('agent_data', 'reasoning_traces')


def _file_for(day: Optional[str] = None) -> str:
    root = _resolve_trace_dir()
    if day is None:
        day = datetime.utcnow().strftime('%Y-%m-%d')
    return os.path.join(root, f'{day}.jsonl')


def record_decision(
    action: str,
    approved: bool,
    votes: Dict[str, Any],
    subject: Dict[str, Any],
    reason: str = '',
    event_bus_emit: bool = True,
) -> bool:
    """Record one consensus decision.

    Args:
        action: short verb ("upgrade_proposal", "rollback", "halt", etc.)
        approved: final verdict
        votes: mapping of voter_name → {passed: bool, reason: str}
        subject: what the decision was about (agent_id, new_prompt, etc.)
        reason: free-text explanation surfaced to the dashboard
        event_bus_emit: also fire a `learning.federation_update`
                        EventBus event so peers can observe the decision
                        (consent-gated upstream by ScopeGuard)
    """
    entry = {
        'timestamp': time.time(),
        'datetime_utc': datetime.utcnow().isoformat() + 'Z',
        'action': action,
        'approved': bool(approved),
        'votes': votes,
        'subject': subject,
        'reason': reason,
    }
    path = _file_for()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(entry, sort_keys=True)
        with _TRACE_LOCK:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except Exception as exc:
        logger.warning(f'[reasoning_trace] persist failed: {exc}')
        return False

    if event_bus_emit:
        try:
            from core.platform.events import emit_event
            emit_event('learning.federation_update', {
                'kind': 'consensus_decision',
                'action': action,
                'approved': approved,
                'subject_summary': {
                    k: v for k, v in subject.items()
                    if k in ('agent_id', 'goal_type', 'version')
                },
            })
        except Exception as exc:
            logger.debug(f'[reasoning_trace] event emit failed: {exc}')
    return True


def read_recent(limit: int = 200) -> list:
    """Read the most recent decisions for dashboard / audit display."""
    path = _file_for()
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.debug(f'[reasoning_trace] read failed: {exc}')
        return []
    return rows[-limit:]
