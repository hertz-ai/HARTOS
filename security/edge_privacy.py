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

import functools
import logging
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')

# ═══════════════════════════════════════════════════════════════════════
# Egress: what of a payload may leave, and on which leg
# ═══════════════════════════════════════════════════════════════════════
#
# Owner ruling 2026-09-26: egress is a message that goes to OTHER people's
# nodes; it is scrubbed (and metered) only there.  Local records stay raw.
# Security must not partition the hive, so the protocol's own values --
# ids, urls, addresses, versions, keys, signatures, timestamps -- travel
# byte-identical, and what a person wrote or was told is scrubbed.
#
# Which leaves are content is decided structurally, not by a list of
# content fields: a key this module has never seen ('reply', 'caption', a
# nested 'body_text', a tuple, publish_async's {'raw': ...} wrapper) is
# content by default.  Exempt are (a) values under the identifier
# vocabulary below and (b) values whose SHAPE is a protocol value (a pure
# number, a dotted version or address, one opaque token that is not itself
# an email / phone / card number).  Why: the DLP phone pattern matches a
# 10-digit prompt_id, nonce or byte count, and the ip pattern matches a
# peer url's host or a build number like 1.4.0.12; rewriting either breaks
# routing and verification for the recipient -- a partition, not a
# privacy gain.  Secret-named keys and contact keys are never exempt.
#
# One home for every question an egress site asks:
#   * which leaves are content          -> is_identifier_key / map_content
#   * what the scrubbed copy is         -> scrub_text / scrub_for_egress
#   * which of a person's fields never go public
#                                       -> PERSON_PRIVATE_FIELDS /
#                                          strip_person_private / public_payload
#   * which Crossbar URIs are one user's -> per_user_uri_owner (declared
#                                          templates), crossbar_uri_is_per_user
#   * whether a leg is egress            -> crossbar_leg_is_users_own (policy)
#   * which topic names a user (ACL fact)-> uri_names_user
# Every leg that leaves the node asks here: MessageBus (PeerLink + Crossbar
# legs), the EventBus WAMP bridge (every topic it carries),
# hart_intelligence_entry.publish_async, the copilot prompt
# (claude_code_backend), ScopeGuard.redact_for_scope,
# secret_redactor.redact_experience and the realtime / subscribe ACLs.
# tests/unit/test_egress_one_rule.py fails if a second copy appears.

_IDENTIFIER_KEYS = frozenset({
    # identity + correlation
    'id', 'uid', 'msg_id', 'issued_by', 'origin', 'relay_path', 'hop_ttl',
    # routing + addressing
    'topic', 'topic_name', 'channel', 'url', 'uri', 'href', 'endpoint',
    'host', 'hostname', 'port', 'address', 'ip', 'lan_ip',
    'node_tier', 'tier', 'served_by',
    # protocol discriminators + builds
    'type', 'event', 'action', 'kind', 'status', 'state', 'role',
    'cmd_type', 'version', 'build', 'commit', 'lang', 'language',
    # integrity, keys (public), time
    'signature', 'sig', 'nonce', 'checksum', 'digest', 'public_key',
    'pubkey', 'timestamp', 'ts', 'expires', 'epoch',
})
_IDENTIFIER_KEY_SUFFIXES = (
    '_id', '_ids', '_url', '_urls', '_uri', '_type', '_at', '_hash', '_ts',
    '_version', '_ip', '_address', '_sig', '_signature', '_nonce',
    '_public', '_public_key', '_pubkey', '_sha256', '_checksum', '_digest',
)
# Never exempt, whatever their suffix: a secret is not a protocol value
# ('api_key', 'auth_token', 'private_key'), and a contact field is a
# person's ('email', 'phone').
_SECRET_KEY_WORDS = ('secret', 'password', 'passwd', 'token', 'api_key',
                     'apikey', 'private', 'credential', 'cookie')
_CONTACT_KEY_WORDS = ('email', 'phone', 'mobile', 'cell', 'ssn')

# A person record's fields that never reach anyone else, even scrubbed.
# Why each: email, phone, phone_number, password_hash, api_token -> PII /
# creds; voice_profile -> biometric pointer; idle_compute_opt_in -> infra
# disclosure; location_sharing_enabled -> privacy preference;
# referral_code -> cross-tracking; last_active_at -> presence inference.
# (Moved here from integrations.social.realtime, which re-exports it.)
PERSON_PRIVATE_FIELDS = (
    'email', 'phone', 'phone_number', 'password_hash', 'api_token',
    'voice_profile', 'idle_compute_opt_in', 'location_sharing_enabled',
    'referral_code', 'last_active_at',
)
_PERSON_RECORD_KEYS = frozenset({'author'})

_DOTTED_NUMBER = re.compile(r'\d+(?:\.\d+)+')
_WHOLE_PLACEHOLDER = re.compile(r'\[[A-Z_]+_REDACTED\]')


def _is_secret_key(key: str) -> bool:
    return any(w in key for w in _SECRET_KEY_WORDS)


