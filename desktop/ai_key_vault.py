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
  - store_credential() that persists + injects into os.environ
  - is_local_request() gate for credential endpoints

Usage:
    from desktop.ai_key_vault import AIKeyVault
    vault = AIKeyVault.get_instance()
    key = vault.get_tool_key('OPENAI_API_KEY')
    token = vault.get_channel_secret('discord', 'BOT_TOKEN')
"""

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_security')

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
        """Get a tool/API key. Delegates to SecretsManager."""
        return self._secrets_manager().get_secret(key_name)

    def get_channel_secret(self, channel_type: str, key_name: str) -> str:
        """Get a channel-specific secret.

        Resolves name then delegates to SecretsManager.
        """
        resolved = self._resolve_channel_key(channel_type, key_name)
        return self._secrets_manager().get_secret(resolved)

    # ── Storage ────────────────────────────────────────────────────

    def store_credential(self, key_name: str, value: str,
                         channel_type: str = '') -> str:
        """Store a credential in vault + inject into os.environ.

        Returns the resolved key name.
        """
        resolved = self._resolve_channel_key(channel_type, key_name) \
            if channel_type else key_name.upper()

        with self._lock:
            # Persist to encrypted vault
            try:
                self._secrets_manager().set_secret(resolved, value)
            except RuntimeError:
                # HEVOLVE_MASTER_KEY not set — env-only fallback
                logger.warning(
                    f"Vault unavailable, storing {resolved} in env only "
                    "(will not persist across restarts)"
                )

            # Inject into current process
            os.environ[resolved] = value

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

    # ── Boot Preload ───────────────────────────────────────────────

    def preload_env(self) -> int:
        """Load all vault secrets into os.environ.

        Called at boot BEFORE config_cache runs.
        Skips keys already present in os.environ.
        Returns count of keys loaded.
        """
        sm = self._secrets_manager()
        loaded = 0

        # Load from encrypted vault cache
        for key, value in sm._cache.items():
            if key not in os.environ and value:
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


# ── Localhost enforcement ──────────────────────────────────────────

# Addresses that are "this machine" — secrets never leave the device
_LOCAL_ADDRS = frozenset({
    '127.0.0.1', '::1', 'localhost', '0.0.0.0',
})


def is_local_request(remote_addr: str) -> bool:
    """Check if a request originates from the local device.

    Credential endpoints MUST reject non-local requests.
    Secrets never leave the user's device — no exceptions.
    """
    if not remote_addr:
        return False
    return remote_addr in _LOCAL_ADDRS
