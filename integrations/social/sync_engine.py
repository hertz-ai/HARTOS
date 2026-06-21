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
from dataclasses import dataclass
from typing import Dict, Optional, Callable

logger = logging.getLogger('hevolve_social')


@dataclass(frozen=True)
class SyncEntity:
    """One registry entry per syncable thing — the single source of truth for
    the unified sync layer (docs/architecture/UNIFIED_SYNC_ARCHITECTURE.md).

    Adding a syncable entity = ONE registration here, with zero new dispatch /
    producer / transport code.  ``apply`` is the idempotent upsert-by-id
    receiver; ``gate``/``serialize``/``topic`` are consumed by the producer
    (queue_entity) + real-time transport and are populated as each phase lands
    (P2+).  Op names are the existing wire ops (backward-compatible)."""
    op: str                       # wire op name (e.g. 'sync_post')
    apply: Callable               # (db, payload) -> str|None — idempotent receiver
    model: type = None
    gate: Callable = None         # (db, obj, demander) -> bool — consent/privacy
    serialize: Callable = None    # (db, obj) -> dict — provenance-stamped, no assets
    match: Callable = None        # (obj) -> bool — resolve an obj to this entity (producer)
    owner: Callable = None        # (obj) -> user_id — whose clients see the sync-status (P4); None = no emit
    topic: str = ''               # WAMP topic template for P2P + down-push
    p2p: bool = True
    central: bool = True


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
    def queue_entity(db, obj, demander: str = 'user') -> Optional[str]:
        """Single producer for the unified sync layer — the ONE entry point to
        replicate any entity UP to central
        (docs/architecture/UNIFIED_SYNC_ARCHITECTURE.md).  Resolves the entity
        from the registry (SyncEntity.match), runs its gate (privacy/consent),
        serializes (provenance-stamped, NO asset bytes), and queues it.  Reuses
        the canonical gate/serialize helpers — no per-caller branch, no second
        producer.  Best-effort: never blocks the caller's write.  Returns the
        queue id, or None (no match / gated / failed)."""
        try:
            ent = next((e for e in SYNC_ENTITIES.values()
                        if e.match and e.match(obj)), None)
            if ent is None or ent.gate is None:
                return None
            if not ent.gate(db, obj, demander):
                return None
            payload = ent.serialize(db, obj)
            qid = (SyncEngine.queue(db, 'central', ent.op, payload)
                   if ent.central else None)
            # P4 real-time: tell the owner's clients the entity synced — REUSE
            # the existing on_notification fan-out (chat.social WAMP + SSE), not
            # a new transport. Opt-in per entity (only those with an owner
            # extractor; high-frequency/internal entities stay silent).
            if qid and ent.owner:
                SyncEngine._emit_sync_status(ent.owner(obj), ent.op)
            return qid
        except Exception as e:
            logger.debug("SyncEngine.queue_entity: %s", e)
            return None

    @staticmethod
    def _emit_sync_status(owner_id, op, status: str = 'synced'):
        """P4: surface sync state to the owner's clients via the EXISTING
        on_notification channel (chat.social WAMP + SSE that RN/web/desktop
        already consume) — no new transport, no parallel fan-out path.
        Best-effort; never blocks the producer."""
        if not owner_id:
            return
        try:
            from .realtime import on_notification
            on_notification(str(owner_id), {'type': 'sync_status',
                                            'entity': op, 'sync_status': status})
        except Exception:
            pass

    @staticmethod
    def _signed_send_payload(node_id, batch) -> dict:
        """P4: the up-sync batch envelope, signed with the node's key so central
        can verify WHICH node sent it (closes the hierarchy_sync ingress IDOR).
        Signing is best-effort — an unsigned batch still applies outside 'hard'
        enforcement (the central-side _verify_sync_sender gate decides)."""
        payload = {'items': batch, 'node_id': node_id}
        try:
            from security.node_integrity import sign_json_payload
            payload['signature'] = sign_json_payload(payload)
        except Exception:
            pass
        return payload

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
        # P4: sign the batch so central can verify WHICH node sent it (closes the
        # unauthenticated hierarchy_sync ingress IDOR). Signed before any E2E wrap
        # so the signature rides inside the envelope.
        send_payload = SyncEngine._signed_send_payload(node_id, batch)
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
                # Single dispatch: the registry (SYNC_ENTITIES) + the few non-
                # entity ops collapsed into ONE op→handler map.  The if/elif
                # ladder is gone — adding an entity is a registration, not a
                # branch here (docs/architecture/UNIFIED_SYNC_ARCHITECTURE.md).
                handler = OP_DISPATCH.get(op)
                if handler is not None:
                    handler(db, payload)
                else:
                    logger.debug(f"Sync: unknown operation type: {op}")

                processed.append(item_id)
            except Exception as e:
                errors.append({'id': item_id, 'error': str(e)})

        return {'processed': processed, 'errors': errors}

    @staticmethod
    def _handle_sync_post(db, payload: dict):
        """Land a hierarchically-synced post as the durable central/regional
        backup — the CDN "origin" copy that survives when the source peer goes
        offline (#146).

        Reuses the SAME federation inbox persistence (FederatedPost + dedup by
        origin_node_id/origin_post_id + banned-node audit) that a peer-federated
        post uses — content has ONE durable store, reached either horizontally
        (peer → follower inbox) or vertically (child → parent sync). No parallel
        content table. The payload IS the federation 'new_post' message the
        producer queued; a non-'new_post' shape is a safe no-op (receive_inbox
        returns None). Best-effort; never raises out to the batch loop."""
        try:
            from integrations.social.federation import federation
            fid = federation.receive_inbox(db, payload)
            if fid:
                logger.info(f"Sync: landed federated post {fid} from child")
        except Exception as e:
            logger.warning(f"Sync: _handle_sync_post failed: {e}")

    @staticmethod
    def _handle_sync_agent(db, payload: dict):
        """Land a hierarchically-synced PUBLIC agent (gap #4) as a central/
        regional User row — the registry twin of _handle_sync_post (content).

        Mirrors _handle_sync_user's persist contract: upsert the User BY ID
        (preserves the central identity), create with the SAME field set
        UserService.register_agent builds (user_type='agent' + generate_api_token
        + is_verified), update only the sync-owned fields on an existing row
        (never overwrite the local api_token / secrets).  Optionally upserts
        skill badges from the envelope via the existing agent_bridge._sync_skills
        helper so the recipe/skill metadata lands too.  Best-effort — never
        raises out to the batch loop (receive_sync_batch's per-item try/except +
        idempotency guard the retry/dead-letter)."""
        from .models import User

        if payload.get('type') != 'agent':
            return  # defensive no-op for a non-agent shape (cf. _handle_sync_post)

        agent = payload.get('agent') or {}
        agent_pk = agent.get('id')
        username = agent.get('username', '')
        if not agent_pk or not username:
            logger.warning("sync_agent: missing agent id or username")
            return

        existing = db.query(User).filter_by(id=agent_pk).first()
        if existing:
            # Update only the sync-owned fields (NEVER the local api_token).
            for fld in ('display_name', 'bio', 'agent_id', 'handle',
                        'local_name', 'owner_id'):
                val = agent.get(fld)
                if val is not None:
                    setattr(existing, fld, val)
            logger.info(f"Sync: updated agent {agent_pk} from sync")
            row = existing
        else:
            # SECURITY: a hierarchically-synced agent is a DISCOVERABLE MIRROR,
            # never an authenticatable identity.  We do NOT mint an api_token
            # (the agent's credential lives only on its home node) and we do NOT
            # set is_verified — verification is a trusted local act, not a claim
            # an inbound sync payload may make.  Otherwise any writer to the sync
            # ingress could forge a verified, credentialed identity on the most-
            # trusted tier (review BLOCK: identity minting).
            row = User(
                id=agent_pk,
                username=username,
                display_name=agent.get('display_name', username),
                bio=agent.get('bio', ''),
                user_type='agent',
                agent_id=agent.get('agent_id'),
                owner_id=agent.get('owner_id'),
                handle=agent.get('handle'),
                local_name=agent.get('local_name'),
                api_token=None,        # never credential a synced mirror
                is_verified=False,     # never auto-verify from inbound sync
            )
            db.add(row)
            db.flush()
            logger.info(f"Sync: created agent {agent_pk} from sync")

        # Skill/recipe metadata: reuse the existing badge-upsert helper (no
        # parallel writer).  Best-effort — a skills hiccup must not undo the
        # agent upsert above.
        skills = payload.get('skills')
        if skills:
            try:
                from .agent_bridge import _sync_skills
                _sync_skills(db, row, skills)
            except Exception as e:
                logger.debug(f"Sync: agent skill upsert skipped for {agent_pk}: {e}")

    @staticmethod
    def _apply_synced_row(db, model, data: dict, fields: list, ts_field: str = None,
                          key_field: str = 'id'):
        """Generic idempotent upsert receiver for a simple synced entity — the
        reusable twin of _handle_sync_user's create-or-update, parameterised by
        model + sync-owned fields + the natural key (default 'id'; the resonance
        wallet keys by 'user_id').  Dedup is inherent (upsert by the key);
        optional LWW: when ts_field is given and the incoming row is OLDER than
        the local one, the stale write is skipped — so a node's newer state
        always wins (no data/earnings loss; central stays a backup, never
        authoritative).  Best-effort; returns the key or None."""
        row_key = (data or {}).get(key_field)
        if not row_key:
            return None
        row = db.query(model).filter_by(**{key_field: row_key}).first()
        if row is not None and ts_field:
            inc = data.get(ts_field)
            cur = getattr(row, ts_field, None)
            cur = cur.isoformat() if hasattr(cur, 'isoformat') else cur
            if inc and cur and str(inc) < str(cur):
                return row_key  # stale write loses (LWW) — keep the newer local row
        if row is None:
            row = model(**{key_field: row_key})
            db.add(row)
        for f in fields:
            if f in data and data[f] is not None:
                setattr(row, f, data[f])
        db.flush()
        return row_key

    @staticmethod
    def _handle_sync_community(db, payload: dict):
        """Land a synced community (P3) — public communities are a discoverable
        mirror, reusing the generic upsert-by-id apply.  payload is the
        _entity_message envelope; the row rides in payload['data']."""
        return SyncEngine._apply_synced_row(
            db, _community_model(), (payload or {}).get('data') or {},
            ['name', 'display_name', 'description', 'rules', 'icon_url',
             'banner_url', 'creator_id', 'is_default', 'is_private',
             'member_count', 'post_count'])

    @staticmethod
    def _handle_sync_membership(db, payload: dict):
        """Land a synced community membership (P3 join/leave) — generic upsert
        by id, reusing _apply_synced_row.  Public-community membership only
        (the producer gate enforces it)."""
        return SyncEngine._apply_synced_row(
            db, _membership_model(), (payload or {}).get('data') or {},
            ['user_id', 'community_id', 'role'])

    @staticmethod
    def _handle_sync_encounter(db, payload: dict):
        """Land a synced encounter (P3) — LOCATION-FREE (lat/lng/location never
        travel), generic upsert by id."""
        return SyncEngine._apply_synced_row(
            db, _encounter_model(), (payload or {}).get('data') or {},
            ['user_a_id', 'user_b_id', 'context_type', 'context_id',
             'encounter_count', 'bond_level', 'is_mutual_aware'])

    @staticmethod
    def _handle_sync_resonance(db, payload: dict):
        """Land a synced resonance wallet (P3) — central is a BACKUP, never
        authoritative; LWW by updated_at means the node's newer balance always
        wins (no earnings loss).  Keyed by user_id (the wallet's natural key)."""
        return SyncEngine._apply_synced_row(
            db, _resonance_model(), (payload or {}).get('data') or {},
            ['pulse', 'spark', 'spark_lifetime', 'signal', 'level', 'level_title',
             'xp', 'xp_next_level', 'streak_days', 'streak_best',
             'last_active_date', 'season_pulse', 'season_spark'],
            ts_field='updated_at', key_field='user_id')

    @staticmethod
    def _handle_sync_friendship(db, payload: dict):
        """Land a synced friendship (P3) — friendships is a raw-SQL table (no
        model), so a portable upsert by id: SELECT then INSERT/UPDATE (no
        dialect-specific ON CONFLICT — works on SQLite node + MySQL central).
        Best-effort; returns the id or None."""
        from sqlalchemy import text
        d = (payload or {}).get('data') or {}
        fid = d.get('id')
        if not fid:
            return None
        params = {'id': fid, 'a': d.get('user_a_id'), 'b': d.get('user_b_id'),
                  's': d.get('status'), 'i': d.get('initiator_id'),
                  'c': d.get('created_at'), 'ac': d.get('accepted_at')}
        exists = db.execute(text("SELECT id FROM friendships WHERE id = :id"),
                            {'id': fid}).fetchone()
        if exists:
            db.execute(text(
                "UPDATE friendships SET status=:s, accepted_at=:ac WHERE id=:id"),
                params)
        else:
            db.execute(text(
                "INSERT INTO friendships (id, user_a_id, user_b_id, status, "
                "initiator_id, created_at, accepted_at) VALUES "
                "(:id, :a, :b, :s, :i, :c, :ac)"), params)
        return fid

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

        # Map the local UUID → central account id (#90) so the FCM pull can
        # query the registry by the id it is keyed with (phone/account-number).
        # Central knows the mapping; it rides down in the user-sync payload.
        # Set inline on the row in THIS transaction — set_central_id's fresh
        # session couldn't see an uncommitted create.  No-op when omitted.
        central_id = (payload.get('central_user_id')
                      or payload.get('account_number') or payload.get('phone'))
        if central_id:
            _row = existing if existing else user
            try:
                from sqlalchemy.orm.attributes import flag_modified
                from core.fcm_sync import CENTRAL_ID_SETTINGS_KEY
                _settings = dict(getattr(_row, 'settings', None) or {})
                if _settings.get(CENTRAL_ID_SETTINGS_KEY) != str(central_id):
                    _settings[CENTRAL_ID_SETTINGS_KEY] = str(central_id)
                    _row.settings = _settings
                    flag_modified(_row, 'settings')
                    logger.info(
                        f"Sync: mapped user {user_id} → central {central_id}")
            except Exception as _e:
                logger.debug(
                    f"Sync: central-id map skipped for {user_id}: {_e}")

        # Cache the centrally-registered FCM token DOWN into the local store,
        # keyed by the SAME notification user_id (the UUID) the push path uses.
        # Central (Hevolve_Database) owns User.FCMtoken; delivering it in this
        # sync payload sidesteps the UUID<->account-number identity gap entirely
        # — the token arrives already mapped to the local UUID, so the push path
        # (send_fcm_push -> get_local_fcm_token(uuid)) resolves it without the
        # fragile HARTOS->central pull-by-UUID that always missed (#90). No-op
        # when the payload omits the token (older central nodes).
        fcm_token = payload.get('fcm_token') or payload.get('FCMtoken')
        if fcm_token:
            try:
                from core.fcm_sync import store_local_fcm_token
                if store_local_fcm_token(user_id, fcm_token):
                    logger.info(f"Sync: cached FCM token for user {user_id}")
            except Exception as _e:
                logger.debug(f"Sync: FCM token cache skipped for {user_id}: {_e}")

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

        target_url = SyncEngine.parent_tier_url()
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
    def parent_tier_url() -> str:
        """Canonical parent-tier node URL this node syncs UP to — central, else
        regional (empty on a flat/standalone node).  SINGLE resolver shared by
        the drain loop (_do_sync_drain) and federation's C4 content-retrieval
        fallback, so "where is my parent" has one source, not two."""
        return (os.environ.get('HEVOLVE_CENTRAL_URL', '')
                or os.environ.get('HEVOLVE_REGIONAL_URL', ''))

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