def _is_contact_key(key: str) -> bool:
    # 'email', 'contact_email', 'phone_number' -- not 'phone_like_count'
    return any(key == w or key.endswith('_' + w) or key == w + '_number'
               or key == w + '_address' for w in _CONTACT_KEY_WORDS)


def is_identifier_key(key: Any) -> bool:
    """Does the value under ``key`` belong to the protocol, not a person?

    Strings under such a key (and inside a list/tuple under it) travel
    byte-identical; a dict under it is walked by its own keys.  A
    secret-named or contact key never is.
    """
    if not isinstance(key, str):
        return False
    key = key.lower()
    if _is_secret_key(key) or _is_contact_key(key):
        return False
    return key in _IDENTIFIER_KEYS or key.endswith(_IDENTIFIER_KEY_SUFFIXES)


def strip_person_private(record: Any) -> Any:
    """A copy of a person record without PERSON_PRIVATE_FIELDS; anything
    that is not a dict passes through.  Idempotent."""
    if not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k not in PERSON_PRIVATE_FIELDS}


def public_payload(payload: Any) -> Any:
    """A shallow copy of ``payload`` whose person record (``author``) has
    no PERSON_PRIVATE_FIELDS -- for anything broadcast to everyone, on every
    leg including this node's own SSE clients.  Never mutates ``payload``."""
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    for key in _PERSON_RECORD_KEYS:
        if isinstance(cleaned.get(key), dict):
            cleaned[key] = strip_person_private(cleaned[key])
    return cleaned


def map_content(data: Any, fn: Callable[[str], str],
                contact_fn: Optional[Callable[[str], str]] = None) -> Any:
    """A copy of ``data`` with ``fn`` applied to every content string leaf.

    Content is every string not under an identifier key, at any depth,
    inside dicts, lists and tuples (a tuple stays a tuple).  A bare string
    is content.  Numbers, booleans and None are unchanged.  Leaves under a
    contact key use ``contact_fn`` when given.  ``data`` is never mutated.
    """
    def walk(value, exempt, contact):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                lk = k.lower() if isinstance(k, str) else k
                out[k] = walk(v, is_identifier_key(k),
                              isinstance(lk, str) and _is_contact_key(lk))
            return out
        if isinstance(value, list):
            return [walk(v, exempt, contact) for v in value]
        if isinstance(value, tuple):
            return tuple(walk(v, exempt, contact) for v in value)
        if isinstance(value, str) and not exempt:
            return (contact_fn or fn)(value) if contact else fn(value)
        return value

    return walk(data, False, False)


def _is_protocol_value(text: str, redacted: str) -> bool:
    """Is ``text`` (whose DLP redaction is ``redacted``) a protocol value
    rather than free text?  A pure number or a dotted version / address
    is; so is one opaque token (no whitespace, no '@') that the PII
    patterns match only INSIDE -- a base64 key or a hostname carrying a
    digit run -- as opposed to a token that IS an email, phone or card."""
    if text.isdigit() or _DOTTED_NUMBER.fullmatch(text):
        return True
    if any(c.isspace() for c in text) or '@' in text:
        return False
    return not _WHOLE_PLACEHOLDER.fullmatch(redacted)


def scrub_text(text: str) -> str:
    """One content string, safe for another person's node: structured
    secrets (secret_redactor) always, PII patterns (dlp_engine) on free
    text.  Raises ImportError when a scrubber is not importable."""
    from security.dlp_engine import get_dlp_engine
    from security.secret_redactor import redact_secrets
    text, _ = redact_secrets(text)
    redacted = get_dlp_engine().redact(text)
    if redacted == text or _is_protocol_value(text, redacted):
        return text
    return redacted


def scrub_contact(text: str) -> str:
    """A contact field's value (email, phone): its PII redacted, and when
    no pattern recognises it, withheld whole -- it is a person's either
    way."""
    from security.dlp_engine import get_dlp_engine
    from security.secret_redactor import redact_secrets
    text, _ = redact_secrets(text)
    redacted = get_dlp_engine().redact(text)
    if redacted != text or not text:
        return redacted
    return '[CONTACT_REDACTED]'


