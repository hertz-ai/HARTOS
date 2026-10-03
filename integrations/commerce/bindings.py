"""
Commerce bindings -- which McGroce customer a HARTOS user acts as.

McGroce's REST API picks the acting customer from a ``customerId`` header
(RestApiCustomerStateFilter, which by its own Javadoc "DOES NOT provide any
security").  So the customer id must never come from a tool argument the
model can fill in: it comes from this table, which only the server-to-server
session exchange (POST /api/commerce/session, shared-secret gated) writes.

Single writer of ``commerce_bindings.json`` under
core.platform_paths.get_agent_data_dir().
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

TENANT = 'mcgroce'
_FILENAME = 'commerce_bindings.json'
_MERCHANT_ROLES = frozenset({'merchant', 'admin', 'vendor'})
_SAFE_ID = re.compile(r'^[A-Za-z0-9-]{1,64}$')

#: HARTOS identity prefix for a McGroce principal.  A HYPHEN, not an
#: underscore or colon: the id lands in file names (Windows) and in the
#: "{user_id}_{prompt_id}" session key, whose FIRST underscore separates the
#: user from the prompt.
IDENTITY_PREFIX = 'mcg-'


def hartos_user_id(customer_id, role: str = 'customer') -> str:
    """The HARTOS user_id a McGroce principal maps to.

    ``mcg-<id>`` for shoppers, ``mcg-m-<id>`` for merchants (admin users),
    whose ids come from a different McGroce table.  An id that is not already
    [A-Za-z0-9-] (an email, a dotted username) is hashed, so the result is
    always file-name and topic safe and stable for the same principal.
    """
    cid = str(customer_id if customer_id is not None else '').strip()
    if not cid:
        raise ValueError('a McGroce id is required')
    merchant = str(role).lower() in _MERCHANT_ROLES
    # A shopper id that itself starts with 'm-' would map onto the merchant
    # namespace ('mcg-m-5' for customer 'm-5' == merchant 5): hash it, as an
    # id that is not file-name safe already is.
    if not _SAFE_ID.match(cid) or (not merchant and cid.lower().startswith('m-')):
        cid = hashlib.sha256(cid.encode('utf-8')).hexdigest()[:32]
    return f"{IDENTITY_PREFIX}{'m-' if merchant else ''}{cid}"


def is_merchant(binding: Optional[dict]) -> bool:
    """A binding for a merchant (an admin-table id), not a shopper."""
    return bool(binding) and str(binding.get('role') or '').lower() in _MERCHANT_ROLES


class CommerceBindings:
    """Thread-safe store of user_id -> {customer_id, username, role,
    store_id, tenant}."""

    def __init__(self, path: Optional[str] = None):
        if path is None:
            from core.platform_paths import get_agent_data_dir
            path = os.path.join(get_agent_data_dir(), _FILENAME)
        self.path = path
        self._lock = threading.Lock()
        self._rows: Dict[str, dict] = {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self._rows = dict(json.load(f).get('bindings') or {})
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f'commerce bindings unreadable ({path}): {e}')

    def upsert(self, customer_id, username: str, role: str = 'customer',
               store_id=None) -> dict:
        user_id = hartos_user_id(customer_id, role)
        row = {
            'user_id': user_id,
            'customer_id': str(customer_id),
            'username': str(username or '')[:128],
            'role': str(role or 'customer').lower()[:32],
            'store_id': None if store_id in (None, '') else str(store_id),
            'tenant': TENANT,
            'updated_at': time.time(),
        }
        from core.file_cache import atomic_json_write
        with self._lock:
            self._rows[user_id] = row
            atomic_json_write(self.path, {'bindings': self._rows})
        return dict(row)

    def get(self, user_id) -> Optional[dict]:
        with self._lock:
            row = self._rows.get(str(user_id))
            return dict(row) if row else None

    def remove(self, user_id) -> bool:
        from core.file_cache import atomic_json_write
        with self._lock:
            if self._rows.pop(str(user_id), None) is None:
                return False
            atomic_json_write(self.path, {'bindings': self._rows})
            return True


_bindings: Optional[CommerceBindings] = None
_bindings_lock = threading.Lock()


def get_bindings() -> CommerceBindings:
    global _bindings
    if _bindings is None:
        with _bindings_lock:
            if _bindings is None:
                _bindings = CommerceBindings()
    return _bindings