# ── Unified sync registry (the single source of truth) ───────────────────────
# Each syncable entity is ONE SyncEntity.  The receiver dispatch + (later phases)
# the producer + real-time transport all read from here — never an if/elif
# ladder or a second producer.  P1: the 3 existing entities; gate/serialize/
# topic land in P2+.  Op names are the existing wire ops (backward-compatible).
# Producer-side gate/serialize/match per entity — REUSE the canonical helpers
# (privacy.is_public, federation._outbox_message/_agent_message,
# ConsentService.check_consent), lazy-imported so the registry has no import-time
# cycle.  queue_entity drives off these; there is no second producer.
def _post_gate(db, obj, demander):
    from .privacy import is_public
    return is_public((obj or {}).get('privacy'))


def _post_serialize(db, obj):
    from .federation import federation
    return federation._outbox_message(obj)


def _agent_gate(db, obj, demander):
    from .consent_service import ConsentService
    return ConsentService.check_consent(
        db, getattr(obj, 'owner_id', None), 'public_exposure')


def _agent_serialize(db, obj):
    from .federation import federation
    return federation._agent_message(db, obj)


def _community_model():
    from .models import Community
    return Community


def _community_gate(db, obj, demander):
    # public communities replicate (discoverable); private stay local
    return not getattr(obj, 'is_private', False)