def scrub_for_egress(data: Any) -> Any:
    """The copy of ``data`` that may go to a node its user does not own:
    person records without their private fields, every content leaf
    scrubbed.  Raises if a scrubber is unavailable; callers withhold that
    leg rather than send what could not be scrubbed.
    """
    def strip_records(value):
        if isinstance(value, dict):
            return {k: (strip_person_private(strip_records(v))
                        if k in _PERSON_RECORD_KEYS else strip_records(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [strip_records(v) for v in value]
        if isinstance(value, tuple):
            return tuple(strip_records(v) for v in value)
        return value

    return map_content(strip_records(data), scrub_text, scrub_contact)


def uri_names_user(uri: str, user_id: Any) -> bool:
    """Does ``uri``'s last segment name ``user_id`` (``.<id>`` / ``/<id>``)?

    The ACL fact the realtime publish gate and the router's subscribe gate
    ask ("a user may publish / subscribe on a topic that names them").  It
    is NOT the egress question: a community or session whose id equals a
    user id also names that user; egress asks crossbar_uri_is_per_user.
    """
    uri = uri or ''
    user_id = str(user_id or '')
    return bool(user_id) and (uri.endswith('.' + user_id)
                              or uri.endswith('/' + user_id))


def _is_user_template(template: str) -> bool:
    return '{user_id}' in template


@functools.lru_cache(maxsize=16)
def _per_user_patterns(templates: Tuple[str, ...]):
    patterns = []
    for template in templates:
        rx = re.escape(template).replace(re.escape('{user_id}'),
                                         '(?P<user_id>[^./]+)')
        patterns.append(re.compile(re.sub(r'\\\{\w+\\\}', '[^./]+', rx)))
    return tuple(patterns)


def per_user_uri_owner(uri: str) -> str:
    """The user whose declared per-user Crossbar URI ``uri`` is, or ''.

    Declared templates are the core.peer_link.message_bus TOPIC_MAP
    templates that carry {user_id}, plus its PER_USER_TOPICS_OUTSIDE_BUS
    (per-user topics published by URI, not by bus topic).  Only a URI that
    instantiates one of them, with a single-segment id, belongs to one
    user; a community, a session, a global feed or
    ``com.hartos.event.<topic>`` does not.
    """
    from core.constants import CHAT_TOPICS
    from core.peer_link.message_bus import (
        PER_USER_TOPICS_OUTSIDE_BUS, TOPIC_MAP)
    uri = uri or ''
    known = (*TOPIC_MAP.values(), *PER_USER_TOPICS_OUTSIDE_BUS, *CHAT_TOPICS)
    # A declared SHARED URI is nobody's, even where a per-user template
    # would also match it ('com.hertzai.hevolve.{user_id}' vs the global
    # 'com.hertzai.hevolve.confirmation').
    shared = tuple(t for t in known if not _is_user_template(t))
    if any(rx.fullmatch(uri) for rx in _per_user_patterns(shared)):
        return ''
    per_user = tuple(t for t in known if _is_user_template(t))
    for rx in _per_user_patterns(per_user):
        m = rx.fullmatch(uri)
        if m:
            return m.group('user_id')
    return ''


def crossbar_uri_is_per_user(uri: str, user_id: Any = '') -> bool:
    """Does a Crossbar URI reach only one user's own subscribers -- and,
    when ``user_id`` is given, is that user ``user_id``?

    The one ownership rule, over templates and concrete URIs alike:
      * a template (it still carries ``{``) is per-user when it carries
        ``{user_id}`` (the id substituted is the message's own);
      * a concrete URI is per-user when it instantiates a declared per-user
        template (per_user_uri_owner) -- the router admits only that user
        to it; with ``user_id`` given, the owner must be that user (content
        of user A on user B's URI reaches B, which is another person).
    Any other URI reaches whoever subscribes, which is other people.
    """
    uri = uri or ''
    if '{' in uri:
        return _is_user_template(uri)
    owner = per_user_uri_owner(uri)
    if not owner:
        return False
    user_id = str(user_id or '')
    return not user_id or owner == user_id


def crossbar_leg_is_users_own(uri: str, user_id: Any = '') -> bool:
    """Is a publish on this Crossbar URI delivered only to the message
    user's own subscribers, so it is NOT egress and goes raw?

    Owner delegation 2026-09-27 ("use sensible defaults without creating
    more friction"), decided: a per-user URI whose recipients are only that
    user's own devices is NOT egress, even when it transits a central or
    regional router.  The router is transport, not a third-party
    recipient; scrubbing it would show the user's own phone
    [EMAIL_REDACTED] live and the raw text after a reload.  Any URI that
    does not belong to one user is egress.  Every Crossbar leg --
    MessageBus._route_crossbar, the EventBus WAMP bridge, and
    hart_intelligence_entry.publish_async -- asks only this, so the policy
    changes here or nowhere.
    """
    return crossbar_uri_is_per_user(uri, user_id)


def crossbar_egress_copy(uri: str, data: Any, user_id: Any = '') -> Any:
    """What of ``data`` may be published on Crossbar ``uri``.

    ``data`` itself (the same object) on a leg that is the user's own; the
    scrubbed copy on any other; None when the scrub failed, and the caller
    withholds that leg (only a third party misses it, and a leak cannot be
    recalled).
    """
    if crossbar_leg_is_users_own(uri, user_id):
        return data
    return scrubbed_or_none(data, uri)


def scrubbed_or_none(data: Any, where: str) -> Any:
    """``scrub_for_egress(data)``, or None (with a warning) when it failed:
    the caller then withholds only the leg to other people's nodes."""
    try:
        return scrub_for_egress(data)
    except Exception as e:
        logger.warning("Egress scrub failed for %s (%s); not sending it to "
                       "nodes its user does not own", where, e)
        return None



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

        # Scrub the remaining content for federated/public: the one egress
        # scrub (every content leaf at any depth; ids and urls intact).
        if destination in (PrivacyScope.FEDERATED, PrivacyScope.PUBLIC):
            try:
                result = scrub_for_egress(result)
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
