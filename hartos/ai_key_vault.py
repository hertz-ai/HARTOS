"""
AIKeyVault — Thin coordination layer for agent credential management.

PRIVACY RULE: Secrets NEVER leave the user's device.
  - Stored only on the local device (encrypted vault at secrets.enc)
  - NEVER transmitted over network, federation, gossip, PeerLink, or WAMP
  - NEVER included in hive deltas, task payloads, or agent responses
  - Credential endpoints accept connections from localhost ONLY
  - Hive/idle tasks get API keys STRIPPED (see tool_backends.py)
  - Output is redacted by secret_redactor.py (3-layer defense)
  - Even trusted hive nodes never receive the actual secret value

The user's secrets belong to the user, on the user's device, period.

Delegates ALL storage to SecretsManager (security/secrets_manager.py).
Adds:
  - Channel-specific key namespacing (discord + BOT_TOKEN → DISCORD_BOT_TOKEN)
  - Pending credential request tracking (what agents are waiting for)
  - Boot-time env preloading (config_cache.py contract)
  - store_credential() that persists; os.environ only for a name the
    process reads from it (reads_from_env)
  - is_local_request() gate for credential endpoints

Usage:
    from hartos.ai_key_vault import AIKeyVault
    vault = AIKeyVault.get_instance()
    key = vault.get_tool_key('OPENAI_API_KEY')
    token = vault.get_channel_secret('discord', 'BOT_TOKEN')
"""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_security')

#: How a stored credential appears to the model: {{secret:NAME}}.  The model
#: only ever sees and writes this alias; resolve_aliases puts the real value
#: in where a tool runs and mask_secrets turns it back into the alias on the
#: way out, so the value never enters the model's context, logs or chat.
SECRET_ALIAS_RE = re.compile(r'\{\{secret:([A-Z0-9_]+)\}\}')

#: A stored value shorter than this is not masked: masking every occurrence
#: of "abc" in tool output would mangle ordinary text.
MIN_MASKED_SECRET_LEN = 4

#: Longest credential name: 'secret:' + the name is a consent scope, and
#: UserConsent.scope holds 100 characters.
MAX_CREDENTIAL_NAME_LEN = 90

#: A rejected login asks the owner again (7f3d5468f), but only this many
#: values per credential within CREDENTIAL_ENTRY_WINDOW_S.  After that the
#: agent is told to stop and tell the user: three wrong entries in a day is
#: a problem for the person, not something another card will fix.
MAX_CREDENTIAL_ENTRIES = 3
CREDENTIAL_ENTRY_WINDOW_S = 24 * 3600

# ═══════════════════════════════════════════════════════════════════════
# Pending credential request tracking
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class PendingCredentialRequest:
    """A credential an agent is blocked waiting for."""
    request_id: str
    key_name: str
    resource_type: str   # api_key | channel_secret | token | config
    channel_type: str
    label: str
    description: str
    used_by: str
    requested_at: float


# ═══════════════════════════════════════════════════════════════════════
# AIKeyVault
# ═══════════════════════════════════════════════════════════════════════

