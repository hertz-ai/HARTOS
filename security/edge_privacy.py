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
# ids, urls, peer addresses, versions, public keys, signatures, timestamps
# -- travel byte-identical, and what a person wrote or was told is scrubbed.
#
# Which leaves are content is decided structurally, not by a list of
# content fields: a key this module has never seen ('reply', 'caption', a
# nested 'body_text', a tuple, publish_async's {'raw': ...} wrapper) is
# content by default.  A value is exempt only when it sits under a
# protocol key AND has that key's shape: an 'ip' that is an address, a
# 'commit' that is a hash, a label that is a word, a url that names no
# person, an id with no whitespace, '@' or secret in it.  Why the
# exemption: the DLP phone pattern matches a 10-digit prompt_id or nonce
# and the ip pattern matches a peer url's host or a build number like
# 1.4.0.12; rewriting either breaks routing and verification for the
# recipient -- a partition, not a privacy gain.  In content every matched
# span is redacted where it sits, inside a token or not.  Contact keys
# (email, phone, and person handles: sender_*, caller_*, wa_*, client_ip)
# and secret keys (the whole key or its last word: api_key, auth_token,
# db_password) are never exempt; a value under them that no pattern
# recognises is withheld whole.  The one explicit exception is the
# capability advert's auth_token (for peers).
#
# One home for every question an egress site asks:
#   * which leaves are content          -> _leaf_policy / map_content
#   * what the scrubbed copy is         -> scrub_text / scrub_for_egress
#   * which of a person's fields never go public
#                                       -> PERSON_PRIVATE_FIELDS /
#                                          strip_person_private / public_payload
#   * which Crossbar URIs are one user's -> per_user_uri_owner (declared
#                                          templates), crossbar_uri_is_per_user
#   * whether a leg is egress            -> crossbar_leg_is_users_own (policy)
#   * which topic names a user (ACL fact)-> uri_names_user
#   * a person handle that must travel   -> pseudonym (salted, per node)
# Every leg that leaves the node asks here: MessageBus (PeerLink + Crossbar
# legs), the EventBus WAMP bridge (every topic it carries),
# hart_intelligence_entry.publish_async, the copilot prompt
# (claude_code_backend), ScopeGuard.redact_for_scope,
# secret_redactor.redact_experience and the realtime / subscribe ACLs.
# tests/unit/test_egress_one_rule.py fails if a second copy appears.

_IDENTIFIER_KEYS = frozenset({
    # identity + correlation
    'id', 'uid', 'msg_id', 'issued_by', 'origin', 'relay_path', 'hop_ttl',
    # integrity, public keys, time
    'signature', 'sig', 'nonce', 'public_key', 'pubkey',
    'timestamp', 'ts', 'expires', 'epoch',
})
_IDENTIFIER_KEY_SUFFIXES = (
    '_id', '_ids', '_at', '_ts', '_sig', '_signature', '_nonce',
    '_public', '_public_key', '_pubkey',
)
# Protocol LABELS: a word, never a number or an address ('status' holding a
# phone number is content).
_LABEL_KEYS = frozenset({
    'topic', 'topic_name', 'channel', 'type', 'event', 'action', 'kind',
    'status', 'state', 'role', 'cmd_type', 'lang', 'language', 'node_tier',
    'tier', 'served_by',
})
_LABEL_KEY_SUFFIXES = ('_type',)
# Where a node or a resource lives: exempt unless it carries a person's
# email / phone (a peer url's ip host is protocol).
_URL_KEYS = frozenset({'url', 'uri', 'href', 'endpoint', 'host', 'hostname',
                       'port'})
