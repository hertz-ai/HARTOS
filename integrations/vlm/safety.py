"""
integrations.vlm.safety — guards for the VLM action pipeline.

Phase 6 of memory/vlm_best_of_all_worlds_plan.md §5.  Three layers
of protection between the VLM's decisions and the user's screen:

  1. SessionGuard       per-session action cap + per-second throttle
                        (avoid runaway loops spamming clicks)
  2. WindowBlocklist    refuse to click in sensitive apps
                        (lsass / password managers / banking-titled
                         windows) and an admin-overridable allowlist
  3. AuditLogger        JSONL trail at ~/.nunba/audit/vlm_actions_*.jsonl
                        with timestamp / window / coords / hash / exit
                        code so post-incident review can reconstruct
                        what the VLM did

All three are OPT-IN via ``execute_action(..., safety=True)`` so
existing call sites stay unchanged unless they explicitly opt in.
The plan §5 calls these out as production-readiness, not always-
on hard limits.

Configuration via ``SafetyConfig`` dataclass; module-level singletons
returned by ``get_session_guard()`` / ``get_audit_logger()``.  The
singletons are reset between distinct user sessions via
``reset_session_guard()`` (called by /api/vlm/stop and by the loop
when it terminates a goal).
"""

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve.vlm.safety')


# ─── Defaults ─────────────────────────────────────────────────────────

#: Process names that must never receive VLM clicks.  Lowercased.
#: Includes Windows credential broker (lsass), session manager
#: (winlogon), known password managers, and the Windows logon UI
#: (LogonUI.exe).  Admins may extend at runtime via
#: ``SafetyConfig(blocked_processes=...)``.
DEFAULT_BLOCKED_PROCESSES: Tuple[str, ...] = (
    'lsass.exe', 'winlogon.exe', 'logonui.exe', 'consent.exe',
    'bitwarden.exe', '1password.exe', 'keepass.exe', 'keepassxc.exe',
    'lastpass.exe', 'dashlane.exe', 'enpass.exe',
)

#: Window-title regex patterns that suggest sensitive content.
#: Case-insensitive.  Designed to be conservative — false positives
#: are recoverable (user can override per-window), false negatives
#: are not.
DEFAULT_BLOCKED_TITLE_PATTERNS: Tuple[str, ...] = (
    r'\b(?:online[\s-]?)?bank(?:ing)?\b',
    r'\bcredit[\s-]?card\b',
    r'\b(?:enter|change|reset)[\s-]+password\b',
    r'\b(?:UAC|elevation|administrator)\s*prompt\b',
    r'\b(?:pin|cvv|security[\s-]?code)\b',
)


# ─── Configuration ────────────────────────────────────────────────────

@dataclass
class SafetyConfig:
    """Tuneable knobs.  All env-overridable so per-host policies
    don't require code changes."""

    max_actions_per_session: int = int(
        os.environ.get('HEVOLVE_VLM_MAX_ACTIONS_PER_SESSION', '100'))
    max_actions_per_second: float = float(
        os.environ.get('HEVOLVE_VLM_MAX_ACTIONS_PER_SECOND', '5.0'))
    blocked_processes: Tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_BLOCKED_PROCESSES)
    blocked_title_patterns: Tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_BLOCKED_TITLE_PATTERNS)
    audit_enabled: bool = (
        os.environ.get('HEVOLVE_VLM_AUDIT_ENABLED', '1') not in ('0', 'false', 'no'))
    # Override with HEVOLVE_VLM_AUDIT_DIR; empty default → ~/.nunba/audit
    # via _default_dir().
    audit_dir: str = field(
        default_factory=lambda: os.environ.get('HEVOLVE_VLM_AUDIT_DIR', ''))


# ─── Session guard (count + throttle) ─────────────────────────────────

class SessionGuard:
    """Tracks per-session action count + per-second rate.

    Returns a non-None block reason string from :meth:`check` when the
    limit has been reached; the caller MUST treat this as a refusal
    to act.  :meth:`record` is called after a successful action to
    increment counters.

    Thread-safe: a single lock protects counter updates so concurrent
    VLM calls (e.g. the agentic loop dispatching from a worker pool)
    don't double-count.
    """

    def __init__(self, config: Optional[SafetyConfig] = None):
        self.config = config or SafetyConfig()
        self.action_count: int = 0
        # Bounded deque so memory doesn't grow unbounded over a long
        # session; capacity covers ~1 second of max-rate actions.
        self.recent_action_times: collections.deque = collections.deque(
            maxlen=max(64, int(self.config.max_actions_per_second * 4)))
        self._lock = threading.Lock()

    def check(self) -> Optional[str]:
        """Return None when OK; otherwise a reason string."""
        with self._lock:
            if self.action_count >= self.config.max_actions_per_session:
                return (f'session-cap reached '
                        f'({self.config.max_actions_per_session} actions)')
            now = time.time()
            recent = sum(
                1 for t in self.recent_action_times if now - t < 1.0)
            if recent >= self.config.max_actions_per_second:
                return (f'throttle exceeded '
                        f'(>{self.config.max_actions_per_second}/s)')
        return None

    def record(self) -> None:
        with self._lock:
            self.action_count += 1
            self.recent_action_times.append(time.time())

    def reset(self) -> None:
        with self._lock:
            self.action_count = 0
            self.recent_action_times.clear()