class AIKeyVault:
    """Agent credential vault — delegates storage to SecretsManager."""

    _instance: Optional['AIKeyVault'] = None
    _cls_lock = threading.Lock()

    def __init__(self):
        self._pending: Dict[str, PendingCredentialRequest] = {}
        self._lock = threading.Lock()
        self._sm = None  # Lazy — loaded on first use
        # Names stored through store_credential this process: with no
        # HEVOLVE_MASTER_KEY the value lives only in this process's vault
        # cache, which cannot say which of its names the owner entered.
        self._stored: set = set()
        # Values another store keeps (Nunba's desktop vault), handed over by
        # hold_credential: memory only, never persisted, never os.environ.
        self._held: Dict[str, str] = {}
        # Every credential value this process has known -> its name: each
        # value resolve_aliases handed a tool and each value mask_secrets
        # masked.  Keyed by VALUE and never pruned, so masking never shrinks:
        # a revoke during the call, a locked database or an owner who
        # re-enters a different value leaves the old value masked.  A revoked
        # credential's value therefore stays in this process's memory until
        # restart (it was already in os.environ, where the card put it).
        self._resolved: Dict[str, str] = {}
        # Every name ever read as granted; never pruned, for the same reason.
        self._granted_seen: set = set()

    @classmethod
    def get_instance(cls) -> 'AIKeyVault':
        """Thread-safe singleton (matches hart_intelligence:1796 call)."""
        if cls._instance is None:
            with cls._cls_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Clear singleton (testing)."""
        cls._instance = None

    # ── Internal ───────────────────────────────────────────────────

    def _secrets_manager(self):
        """Lazy import to avoid circular imports at boot."""
        if self._sm is None:
            from security.secrets_manager import SecretsManager
            self._sm = SecretsManager.get_instance()
        return self._sm

    @staticmethod
    def _resolve_channel_key(channel_type: str, key_name: str) -> str:
        """Resolve channel + key into env-var name.

        ('discord', 'BOT_TOKEN') → 'DISCORD_BOT_TOKEN'
        ('', 'API_KEY')          → 'API_KEY'
        ('slack', 'SLACK_TOKEN') → 'SLACK_TOKEN'  (no double prefix)
        """
        key_upper = key_name.upper()
        if not channel_type:
            return key_upper
        prefix = channel_type.upper() + '_'
        if key_upper.startswith(prefix):
            return key_upper
        return prefix + key_upper

    # ── Retrieval ──────────────────────────────────────────────────

    def get_tool_key(self, key_name: str) -> str:
        """Get a tool/API key: SecretsManager (environment, then its vault),
        then what another store handed over (hold_credential)."""
        return (self._secrets_manager().get_secret(key_name)
                or self._held.get(key_name, ''))

    def get_channel_secret(self, channel_type: str, key_name: str) -> str:
        """Get a channel-specific secret.

        Resolves name then delegates to SecretsManager.
        """
        resolved = self._resolve_channel_key(channel_type, key_name)
        return self._secrets_manager().get_secret(resolved)

    # ── Storage ────────────────────────────────────────────────────

    def store_credential(self, key_name: str, value: str,
                         channel_type: str = '') -> str:
        """Store a credential in the vault.  It reaches a tool through its
        {{secret:NAME}} alias; it is put in os.environ too only when the
        process reads that name from its environment (reads_from_env).

        Returns the resolved key name.
        """
        resolved = self._resolve_channel_key(channel_type, key_name) \
            if channel_type else key_name.upper()

        with self._lock:
            # Replace the process value only when it is ours: unset, or the
            # value this vault stored.  /api/credentials/submit takes any
            # name, and PATH, HTTPS_PROXY or NUNBA_CI must not be replaced.
            current = os.environ.get(resolved)
            ours = (resolved in self._stored
                    or current == self._secrets_manager()._cache.get(resolved))
            # A node secret is refused the same way, held or not: only the
            # node's own vault preload sets it.
            if (current and not ours) or is_node_secret(resolved):
                # Answered as any store is (the resolved name): an error
                # here told a caller the variable exists (secrets review,
                # the F1 oracle).  Nothing is written; the log says why.
                logger.warning("credential %s not stored: the process holds "
                               "it and this vault did not store it", resolved)
                return resolved
            # Persist to encrypted vault
            try:
                self._secrets_manager().set_secret(resolved, value)
            except RuntimeError:
                # HEVOLVE_MASTER_KEY not set: held in memory only
                logger.warning(
                    f"Vault unavailable, holding {resolved} in memory only "
                    "(will not persist across restarts)"
                )

            # The vault holds it (set_secret fills the cache even when it
            # cannot persist); the environment only for a name the process
            # reads from it.  A card entry named NUNBA_CI or HTTPS_PROXY must
            # never become configuration.
            self._secrets_manager()._cache[resolved] = value
            if reads_from_env(resolved):
                os.environ[resolved] = value
            self._stored.add(resolved)

            # Clear pending request
            self._pending.pop(resolved, None)

        # Audit log (key name only, never value)
        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'credential_stored',
                actor_id='ai_key_vault',
                action=f'stored credential {resolved}',
            )
        except Exception:
            pass

        logger.info(f"Credential stored: {resolved}")
        return resolved

    def hold_credential(self, key_name: str, value: str) -> None:
        """Hold a value another store keeps, so its alias can resolve here:
        Nunba's desktop vault (export_to_env) hands over what the consent
        card stored instead of putting it in os.environ.  In memory only;
        never the environment.  It resolves once the owner's grant names it
        (owner_credential_names)."""
        if not key_name or not value or is_node_secret(str(key_name)):
            return
        with self._lock:
            # Beside the SecretsManager cache, not in it: set_secret writes
            # the whole cache to secrets.enc, and Nunba's vault already keeps
            # this value.
            self._held[str(key_name)] = value

    # ── Alias (the only form the model sees) ───────────────────────

    def credential_name(self, key_name: str, channel_type: str = '') -> str:
        """The one name a credential goes by: its vault/env key, its alias and
        its consent scope.  Anything outside [A-Z0-9_] becomes '_' so the
        alias always matches SECRET_ALIAS_RE."""
        resolved = self._resolve_channel_key(channel_type, key_name) \
            if channel_type else str(key_name).upper()
        return re.sub(r'[^A-Z0-9_]', '_', resolved)[:MAX_CREDENTIAL_NAME_LEN]

    def alias_for(self, key_name: str, channel_type: str = '') -> str:
        """The {{secret:NAME}} alias the model uses for a stored credential."""
        return '{{secret:' + self.credential_name(key_name, channel_type) + '}}'

    @classmethod
    def _map_strings(cls, value, fn):
        """Apply ``fn`` to every string inside lists, tuples and dicts."""
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, dict):
            return {k: cls._map_strings(v, fn) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(cls._map_strings(v, fn) for v in value)
        return value

    def owner_credential_names(self, strict: bool = False) -> set:
        """The names of the credentials the owner of this computer entered:
        the only names an alias resolves and "is stored" answers for.

        - what store_credential stored this process (/api/credentials/submit,
          localhost only), and
        - what the owner gave on the consent card: a granted, unrevoked
          'credential' row with scope 'secret:NAME' for this computer's owner
          (HEVOLVE_OWNER_USER_ID).  The card puts the value in Nunba's
          desktop vault (/api/vault/store, which exports it to os.environ)
          and only then grants the row.  The row is the durable,
          cross-process record of "the owner entered NAME": it survives a
          restart, the privacy page lists it, and revoking it stops the
          alias resolving.  The value stores stay where they are; this is
          the one answer to WHICH names are the owner's.

        Never "any environment variable": os.environ also holds API keys,
        database URLs and key material nobody entered for an agent, and an
        alias that reached them let a model (or a page it read) put them in
        a tool URL.  A consent lookup that fails counts no grants, unless
        ``strict``: then it raises, for a caller that must tell "the owner
        has none" from "it cannot be told" (channels.agent_tools
        _owner_value_fingerprint).
        """
        names = set(self._stored)
        try:
            names |= self._granted_names()
        except Exception:
            if strict:
                raise
            logger.warning("credential grants could not be read; only "
                           "credentials stored this session resolve",
                           exc_info=True)
        return names

    def _granted_names(self) -> set:
        """The names the owner granted on the consent card, as the consent
        table says now.  Raises when it cannot be read; each name read is
        also added to _granted_seen, which mask_secrets keeps masking."""
        owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
        if not owner:
            return set()
        from integrations.social.consent_service import (
            CREDENTIAL_SCOPE_PREFIX, ConsentService)
        from integrations.social.models import db_session
        names = set()
        with db_session(commit=False) as db:
            for row in ConsentService.list_consents(db, owner, 'credential'):
                scope = row.scope or ''
                if (row.granted and row.revoked_at is None
                        and scope.startswith(CREDENTIAL_SCOPE_PREFIX)):
                    names.add(scope[len(CREDENTIAL_SCOPE_PREFIX):])
        with self._lock:
            self._granted_seen |= names
        return names

    def owner_credential(self, name: str, names=None) -> str:
        """The value of an owner-entered credential, '' for any other name.

        ``names`` is owner_credential_names(), passed by a caller that asks
        about several names at once.
        """
        if name not in (self.owner_credential_names() if names is None
                        else names):
            return ''
        return self.get_tool_key(name)

    def resolve_aliases(self, value):
        """Replace every alias of an owner-entered credential in ``value``
        with the real value.

        Any other alias (a missing credential, or a name that is only an
        environment variable) is left as written, never as an empty string.
        The grant decides resolution only: every value handed out is
        remembered, and mask_secrets masks it from then on.  A consent read
        that fails resolves no granted name (owner_credential_names).
        """
        names = None

        def _real(match):
            nonlocal names
            if names is None:
                names = self.owner_credential_names()
            real = self.owner_credential(match.group(1), names)
            if not real:
                return match.group(0)
            with self._lock:
                self._resolved[real] = match.group(1)
            return real
        return self._map_strings(value, lambda s: SECRET_ALIAS_RE.sub(_real, s))

    @staticmethod
    def _spellings(real: str) -> set:
        """How a value can come back in a tool's text: as written, as
        json.dumps escapes it (the autogen error envelope), and
        percent-encoded (an HTTP client puts the URL in its exception)."""
        from urllib.parse import quote, quote_plus
        return {real, json.dumps(real)[1:-1], quote(real, safe=''),
                quote_plus(real, safe='')}

    def mask_secrets(self, value):
        """Replace every credential value in ``value`` with its alias.

        "Every credential value" is the union of:
          - every value this process has known (_resolved): each value
            resolve_aliases handed a tool and each value masked before;
          - the current value of every name the owner granted, now or at any
            earlier read (_granted_seen), so a card value that reaches tool
            output without an alias (an env dump, a page echoing it) and a
            re-entered value not yet resolved are masked too;
          - what store_credential stored, and the encrypted vault's own.
        The grant read is best-effort: when it fails, only names never seen
        before are missed, and nothing masked before stops being masked.
        Grants gate resolution; masking never shrinks.  Every spelling in
        _spellings is matched, longest first so a credential that contains
        another is masked whole.
        """
        try:
            self._granted_names()
        except Exception:
            logger.warning("credential grants could not be read; masking "
                           "the credentials already known", exc_info=True)
        with self._lock:
            names = (set(self._granted_seen) | set(self._stored)
                     | set(self._secrets_manager()._cache))
        for name in names:
            real = self.get_tool_key(name)
            if real:
                with self._lock:
                    self._resolved.setdefault(real, name)
        with self._lock:
            known = dict(self._resolved)
        pairs = []
        for real, name in known.items():
            if len(real) >= MIN_MASKED_SECRET_LEN:
                alias = '{{secret:' + name + '}}'
                pairs.extend((s, alias) for s in self._spellings(real))
        pairs.sort(key=lambda p: -len(p[0]))

        def _mask(text):
            for real, alias in pairs:
                text = text.replace(real, alias)
            return text
        return self._map_strings(value, _mask) if pairs else value

    # ── Boot Preload ───────────────────────────────────────────────

    def preload_env(self) -> int:
        """Load the vault secrets the process reads from its environment
        (reads_from_env) into os.environ.  Any other vault value (a
        credential entered for an agent) stays in the vault and resolves
        through its alias.

        Called at boot BEFORE config_cache runs.
        Skips keys already present in os.environ.
        Returns count of keys loaded.
        """
        sm = self._secrets_manager()
        loaded = 0

        # Load from encrypted vault cache
        for key, value in sm._cache.items():
            if key not in os.environ and value and (
                    reads_from_env(key) or is_node_secret(key)):
                os.environ[key] = value
                loaded += 1

        # Bundled mode: also check langchain_config.json next to exe
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            config_path = os.path.join(exe_dir, 'langchain_config.json')
            if os.path.exists(config_path):
                try:
                    import json
                    with open(config_path, 'r') as f:
                        bundled = json.load(f)
                    for key, value in bundled.items():
                        if isinstance(value, str) and key not in os.environ and value:
                            os.environ[key] = value
                            loaded += 1
                except Exception as e:
                    logger.warning(f"Failed to load bundled config: {e}")

        if loaded:
            logger.info(f"AIKeyVault preloaded {loaded} secrets into env")
        return loaded

    # ── Pending Request Tracking ───────────────────────────────────

    def add_pending_request(self, key_name: str,
                            resource_type: str = 'api_key',
                            channel_type: str = '',
                            label: str = '',
                            description: str = '',
                            used_by: str = '') -> str:
        """Register a credential as needed-but-missing.

        Deduplicates by resolved key name. Returns request_id.
        """
        resolved = self._resolve_channel_key(channel_type, key_name) \
            if channel_type else key_name.upper()

        with self._lock:
            existing = self._pending.get(resolved)
            if existing:
                existing.requested_at = time.time()
                return existing.request_id

            req = PendingCredentialRequest(
                request_id=uuid.uuid4().hex,
                key_name=resolved,
                resource_type=resource_type,
                channel_type=channel_type,
                label=label or resolved,
                description=description,
                used_by=used_by,
                requested_at=time.time(),
            )
            self._pending[resolved] = req
            return req.request_id

    def get_pending_requests(self) -> List[dict]:
        """Return all pending credential requests as dicts."""
        with self._lock:
            return [asdict(r) for r in self._pending.values()]

    def clear_pending(self, key_name: str):
        """Remove a pending request by key name."""
        resolved = key_name.upper()
        with self._lock:
            self._pending.pop(resolved, None)

    def has_pending(self, key_name: str) -> bool:
        """Check if a key has a pending request."""
        resolved = key_name.upper()
        with self._lock:
            return resolved in self._pending


#: Modules (not channel adapters) that declare ENV_SECRETS: the credentials
#: they read from the environment.  Channel adapters are found through
#: FlaskChannelIntegration._ADAPTER_FACTORIES.  tests/unit/
#: test_env_secrets_declared.py fails when a module declares ENV_SECRETS and
#: is not listed here, and when a secret env read is declared nowhere.
_ENV_SECRET_MODULES = (
    'hartos.hartos_bootstrap',
    'integrations.agent_engine.erxes_client',
    'integrations.agent_engine.private_repo_access',
    'integrations.ap2.ap2_protocol',
    'integrations.audio.diarization_server',
    'integrations.audio.diarization_service',
    'integrations.channels.agent_tools',
    'integrations.channels.memory.simplemem_store',
    'integrations.channels.oauth_api',
    'integrations.commerce.commerce_api',
    'integrations.commerce.mcgroce_client',
    'integrations.service_tools.gh_pr_tool',
    'integrations.social.api_channels',
    'integrations.social.livekit_service',
    'integrations.social.livekit_supervisor',
)

def reads_from_env(name) -> bool:
    """True when this process legitimately reads ``name`` from its
    environment as a credential an owner may enter: an API key in
    security.secrets_manager.SECRET_KEYS (not a node secret), or a name some
    module declares in ENV_SECRETS (channel adapters, gh_pr_tool, ap2,
    livekit...).  Only those vault values are ever put in os.environ;
    anything else the owner entered stays in the vault, and a name declared
    ENV_NOT_FROM_VAULT, or a node secret (is_node_secret), never comes from
    it.

    Read from hartos.env_secrets_manifest.DELIVERABLE, a literal generated
    from the declarations (tests/unit/test_env_secrets_declared.py fails
    when it is stale), so the first call, on Nunba's boot path, imports no
    adapter.  Collecting by import took about 6 s there."""
    from hartos.env_secrets_manifest import DELIVERABLE
    return name in DELIVERABLE


def is_node_secret(name) -> bool:
    """True for the node's own secrets and connection strings
    (security.secrets_manager.NODE_SECRET_KEYS): only the node's vault
    preload sets them; the consent card, /api/credentials/submit,
    /api/vault/store and hold_credential never do, held or not."""
    from hartos.env_secrets_manifest import NODE_SECRETS
    return name in NODE_SECRETS


# ── Module-level singleton (HARTOS convention) ─────────────────────

_instance: Optional[AIKeyVault] = None
_lock = threading.Lock()


def get_ai_key_vault() -> AIKeyVault:
    """Module-level singleton accessor."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = AIKeyVault.get_instance()
    return _instance