_URL_KEY_SUFFIXES = ('_url', '_urls', '_uri')
# Identifier keys whose value must also LOOK like the thing the key names:
# 'ip' holding an email, 'commit' holding an email, 'version' holding a
# sentence are content (review of d89d50223 F2).
_IP_KEYS, _IP_KEY_SUFFIXES = frozenset({'ip', 'lan_ip'}), ('_ip',)
_VERSION_KEYS, _VERSION_KEY_SUFFIXES = frozenset({'version', 'build'}), ('_version',)
_COMMIT_KEYS = frozenset({'commit'})
_DIGEST_KEYS = frozenset({'checksum', 'digest'})
_DIGEST_KEY_SUFFIXES = ('_hash', '_sha256', '_checksum', '_digest')
_MEASURE_KEY_SUFFIXES = ('_bytes', '_count', '_size', '_ms', '_mb', '_gb')
# 'address' / '*_address': a peer's host:port is protocol, anything else
# (a street, a phone number) is a person's and is withheld like a contact.
_ADDRESS_KEYS, _ADDRESS_KEY_SUFFIXES = frozenset({'address'}), ('_address',)

_IP_SHAPE = re.compile(
    r'(?:\d{1,3}(?:\.\d{1,3}){3}|\[?[0-9A-Fa-f]*:[0-9A-Fa-f:]+\]?)(?::\d{1,5})?')
_HOST_PORT_SHAPE = re.compile(
    r'(?=[^:]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+(?::\d{1,5})?'
    r'|[A-Za-z0-9][A-Za-z0-9.-]*:\d{1,5}')
_VERSION_SHAPE = re.compile(r'v?\d+(?:[._+-][0-9A-Za-z]+)*')
_COMMIT_SHAPE = re.compile(r'[0-9a-fA-F]{7,64}')
_DIGEST_SHAPE = re.compile(r'(?:[a-z0-9]+:)?[A-Za-z0-9+/=_-]{16,}')
_NUMBER_SHAPE = re.compile(r'-?\d+(?:\.\d+)?')
_LABEL_SHAPE = re.compile(r'[A-Za-z][\w.:/-]*')

# A person's handle, whatever id-like suffix its key has (review CRITICAL:
# hive.signal.* sent sender_id = a Signal / iMessage phone number raw):
# a key with one of these words as a whole segment is a contact field.
_PERSON_HANDLE_SEGMENTS = frozenset({
    'sender', 'caller', 'callee', 'contact', 'recipient', 'wa', 'msisdn'})
_PERSON_HANDLE_KEYS = frozenset({'from', 'to'})
# An ip that is a PERSON's (the far end of a user's request), not a node's.
_PERSON_IP_SEGMENTS = frozenset({'client', 'remote', 'user', 'source',
                                 'caller', 'sender', 'visitor', 'mobile',
                                 'phone', 'home', 'device'})

# Never exempt: a secret is not a protocol value ('api_key', 'auth_token',
# 'db_password', 'private_key'), and a contact field is a person's
# ('email', 'phone').  A value under either that no pattern recognises is
# withheld whole, not sent raw.  Secret words match the whole key or its
# last segment -- 'tokenizer', 'token_type', 'max_tokens', 'secret_name',
# 'private_mode' are protocol, not secrets.
_SECRET_KEY_WORDS = ('secret', 'password', 'passwd', 'pwd', 'pass', 'token',
                     'api_key', 'apikey', 'access_key', 'private_key',
                     'credential', 'credentials', 'cookie')
_CONTACT_KEY_WORDS = ('email', 'phone', 'mobile', 'cell', 'ssn')
# A secret-keyed value that says there is no secret stays readable.
_SECRET_STATUS_WORDS = frozenset({
    '', 'none', 'null', 'nil', 'na', 'n/a', 'unset', 'empty', 'missing',
    'expired', 'invalid', 'revoked', 'pending', 'unknown', 'true', 'false'})

# The ONE secret that is meant for other nodes: the capability advert's
# auth_token (peers call the advertised endpoint with it).  Exempt only in
# a dict that also carries the signed origin_attestation -- the advert
# shape (hive_capability_advertiser._build_payload) -- and nowhere else.
_GOSSIP_SECRET_KEYS = frozenset({'auth_token'})
_GOSSIP_MARKER_KEY = 'origin_attestation'

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

_PLACEHOLDER = re.compile(r'\[[A-Z_]+_REDACTED\]')
_PERSON_PLACEHOLDER = re.compile(r'\[(?!IP_)[A-Z_]+_REDACTED\]')