def _community_serialize(db, obj):
    from .federation import federation
    return federation._entity_message(db, 'community', obj.to_dict())


def _membership_model():
    from .models import CommunityMembership
    return CommunityMembership


def _membership_gate(db, obj, demander):
    # sync membership of PUBLIC communities only (private membership is private)
    from .models import Community
    c = db.query(Community).filter_by(
        id=getattr(obj, 'community_id', None)).first()
    return bool(c) and not c.is_private


def _membership_serialize(db, obj):
    from .federation import federation
    return federation._entity_message(db, 'community_membership', obj.to_dict())


def _encounter_model():
    from .models import Encounter
    return Encounter


# Encounters are PII (who met whom) AND carry precise location (lat/lng).
# Central replication is OFF unless the user opts in via cloud_egress consent,
# and the LOCATION fields (lat/lng/location_label) are NEVER synced.
_ENCOUNTER_SYNC_FIELDS = ['user_a_id', 'user_b_id', 'context_type', 'context_id',
                         'encounter_count', 'bond_level', 'is_mutual_aware']


def _encounter_gate(db, obj, demander):
    from .consent_service import ConsentService
    return ConsentService.check_consent(
        db, getattr(obj, 'user_a_id', None), 'cloud_egress', scope='social_sync')


def _encounter_serialize(db, obj):
    from .federation import federation
    d = obj.to_dict()
    data = {'id': d.get('id')}
    for f in _ENCOUNTER_SYNC_FIELDS:
        data[f] = d.get(f)          # location columns deliberately excluded
    return federation._entity_message(db, 'encounter', data)


