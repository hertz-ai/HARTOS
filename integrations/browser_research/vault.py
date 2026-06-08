"""Browser Research — encrypted credential vault (C1 stub).

C1 ships the shape only — no actual cookies stored yet (no T2 platform ships
in C1).  C4 (Twitter) is the first commit that needs real encryption + cookie
persistence; the full DPAPI/keyring + AES-GCM implementation lands then.

Until then this module exists so the rest of the package can `from .vault
import AccountVault` without ImportError.  In-memory dict, no I/O.
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger('browser_research.vault')


@dataclass
class Account:
    """One stored platform account.  Cookies field deliberately omitted in C1."""
    platform: str
    handle: str
    capabilities: set[str] = field(default_factory=set)  # e.g. {'read', 'post'}
    last_used_ts: float = 0.0
    # cookies: bytes  ← lands in C4 with AES-GCM + OS-keyring master key


class AccountVault:
    """In-memory stub.  Replace with encrypted-at-rest store in C4.

    Single instance per process; reuses HARTOS canonical secrets module when it
    lands.  This stub matches the future signature so callers don't change.
    """
    def __init__(self) -> None:
        self._accounts: dict[tuple[str, str], Account] = {}
        self._lock = threading.Lock()

    def add(self, account: Account) -> None:
        with self._lock:
            self._accounts[(account.platform, account.handle)] = account

    def get(self, platform: str, handle: str) -> Optional[Account]:
        with self._lock:
            return self._accounts.get((platform, handle))

    def list_platforms(self) -> list[str]:
        with self._lock:
            return sorted({p for p, _ in self._accounts})

    def revoke(self, platform: str, handle: str) -> bool:
        with self._lock:
            return self._accounts.pop((platform, handle), None) is not None


# Single canonical instance — callers go through `get_vault()`, not their own.
_VAULT_SINGLETON: Optional[AccountVault] = None
_VAULT_LOCK = threading.Lock()


def get_vault() -> AccountVault:
    global _VAULT_SINGLETON
    with _VAULT_LOCK:
        if _VAULT_SINGLETON is None:
            _VAULT_SINGLETON = AccountVault()
        return _VAULT_SINGLETON
