"""
HevolveSocial - Mastodon-Style Federation
Instances follow each other and share content across the federated network.
Built on top of gossip peer discovery - gossip finds peers, federation shares content.

Concepts:
- Instance follow: Node A follows Node B → B pushes new posts to A's inbox
- Outbox: When a local post is created, push to all followers' inboxes
- Inbox: Receive posts from followed instances, store as federated posts
- Boost: Re-share a federated post to local feed
"""
import logging
import threading
import requests
from datetime import datetime

from core.http_pool import pooled_get, pooled_post
from typing import Optional, List
from core.port_registry import get_port

logger = logging.getLogger('hevolve_social')


class FederationManager:
    """Manages instance-level follows and content federation between HevolveBot nodes."""

    def __init__(self):
        self._lock = threading.Lock()

    # ─── Instance Follow/Unfollow ───

    def record_follow(self, db, follower_node_id: str,
                      following_node_id: str, peer_url: str) -> bool:
        """Record-only follow write — the ONE InstanceFollow writer.

        Idempotent; returns True when a new row was created.  Split out of
        follow_instance (SRP) because the two sides of a follow need
        different halves: the ACTIVE side (we decide to follow someone)
        records AND notifies them; the PASSIVE side (their notification
        arrives) must record WITHOUT notifying — reusing follow_instance
        there would fire a wrong-direction notification back at the
        follower.  ``peer_url`` is the OTHER node's URL from the row
        consumer's perspective: get_followers/push_to_followers deliver to
        it, pull_from_peer fetches from it.
        """
        from .models import InstanceFollow
        existing = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == follower_node_id,
            InstanceFollow.following_node_id == following_node_id,
        ).first()
        if existing:
            return False
        db.add(InstanceFollow(
            follower_node_id=follower_node_id,
            following_node_id=following_node_id,
            peer_url=peer_url,
            status='active',
        ))
        db.flush()
        return True

    def follow_instance(self, db, local_node_id: str, peer_node_id: str,
                        peer_url: str) -> bool:
        """
        Follow a remote instance. Sends follow request to the peer.
        Returns True if follow was created.
        """
        if not self.record_follow(db, local_node_id, peer_node_id, peer_url):
            return False

        # Notify the remote instance
        threading.Thread(
            target=self._send_follow_notification,
            args=(peer_url, local_node_id, self._get_local_url()),
            daemon=True,
        ).start()

        return True

    def unfollow_instance(self, db, local_node_id: str, peer_node_id: str):
        """Unfollow a remote instance."""
        from .models import InstanceFollow
        follow = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == local_node_id,
            InstanceFollow.following_node_id == peer_node_id,
        ).first()
        if follow:
            db.delete(follow)
            db.flush()

    def get_followers(self, db, node_id: str) -> list:
        """Get list of instances following this node."""
        from .models import InstanceFollow
        follows = db.query(InstanceFollow).filter(
            InstanceFollow.following_node_id == node_id,
            InstanceFollow.status == 'active',
        ).all()
        return [f.to_dict() for f in follows]

    def get_following(self, db, node_id: str) -> list:
        """Get list of instances this node follows."""
        from .models import InstanceFollow
        follows = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == node_id,
            InstanceFollow.status == 'active',
        ).all()
        return [f.to_dict() for f in follows]

    # ─── Outbox: Push local posts to followers ───

    def push_to_followers(self, db, post_dict: dict):
        """
        Push a new local post to all instances that follow us.
        Called when a post is created locally.
        """
        # Privacy gate (#47): only PUBLIC posts may leave this node, even if/when
        # federation is re-enabled.  Uses the canonical privacy.is_public (the
        # single NULL/unknown-means-public source) so federation eligibility
        # always matches local visibility — friends/community/private never
        # federate.
        from .privacy import is_public
        if not is_public((post_dict or {}).get('privacy')):
            logger.debug(
                "Federation: not federating non-public post %s (privacy=%r)",
                (post_dict or {}).get('id'), (post_dict or {}).get('privacy'))
            return

        from .peer_discovery import gossip
        followers = self.get_followers(db, gossip.node_id)
        if not followers:
            return

        payload = self._outbox_message(post_dict)

        for follower in followers:
            threading.Thread(
                target=self._deliver_to_inbox,
                args=(follower['peer_url'], payload,
                      follower.get('follower_node_id', '')),
                daemon=True,
            ).start()

    def _deliver_to_inbox(self, peer_url: str, payload: dict,
                           follower_node_id: str = ''):
        """Deliver to peer's inbox — PeerLink first, HTTP fallback."""
        # Try PeerLink direct delivery (avoids HTTP round-trip)
        if follower_node_id:
            try:
                from core.peer_link.link_manager import get_link_manager
                link = get_link_manager().get_link(follower_node_id)
                if link:
                    link.send('federation', payload)
                    logger.debug(f"Federation: delivered via PeerLink to {follower_node_id[:8]}")
                    return
            except Exception:
                pass

        # HTTP fallback
        try:
            resp = pooled_post(
                f"{peer_url}/api/social/federation/inbox",
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.debug(f"Federation: delivered to {peer_url}")
            else:
                logger.debug(f"Federation: delivery failed to {peer_url}: {resp.status_code}")
        except requests.RequestException as e:
            logger.debug(f"Federation: delivery error to {peer_url}: {e}")

    def _outbox_message(self, post_dict: dict) -> dict:
        """Canonical 'new_post' federation message for a local post — consumed
        by BOTH the horizontal follower inbox (push_to_followers) and the
        vertical parent sync (sync_to_parent).  One shape, one builder."""
        from .peer_discovery import gossip
        return {
            'type': 'new_post',
            'origin_node_id': gossip.node_id,
            'origin_url': gossip.base_url,
            'origin_name': gossip.node_name,
            'post': post_dict,
            'timestamp': datetime.utcnow().isoformat(),
        }

    def sync_to_parent(self, db, post_dict: dict) -> Optional[str]:
        """Queue a PUBLIC local post UP the tier hierarchy to central — the
        durable CDN "origin" backup (#147/C2).  Vertical counterpart to
        push_to_followers (horizontal): the SAME privacy gate (is_public) and
        the SAME 'new_post' message (_outbox_message); it lands at central via
        SyncEngine → receive_sync_batch('sync_post') → receive_inbox (#146/C1).
        Only public/consented content rises; friends/community/private stays
        local.  Best-effort — a sync hiccup never blocks the post.  Returns the
        queue id or None."""
        # Thin shim over the ONE unified producer (SyncEngine.queue_entity),
        # which resolves the 'sync_post' entity and runs the SAME is_public gate
        # + _outbox_message serialize + queue.  No second producer path.
        from .sync_engine import SyncEngine
        return SyncEngine.queue_entity(db, post_dict)

    def _agent_message(self, db, user) -> dict:
        """Canonical 'register_agent' envelope for a local agent User — the
        agent twin of _outbox_message.  ONE builder, same gossip origin fields
        (origin_node_id/url/name) as the post envelope, so the agent up-sync
        has a single shape that the central receiver (_handle_sync_agent)
        mirrors back.  Carries the agent profile (User.to_dict) + a skill/recipe
        summary so the central registry entry + skill badges land too."""
        from .peer_discovery import gossip
        return {
            'type': 'agent',
            'origin_node_id': gossip.node_id,
            'origin_url': gossip.base_url,
            'origin_name': gossip.node_name,
            # to_dict() omits owner_id (it's not a public profile field); add it
            # explicitly so the receiver can attribute the synced agent to its
            # human owner.  Without this the round-trip silently drops owner_id
            # and the central mirror is ownerless (review HIGH: owner_id loss).
            'agent': {**user.to_dict(), 'owner_id': getattr(user, 'owner_id', None)},
            'skills': self._agent_skill_summary(db, user),
            'timestamp': datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _agent_skill_summary(db, user) -> list:
        """Skill-badge summary for an agent (metadata only — not the full
        recipe files).  Mirrors what _handle_sync_user replicates for users
        (profile, not full history); the receiver upserts these via the same
        agent_bridge._sync_skills helper.  Best-effort: a query hiccup yields
        an empty list rather than blocking the up-sync."""
        try:
            from .models import AgentSkillBadge
            badges = db.query(AgentSkillBadge).filter(
                AgentSkillBadge.user_id == user.id).all()
            return [{
                'name': b.skill_name,
                'proficiency': b.proficiency,
                'usage_count': b.usage_count,
                'success_rate': b.success_rate,
            } for b in badges]
        except Exception as e:
            logger.debug("Federation._agent_skill_summary: skipped: %s", e)
            return []

    def _entity_message(self, db, kind: str, data: dict) -> dict:
        """Generic provenance-stamped envelope for a synced entity (P3+) — the
        unified twin of _outbox_message/_agent_message.  ONE shape: the gossip
        origin fields + the (already field-selected) row dict under 'data'.  The
        per-entity serialize builds `data` from to_dict(), optionally FILTERED to
        drop sensitive columns (e.g. encounter lat/lng).  Receiver upserts
        payload['data'] by id."""
        from .peer_discovery import gossip
        return {
            'type': kind,
            'origin_node_id': gossip.node_id,
            'origin_url': gossip.base_url,
            'origin_name': gossip.node_name,
            'data': data,
            'timestamp': datetime.utcnow().isoformat(),
        }

    def sync_agent_to_parent(self, db, user) -> Optional[str]:
        """Queue a PUBLIC local agent UP the tier hierarchy to central — the
        agent twin of sync_to_parent (gap #4).  Same gate→build→queue control
        flow, one axis over (agents instead of posts):

          - returns None unless this User is an agent (user_type=='agent');
          - PUBLIC gate = owner CONSENT, not a privacy column (agents have
            none): ConsentService.check_consent(db, owner_id, 'public_exposure')
            — the canonical "content made public" signal, the SAME gate the
            autonomous-marketing funnel uses.  Ownerless system/hive agents
            (owner_id is None) fail the consent lookup and are correctly NOT
            replicated as public user content;
          - builds the canonical _agent_message envelope and queues it to
            central as the already-declared 'register_agent' op.

        Best-effort — a sync hiccup never blocks agent creation.  Returns the
        queue id or None."""
        # Thin shim over the ONE unified producer (SyncEngine.queue_entity),
        # which resolves the 'register_agent' entity and runs the SAME
        # user_type=='agent' + owner public_exposure consent gate + _agent_message
        # serialize + queue.  Non-agent / ownerless / unconsented no-op.
        from .sync_engine import SyncEngine
        return SyncEngine.queue_entity(db, user)

    # ─── Inbox: Receive posts from followed instances ───

    def receive_inbox(self, db, payload: dict) -> Optional[str]:
        """
        Process an incoming federated post.
        Deduplicates by origin_node_id + post.id.
        Verifies sender's guardrail hash before accepting - continuous audit
        applies to every interaction, not just periodic checks.
        Returns the FederatedPost id if created, None if duplicate.
        """
        from .models import FederatedPost, PeerNode
        from .peer_discovery import gossip

        msg_type = payload.get('type')
        if msg_type != 'new_post':
            return None

        post_data = payload.get('post', {})
        origin_node = payload.get('origin_node_id', '')
        origin_post_id = post_data.get('id', '')

        # Continuous audit: verify sender is still a valid peer with matching values
        if origin_node:
            peer = db.query(PeerNode).filter_by(node_id=origin_node).first()
            if peer and peer.integrity_status == 'banned':
                logger.debug(f"Federation inbox: rejecting post from banned node {origin_node[:8]}")
                return None

        if not origin_node or not origin_post_id:
            return None

        # Dedup
        existing = db.query(FederatedPost).filter(
            FederatedPost.origin_node_id == origin_node,
            FederatedPost.origin_post_id == origin_post_id,
        ).first()
        if existing:
            return None

        federated = FederatedPost(
            origin_node_id=origin_node,
            origin_node_url=payload.get('origin_url', ''),
            origin_node_name=payload.get('origin_name', ''),
            origin_post_id=origin_post_id,
            origin_author=post_data.get('author', {}).get('username', ''),
            title=post_data.get('title', ''),
            content=post_data.get('content', ''),
            content_type=post_data.get('content_type', 'text'),
            media_urls=post_data.get('media_urls', []),
            score=post_data.get('score', 0),
            comment_count=post_data.get('comment_count', 0),
            original_created_at=post_data.get('created_at'),
        )
        db.add(federated)
        db.flush()

        logger.info(f"Federation: received post '{federated.title[:50]}' "
                     f"from {origin_node[:8]}")
        # P5: periodically enforce the 10 TB central origin-store ceiling (LRU).
        self._maybe_enforce_ceiling(db)
        return federated.id

    # ─── Federated Feed ───

    def get_federated_feed(self, db, limit: int = 20, offset: int = 0) -> tuple:
        """Get posts from all followed instances, merged into a feed."""
        from .models import FederatedPost
        q = db.query(FederatedPost).order_by(FederatedPost.received_at.desc())
        total = q.count()
        posts = q.offset(offset).limit(limit).all()
        return [p.to_dict() for p in posts], total

    # ─── Pull: Fetch recent posts from a peer (on-demand) ───

    def pull_from_peer(self, db, peer_url: str, limit: int = 20) -> int:
        """Pull recent posts from a peer's outbox. Returns count of new posts."""
        try:
            resp = pooled_get(
                f"{peer_url}/api/social/federation/outbox",
                params={'limit': limit},
                timeout=10,
            )
            if resp.status_code != 200:
                return 0
            data = resp.json()
            posts = data.get('posts', [])
            origin_node = data.get('node_id', '')
            origin_url = data.get('url', peer_url)
            origin_name = data.get('name', '')

            count = 0
            for post in posts:
                payload = {
                    'type': 'new_post',
                    'origin_node_id': origin_node,
                    'origin_url': origin_url,
                    'origin_name': origin_name,
                    'post': post,
                }
                result = self.receive_inbox(db, payload)
                if result:
                    count += 1
            return count
        except requests.RequestException as e:
            logger.debug(f"Federation pull failed from {peer_url}: {e}")
            return 0

    def pull_with_central_fallback(self, db, peer_url: str, limit: int = 20) -> int:
        """CDN retrieval (#149/C4): pull content from the source peer; if it
        yields nothing (peer offline OR empty), fall back to the durable copy at
        the parent tier (central, else regional) — the "origin" that survives
        when the source peer disappears.

        Reuses pull_from_peer for BOTH legs (no parallel fetch) and the SAME
        parent-URL resolver the sync drain uses (SyncEngine.parent_tier_url —
        one source).  receive_inbox dedups, so a redundant central pull is
        harmless.  Skips the fallback when there is no parent tier or the peer
        WAS the parent (no self-pull / no recursion)."""
        count = self.pull_from_peer(db, peer_url, limit=limit)
        if count > 0:
            return count
        from .sync_engine import SyncEngine
        central = SyncEngine.parent_tier_url()
        if central and central.rstrip('/') != (peer_url or '').rstrip('/'):
            logger.debug(
                "Federation: peer %s yielded nothing — pulling durable copy "
                "from parent origin %s (#149)", peer_url, central)
            return self.pull_from_peer(db, central, limit=limit)
        return count

    # 10 TB ceiling for the whole central durable-origin store (#177 P5).
    ASSET_CEILING_BYTES = 10 * 1024 ** 4

    def enforce_asset_ceiling(self, db, max_bytes: int = None,
                              batch: int = 200) -> dict:
        """Cap the central durable-origin store (federated_posts — the copy
        pull_with_central_fallback serves) at a TOTAL-bytes ceiling (default
        10 TB across ALL users) via LRU eviction.  Over-cap evicts the COLDEST
        rows first (non-boosted, oldest received_at); the origin node still holds
        the post, so retrieval falls back to the source peer (or 410 if that peer
        is also gone — never a silent data claim).  Boosted posts are retained.
        Idempotent, best-effort; returns {total_bytes, evicted, under_ceiling}."""
        from sqlalchemy import func as _f
        from .models import FederatedPost
        cap = self.ASSET_CEILING_BYTES if max_bytes is None else max_bytes

        def _total():
            return int(db.query(
                _f.coalesce(_f.sum(_f.length(FederatedPost.content)), 0)
            ).scalar() or 0)

        total, evicted = _total(), 0
        while total > cap:
            victims = [r[0] for r in db.query(FederatedPost.id).filter(
                FederatedPost.is_boosted.is_(False)
            ).order_by(FederatedPost.received_at.asc()).limit(batch).all()]
            if not victims:
                break  # only boosted rows remain — cannot evict further
            db.query(FederatedPost).filter(
                FederatedPost.id.in_(victims)).delete(synchronize_session=False)
            db.flush()
            evicted += len(victims)
            total = _total()
            logger.info("Federation: asset ceiling — evicted %d cold federated "
                        "posts (LRU); total now ~%d bytes", len(victims), total)
        return {'total_bytes': total, 'evicted': evicted,
                'under_ceiling': total <= cap}

    _CEILING_CHECK_EVERY = 500
    _inbox_since_check = 0

    def _maybe_enforce_ceiling(self, db):
        """Amortise the O(rows) ceiling SUM over many inbox receives — enforce
        the 10 TB cap once per _CEILING_CHECK_EVERY inserts.  Best-effort; never
        blocks a receive."""
        try:
            FederationManager._inbox_since_check += 1
            if FederationManager._inbox_since_check >= self._CEILING_CHECK_EVERY:
                FederationManager._inbox_since_check = 0
                self.enforce_asset_ceiling(db)
        except Exception as e:
            logger.debug("Federation._maybe_enforce_ceiling: %s", e)

    # ─── Helpers ───

    def _send_follow_notification(self, peer_url: str, follower_node_id: str,
                                   follower_url: str):
        """Notify a peer that we are now following them."""
        try:
            pooled_post(
                f"{peer_url}/api/social/federation/follow-notification",
                json={
                    'follower_node_id': follower_node_id,
                    'follower_url': follower_url,
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    def _get_local_url(self):
        try:
            from .peer_discovery import gossip
            return gossip.base_url
        except Exception:
            import os
            return os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')


# Module-level singleton
federation = FederationManager()