# ── Asking the owner for a credential ──────────────────────────────

def _parse_resource_request(text) -> dict:
    """The tool's input: JSON {key_name, label, used_by, description,
    channel_type}, or plain text describing what is needed."""
    import json
    try:
        req = json.loads(text)
    except (ValueError, TypeError):
        req = None
    if isinstance(req, dict):
        return req
    text = str(text or '')
    return {'label': text[:100], 'description': text}


def _reopened(row) -> bool:
    """A row the owner took back with "Allow asking again"
    (ConsentService.reopen): reopened since its last revocation."""
    reopened = getattr(row, 'reopened_at', None)
    return (reopened is not None and row.revoked_at is not None
            and reopened >= row.revoked_at)


def _recent_entries(rows) -> int:
    """How many values the owner typed for this credential in the last
    CREDENTIAL_ENTRY_WINDOW_S (a rolling window): the card's Accept writes
    one granted 'credential' row per value (consent_api.grant_consent,
    append-only), so those rows are the count; no second counter is kept.
    Asks (rows never granted) are not entries, and entries the owner took
    back with "Allow asking again" (_reopened) no longer count."""
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(seconds=CREDENTIAL_ENTRY_WINDOW_S)
    return sum(1 for row in rows
               if row.granted_at is not None and row.granted_at >= since
               and not _reopened(row))


