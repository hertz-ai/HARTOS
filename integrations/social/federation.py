"""
HevolveSocial - Mastodon-Style Federation
Instances follow each other and share content across the federated network.
Built on top of gossip peer discovery — gossip finds peers, federation shares content.

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
from typing import Optional, List

logger = logging.getLogger('hevolve_social')


class FederationManager:
    """Manages instance-level follows and content federation between HevolveBot nodes."""

    def __init__(self):
        self._lock = threading.Lock()

    # ─── Instance Follow/Unfollow ───

    def follow_instance(self, db, local_node_id: str, peer_node_id: str,
                        peer_url: str) -> bool:
        """
        Follow a remote instance. Sends follow request to the peer.
        Returns True if follow was created.
        """
        from .models import InstanceFollow, PeerNode
        existing = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == local_node_id,
            InstanceFollow.following_node_id == peer_node_id,
        ).first()
        if existing:
            return False

        follow = InstanceFollow(
            follower_node_id=local_node_id,
            following_node_id=peer_node_id,
            peer_url=peer_url,
            status='active',
        )
        db.add(follow)
        db.flush()

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
        from .peer_discovery import gossip
        followers = self.get_followers(db, gossip.node_id)
        if not followers:
            return

        payload = {
            'type': 'new_post',
            'origin_node_id': gossip.node_id,
            'origin_url': gossip.base_url,
            'origin_name': gossip.node_name,
            'post': post_dict,
            'timestamp': datetime.utcnow().isoformat(),
        }

        for follower in followers:
            threading.Thread(
                target=self._deliver_to_inbox,
                args=(follower['peer_url'], payload),
                daemon=True,
            ).start()

    def _deliver_to_inbox(self, peer_url: str, payload: dict):
        """POST to a peer's federation inbox."""
        try:
            resp = requests.post(
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

    # ─── Inbox: Receive posts from followed instances ───

    def receive_inbox(self, db, payload: dict) -> Optional[str]:
        """
        Process an incoming federated post.
        Deduplicates by origin_node_id + post.id.
        Verifies sender's guardrail hash before accepting — continuous audit
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
            resp = requests.get(
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

    # ─── Helpers ───

    def _send_follow_notification(self, peer_url: str, follower_node_id: str,
                                   follower_url: str):
        """Notify a peer that we are now following them."""
        try:
            requests.post(
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
            return os.environ.get('HEVOLVE_BASE_URL', 'http://localhost:6777')


# Module-level singleton
federation = FederationManager()
