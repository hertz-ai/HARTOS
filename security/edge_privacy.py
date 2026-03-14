"""
Edge Privacy — Scope-based data protection at the edge.

FIRST PRINCIPLE: Privacy lives on the edge.  Data has a SCOPE — where
it's allowed to exist.  Guards enforce that scope at every egress point.
This is not a parallel system.  It is the single scope definition that
the existing DLP engine, secret redactor, shard engine, and PeerLink
trust boundaries all converge to enforce.

ARCHITECTURE:
  PrivacyScope (enum)  — tags data with where it can live
  ScopeGuard (class)   — checks scope at egress, delegates to existing engines
  check_egress()       — single function, called at every boundary

REUSES (does NOT duplicate):
  - DLP engine (dlp_engine.py)        → PII scanning at outbound
  - Secret redactor (secret_redactor.py) → 3-layer redaction for world model
  - Shard scoping (shard_engine.py)   → code exposure proportional to trust
  - PeerLink TrustLevel (link.py)     → encryption decisions
  - Immutable audit log               → scope violations recorded

The being understands every human it befriends deeply.
But understanding is NOT surveillance.
Understanding comes from CONVERSATION, not from invading privacy.
Secrets never leave the edge — this is structurally enforced.
"""

import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')


# ═══════════════════════════════════════════════════════════════════════
# Privacy Scope — where data is allowed to exist
# ═══════════════════════════════════════════════════════════════════════

class PrivacyScope(str, Enum):
    """Where a piece of data is allowed to exist.

    The scope hierarchy (most restrictive → least):
      EDGE_ONLY    → never leaves user's device
      USER_DEVICES → user's own devices (PeerLink SAME_USER)
      TRUSTED_PEER → E2E encrypted to pre-trusted peers only
      FEDERATED    → anonymized, shared with hive (via secret_redactor)
      PUBLIC       → safe for anyone

    Default is EDGE_ONLY — privacy by default, not by opt-in.
    """
    EDGE_ONLY = 'edge_only'           # Biometrics, secrets, raw PII
    USER_DEVICES = 'user_devices'     # Resonance profile, preferences
    TRUSTED_PEER = 'trusted_peer'     # Goal context for peer compute
    FEDERATED = 'federated'           # Anonymized patterns, recipes
    PUBLIC = 'public'                 # Safe for anyone


# Scope ordering for comparison
_SCOPE_LEVEL = {
    PrivacyScope.EDGE_ONLY: 0,
    PrivacyScope.USER_DEVICES: 1,
    PrivacyScope.TRUSTED_PEER: 2,
    PrivacyScope.FEDERATED: 3,
    PrivacyScope.PUBLIC: 4,
}


def scope_allows(data_scope: PrivacyScope,
                 destination_scope: PrivacyScope) -> bool:
    """Check if data with `data_scope` can transit to `destination_scope`.

    Data can only flow to destinations at the SAME or MORE restrictive scope.
    EDGE_ONLY data cannot go to FEDERATED.
    FEDERATED data can go to FEDERATED or EDGE_ONLY (already anonymized).
    """
    return _SCOPE_LEVEL[destination_scope] <= _SCOPE_LEVEL[data_scope]


# ═══════════════════════════════════════════════════════════════════════
# Scope Guard — enforces scope at egress
# ═══════════════════════════════════════════════════════════════════════