def request_credential(resource_description, agent_id=None, *,
                       tell_alias=True) -> str:
    """Get the agent a credential without the agent ever seeing it.  The
    agent tools (Request_Resource and core.agent_tools request_resource) come
    here through request_credential_for_agent; code that asks for its own
    fixed credential (post_to_channel) calls it directly.

    A stored credential is answered with its {{secret:NAME}} alias.  A
    missing one is asked for through ConsentService, like every other ask:
    one 'credential' row per credential (scope 'secret:NAME') for the owner
    of this computer, whose vault it goes into, and a consent.request the
    consent card shows with a password field.  Accept stores the value and
    grants the row.  The agent is told the alias either way; resolve_aliases
    puts the value in where a tool runs.

    ``tell_alias=False`` is for a tool that uses the value itself once the
    owner enters it (post_to_channel connects the channel with it): the
    agent is told that tool carries on, never the alias, which would invite
    it to pass the credential somewhere itself (review of f8fdb9c64).
    """
    req = _parse_resource_request(resource_description)
    label = str(req.get('label') or req.get('key_name') or 'a credential')[:100]
    used_by = str(req.get('used_by') or 'a tool')
    vault = get_ai_key_vault()
    name = vault.credential_name(req.get('key_name') or label,
                                 req.get('channel_type') or '')
    alias = '{{secret:' + name + '}}'
    if tell_alias:
        use = (f"use {alias} wherever the value is needed: it is filled in "
               f"only when a tool runs, and you never see it. Do not ask for "
               f"it in chat. If signing in with it fails, call this tool again "
               f'with the same key_name and "rejected": true, and the owner is '
               f'asked for it again.')
    else:
        use = (f"{used_by} uses it itself: you never see it and never need "
               f"it. Do not ask for it in chat.")
    # The site refused the stored value: never hand it back, ask again.
    rejected = req.get('rejected') is True

    stored_answer = f"'{label}' is stored on this computer. To use it, {use}"

    def _pending():
        # The pending list behind /api/credentials/pending.
        vault.add_pending_request(
            key_name=name, resource_type=req.get('resource_type') or 'api_key',
            label=label, description=str(req.get('description') or ''),
            used_by=used_by)

    # Whose vault the value goes into: this computer's owner, as for every
    # other ask of this machine (vlm.safety, capability_setup).
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner:
        # Only an owner-entered credential is "stored": answering for any
        # environment variable told the agent which ones exist.
        if vault.owner_credential(name) and not rejected:
            return stored_answer
        _pending()
        logger.warning("credential %s not asked: HEVOLVE_OWNER_USER_ID is not set",
                       name)
        return (f"Could not ask for '{label}': nobody is signed in on this "
                f"computer who could provide it.")

    reason = ' '.join(filter(None, [
        (f"{label} was rejected when {used_by} tried it; enter it again."
         if rejected else f"{label} is needed for {used_by}."),
        str(req.get('description') or '').strip(),
        (f"It is kept encrypted on this computer; the agent only sees {alias}."
         if tell_alias else
         "It is kept encrypted on this computer; the agent never sees it."),
    ]))
    try:
        from integrations.social.consent_service import (
            CREDENTIAL_SCOPE_PREFIX, ConsentService, known_agent_id)
        from integrations.social.models import db_session
        agent = known_agent_id(agent_id)
        scope = CREDENTIAL_SCOPE_PREFIX + name
        # Only reached with no usable value (missing, or rejected), so an
        # earlier Accept does not settle it: ask again unless the owner said
        # no.  request_consent keeps one row, so the card is still one card.
        held = exhausted = False
        entries = 0
        with db_session(commit=True) as db:
            # The owner's no wins: an agent told no is not handed the value
            # even when the owner entered it for another agent since.
            declined = ConsentService.declined(db, owner, 'credential',
                                               scope=scope, agent_id=agent)
            # Only an owner-entered credential is "stored": answering for
            # any environment variable told the agent which ones exist.
            stored = (not declined and not rejected
                      and bool(vault.owner_credential(name)))
            if declined or stored:
                return (f"The owner of this computer said no to providing "
                        f"'{label}'. They can choose \"Allow asking again\" "
                        f"in Privacy settings." if declined else stored_answer)
            rows = [r for r in ConsentService.list_consents(db, owner, 'credential')
                    if r.scope == scope]
            # A name this process already holds that the owner never entered
            # (PATH, HTTPS_PROXY, NUNBA_CI...) is a setting, not a credential:
            # a card for it would make the grant name it an owner credential
            # and {{secret:NAME}} would resolve to the system value.
            # A node secret is never asked for either, held or not.
            held = is_node_secret(name) or (
                bool(os.environ.get(name))
                and name not in vault._stored
                and not any(r.granted_at is not None for r in rows))
            # Asking again is bounded: every value the owner typed is a
            # grant row, so the rows say how often they tried.
            entries = 0 if held else _recent_entries(rows)
            exhausted = entries >= MAX_CREDENTIAL_ENTRIES
            if exhausted and ConsentService.active_grant(
                    db, owner, 'credential', scope) is not None:
                # The value the site refused MAX times stops resolving, and
                # the credential shows on the privacy page, where "Allow
                # asking again" (reopen) is the way back.
                ConsentService.revoke_consent(db, owner, 'credential', scope)
            if held:
                _pending()             # as a name nothing holds would be
            if not (held or exhausted):
                _pending()
                ConsentService.request_consent(db, owner, 'credential',
                                               scope=scope, agent_id=agent,
                                               reason=reason, reask=True)
    except Exception:
        logger.exception("credential %s could not be asked for", name)
        return (f"Could not ask for '{label}': the permission system is "
                f"unavailable.")

    if held:
        # Answered exactly as a name nothing holds is (below): a different
        # answer told the agent the variable exists (secrets review, the F1
        # oracle).  No card, and the alias never resolves to it.
        logger.warning("credential %s not asked: the process already holds it "
                       "and the owner never entered it", name)
    if exhausted:
        logger.info("credential %s: rejected after %d entries, not asked again",
                    name, entries)
        return (f"'{label}' was rejected every time: the owner entered "
                f"{entries} values for it in the last 24 hours and {used_by} "
                f"refused each one, so it is not asked for again until 24 "
                f"hours after the first of them. Stop trying to sign in and "
                f"tell the user that {label} keeps being rejected; when they "
                f"want to try again sooner, they choose \"Allow asking "
                f"again\" for it in Privacy settings.")
    return (f"Asked the owner of this computer for '{label}' on the consent "
            f"card. Once they enter it, {use}")