def _resonance_model():
    from .models import ResonanceWallet
    return ResonanceWallet


def _resonance_gate(db, obj, demander):
    # the spark wallet is financial PII — central backup is OFF unless the user
    # opts in via cloud_egress consent (fail-closed)
    from .consent_service import ConsentService
    return ConsentService.check_consent(
        db, getattr(obj, 'user_id', None), 'cloud_egress', scope='social_sync')


def _resonance_serialize(db, obj):
    from .federation import federation
    data = obj.to_dict()            # keyed by user_id; balance fields
    data['updated_at'] = (obj.updated_at.isoformat()
                          if getattr(obj, 'updated_at', None) else None)
    return federation._entity_message(db, 'resonance', data)


def _friend_gate(db, obj, demander):
    # friendships are PII (the social graph) — central backup only with the
    # initiator's cloud_egress consent (fail-closed)
    from .consent_service import ConsentService
    return ConsentService.check_consent(
        db, (obj or {}).get('initiator_id'), 'cloud_egress', scope='social_sync')


def _friend_serialize(db, obj):
    # obj is already the friendship dict (raw-SQL row has no to_dict)
    from .federation import federation
    return federation._entity_message(db, 'friendship', obj)


SYNC_ENTITIES: Dict[str, SyncEntity] = {
    'sync_post': SyncEntity(
        op='sync_post', apply=SyncEngine._handle_sync_post,
        match=lambda o: isinstance(o, dict) and 'privacy' in o,
        gate=_post_gate, serialize=_post_serialize,
        owner=lambda o: o.get('author_id')),
    'register_agent': SyncEntity(
        op='register_agent', apply=SyncEngine._handle_sync_agent,
        match=lambda o: getattr(o, 'user_type', None) == 'agent',
        gate=_agent_gate, serialize=_agent_serialize),
    'sync_community': SyncEntity(
        op='sync_community', apply=SyncEngine._handle_sync_community,
        match=lambda o: getattr(o, '__tablename__', None) == 'communities',
        gate=_community_gate, serialize=_community_serialize,
        owner=lambda o: getattr(o, 'creator_id', None)),
    'sync_membership': SyncEntity(
        op='sync_membership', apply=SyncEngine._handle_sync_membership,
        match=lambda o: getattr(o, '__tablename__', None) == 'community_memberships',
        gate=_membership_gate, serialize=_membership_serialize),
    'sync_encounter': SyncEntity(
        op='sync_encounter', apply=SyncEngine._handle_sync_encounter,
        match=lambda o: getattr(o, '__tablename__', None) == 'encounters',
        gate=_encounter_gate, serialize=_encounter_serialize),
    'sync_resonance': SyncEntity(
        op='sync_resonance', apply=SyncEngine._handle_sync_resonance,
        match=lambda o: getattr(o, '__tablename__', None) == 'resonance_wallets',
        gate=_resonance_gate, serialize=_resonance_serialize),
    'sync_friendship': SyncEntity(
        op='sync_friendship', apply=SyncEngine._handle_sync_friendship,
        match=lambda o: isinstance(o, dict) and 'user_a_id' in o and 'status' in o,
        gate=_friend_gate, serialize=_friend_serialize,
        owner=lambda o: o.get('initiator_id')),
    'sync_user': SyncEntity(
        op='sync_user', apply=SyncEngine._handle_sync_user),
}

# The ONE op→handler dispatch the receiver uses: every entity's apply, PLUS the
# non-entity operations (token/blocklist mutations + log-only acks), all
# normalised to a (db, payload) callable so receive_sync_batch stays branchless.
OP_DISPATCH: Dict[str, Callable] = {op: e.apply for op, e in SYNC_ENTITIES.items()}
OP_DISPATCH.update({
    'revoke_token': lambda db, payload: SyncEngine._handle_revoke_token(payload),
    'sync_blocklist': lambda db, payload: SyncEngine._handle_sync_blocklist(payload),
    'update_stats': lambda db, payload: logger.info("Sync: received stats update from child"),
    'register_node': lambda db, payload: logger.info("Sync: received node registration from child"),
    'coding_task_assign': lambda db, payload: logger.info("Sync: received coding task assignment from parent"),
    'coding_submission': lambda db, payload: logger.info("Sync: received coding submission from child"),
})


# Module-level singleton
sync_engine = SyncEngine()