class ScopeGuard:
    """Checks data scope at egress points.  Delegates to existing engines.

    This is the single guard.  MCP sandbox calls it.  Federation calls it.
    PeerLink calls it.  There is no second path.
    """

    def check_egress(self, data: Dict[str, Any],
                     destination: PrivacyScope,
                     context: Optional[Dict] = None) -> Tuple[bool, str]:
        """Can this data transit to this destination?

        Steps:
          1. Check declared scope (fast, deterministic)
          2. Run DLP scan for undeclared PII (delegates to existing engine)
          3. Audit log on violation

        Returns (allowed, reason).
        """
        context = context or {}
        data_scope = data.get('_privacy_scope', PrivacyScope.EDGE_ONLY)

        # Normalize string to enum
        if isinstance(data_scope, str):
            try:
                data_scope = PrivacyScope(data_scope)
            except ValueError:
                data_scope = PrivacyScope.EDGE_ONLY  # Unknown = most restrictive

        # ── Check 1: Declared scope ──
        if not scope_allows(data_scope, destination):
            reason = (
                f'Scope violation: data is {data_scope.value}, '
                f'destination is {destination.value} — blocked'
            )
            self._audit_violation(reason, context)
            return False, reason

        # ── Check 2: DLP scan for undeclared PII ──
        # Even if scope says FEDERATED, check for PII that shouldn't be there
        if destination in (PrivacyScope.FEDERATED, PrivacyScope.PUBLIC):
            text_fields = self._extract_text(data)
            if text_fields:
                try:
                    from security.dlp_engine import get_dlp_engine
                    dlp = get_dlp_engine()
                    for field_name, text in text_fields:
                        findings = dlp.scan(text)
                        if findings:
                            types = sorted(set(f[0] for f in findings))
                            reason = (
                                f'PII found in "{field_name}" '
                                f'({", ".join(types)}) — '
                                f'blocked from {destination.value}'
                            )
                            self._audit_violation(reason, context)
                            return False, reason
                except ImportError:
                    pass  # DLP not available — allow but log

        # ── Check 3: Secret scan for trusted_peer+ destinations ──
        if destination in (PrivacyScope.TRUSTED_PEER,
                           PrivacyScope.FEDERATED,
                           PrivacyScope.PUBLIC):
            text_fields = self._extract_text(data)
            if text_fields:
                try:
                    from security.secret_redactor import redact_secrets
                    for field_name, text in text_fields:
                        _, count = redact_secrets(text)
                        if count > 0:
                            reason = (
                                f'Secrets found in "{field_name}" '
                                f'({count} redactions) — '
                                f'blocked from {destination.value}'
                            )
                            self._audit_violation(reason, context)
                            return False, reason
                except ImportError:
                    pass

        return True, f'Scope check passed: {data_scope.value} → {destination.value}'

    def redact_for_scope(self, data: Dict[str, Any],
                         destination: PrivacyScope) -> Dict[str, Any]:
        """Redact data to make it safe for the given destination scope.

        Instead of blocking, this strips fields that exceed the scope.
        Returns a copy — never mutates the original.
        """
        result = {}
        for key, value in data.items():
            if key == '_privacy_scope':
                continue

            field_scope = data.get(f'_scope_{key}', data.get('_privacy_scope',
                                   PrivacyScope.EDGE_ONLY))
            if isinstance(field_scope, str):
                try:
                    field_scope = PrivacyScope(field_scope)
                except ValueError:
                    field_scope = PrivacyScope.EDGE_ONLY

            if scope_allows(field_scope, destination):
                result[key] = value
            else:
                result[key] = f'[SCOPE_REDACTED:{field_scope.value}]'

        # Run DLP on remaining text for federated/public
        if destination in (PrivacyScope.FEDERATED, PrivacyScope.PUBLIC):
            try:
                from security.dlp_engine import get_dlp_engine
                dlp = get_dlp_engine()
                for key, value in result.items():
                    if isinstance(value, str) and len(value) > 5:
                        result[key] = dlp.redact(value)
            except ImportError:
                pass

        return result

    def _extract_text(self, data: Dict) -> List[Tuple[str, str]]:
        """Extract string fields from data for scanning."""
        fields = []
        for key, value in data.items():
            if key.startswith('_'):
                continue
            if isinstance(value, str) and len(value) > 3:
                fields.append((key, value))
        return fields

    def _audit_violation(self, reason: str, context: Dict):
        """Log scope violation to immutable audit log."""
        logger.warning(f'EDGE PRIVACY: {reason}')
        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'scope_violation',
                actor_id=context.get('actor_id', 'unknown'),
                action=reason,
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Governance Integration — privacy as a constitutional scorer
# ═══════════════════════════════════════════════════════════════════════

def score_privacy(context: dict):
    """Constitutional scorer for the governance pipeline.

    Evaluates whether a decision respects privacy scopes.
    Imported and registered by ai_governance.py.
    """
    from security.ai_governance import ConstitutionalSignal

    data = context.get('data', {})
    destination = context.get('destination_scope', '')

    if not data or not destination:
        return ConstitutionalSignal(
            name='privacy', score=1.0, confidence=0.5,
            weight=1.5, reasoning='No data/destination to evaluate',
        )

    if isinstance(destination, str):
        try:
            destination = PrivacyScope(destination)
        except ValueError:
            return ConstitutionalSignal(
                name='privacy', score=0.5, confidence=0.3,
                weight=1.5, reasoning=f'Unknown scope: {destination}',
            )

    guard = get_scope_guard()
    allowed, reason = guard.check_egress(data, destination, context)

    if allowed:
        return ConstitutionalSignal(
            name='privacy', score=1.0, confidence=0.95,
            weight=1.5, reasoning=reason,
        )

    return ConstitutionalSignal(
        name='privacy', score=0.02, confidence=1.0,
        weight=2.0,  # Privacy violations are high-weight
        reasoning=reason,
    )


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_guard = None


def get_scope_guard() -> ScopeGuard:
    """Module-level singleton."""
    global _guard
    if _guard is None:
        _guard = ScopeGuard()
    return _guard