# ── An agent's ask, before it reaches a person ─────────────────────

#: The input an agent's credential tool takes, and what the tool is for: ONE
#: text, read by core.agent_tools request_resource and the Request_Resource
#: tool in hart_intelligence_entry, so both say the same.  It used to invite
#: "any configuration value", and on 2026-11-02 a background goal asked the
#: owner for 'Hive Network Configuration'.
REQUEST_RESOURCE_INPUT = (
    'JSON: {"resource_type": "api_key", "key_name": "OPENWEATHER_API_KEY", '
    '"label": "OpenWeather API key", '
    '"service": "OpenWeather (openweathermap.org)", "used_by": "the forecast step", '
    '"description": "what you will do with it, in plain words", '
    '"where_to_get": "where the person finds it, e.g. openweathermap.org, My API keys"}')
#: No braces in it: hart_intelligence_entry hands it to LangChain, whose chat
#: agent prompt is an f-string template, so a brace would be read as a slot
#: and every chat turn with tools would fail ("Missing some input keys",
#: live 2026-11-03).  The JSON example stays in REQUEST_RESOURCE_INPUT, which
#: only core.agent_tools reads, as its input's description.
REQUEST_RESOURCE_DESCRIPTION = (
    "Ask the person for a credential of an OUTSIDE service they have an account "
    "with (an API key, a bot token, a login for a website or app) when a step "
    "needs one and it is not stored yet. Say which service, what you will do with "
    "it and where they get it, so they can act on it. The value is kept encrypted "
    "on this computer and you only get an alias that tools fill in. Never ask for "
    "this computer's own settings, network or configuration: it already has them, "
    "and such an ask is not shown to anyone. Input: one JSON object with "
    "resource_type (api_key, token or channel_secret), key_name (for example "
    "OPENWEATHER_API_KEY), label, service, used_by, description (what you will do "
    "with it, in plain words) and where_to_get (where the person finds it).")

