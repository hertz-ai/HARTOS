"""
Commerce drafts -- merchant and SKU onboarding a person reviews first.

An agent never writes to the McGroce admin API directly.  It files a draft
(this store), shows it on a ``form`` / ``product_card`` fragment with an
``approval`` card, and only the draft's owner approving it through
POST /api/agent/approval (``merchant_onboard:<draft_id>`` /
``merchant_sku:<draft_id>``) submits it.

Single writer of ``commerce_drafts.json`` under get_agent_data_dir().
"""

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DRAFT_KINDS = ('merchant', 'sku')
DRAFT_TTL_S = 24 * 3600
_FILENAME = 'commerce_drafts.json'


class DraftStore:

    def __init__(self, path: Optional[str] = None):
        if path is None:
            from core.platform_paths import get_agent_data_dir
            path = os.path.join(get_agent_data_dir(), _FILENAME)
        self.path = path
        self._lock = threading.Lock()
        self._drafts: Dict[str, dict] = {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self._drafts = dict(json.load(f).get('drafts') or {})
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f'commerce drafts unreadable ({path}): {e}')

    def _save(self) -> None:
        from core.file_cache import atomic_json_write
        atomic_json_write(self.path, {'drafts': self._drafts})

    def create(self, user_id, kind: str, payload: Dict[str, Any]) -> dict:
        if kind not in DRAFT_KINDS:
            raise ValueError(f'unknown draft kind: {kind}')
        now = time.time()
        # lowercase hex: /api/agent/approval lower-cases the action it reads
        draft = {'draft_id': uuid.uuid4().hex, 'user_id': str(user_id),
                 'kind': kind, 'payload': payload, 'status': 'pending',
                 'created_at': now, 'expires_at': now + DRAFT_TTL_S,
                 'result': None}
        with self._lock:
            self._drafts[draft['draft_id']] = draft
            self._save()
        return dict(draft)

    def get(self, draft_id) -> Optional[dict]:
        with self._lock:
            d = self._drafts.get(str(draft_id))
            return json.loads(json.dumps(d)) if d else None

    def claim(self, draft_id, kind: str, approver_id) -> Tuple[Optional[dict], str]:
        """Move a pending draft to 'submitting' for its owner, once.

        Returns (draft, 'ok') or (None, reason).  The claim is what stops a
        double-tapped Approve from creating the merchant twice.
        """
        with self._lock:
            d = self._drafts.get(str(draft_id))
            if d is None or d['kind'] != kind:
                return None, 'draft not found'
            if d['user_id'] != str(approver_id):
                return None, 'only the person who asked can approve this'
            if d['status'] != 'pending':
                return None, f"draft is {d['status']}"
            if time.time() >= d['expires_at']:
                d['status'] = 'expired'
                self._save()
                return None, 'draft expired'
            d['status'] = 'submitting'
            self._save()
            return json.loads(json.dumps(d)), 'ok'

    def finish(self, draft_id, status: str, result: Any = None) -> None:
        with self._lock:
            d = self._drafts.get(str(draft_id))
            if d is None:
                return
            d['status'] = status
            d['result'] = result
            self._save()

    def reject(self, draft_id, kind: str, approver_id) -> Tuple[bool, str]:
        with self._lock:
            d = self._drafts.get(str(draft_id))
            if d is None or d['kind'] != kind:
                return False, 'draft not found'
            if d['user_id'] != str(approver_id):
                return False, 'only the person who asked can decline this'
            if d['status'] != 'pending':
                return False, f"draft is {d['status']}"
            d['status'] = 'rejected'
            self._save()
            return True, 'rejected'


_store: Optional[DraftStore] = None
_store_lock = threading.Lock()


def get_draft_store() -> DraftStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DraftStore()
    return _store
