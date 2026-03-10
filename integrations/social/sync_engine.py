"""
HevolveSocial - Offline-First Sync Engine

Queues operations locally when offline, drains to regional/central when connected.
Used by regional (sync to central) and local (sync to regional) tiers.
"""
import os
import time
import logging
import threading
import requests
from datetime import datetime

from core.http_pool import pooled_get, pooled_post
from typing import Dict, Optional

logger = logging.getLogger('hevolve_social')


class SyncEngine:
    """Offline-first sync engine for regional/local tiers."""

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_SYNC_INTERVAL', '60'))
        self._batch_size = int(os.environ.get('HEVOLVE_MAX_SYNC_BATCH', '50'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

    MAX_QUEUE_SIZE = 10000

    @staticmethod
    def queue(db, target_tier: str, operation_type: str, payload: dict) -> Optional[str]:
        """Queue a sync operation for later delivery."""
        from .models import SyncQueue
        from security.node_integrity import get_public_key_hex

        try:
            node_id = get_public_key_hex()[:16]
        except Exception:
            node_id = 'unknown'

        # Backpressure: reject if queue is too large for this node
        current_count = db.query(SyncQueue).filter(
            SyncQueue.node_id == node_id,
            SyncQueue.status.in_(['queued', 'failed']),
        ).count()
        if current_count >= SyncEngine.MAX_QUEUE_SIZE:
            logger.warning(f"Sync queue backpressure: {current_count} items for node {node_id}, skipping insertion")
            return None

        item = SyncQueue(
            node_id=node_id,
            target_tier=target_tier,
            operation_type=operation_type,
            payload_json=payload,
            status='queued',
        )
        db.add(item)
        db.flush()
        return item.id

    @staticmethod
    def drain_queue(db, node_id: str, target_url: str, batch_size: int = 50) -> Dict:
        """Send queued operations to target. Returns counts."""
        from .models import SyncQueue

        items = db.query(SyncQueue).filter(
            SyncQueue.node_id == node_id,
            SyncQueue.status.in_(['queued', 'failed']),
        ).order_by(SyncQueue.created_at).limit(batch_size).all()

        if not items:
            return {'sent': 0, 'failed': 0, 'remaining': 0}

        # Optimistic locking: atomically update status to 'in_progress' only for items still 'queued'/'failed'
        item_ids = [item.id for item in items]
        updated_count = db.query(SyncQueue).filter(
            SyncQueue.id.in_(item_ids),
            SyncQueue.status.in_(['queued', 'failed']),
        ).update({'status': 'in_progress', 'last_attempt_at': datetime.utcnow()}, synchronize_session='fetch')
        db.flush()

        if updated_count == 0:
            return {'sent': 0, 'failed': 0, 'remaining': 0}

        # Re-fetch only items that were successfully claimed
        items = db.query(SyncQueue).filter(
            SyncQueue.id.in_(item_ids),
            SyncQueue.status == 'in_progress',
        ).all()

        if not items:
            return {'sent': 0, 'failed': 0, 'remaining': 0}

        # Build batch payload
        batch = []
        for item in items:
            batch.append({
                'id': item.id,
                'operation_type': item.operation_type,
                'payload': item.payload_json,
            })

        # Send batch — E2E encrypt if target has X25519 key
        sent = 0
        failed = 0
        send_payload = {'items': batch, 'node_id': node_id}
        try:
            target_x25519 = SyncEngine._get_target_x25519(db, target_url)
            if target_x25519:
                try:
                    from security.channel_encryption import encrypt_json_for_peer
                    send_payload = {'encrypted': True,
                                    'envelope': encrypt_json_for_peer(send_payload, target_x25519)}
                except Exception:
                    pass  # Encryption unavailable, send plaintext
        except Exception:
            pass
        try:
            resp = pooled_post(
                f"{target_url}/api/social/hierarchy/sync",
                json=send_payload,
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                processed_ids = set(result.get('processed', []))
                for item in items:
                    if item.id in processed_ids:
                        item.status = 'completed'
                        item.completed_at = datetime.utcnow()
                        sent += 1
                    else:
                        item.status = 'failed'
                        item.retry_count = (item.retry_count or 0) + 1
                        item.error_message = 'Not in processed list'
                        failed += 1
            else:
                for item in items:
                    item.status = 'failed'
                    item.retry_count = (item.retry_count or 0) + 1
                    item.error_message = f'HTTP {resp.status_code}'
                    failed += 1
        except requests.RequestException as e:
            for item in items:
                item.status = 'failed'
                item.retry_count = (item.retry_count or 0) + 1
                item.error_message = str(e)
                failed += 1

        # Mark items that exceeded max retries as dead (stop retrying)
        for item in items:
            if item.status == 'failed' and (item.retry_count or 0) >= (item.max_retries or 5):
                item.status = 'dead'
                item.error_message = f'Max retries exceeded: {item.error_message}'

        db.flush()

        remaining = db.query(SyncQueue).filter(
            SyncQueue.node_id == node_id,
            SyncQueue.status.in_(['queued', 'failed']),
            SyncQueue.retry_count < SyncQueue.max_retries,
        ).count()

        return {'sent': sent, 'failed': failed, 'remaining': remaining}

    @staticmethod
    def _get_target_x25519(db, target_url: str) -> str:
        """Look up X25519 public key for a target node by URL."""
        try:
            from .models import PeerNode
            peer = db.query(PeerNode).filter(
                PeerNode.url == target_url.rstrip('/'),
                PeerNode.status == 'active',
            ).first()
            if peer and getattr(peer, 'x25519_public', None):
                return peer.x25519_public
        except Exception:
            pass
        return ''

    @staticmethod
    def receive_sync_batch(db, items: list) -> Dict:
        """Process incoming sync items from a child node."""
        processed = []
        errors = []

        for item in items:
            op = item.get('operation_type', '')
            payload = item.get('payload', {})
            item_id = item.get('id', '')

            # Idempotency: skip already-processed items
            if item_id and db:
                from .models import SyncQueue
                existing = db.query(SyncQueue).filter_by(id=item_id).first()
                if existing and existing.status in ('completed', 'dead'):
                    processed.append(item_id)
                    continue

            try:
                if op == 'register_agent':
                    # Agent data sync - store as metadata for now
                    logger.info(f"Sync: received agent registration from child")
                elif op == 'sync_post':
                    logger.info(f"Sync: received post sync from child")
                elif op == 'update_stats':
                    logger.info(f"Sync: received stats update from child")
                elif op == 'register_node':
                    logger.info(f"Sync: received node registration from child")
                elif op == 'coding_task_assign':
                    logger.info(f"Sync: received coding task assignment from parent")
                elif op == 'coding_submission':
                    logger.info(f"Sync: received coding submission from child")
                elif op == 'sync_user':
                    SyncEngine._handle_sync_user(db, payload)
                elif op == 'revoke_token':
                    SyncEngine._handle_revoke_token(payload)
                elif op == 'sync_blocklist':
                    SyncEngine._handle_sync_blocklist(payload)
                else:
                    logger.debug(f"Sync: unknown operation type: {op}")

                processed.append(item_id)
            except Exception as e:
                errors.append({'id': item_id, 'error': str(e)})

        return {'processed': processed, 'errors': errors}

    @staticmethod
    def _handle_sync_user(db, payload: dict):
        """Create or update a User record from sync data."""
        from .models import User

        user_id = payload.get('user_id')
        username = payload.get('username', '')
        if not user_id or not username:
            logger.warning("sync_user: missing user_id or username")
            return

        existing = db.query(User).filter_by(id=user_id).first()
        if existing:
            # Update fields from sync (don't overwrite local-only fields)
            if payload.get('handle'):
                existing.handle = payload['handle']
            if payload.get('display_name'):
                existing.display_name = payload['display_name']
            if payload.get('role'):
                existing.role = payload['role']
            logger.info(f"Sync: updated user {user_id} from sync")
        else:
            # Create new user record from sync
            from .auth import generate_api_token
            user = User(
                id=user_id,
                username=username,
                display_name=payload.get('display_name', username),
                handle=payload.get('handle', ''),
                role=payload.get('role', 'flat'),
                user_type=payload.get('user_type', 'human'),
                api_token=generate_api_token(),
            )
            db.add(user)
            logger.info(f"Sync: created user {user_id} from sync")

    @staticmethod
    def _handle_revoke_token(payload: dict):
        """Add a JTI to the local token blocklist."""
        jti = payload.get('jti', '')
        if not jti:
            logger.warning("revoke_token sync: missing jti")
            return
        try:
            from security.jwt_manager import _blocklist, ACCESS_TOKEN_EXPIRY
            expires_in = payload.get('expires_in', ACCESS_TOKEN_EXPIRY)
            _blocklist.add(jti, expires_in)
            logger.info(f"Sync: revoked token jti={jti}")
        except Exception as e:
            logger.warning(f"Sync: failed to revoke token: {e}")

    @staticmethod
    def _handle_sync_blocklist(payload: dict):
        """Bulk sync of blocked JTIs."""
        jtis = payload.get('jtis', [])
        if not jtis:
            return
        try:
            from security.jwt_manager import _blocklist, ACCESS_TOKEN_EXPIRY
            expires_in = payload.get('expires_in', ACCESS_TOKEN_EXPIRY)
            for jti in jtis:
                _blocklist.add(jti, expires_in)
            logger.info(f"Sync: bulk-revoked {len(jtis)} tokens")
        except Exception as e:
            logger.warning(f"Sync: failed to sync blocklist: {e}")

    @staticmethod
    def queue_user_sync(db, user_data: dict, direction: str = 'up'):
        """Queue a user creation/update for sync.

        Args:
            db: Database session
            user_data: Dict with user_id, username, handle, role, etc.
            direction: 'up' (to central) or 'down' (from central to nodes)
        """
        target = 'central' if direction == 'up' else 'regional'
        return SyncEngine.queue(db, target, 'sync_user', user_data)

    @staticmethod
    def is_connected_to(target_url: str) -> bool:
        """Check if we can reach the target URL."""
        try:
            resp = pooled_get(
                f"{target_url}/api/social/peers/health",
                timeout=5,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def start_background_sync(self):
        """Start background sync drain thread (daemon)."""
        with self._lock:
            if self._running:
                return
            self._running = True

        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(f"Sync engine started (interval={self._interval}s)")

    def stop_background_sync(self):
        """Stop the background sync thread."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking operations."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('sync_engine')
        except Exception:
            pass

    def _sync_loop(self):
        """Background loop: periodically drain sync queue."""
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            self._wd_heartbeat()
            try:
                self._do_sync_drain()
            except Exception as e:
                logger.debug(f"Sync drain error: {e}")
            self._wd_heartbeat()

    def _do_sync_drain(self):
        """Attempt to drain queued items to target."""
        from .models import get_db

        target_url = os.environ.get('HEVOLVE_CENTRAL_URL', '') or \
                     os.environ.get('HEVOLVE_REGIONAL_URL', '')
        if not target_url:
            return

        if not self.is_connected_to(target_url):
            return

        db = get_db()
        try:
            from security.node_integrity import get_public_key_hex
            node_id = get_public_key_hex()[:16]
        except Exception:
            node_id = 'unknown'

        try:
            result = self.drain_queue(db, node_id, target_url, self._batch_size)
            if result['sent'] > 0:
                logger.info(f"Sync: drained {result['sent']} items to {target_url}")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Sync drain error: {e}")
        finally:
            db.close()

    @staticmethod
    def get_queue_stats(db, node_id: str) -> Dict:
        """Get sync queue statistics for a node."""
        from .models import SyncQueue

        queued = db.query(SyncQueue).filter_by(
            node_id=node_id, status='queued').count()
        in_progress = db.query(SyncQueue).filter_by(
            node_id=node_id, status='in_progress').count()
        completed = db.query(SyncQueue).filter_by(
            node_id=node_id, status='completed').count()
        failed = db.query(SyncQueue).filter_by(
            node_id=node_id, status='failed').count()

        return {
            'queued': queued,
            'in_progress': in_progress,
            'completed': completed,
            'failed': failed,
            'total_pending': queued + in_progress,
        }


# Module-level singleton
sync_engine = SyncEngine()