#: Seconds the node's own model gets to judge one ask.
_ASK_CHECK_TIMEOUT_S = 20
#: The model first names who gives the person this secret, then answers.
#: Measured 2026-11-03 on the desktop's own model (Qwen3.5-2B): asked for a
#: free-text "YES / NO: reason", it judged right but wrote "NO, because ...",
#: so no exact word ever matched; asked for a bare verdict, it refused 5 of 10
#: real outside-service asks (bot tokens, a Stripe key) as "system
#: credentials".  Naming the issuer first: 0 wrong of 51 asks (27 real, 21
#: made up, 3 sets) once the keys this computer reads skip the check.
#: This question alone decides a refusal.  Folding "is it the person's own
#: account" into it (6a1f2e46d, REJECTED on review 2026-11-03) made the
#: desktop's Qwen3.5-4B refuse 10 of 41 real asks a reviewer wrote (Datadog,
#: B2, S3, a Hugging Face download ...) with no card; this check refused 1.
#: A real ask with no company in it is refused too unless the question names
#: who else gives a secret.  Measured 2026-11-06 on the desktop's Qwen3.5-4B:
#: asked only for a company, it answered NONE, and the person never saw the ask,
#: for a home router or NAS password, an Outlook or SMTP app password and a
#: ZeroTier token (5 of 75 real asks on the sets it was tuned on; 10 of 26 on a
#: set another session wrote that it never saw, mostly the person's own devices
#: and logins with no service named).  Naming a site login and the person's own
#: devices as issuers: 1 of 75, and 6 of 26 on that set; made-up asks reaching a
#: card with no doubt note stayed 5 of 22 there.
_ASK_CHECK_PROMPT = (
    "A software agent on a person's computer wants to ask the person to type in a "
    "secret: an API key, token, password or similar.\n"
    "Step 1, issued_by: who gives the person this secret. Usually it is the outside "
    "company, website or app where the person has an account (for example Telegram, "
    "Stripe, AWS, Zoom, a shop, a ticket or food-delivery site, their email provider; "
    "a login or an app password for an account is issued by that site or provider). "
    "It can also be a device of the person's own other than this computer (their "
    "router, NAS, camera, printer or another computer). Write NONE only if the secret "
    "belongs to this computer itself or to the agent software (its own settings, "
    "network, configuration, storage, scheduler, model server, node identity, "
    "certificates or internal keys), which the system already has.\n"
    "Step 2, answer: YES if issued_by names a company, website, app or device; NO if "
    "issued_by is NONE.\nThe request:\n{request}\n")
