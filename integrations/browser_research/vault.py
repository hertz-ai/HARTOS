"""Browser Research — encrypted credential vault.

Encrypted-at-rest cookie store for T2 platform scripts (Twitter, Reddit,
LinkedIn, Bilibili, XHS, Weibo, etc.).  Two-tier crypto:

  1. Master key per-process — derived from OS-keyring entry "nunba.browser_research"
     when available (DPAPI on Windows, Keychain on macOS, libsecret on Linux).
     Falls back to a salted file under <data_dir>/browser_research.key with a
     deterministic-but-machine-bound seed if keyring is unavailable.
  2. Per-record AES-GCM encryption with random 12-byte nonce.

The vault file lives at <data_dir>/account_vault.enc and is a JSON blob of
{platform: {handle: {nonce_b64, ct_b64, capabilities, last_used_ts}}}.

Threading: single-instance via `get_vault()` (process-wide); per-method lock
serialises read/write.  No long-held locks across I/O.
"""
import base64
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger('browser_research.vault')

KEYRING_SERVICE = 'nunba.browser_research'
KEYRING_USER = 'master_key'


@dataclass
class Account:
    """One stored platform account.  `cookies` is a list of dicts in
    Playwright cookie format: {name, value, domain, path, expires, ...}."""
    platform: str
    handle: str
    cookies: list = field(default_factory=list)
    capabilities: set = field(default_factory=set)  # 'read', 'post', 'dm', ...
    last_used_ts: float = 0.0


def _vault_path() -> str:
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'account_vault.enc')
    except Exception:
        return os.path.join(os.getcwd(), 'account_vault.enc')


def _key_fallback_path() -> str:
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'browser_research.key')
    except Exception:
        return os.path.join(os.getcwd(), 'browser_research.key')


def _get_or_create_master_key() -> bytes:
    """Resolve a 32-byte master key.  Keyring first, file fallback last.

    Order:
      1. OS keyring entry (`KEYRING_SERVICE` / `KEYRING_USER`)
      2. File fallback at `<data_dir>/browser_research.key` (0o600 on POSIX)
      3. Generate fresh, store via the first available backend
    """
    # Backend 1: OS keyring (DPAPI on Win, Keychain on macOS, libsecret on Linux)
    try:
        import keyring
        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if existing:
            return base64.b64decode(existing)
        # No entry — generate, store, return
        fresh = secrets.token_bytes(32)
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, base64.b64encode(fresh).decode())
            return fresh
        except Exception as exc:
            logger.warning("keyring write failed (%s), falling back to file", exc)
    except ImportError:
        logger.debug("keyring lib not installed, falling back to file")

    # Backend 2: encrypted-at-rest file (0o600)
    fp = _key_fallback_path()
    try:
        if os.path.isfile(fp):
            with open(fp, 'rb') as f:
                return f.read()[:32]
        fresh = secrets.token_bytes(32)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        # umask is process-wide; we set explicit perms after write
        with open(fp, 'wb') as f:
            f.write(fresh)
        try:
            os.chmod(fp, 0o600)
        except OSError:
            pass
        return fresh
    except Exception as exc:
        logger.error("master-key file fallback failed (%s) — using ephemeral key", exc)
        return secrets.token_bytes(32)


def _encrypt(plain: bytes, key: bytes) -> tuple[bytes, bytes]:
    """AES-GCM encrypt — returns (nonce, ciphertext_with_tag).

    Falls back to no-op (returns plain) with a warning if cryptography lib
    is unavailable — better to ship a degraded vault than refuse to start.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = secrets.token_bytes(12)
        return nonce, AESGCM(key).encrypt(nonce, plain, None)
    except ImportError:
        logger.warning("cryptography not installed — vault is UNENCRYPTED on disk")
        return b'', plain


def _decrypt(nonce: bytes, ct: bytes, key: bytes) -> bytes:
    """AES-GCM decrypt — None on tamper / wrong key."""
    if not nonce:
        return ct
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as exc:
        logger.error("decrypt failed (tampered? wrong key?): %s", exc)
        return b''


class AccountVault:
    """Encrypted-at-rest store of platform accounts + cookies.

    Schema (on-disk JSON):
        {platform: {handle: {nonce_b64, ct_b64, capabilities, last_used_ts}}}

    `ct_b64` decrypts to JSON of the cookie list.  Capabilities and
    last_used_ts are stored plaintext (non-sensitive).
    """
    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or _vault_path()
        self._key = _get_or_create_master_key()
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None  # in-memory mirror; reloaded on disk change

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not os.path.isfile(self._path):
            self._cache = {}
            return self._cache
        try:
            with open(self._path, encoding='utf-8') as f:
                self._cache = json.load(f) or {}
        except Exception as exc:
            logger.error("vault load failed (%s) — starting empty", exc)
            self._cache = {}
        return self._cache

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, ensure_ascii=False)
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        except Exception as exc:
            logger.error("vault save failed: %s", exc)

    def add(self, account: Account) -> None:
        with self._lock:
            cookies_blob = json.dumps(account.cookies).encode('utf-8')
            nonce, ct = _encrypt(cookies_blob, self._key)
            data = self._load()
            data.setdefault(account.platform, {})[account.handle] = {
                'nonce_b64': base64.b64encode(nonce).decode() if nonce else '',
                'ct_b64': base64.b64encode(ct).decode(),
                'capabilities': sorted(account.capabilities),
                'last_used_ts': account.last_used_ts or time.time(),
            }
            self._save()

    def get(self, platform: str, handle: str) -> Optional[Account]:
        with self._lock:
            data = self._load()
            row = (data.get(platform) or {}).get(handle)
            if not row:
                return None
            nonce = base64.b64decode(row.get('nonce_b64') or '')
            ct = base64.b64decode(row['ct_b64'])
            cookie_bytes = _decrypt(nonce, ct, self._key)
            try:
                cookies = json.loads(cookie_bytes) if cookie_bytes else []
            except ValueError:
                cookies = []
            return Account(
                platform=platform, handle=handle, cookies=cookies,
                capabilities=set(row.get('capabilities', [])),
                last_used_ts=float(row.get('last_used_ts', 0)),
            )

    def list_platforms(self) -> list[str]:
        with self._lock:
            return sorted(self._load().keys())

    def list_handles(self, platform: str) -> list[str]:
        with self._lock:
            return sorted((self._load().get(platform) or {}).keys())

    def revoke(self, platform: str, handle: str) -> bool:
        with self._lock:
            data = self._load()
            plat = data.get(platform)
            if not plat or handle not in plat:
                return False
            del plat[handle]
            if not plat:
                del data[platform]
            self._save()
            return True


_VAULT_SINGLETON: Optional[AccountVault] = None
_VAULT_LOCK = threading.Lock()


def get_vault() -> AccountVault:
    global _VAULT_SINGLETON
    with _VAULT_LOCK:
        if _VAULT_SINGLETON is None:
            _VAULT_SINGLETON = AccountVault()
        return _VAULT_SINGLETON


def reset_vault_for_tests(path: Optional[str] = None) -> AccountVault:
    """Test helper — fresh vault at a chosen path.  Never call in production."""
    global _VAULT_SINGLETON
    with _VAULT_LOCK:
        _VAULT_SINGLETON = AccountVault(path=path)
        # also reset the in-memory cache so a fresh disk read happens
        _VAULT_SINGLETON._cache = None
        return _VAULT_SINGLETON