# Leaf policies (what a string under a key is)
_CONTENT, _EXEMPT, _CONTACT, _SECRET = 'content', 'exempt', 'contact', 'secret'


def _is_secret_key(key: str) -> bool:
    if {'password', 'passwd', 'pwd'}.intersection(key.split('_')):
        return True                              # password_hash, pwd_salt
    return any(key == w or key.endswith('_' + w) for w in _SECRET_KEY_WORDS)


def _is_contact_key(key: str) -> bool:
    # 'email', 'contact_email', 'phone_number', 'sender_id', 'wa_id',
    # 'client_ip' -- not 'phone_like_count' or 'peer_ip'
    if key in _PERSON_HANDLE_KEYS:
        return True
    segments = key.split('_')
    if _PERSON_HANDLE_SEGMENTS.intersection(segments):
        return True
    if segments[-1] == 'ip' and _PERSON_IP_SEGMENTS.intersection(segments):
        return True
    return any(key == w or key.endswith('_' + w) or key == w + '_number'
               or key == w + '_address' for w in _CONTACT_KEY_WORDS)


def _names_a_person(value: str) -> bool:
    """Does ``value`` contain an email / phone / ssn / card (an ip alone is
    not a person here: it is where a node lives)?"""
    dlp, redact_secrets = _redactors()
    return bool(_PERSON_PLACEHOLDER.search(dlp.redact(value))) or bool(
        redact_secrets(value)[1])


def _exempt_shape(key: str, value: str) -> bool:
    """Is ``value`` under the protocol key ``key`` shaped like what the key
    names, so it travels byte-identical?

    A key NAME alone never exempts a value; the value must also match the
    shape its key names.  Do not "simplify" this into a name allowlist:
    senders put a phone number under sender_id, an email under 'ip', a
    sentence under 'status', and each of those went raw to every hive node
    while only the name was checked (reviews of d89d50223 and the
    c072913e4 set).  When a real protocol value fails its shape, the
    failure is loud, not silent: a routing key the scrub altered withholds
    the leg with a warning (routing_keys_intact).
    """
    if not value or any(c.isspace() for c in value) or '@' in value:
        return False
    if key in _IP_KEYS or key.endswith(_IP_KEY_SUFFIXES):
        return bool(_IP_SHAPE.fullmatch(value))
    if key in _VERSION_KEYS or key.endswith(_VERSION_KEY_SUFFIXES):
        return bool(_VERSION_SHAPE.fullmatch(value))
    if key in _COMMIT_KEYS:
        return bool(_COMMIT_SHAPE.fullmatch(value))
    if key in _DIGEST_KEYS or key.endswith(_DIGEST_KEY_SUFFIXES):
        return bool(_DIGEST_SHAPE.fullmatch(value)) and not _names_a_person(value)
    if key.endswith(_MEASURE_KEY_SUFFIXES):
        return bool(_NUMBER_SHAPE.fullmatch(value))
    if key in _LABEL_KEYS or key.endswith(_LABEL_KEY_SUFFIXES):
        return bool(_LABEL_SHAPE.fullmatch(value))
    if key in _URL_KEYS or key.endswith(_URL_KEY_SUFFIXES):
        return not _names_a_person(value)
    if key in _IDENTIFIER_KEYS or key.endswith(_IDENTIFIER_KEY_SUFFIXES):
        return not _redactors()[1](value)[1]
    return False


def _leaf_policy(key: Any, value: str, gossip: bool) -> str:
    """What the string ``value`` under ``key`` is, in a dict that is (or is
    not) the capability advert."""
    if not isinstance(key, str):
        return _CONTENT
    lk = key.lower()
    if _is_contact_key(lk):
        return _CONTACT
    if _is_secret_key(lk):
        if gossip and lk in _GOSSIP_SECRET_KEYS:
            return _EXEMPT
        return _SECRET
    if lk in _ADDRESS_KEYS or lk.endswith(_ADDRESS_KEY_SUFFIXES):
        return _EXEMPT if (_IP_SHAPE.fullmatch(value)
                           or _HOST_PORT_SHAPE.fullmatch(value)) else _CONTACT
    return _EXEMPT if _exempt_shape(lk, value) else _CONTENT