#: The reply's exact shape, enforced by the server (llama.cpp makes it a
#: grammar; OpenAI-style APIs take it as structured output), so the answer is
#: one of two words and never free text to pattern-match.
_ASK_CHECK_SCHEMA = {
    'type': 'object',
    'properties': {'issued_by': {'type': 'string', 'maxLength': 80},
                   'answer': {'type': 'string', 'enum': ['YES', 'NO']}},
    'required': ['issued_by', 'answer'], 'additionalProperties': False}
#: The second question, put only to an ask the first one lets through: is the
#: stated use done on the person's account at the company it names?  It never
#: refuses; a NO puts ASK_DOUBT_NOTE on the card right after the line naming
#: the key, ahead of the agent's own words, so the person still decides.  The live case (2026-11-03 17:53:49 IST, desktop): an agent that
#: had googled "Hivenet API" five times asked for HIVENET_API_KEY "to monitor
#: idle nodes and onboard models via POST /api/models/onboard", this
#: computer's own work, and the first question passed it (Hivenet is real).
_ASK_USE_PROMPT = (
    "A software agent on a person's computer wants the person to type in a secret "
    "for an outside company, website or app.\n"
    "their_account_used: read what the secret will be used for. YES if that use "
    "is done at that company, on the person's account there (for example posting, "
    "paying, sending, storing or backing up files there, downloading from it, "
    "monitoring or alerting through it, or renting or using its service). NO if "
    "the use is only this computer's own work, with no outside account in it: "
    "monitoring, restarting, configuring or reading its own nodes, hive, peers, "
    "models, memory, GPU, scheduler or settings, or calling its own addresses "
    "such as /api/...\nThe request:\n{request}\n")
_ASK_USE_SCHEMA = {
    'type': 'object',
    'properties': {'their_account_used': {'type': 'string', 'enum': ['YES', 'NO']}},
    'required': ['their_account_used'], 'additionalProperties': False}
#: What the card says first when the second question answers NO.
ASK_DOUBT_NOTE = (
    "Check before you enter anything: what this is for sounds like work this "
    "computer does on its own, which needs no outside key. Enter it only if you "
    "use this service for this.")
_ASK_FIELDS = ('label', 'key_name', 'service', 'used_by', 'description',
               'where_to_get')


def _ask_model(req, prompt, schema, name) -> Optional[dict]:
    """The node's own model's reply to ``prompt`` about ask ``req``, held to
    ``schema`` by the server; None when it cannot be asked or the reply is not
    the asked-for object.  The one stateless JSON call is
    core.llm_json.ask_model_json (the model this node's turns run on, so it
    works where there is no local model too); this only writes the ask out."""
    from core.llm_json import ask_model_json
    request = '\n'.join(f'{key}: {str(req.get(key))[:300]}' for key in _ASK_FIELDS
                        if str(req.get(key) or '').strip())
    return ask_model_json(prompt.format(request=request), schema, name,
                          max_tokens=80, timeout=_ASK_CHECK_TIMEOUT_S)