# ─── Window blocklist ─────────────────────────────────────────────────

def is_window_blocked(window_meta: Optional[dict],
                      config: Optional[SafetyConfig] = None
                      ) -> Optional[str]:
    """Return a block-reason string when the window is sensitive,
    None otherwise.  Safe to call with ``window_meta=None`` (returns
    None — no info to block on).

    ``window_meta`` is the dict shape :func:`integrations.remote_desktop.
    window_capture.list_windows` returns: ``{title, process_name, ...}``.
    """
    if not window_meta:
        return None
    config = config or SafetyConfig()
    pname = (window_meta.get('process_name') or '').lower().strip()
    if pname:
        for blocked in config.blocked_processes:
            blocked_l = blocked.lower()
            if pname == blocked_l or pname.endswith('\\' + blocked_l) \
                    or pname.endswith('/' + blocked_l):
                return f'process_blocked: {pname}'
    title = window_meta.get('title') or ''
    for pat in config.blocked_title_patterns:
        if re.search(pat, title, re.IGNORECASE):
            return f'title_pattern_blocked: "{title[:60]}" matches /{pat}/'
    return None


# ─── Audit logger ─────────────────────────────────────────────────────

class AuditLogger:
    """Append-only JSONL audit trail of every VLM action."""

    def __init__(self, config: Optional[SafetyConfig] = None):
        self.config = config or SafetyConfig()
        self.path: Optional[str] = None
        self._lock = threading.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        target = self.config.audit_dir or self._default_dir()
        try:
            os.makedirs(target, exist_ok=True)
            self.path = target
        except Exception as e:
            logger.warning(f'audit dir create failed for {target}: {e}')
            self.path = None  # disables logging

    def _default_dir(self) -> str:
        """Audit log location.

        Plan §5 spec: ``~/.nunba/audit/vlm_actions_{date}.jsonl``.
        Reviewer flagged the prior implementation deferred to
        ``platform_paths.get_data_dir()`` which gave platform-correct
        paths but didn't match the plan literally.  Resolution: use
        the plan-literal ``~/.nunba/audit`` as the default; admins
        who want platform-default paths set
        ``HEVOLVE_VLM_AUDIT_DIR=$(python -c "from core.platform_paths
        import get_data_dir; import os; print(os.path.join(
        get_data_dir(), 'audit'))")`` once at install time.

        Override with ``HEVOLVE_VLM_AUDIT_DIR=...`` env var (read in
        SafetyConfig).  Empty string honored (audit logger inits but
        never writes).
        """
        return os.path.expanduser('~/.nunba/audit')

    def log(self, action: dict, result: dict, *,
            window_meta: Optional[dict] = None,
            screenshot_b64: Optional[str] = None,
            block_reason: Optional[str] = None) -> None:
        """Append one JSONL record.  No-op when audit_enabled is False
        or the dir couldn't be created."""
        if not self.config.audit_enabled or not self.path:
            return
        record = {
            'ts': time.time(),
            'iso': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'action': action.get('action'),
            'coordinate': action.get('coordinate'),
            'text': action.get('text', '')[:80] if action.get('text') else '',
            'translated_from': action.get('_translated_from'),
            'translated_to': action.get('_translated_to'),
            'window': {
                'hwnd': (window_meta or {}).get('hwnd'),
                'title': ((window_meta or {}).get('title') or '')[:80],
                'process_name': (window_meta or {}).get('process_name'),
                'pid': (window_meta or {}).get('pid'),
            } if window_meta else None,
            'screenshot_sha256': (
                hashlib.sha256(screenshot_b64.encode('ascii')).hexdigest()[:16]
                if screenshot_b64 else None),
            'status': result.get('status'),
            'error': result.get('error'),
            'block_reason': block_reason,
            'verify_diff': result.get('verify_diff'),
            'verify_retried': result.get('verify_retried'),
        }
        date = time.strftime('%Y%m%d')
        log_path = os.path.join(self.path, f'vlm_actions_{date}.jsonl')
        line = json.dumps(record, default=str)
        try:
            with self._lock:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
        except Exception as e:
            logger.debug(f'audit write failed: {e}')


# ─── Module-level singletons ──────────────────────────────────────────

_session_guard: Optional[SessionGuard] = None
_audit_logger: Optional[AuditLogger] = None
_singleton_lock = threading.Lock()


def get_session_guard() -> SessionGuard:
    global _session_guard
    if _session_guard is None:
        with _singleton_lock:
            if _session_guard is None:
                _session_guard = SessionGuard()
    return _session_guard


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        with _singleton_lock:
            if _audit_logger is None:
                _audit_logger = AuditLogger()
    return _audit_logger


def reset_session_guard() -> None:
    """Called when a VLM session ends (loop terminated, /api/vlm/stop
    fired, user-id changes) so the next session starts fresh."""
    guard = get_session_guard()
    guard.reset()