def routing_keys_intact(original: Any, scrubbed: Any, keys, where: str) -> bool:
    """Did the scrub leave every payload key a URI or lookup is built from
    byte-identical?  If not, the scrubbed copy would route nowhere (the URI
    says one id, the payload another) and nothing would say so: log a
    WARNING and return False, so the caller withholds that leg exactly as
    it does for a failed scrub.  The fix is to classify the key (a protocol
    key with its shape in _exempt_shape), never to send the raw value."""
    if not isinstance(original, dict) or not isinstance(scrubbed, dict):
        return True
    changed = [k for k in keys
               if k in original and scrubbed.get(k) != original.get(k)]
    if changed:
        logger.warning(
            "Egress withheld for %s: the scrub altered routing key(s) %s, so "
            "the scrubbed copy would not reach its subscribers; classify the "
            "key in security.edge_privacy (a protocol key and its shape)",
            where, ', '.join(sorted(changed)))
        return False
    return True


def pseudonym(value: Any, purpose: str) -> str:
    """A stable, salted per-node stand-in for a person handle (a sender's
    phone number, a chat id): the same handle maps to the same reference on
    this node, and no other node can reverse or correlate it.  The salt is
    this node's social secret key (core.platform_paths
    .read_social_secret_key) under a ``purpose`` label, or a per-process
    random salt when the node has none.  '' stays ''."""
    import hashlib
    import hmac
    value = str(value or '')
    if not value:
        return ''
    global _PSEUDONYM_FALLBACK_SALT
    try:
        from core.platform_paths import read_social_secret_key
        key = read_social_secret_key()
    except Exception:
        key = ''
    if not key:
        if _PSEUDONYM_FALLBACK_SALT is None:
            import os as _os
            _PSEUDONYM_FALLBACK_SALT = _os.urandom(32).hex()
        key = _PSEUDONYM_FALLBACK_SALT
    digest = hmac.new(key.encode(), f'{purpose}|{value}'.encode(),
                      hashlib.sha256).hexdigest()
    return 'anon_' + digest[:16]


_PSEUDONYM_FALLBACK_SALT = None


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
                contact_fn: Optional[Callable[[str], str]] = None,
                secret_fn: Optional[Callable[[str], str]] = None) -> Any:
    """A copy of ``data`` with ``fn`` applied to every content string leaf.

    A string under an identifier key whose value has that key's shape is
    exempt; every other string is content, at any depth, inside dicts,
    lists and tuples (a tuple stays a tuple).  A bare string is content.
    Leaves under a contact / secret key use ``contact_fn`` / ``secret_fn``
    when given.  Numbers, booleans and None are unchanged.  ``data`` is
    never mutated.
    """
    by_policy = {_CONTENT: fn, _CONTACT: contact_fn or fn,
                 _SECRET: secret_fn or fn}

    def walk(value, key, gossip):
        if isinstance(value, dict):
            inner = _GOSSIP_MARKER_KEY in value
            return {k: walk(v, k, inner) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key, gossip) for v in value]
        if isinstance(value, tuple):
            return tuple(walk(v, key, gossip) for v in value)
        if isinstance(value, str):
            policy = _leaf_policy(key, value, gossip)
            return value if policy == _EXEMPT else by_policy[policy](value)
        return value

    return walk(data, None, False)


def _redactors():
    from security.dlp_engine import get_dlp_engine
    from security.secret_redactor import redact_secrets
    return get_dlp_engine(), redact_secrets


def scrub_text(text: str) -> str:
    """One content string, safe for another person's node: structured
    secrets (secret_redactor) and PII patterns (dlp_engine) replaced, span
    by span.  Content has no shape allowance: a phone, email or ip inside
    one spaceless token (wa.me/14155550199, compact JSON, ip=...;port=) is
    redacted where it sits (review of the c072913e4 set).  Raises
    ImportError when a scrubber is not importable.
    """
    dlp, redact_secrets = _redactors()
    return dlp.redact(redact_secrets(text)[0])