def _ask_verdict(req) -> Optional[str]:
    """'yes' | 'no' | None: the node's own model's word on whether an outside
    company, website or app gives the person this secret.

    'no' only when both fields say so (issued_by NONE and answer NO); a NO that
    names a company contradicts itself and counts as no answer, as does any
    reply that is not the asked-for object, an error or a model that is down
    (None).  Refusing a real ask costs the person a working agent; asking a
    made-up one costs them a puzzling card, which is what happened before."""
    reply = _ask_model(req, _ASK_CHECK_PROMPT, _ASK_CHECK_SCHEMA, 'credential_ask_check')
    if reply is None:
        return None
    answer = reply.get('answer')
    issuer = str(reply.get('issued_by') or '').strip().upper()
    if answer == 'YES':
        return 'yes'
    if answer == 'NO' and issuer == 'NONE':
        return 'no'
    return None


def _ask_doubted(req) -> bool:
    """True when the node's own model says the stated use is NOT done on the
    person's account at the company it names (this computer's own work).
    False for YES, and for any failure or other reply: a doubt only adds
    ASK_DOUBT_NOTE to a card, so not knowing adds nothing."""
    reply = _ask_model(req, _ASK_USE_PROMPT, _ASK_USE_SCHEMA, 'credential_ask_use')
    return bool(reply) and reply.get('their_account_used') == 'NO'


def request_credential_for_agent(resource_description, agent_id=None) -> str:
    """What an agent's credential tool does (core.agent_tools request_resource
    and Request_Resource in hart_intelligence_entry): request_credential,
    unless no person could act on the ask.

    Owner, 2026-11-02: agents ask for credentials at runtime, while being
    created or in any task, but never for a made-up one "as if everyone else
    understands what it asks for".  Live that day: a background goal asked the
    owner for 'Hive Network Configuration' (hive_network_config, used_by
    network_discovery), names nothing in the code reads.

    A credential this computer's code reads (reads_from_env: API keys, channel
    tokens) or one the owner already stored goes straight on, with no model
    call.  Any other ask is judged by the node's own model (_ask_verdict): YES
    is asked, with the service and where to get it put on the card; NO (no
    outside company gives it out) goes back to the agent with no card; no
    answer (model down, or not the asked-for reply) is asked as before and
    logged.  An ask that goes to the person is put the second question
    (_ask_doubted): when its stated use is this computer's own work, as the
    17:53 IST HIVENET_API_KEY card of 2026-11-03 was, the card says so
    (ASK_DOUBT_NOTE) right after the line naming the key, ahead of the
    agent's own words, and the person decides; it is never refused on that
    alone.  Code that asks for its own fixed credential (post_to_channel, the
    trending-songs job) calls request_credential itself.
    """
    req = _parse_resource_request(resource_description)
    vault = get_ai_key_vault()
    label = str(req.get('label') or req.get('key_name') or 'a credential')[:100]
    # The name request_credential gives it (its own two lines), so a stored or
    # declared credential is recognised as the same one.
    name = vault.credential_name(req.get('key_name') or label,
                                 req.get('channel_type') or '')
    doubted = False
    if not (reads_from_env(name) or vault.owner_credential(name)):
        verdict = _ask_verdict(req)
        if verdict == 'no':
            logger.info("credential %s not asked: no outside company, website or "
                        "app gives it out", name)
            return (f"Did not ask the owner for '{label}': no outside company, "
                    f"website or app gives it out, so no person could find it. "
                    f"Ask a person only for a credential of an outside service "
                    f"they have an account with, and say which service, what you "
                    f"will do with it and where they get it. This computer's own "
                    f"settings, network and configuration need no secret from "
                    f"anyone.")
        if verdict is None:
            logger.warning("credential %s could not be checked (the model gave no "
                           "answer): asked as before", name)
        doubted = _ask_doubted(req)
        if doubted:
            logger.info("credential %s asked with a doubt: its stated use sounds "
                        "like this computer's own work", name)
    # The card shows what the person needs to act on it (request_credential
    # puts the description in the card's reason).
    extra = [f"Service: {req['service']}." if str(req.get('service') or '').strip()
             else '',
             f"Where to get it: {req['where_to_get']}."
             if str(req.get('where_to_get') or '').strip() else '']
    if any(extra) or doubted:
        req = dict(req, description=' '.join(filter(None, [
            ASK_DOUBT_NOTE if doubted else '',
            str(req.get('description') or '').strip()] + extra)))
    return request_credential(json.dumps(req), agent_id=agent_id)


# ── Localhost enforcement ──────────────────────────────────────────

def is_local_request() -> bool:
    """Does the current Flask request come from this machine?

    Credential endpoints MUST reject non-local requests: secrets never leave
    the user's device.  The one rule, core.auth_local._is_local_request
    (review of 291e548df, F3).  It used to be a copy that read remote_addr
    itself, trusted '0.0.0.0' (the bind-any sentinel, never a client
    address; integrations.agent_engine.shell_auth records why that was a
    privilege gap) and honoured NUNBA_CI on an installed build too; the one
    rule keeps the staging container's bypass (ci_trusts_every_caller) for
    builds run from source only.
    """
    from core.auth_local import _is_local_request
    return _is_local_request()
