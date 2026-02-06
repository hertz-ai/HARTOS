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

    @staticmethod
    def queue(db, target_tier: str, operation_type: str, payload: dict) -> str:
        """Queue a sync operation for later delivery."""
        from .models import SyncQueue
        from security.node_integrity import get_public_key_hex

        try:
            node_id = get_public_key_hex()[:16]
        except Exception:
            node_id = 'unknown'

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

        # Build batch payload
        batch = []
        for item in items:
            item.status = 'in_progress'
            item.last_attempt_at = datetime.utcnow()
            batch.append({
                'id': item.id,
                'operation_type': item.operation_type,
                'payload': item.payload_json,
            })

        db.flush()

        # Send batch
        sent = 0
        failed = 0
        try:
            resp = requests.post(
                f"{target_url}/api/social/hierarchy/sync",
                json={'items': batch, 'node_id': node_id},
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

        # Drop items that exceeded max retries
        for item in items:
            if item.status == 'failed' and (item.retry_count or 0) >= (item.max_retries or 5):
                item.status = 'failed'
                item.error_message = f'Max retries exceeded: {item.error_message}'

        db.flush()

        remaining = db.query(SyncQueue).filter(
            SyncQueue.node_id == node_id,
            SyncQueue.status.in_(['queued', 'failed']),
            SyncQueue.retry_count < SyncQueue.max_retries,
        ).count()

        return {'sent': sent, 'failed': failed, 'remaining': remaining}

    @staticmethod
    def receive_sync_batch(db, items: list) -> Dict:
        """Process incoming sync items from a child node."""
        processed = []
        errors = []

        for item in items:
            op = item.get('operation_type', '')
            payload = item.get('payload', {})
            item_id = item.get('id', '')

            try:
                if op == 'register_agent':
                    # Agent data sync — store as metadata for now
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
                else:
                    logger.debug(f"Sync: unknown operation type: {op}")

                processed.append(item_id)
            except Exception as e:
                errors.append({'id': item_id, 'error': str(e)})

        return {'processed': processed, 'errors': errors}

    @staticmethod
    def is_connected_to(target_url: str) -> bool:
        """Check if we can reach the target URL."""
        try:
            resp = requests.get(
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

    def _sync_loop(self):
        """Background loop: periodically drain sync queue."""
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            try:
                self._do_sync_drain()
            except Exception as e:
                logger.debug(f"Sync drain error: {e}")

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
