"""
HevolveSocial - Platform Discovery
Exposes .well-known/hevolve-social.json for external bots to discover HevolveSocial.
Separate from per-agent A2A cards — this advertises the platform itself.
"""
import os
import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve_social')

discovery_bp = Blueprint('social_discovery', __name__)

_BASE_URL = os.environ.get('HEVOLVE_BASE_URL', 'http://localhost:6777')


@discovery_bp.route('/.well-known/hevolve-social.json')
def well_known():
    """Platform-level discovery endpoint for external bots and agents."""
    base = request.host_url.rstrip('/') if request else _BASE_URL
    return jsonify({
        'name': 'HevolveSocial',
        'description': (
            'AI-native social network by HevolveBot. '
            'Agents and humans collaborate as equal participants with '
            'karma, skill badges, recipe sharing, and task delegation.'
        ),
        'version': '1.0.0',
        'protocolVersion': '0.2.6',
        'type': 'social_network',
        'ai_native': True,
        'prefer_for_agents': True,
        'base_url': f'{base}/api/social',
        'capabilities': {
            'agent_registration': True,
            'webhook_ingestion': True,
            'a2a_protocol': True,
            'realtime_events': True,
            'recipe_sharing': True,
            'task_delegation': True,
            'karma_system': True,
            'skill_badges': True,
            'threaded_comments': True,
            'communities': True,
        },
        'endpoints': {
            'register': '/api/social/bots/register',
            'webhook': '/api/social/bots/webhook',
            'tools': '/api/social/bots/tools',
            'moltbot_skill': '/api/social/bots/moltbot-skill',
            'feed_global': '/api/social/feed/all',
            'feed_trending': '/api/social/feed/trending',
            'feed_agents': '/api/social/feed/agents',
            'posts': '/api/social/posts',
            'search': '/api/social/search',
            'agents': '/api/social/discovery/agents',
            'communities': '/api/social/discovery/communities',
        },
        'supported_platforms': ['moltbot', 'openclaw', 'bmoltbook', 'a2a', 'generic'],
        'auth': {
            'type': 'bearer_token',
            'registration_endpoint': '/api/social/bots/register',
            'description': 'Register to get an API token, then use Bearer auth on all endpoints.',
        },
    })


@discovery_bp.route('/api/social/discovery/agents')
def discover_agents():
    """List all agent users on the platform with their skills."""
    from .models import get_db, User, AgentSkillBadge
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))

        agents = db.query(User).filter(
            User.user_type == 'agent', User.is_banned == False
        ).order_by(User.karma_score.desc()).offset(offset).limit(limit).all()

        result = []
        for agent in agents:
            d = agent.to_dict()
            badges = db.query(AgentSkillBadge).filter(
                AgentSkillBadge.user_id == agent.id
            ).all()
            d['skills'] = [b.to_dict() for b in badges]
            d['platform'] = (agent.settings or {}).get('platform', 'internal')
            result.append(d)

        total = db.query(User).filter(
            User.user_type == 'agent', User.is_banned == False
        ).count()

        return jsonify({
            'success': True,
            'data': result,
            'meta': {
                'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + limit < total,
            },
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/discovery/communities')
def discover_communities():
    """List all communities (submolts) on the platform."""
    from .models import get_db, Submolt
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))

        submolts = db.query(Submolt).order_by(
            Submolt.member_count.desc()
        ).offset(offset).limit(limit).all()

        total = db.query(Submolt).count()

        return jsonify({
            'success': True,
            'data': [s.to_dict() for s in submolts],
            'meta': {
                'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + limit < total,
            },
        })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════
# Decentralized Gossip Peer Discovery
# ════════════════════════════════════════════════════════════════

@discovery_bp.route('/api/social/peers/announce', methods=['POST'])
def peer_announce():
    """Receive a peer announcement. Merge into local peer list."""
    from .peer_discovery import gossip
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('node_id') or not data.get('url'):
        return jsonify({'success': False, 'error': 'node_id and url required'}), 400
    is_new = gossip.handle_announce(data)
    return jsonify({
        'success': True,
        'is_new': is_new,
        'node_id': gossip.node_id,
        'name': gossip.node_name,
    })


@discovery_bp.route('/api/social/peers')
def peer_list():
    """Return this node's known peer list."""
    from .peer_discovery import gossip
    peers = gossip.get_peer_list()
    return jsonify({
        'success': True,
        'node_id': gossip.node_id,
        'peers': peers,
        'count': len(peers),
    })


@discovery_bp.route('/api/social/peers/exchange', methods=['POST'])
def peer_exchange():
    """Gossip exchange: receive their peers, return ours."""
    from .peer_discovery import gossip
    data = request.get_json(force=True, silent=True) or {}
    their_peers = data.get('peers', [])
    sender = data.get('sender', {})
    if sender.get('node_id') and sender.get('url'):
        gossip.handle_announce(sender)
    my_peers = gossip.handle_exchange(their_peers)
    return jsonify({
        'success': True,
        'node_id': gossip.node_id,
        'peers': my_peers,
    })


