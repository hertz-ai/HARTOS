"""Central superadmin report-in client.

When a HARTOS install successfully forms a peer (any topology — flat /
regional / central, any distribution form — bundled / Docker / ISO),
it MUST report its identity to the canonical superadmin allowlist.
This module is the single send path for that report.

Wired from ``GossipProtocol.start()``.  Offline-tolerant: failed
reports persist to an outbox under ``platform_paths.get_data_dir() /
superadmin_outbox`` and replay on the next tick.

See Nunba memory:
- ``project_universal_peer_join_central_report.md`` (the spec)
- ``core/superadmins.py``                            (allowlist + URLs)
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from core.superadmins import (
    SUPERADMIN_CENTRAL_URLS,
    SUPERADMIN_FALLBACK_CENTRAL_URLS,
    ALL_CENTRAL_URLS,
    REPORT_INTERVAL_SEC,
    REPORT_TIMEOUT_CONNECT,
    REPORT_TIMEOUT_READ,
    REPORT_OUTBOX_RETRY_SEC,
)

logger = logging.getLogger('superadmin_report')

# Outbox cap.  If centrals stay unreachable indefinitely, the outbox
# would otherwise grow without bound (one file per failed central per
# REPORT_INTERVAL_SEC).  Drop the oldest on overflow.
MAX_OUTBOX_FILES = 100


# ─── Outbox helpers ───────────────────────────────────────────────────

def _outbox_dir() -> Optional[str]:
    """Return the outbox directory path, or None if data dir is
    unreachable (degraded environment).  Best-effort mkdir."""
    try:
        from core.platform_paths import get_data_dir
        path = os.path.join(get_data_dir(), 'superadmin_outbox')
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        return None


def _outbox_write(report: Dict) -> None:
    path = _outbox_dir()
    if not path:
        return
    # Enforce MAX_OUTBOX_FILES cap before writing — drop the oldest if
    # we'd otherwise exceed.  Filename prefix is ms-timestamp so
    # lexicographic sort = chronological.
    try:
        existing = sorted(
            f for f in os.listdir(path) if f.endswith('.json')
        )
        while len(existing) >= MAX_OUTBOX_FILES:
            try:
                os.remove(os.path.join(path, existing.pop(0)))
            except Exception:
                break  # Stop trying if removal itself fails
    except Exception:
        pass  # Cap is best-effort; better to write than to crash
    fname = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    try:
        with open(os.path.join(path, fname), 'w', encoding='utf-8') as fh:
            json.dump(report, fh)
    except Exception as e:
        logger.debug(f"Outbox write failed: {e}")


def _outbox_drain() -> List[str]:
    """Return list of outbox file paths, oldest first (filename has ms
    timestamp prefix so lexicographic sort = chronological)."""
    path = _outbox_dir()
    if not path:
        return []
    try:
        files = sorted(
            f for f in os.listdir(path)
            if f.endswith('.json')
        )
        return [os.path.join(path, f) for f in files]
    except Exception:
        return []


# ─── HTTP send (one central) ──────────────────────────────────────────

def _post_report(central_url: str, report: Dict) -> bool:
    """POST a single report to one central.  Returns True iff 2xx."""
    try:
        from core.http_pool import pooled_post
        url = f"{central_url.rstrip('/')}/api/social/peers/report-join"
        resp = pooled_post(
            url, json=report,
            timeout=(REPORT_TIMEOUT_CONNECT, REPORT_TIMEOUT_READ),
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.debug(f"Report-in to {central_url} status={resp.status_code}")
        return False
    except Exception as e:
        logger.debug(f"Report-in to {central_url} failed: {e}")
        return False


# ─── Public API ───────────────────────────────────────────────────────

def build_report(node_info: Dict) -> Dict:
    """Wrap node_info into a report envelope.

    Privacy bound: ship identity + topology + bandwidth tier + capability
    summary.  No personal user data, no chat content, no model
    activations.
    """
    return {
        'schema': 'superadmin-report-v1',
        'reported_at': int(time.time()),
        'node_id': node_info.get('node_id', ''),
        'node_name': node_info.get('name', ''),
        'url': node_info.get('url', ''),
        'tier': node_info.get('tier', ''),
        'capability_tier': node_info.get('capability_tier', ''),
        'version': node_info.get('version', ''),
        'release_version': node_info.get('release_version', ''),
        'bandwidth_profile': node_info.get('bandwidth_profile', ''),
        'guardrail_hash': node_info.get('guardrail_hash', ''),
        'code_hash': node_info.get('code_hash', ''),
        'public_key': node_info.get('public_key', ''),
        'hart_tag': node_info.get('hart_tag', ''),
    }


def report_join(node_info: Dict) -> int:
    """Fan out report-in: try primary central(s) first, fall back to
    secondary central(s) ONLY if every primary fails.

    Returns count of centrals that ack'd (HTTP 2xx).  Reports that
    fail are persisted to the outbox for retry — keyed to each failed
    target so the drain loop retries only the right URL.

    Failover order is canonical (`core.superadmins`):
    - `SUPERADMIN_CENTRAL_URLS`           (primary, e.g. central.hevolve.ai)
    - `SUPERADMIN_FALLBACK_CENTRAL_URLS`  (secondary, e.g. azurekong.hertzai.com)

    Secondary endpoints are NOT tried in parallel — they're a true
    failover, so one ack on primary is enough and we don't double-bill
    the central with redundant traffic.
    """
    report = build_report(node_info)
    sent = 0
    primary_failures = []
    for central in SUPERADMIN_CENTRAL_URLS:
        if _post_report(central, report):
            sent += 1
        else:
            primary_failures.append(central)
    if sent > 0:
        # Primary path succeeded — outbox the failed primaries (if any)
        # so they retry, but DO NOT touch the fallback.
        for central in primary_failures:
            report_copy = dict(report)
            report_copy['_target_central'] = central
            _outbox_write(report_copy)
        return sent
    # Every primary failed → try fallbacks.  This is the
    # "central.hevolve.ai unreachable, try azurekong.hertzai.com" path.
    for central in SUPERADMIN_FALLBACK_CENTRAL_URLS:
        if _post_report(central, report):
            sent += 1
        else:
            report_copy = dict(report)
            report_copy['_target_central'] = central
            _outbox_write(report_copy)
    if sent == 0:
        # All centrals offline — outbox primaries too so they retry on
        # the next tick (fallbacks already outboxed above).
        for central in primary_failures:
            report_copy = dict(report)
            report_copy['_target_central'] = central
            _outbox_write(report_copy)
    return sent


def drain_outbox() -> int:
    """Replay any persisted reports.  Called on a slow timer.

    Returns count successfully resent (deletes those files).
    """
    sent = 0
    for fpath in _outbox_drain():
        try:
            with open(fpath, 'r', encoding='utf-8') as fh:
                report = json.load(fh)
            target = report.pop('_target_central', None)
            # Accept ANY known central (primary or fallback) so the
            # drain works for outboxed reports against either tier.
            if not target or target not in ALL_CENTRAL_URLS:
                # Stale / unknown target — drop it rather than letting
                # it accumulate forever.
                os.remove(fpath)
                continue
            if _post_report(target, report):
                os.remove(fpath)
                sent += 1
        except Exception as e:
            logger.debug(f"Outbox drain error for {fpath}: {e}")
    return sent


# ─── Background loop ──────────────────────────────────────────────────

_loop_started = False
_loop_lock = threading.Lock()


def start_background_loop(get_node_info_callable) -> None:
    """Start the periodic report + outbox-drain loop.

    The caller passes a zero-arg callable that returns the latest
    node_info dict (so the loop sees fresh values on every tick — port
    changes, hart_tag late-binding, etc.).  Idempotent: safe to call
    multiple times; only the first call spawns the thread.
    """
    global _loop_started
    with _loop_lock:
        if _loop_started:
            return
        _loop_started = True

    def _loop():
        # First-tick: send immediately, but with a short pre-sleep so
        # any in-flight boot work (DB migration, model warmup) has a
        # chance to settle and the node_info dict is populated.
        time.sleep(15)
        last_report = 0.0

        def _heartbeat_safe():
            """Best-effort watchdog heartbeat — same pattern as the
            other long-cadence daemons (peer_discovery, AutoDiscovery).
            Without this, a watchdog with frozen-threshold < REPORT_
            OUTBOX_RETRY_SEC would flag this thread as stalled."""
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('superadmin_report')
            except Exception:
                pass

        while True:
            _heartbeat_safe()
            try:
                now = time.time()
                if now - last_report >= REPORT_INTERVAL_SEC:
                    try:
                        info = get_node_info_callable() or {}
                        if info.get('node_id'):
                            n = report_join(info)
                            if n > 0:
                                logger.info(
                                    f"Reported to {n}/"
                                    f"{len(SUPERADMIN_CENTRAL_URLS)} "
                                    f"superadmin centrals")
                    except Exception as e:
                        logger.debug(f"Periodic report-in error: {e}")
                    last_report = now
                # Always drain the outbox (cheap when empty).
                try:
                    drained = drain_outbox()
                    if drained:
                        logger.info(f"Outbox drained {drained} stale reports")
                except Exception as e:
                    logger.debug(f"Outbox drain error: {e}")
            except Exception as e:
                logger.debug(f"Report loop tick error: {e}")
            time.sleep(REPORT_OUTBOX_RETRY_SEC)

    t = threading.Thread(target=_loop, name='superadmin-report-in', daemon=True)
    t.start()
    logger.info("Superadmin report-in loop started "
                f"(interval={REPORT_INTERVAL_SEC}s, "
                f"outbox-retry={REPORT_OUTBOX_RETRY_SEC}s)")


__all__ = [
    'build_report',
    'report_join',
    'drain_outbox',
    'start_background_loop',
]