def scrub_contact(text: str) -> str:
    """A contact field's value (email, phone, a street address) is a
    person's whole: it goes as the one placeholder its pattern names when
    it is exactly one email / phone / ..., and withheld whole otherwise (a
    street with an email in it keeps no street)."""
    if not text:
        return text
    dlp, redact_secrets = _redactors()
    redacted = dlp.redact(redact_secrets(text)[0])
    if _PLACEHOLDER.fullmatch(redacted):
        return redacted
    return '[CONTACT_REDACTED]'


def scrub_secret(text: str) -> str:
    """A secret-keyed value: redacted by the secret patterns, and when none
    recognises it, withheld whole (an 'AIzaSy...' fragment or an opaque
    token is still a secret).  A status word ('none', 'expired') stays."""
    _, redact_secrets = _redactors()
    redacted, n = redact_secrets(text)
    if n:
        return redacted
    if text.strip().lower() in _SECRET_STATUS_WORDS:
        return text
    return '[SECRET_REDACTED]'


def scrub_for_egress(data: Any) -> Any:
    """The copy of ``data`` that may go to a node its user does not own:
    person records without their private fields, every content leaf
    scrubbed, contact and secret values withheld when unrecognised.
    Raises if a scrubber is unavailable; callers withhold that leg rather
    than send what could not be scrubbed.
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

    return map_content(strip_records(data), scrub_text, scrub_contact,
                       scrub_secret)


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


def _declared_templates():
    """(the declared templates except catch-alls, the catch-all templates).

    A catch-all template ('com.hertzai.hevolve.{user_id}') would name every
    undeclared URI under its namespace a user's, so it never ATTRIBUTES a
    URI to anyone (review of d89d50223 F7); only its instance for a user
    the caller names is that user's (crossbar_uri_is_per_user)."""
    from core.constants import CHAT_TOPICS
    from core.peer_link.message_bus import (
        CATCH_ALL_TOPICS, PER_USER_TOPICS_OUTSIDE_BUS, TOPIC_MAP)
    catch_all = tuple(TOPIC_MAP[t] for t in CATCH_ALL_TOPICS)
    known = tuple(t for t in (*TOPIC_MAP.values(),
                              *PER_USER_TOPICS_OUTSIDE_BUS, *CHAT_TOPICS)
                  if t not in catch_all)
    return known, catch_all


def _is_declared_shared(uri: str) -> bool:
    """A declared SHARED URI is nobody's, even where a per-user template
    would also match it ('com.hertzai.hevolve.{user_id}' vs the global
    'com.hertzai.hevolve.confirmation')."""
    known, _ = _declared_templates()
    shared = tuple(t for t in known if not _is_user_template(t))
    return any(rx.fullmatch(uri) for rx in _per_user_patterns(shared))


def per_user_uri_owner(uri: str) -> str:
    """The user whose declared per-user Crossbar URI ``uri`` is, or ''.

    Declared templates are the core.peer_link.message_bus TOPIC_MAP
    templates that carry {user_id}, plus its PER_USER_TOPICS_OUTSIDE_BUS
    (per-user topics published by URI, not by bus topic).  Only a URI that
    instantiates one of them, with a single-segment id, belongs to one
    user; a community, a session, a global feed or
    ``com.hartos.event.<topic>`` does not.
    """
    uri = uri or ''
    known, _ = _declared_templates()
    if _is_declared_shared(uri):
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
    user_id = str(user_id or '')
    owner = per_user_uri_owner(uri)
    if owner:
        return not user_id or owner == user_id
    # A catch-all template's own instance for the NAMED user is theirs --
    # one rule, one answer with crossbar_topic_is_per_user('chat.general').
    if not user_id or '.' in user_id or '/' in user_id:
        return False
    _, catch_all = _declared_templates()
    return (uri in {t.replace('{user_id}', user_id) for t in catch_all}
            and not _is_declared_shared(uri))


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