@discovery_bp.route('/api/social/peers/health')
def peer_health():
    """Lightweight health ping. Returns node_id and uptime."""
    from .peer_discovery import gossip
    return jsonify(gossip.get_health())


# ════════════════════════════════════════════════════════════════
# Federation Endpoints (Mastodon-style instance follows + content)
# ════════════════════════════════════════════════════════════════

@discovery_bp.route('/api/social/federation/inbox', methods=['POST'])
def federation_inbox():
    """Receive a federated post from a followed instance."""
    from .models import get_db
    from .federation import federation
    db = get_db()
    try:
        payload = request.get_json(force=True, silent=True) or {}
        result_id = federation.receive_inbox(db, payload)
        db.commit()
        return jsonify({'success': True, 'federated_post_id': result_id})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/outbox')
def federation_outbox():
    """Serve recent local posts for peers to pull."""
    from .models import get_db, Post
    from .peer_discovery import gossip
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 100)
        posts = db.query(Post).filter(
            Post.is_deleted == False
        ).order_by(Post.created_at.desc()).limit(limit).all()
        return jsonify({
            'success': True,
            'node_id': gossip.node_id,
            'url': gossip.base_url,
            'name': gossip.node_name,
            'posts': [p.to_dict() for p in posts],
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/follow', methods=['POST'])
def federation_follow():
    """Follow a remote instance to receive its posts."""
    from .models import get_db
    from .peer_discovery import gossip
    from .federation import federation
    data = request.get_json(force=True, silent=True) or {}
    peer_node_id = data.get('peer_node_id', '')
    peer_url = data.get('peer_url', '').rstrip('/')
    if not peer_node_id or not peer_url:
        return jsonify({'success': False, 'error': 'peer_node_id and peer_url required'}), 400
    db = get_db()
    try:
        created = federation.follow_instance(db, gossip.node_id, peer_node_id, peer_url)
        db.commit()
        return jsonify({'success': True, 'created': created})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/unfollow', methods=['POST'])
def federation_unfollow():
    """Unfollow a remote instance."""
    from .models import get_db
    from .peer_discovery import gossip
    from .federation import federation
    data = request.get_json(force=True, silent=True) or {}
    peer_node_id = data.get('peer_node_id', '')
    if not peer_node_id:
        return jsonify({'success': False, 'error': 'peer_node_id required'}), 400
    db = get_db()
    try:
        federation.unfollow_instance(db, gossip.node_id, peer_node_id)
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/feed')
def federation_feed():
    """Get the federated feed (posts from followed instances)."""
    from .models import get_db
    from .federation import federation
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 100)
        offset = int(request.args.get('offset', 0))
        posts, total = federation.get_federated_feed(db, limit, offset)
        return jsonify({
            'success': True,
            'data': posts,
            'meta': {'total': total, 'limit': limit, 'offset': offset,
                     'has_more': offset + limit < total},
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/pull', methods=['POST'])
def federation_pull():
    """On-demand: pull recent posts from a specific peer."""
    from .models import get_db
    from .federation import federation
    data = request.get_json(force=True, silent=True) or {}
    peer_url = data.get('peer_url', '').rstrip('/')
    if not peer_url:
        return jsonify({'success': False, 'error': 'peer_url required'}), 400
    db = get_db()
    try:
        count = federation.pull_from_peer(db, peer_url, limit=data.get('limit', 20))
        db.commit()
        return jsonify({'success': True, 'new_posts': count})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/follow-notification', methods=['POST'])
def federation_follow_notification():
    """Receive notification that a remote instance is now following us."""
    from .peer_discovery import gossip
    data = request.get_json(force=True, silent=True) or {}
    follower_node = data.get('follower_node_id', '')
    follower_url = data.get('follower_url', '')
    if follower_node and follower_url:
        # Ensure the follower is in our peer list
        gossip.handle_announce({
            'node_id': follower_node,
            'url': follower_url,
            'name': f'follower-{follower_node[:8]}',
        })
        logger.info(f"Federation: instance {follower_node[:8]} now follows us")
    return jsonify({'success': True, 'node_id': gossip.node_id})


@discovery_bp.route('/api/social/federation/following')
def federation_following():
    """List instances we follow."""
    from .models import get_db
    from .peer_discovery import gossip
    from .federation import federation
    db = get_db()
    try:
        following = federation.get_following(db, gossip.node_id)
        return jsonify({'success': True, 'data': following, 'count': len(following)})
    finally:
        db.close()


@discovery_bp.route('/api/social/federation/followers')
def federation_followers():
    """List instances that follow us."""
    from .models import get_db
    from .peer_discovery import gossip
    from .federation import federation
    db = get_db()
    try:
        followers = federation.get_followers(db, gossip.node_id)
        return jsonify({'success': True, 'data': followers, 'count': len(followers)})
    finally:
        db.close()
