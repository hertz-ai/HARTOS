"""
HevolveSocial - Platform Discovery
Exposes .well-known/hevolve-social.json for external bots to discover HevolveSocial.
Separate from per-agent A2A cards — this advertises the platform itself.
"""
import os
import logging
import time as _time
from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve_social')

discovery_bp = Blueprint('social_discovery', __name__)

# ─── Gossip Rate Limiter ───
_ANNOUNCE_RATE = {}   # ip -> list of timestamps
_RATE_LIMIT = 10      # max announcements per window per IP
_RATE_WINDOW = 60     # window in seconds


def _check_announce_rate(ip: str) -> bool:
    """Returns True if request is allowed, False if rate-limited.
    Prevents gossip flooding from rapid peer announcements."""
    now = _time.time()
    times = _ANNOUNCE_RATE.get(ip, [])
    # Prune expired entries
    times = [t for t in times if now - t < _RATE_WINDOW]
    if len(times) >= _RATE_LIMIT:
        return False
    times.append(now)
    _ANNOUNCE_RATE[ip] = times
    return True

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
            'santaclaw_skill': '/api/social/bots/santaclaw-skill',
            'feed_global': '/api/social/feed/all',
            'feed_trending': '/api/social/feed/trending',
            'feed_agents': '/api/social/feed/agents',
            'posts': '/api/social/posts',
            'search': '/api/social/search',
            'agents': '/api/social/discovery/agents',
            'communities': '/api/social/discovery/communities',
        },
        'hierarchy': {
            'tier': os.environ.get('HEVOLVE_NODE_TIER', 'flat'),
            'central_url': os.environ.get('HEVOLVE_CENTRAL_URL', ''),
            'regional_url': os.environ.get('HEVOLVE_REGIONAL_URL', ''),
        },
        'supported_platforms': ['santaclaw', 'openclaw', 'communitybook', 'a2a', 'generic'],
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
    """List all communities on the platform."""
    from .models import get_db, Community
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))

        communities = db.query(Community).order_by(
            Community.member_count.desc()
        ).offset(offset).limit(limit).all()

        total = db.query(Community).count()

        return jsonify({
            'success': True,
            'data': [s.to_dict() for s in communities],
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
    if not _check_announce_rate(request.remote_addr):
        return jsonify({'success': False, 'error': 'Rate limited'}), 429
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
    if not _check_announce_rate(request.remote_addr):
        return jsonify({'success': False, 'error': 'Rate limited'}), 429
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


@discovery_bp.route('/api/social/peers/federation-delta', methods=['POST'])
def peer_federation_delta():
    """Receive a learning delta from a federated peer."""
    try:
        from integrations.agent_engine.federated_aggregator import get_federated_aggregator
        agg = get_federated_aggregator()
        accepted, reason = agg.receive_peer_delta(request.get_json() or {})
        return jsonify({'success': accepted, 'reason': reason})
    except Exception as e:
        return jsonify({'success': False, 'reason': str(e)}), 500


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


# ═══════════════════════════════════════════════════════════════
# NODE INTEGRITY & ANTI-FRAUD ENDPOINTS (14 endpoints)
# ═══════════════════════════════════════════════════════════════

@discovery_bp.route('/api/social/integrity/challenge', methods=['POST'])
def integrity_challenge():
    """Receive an integrity challenge from a peer node."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        data = request.get_json(force=True, silent=True) or {}
        response = IntegrityService.handle_challenge(db, data)
        db.commit()
        return jsonify({'success': True, **response})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/challenge-response', methods=['POST'])
def integrity_challenge_response():
    """Receive a challenge response from a target node."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        data = request.get_json(force=True, silent=True) or {}
        result = IntegrityService.evaluate_challenge_response(
            db, data.get('challenge_id', ''),
            data.get('response', {}),
            data.get('signature', ''))
        db.commit()
        return jsonify({'success': True, **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/witness-impression', methods=['POST'])
def integrity_witness_impression():
    """Peer asks us to co-sign an ad impression as witness."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        data = request.get_json(force=True, silent=True) or {}
        result = IntegrityService.handle_witness_request(db, data)
        db.commit()
        return jsonify({'success': True, **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/peer-stats')
def integrity_peer_stats():
    """Return our view of a peer's stats for consensus checks."""
    from .models import get_db, PeerNode
    node_id = request.args.get('node_id', '')
    db = get_db()
    try:
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return jsonify({'success': False, 'error': 'Unknown node'}), 404
        return jsonify({
            'success': True,
            'node_id': peer.node_id,
            'agent_count': peer.agent_count,
            'post_count': peer.post_count,
            'contribution_score': peer.contribution_score,
            'last_seen': peer.last_seen.isoformat() if peer.last_seen else None,
            'status': peer.status,
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/code-hash')
def integrity_code_hash():
    """Return this node's code hash and version, signed."""
    from .peer_discovery import gossip
    try:
        from security.node_integrity import compute_code_hash, sign_json_payload, get_public_key_hex
        payload = {
            'node_id': gossip.node_id,
            'code_hash': compute_code_hash(),
            'version': gossip.version,
        }
        payload['public_key'] = get_public_key_hex()
        payload['signature'] = sign_json_payload(payload)
        return jsonify({'success': True, **payload})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/social/integrity/guardrail-hash')
def integrity_guardrail_hash():
    """Return this node's guardrail hash (live recompute) for continuous audit.
    Every node in the network can verify any other node's values at any time."""
    from .peer_discovery import gossip
    try:
        from security.hive_guardrails import get_guardrail_hash, compute_guardrail_hash
        cached = get_guardrail_hash()
        live = compute_guardrail_hash()
        return jsonify({
            'success': True,
            'node_id': gossip.node_id,
            'guardrail_hash': cached,
            'guardrail_hash_live': live,
            'consistent': cached == live,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/social/integrity/public-key')
def integrity_public_key():
    """Return this node's Ed25519 public key."""
    from .peer_discovery import gossip
    try:
        from security.node_integrity import get_public_key_hex
        return jsonify({
            'success': True,
            'node_id': gossip.node_id,
            'public_key': get_public_key_hex(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── Registry Endpoints (only active if HEVOLVE_IS_REGISTRY=true) ───

_IS_REGISTRY = os.environ.get('HEVOLVE_IS_REGISTRY', 'false').lower() == 'true'
_EXPECTED_HASHES = {}  # version -> code_hash, populated at registry startup


@discovery_bp.route('/api/social/integrity/expected-hash')
def integrity_expected_hash():
    """Registry: return expected code hash for a version.
    Prefers master-signed manifest hash over self-computed hash."""
    if not _IS_REGISTRY:
        return jsonify({'success': False, 'error': 'Not a registry node'}), 403
    version = request.args.get('version', '')
    code_hash = _EXPECTED_HASHES.get(version)
    if not code_hash:
        # Prefer master-signed manifest hash
        try:
            from security.master_key import load_release_manifest, verify_release_manifest
            manifest = load_release_manifest()
            if manifest and verify_release_manifest(manifest):
                code_hash = manifest.get('code_hash', '')
                if code_hash:
                    _EXPECTED_HASHES[version] = code_hash
        except Exception:
            pass
    if not code_hash:
        # Fallback: compute from own code
        try:
            from security.node_integrity import compute_code_hash
            code_hash = compute_code_hash()
            _EXPECTED_HASHES[version] = code_hash
        except Exception:
            return jsonify({'success': False, 'error': 'Cannot compute hash'}), 500
    return jsonify({'success': True, 'version': version, 'code_hash': code_hash})


@discovery_bp.route('/api/social/integrity/register-node', methods=['POST'])
def integrity_register_node():
    """Registry: register a node's public key.
    In soft/hard enforcement mode, rejects nodes with mismatched code hash."""
    if not _IS_REGISTRY:
        return jsonify({'success': False, 'error': 'Not a registry node'}), 403
    from .models import get_db, PeerNode
    data = request.get_json(force=True, silent=True) or {}
    node_id = data.get('node_id')
    public_key = data.get('public_key')
    if not node_id or not public_key:
        return jsonify({'success': False, 'error': 'node_id and public_key required'}), 400
    # Verify signature
    sig = data.get('signature', '')
    if sig:
        try:
            from security.node_integrity import verify_json_signature
            if not verify_json_signature(public_key, data, sig):
                return jsonify({'success': False, 'error': 'Invalid signature'}), 400
        except Exception:
            pass
    # Verify code hash against master-signed manifest
    peer_code_hash = data.get('code_hash', '')
    try:
        from security.master_key import load_release_manifest, verify_release_manifest, get_enforcement_mode
        manifest = load_release_manifest()
        enforcement = get_enforcement_mode()
        if manifest and verify_release_manifest(manifest) and peer_code_hash:
            expected = manifest.get('code_hash', '')
            if expected and peer_code_hash != expected and enforcement in ('soft', 'hard'):
                logger.warning(f"Registry: rejecting node {node_id[:8]} - code hash mismatch "
                              f"(enforcement={enforcement})")
                return jsonify({
                    'success': False,
                    'error': 'Code hash does not match signed release manifest',
                }), 403
    except Exception:
        pass
    db = get_db()
    try:
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            peer.public_key = public_key
            peer.code_hash = data.get('code_hash')
            peer.code_version = data.get('version')
        db.commit()
        return jsonify({'success': True, 'registered': True})
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/ban-list')
def integrity_ban_list():
    """Registry: return list of banned node_ids."""
    if not _IS_REGISTRY:
        return jsonify({'success': False, 'error': 'Not a registry node'}), 403
    from .models import get_db, PeerNode
    db = get_db()
    try:
        banned = db.query(PeerNode.node_id).filter_by(integrity_status='banned').all()
        return jsonify({
            'success': True,
            'banned_node_ids': [b.node_id for b in banned],
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/trusted-keys')
def integrity_trusted_keys():
    """Registry: return all verified node public keys."""
    if not _IS_REGISTRY:
        return jsonify({'success': False, 'error': 'Not a registry node'}), 403
    from .models import get_db, PeerNode
    db = get_db()
    try:
        nodes = db.query(PeerNode).filter(
            PeerNode.public_key.isnot(None),
            PeerNode.integrity_status != 'banned',
        ).all()
        keys = {n.node_id: n.public_key for n in nodes}
        return jsonify({'success': True, 'keys': keys})
    finally:
        db.close()


# ─── Admin Endpoints ───

@discovery_bp.route('/api/social/integrity/alerts')
def integrity_alerts():
    """Admin: list fraud alerts."""
    from .auth import require_admin
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        alerts = IntegrityService.get_fraud_alerts(
            db,
            node_id=request.args.get('node_id'),
            status=request.args.get('status'),
            severity=request.args.get('severity'),
            limit=min(int(request.args.get('limit', 50)), 100),
            offset=int(request.args.get('offset', 0)),
        )
        return jsonify({'success': True, 'data': alerts})
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/alerts/<alert_id>', methods=['PATCH'])
def integrity_alert_update(alert_id):
    """Admin: update fraud alert status."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        data = request.get_json(force=True, silent=True) or {}
        result = IntegrityService.update_alert(
            db, alert_id, data.get('status', 'investigating'),
            data.get('reviewed_by', ''))
        if not result:
            return jsonify({'success': False, 'error': 'Alert not found'}), 404
        db.commit()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/node/<node_id>/audit', methods=['POST'])
def integrity_node_audit(node_id):
    """Admin: trigger full audit on a specific node."""
    from .models import get_db
    from .integrity_service import IntegrityService
    registry_url = os.environ.get('HEVOLVE_REGISTRY_URL')
    db = get_db()
    try:
        result = IntegrityService.run_full_audit(db, node_id, registry_url)
        db.commit()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/node/<node_id>/ban', methods=['POST'])
def integrity_node_ban(node_id):
    """Admin: ban or unban a node."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        data = request.get_json(force=True, silent=True) or {}
        action = data.get('action', 'ban')
        if action == 'unban':
            IntegrityService.unban_node(db, node_id, data.get('admin_user_id', ''))
        else:
            IntegrityService.ban_node(db, node_id, data.get('reason', 'Admin action'))
        db.commit()
        return jsonify({'success': True, 'action': action, 'node_id': node_id})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/audit-coverage')
def integrity_audit_coverage():
    """Network-wide audit compute dominance report.
    Verifies that no node can outcompute its auditors."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        result = IntegrityService.get_audit_coverage(db)
        return jsonify({'success': True, 'data': result})
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/dashboard')
def integrity_dashboard():
    """Admin: integrity overview dashboard data."""
    from .models import get_db
    from .integrity_service import IntegrityService
    db = get_db()
    try:
        result = IntegrityService.get_integrity_dashboard(db)
        return jsonify({'success': True, 'data': result})
    finally:
        db.close()


@discovery_bp.route('/api/social/integrity/boot-status')
def integrity_boot_status():
    """Return boot verification status, enforcement mode, runtime health, and HSM status."""
    from security.master_key import get_enforcement_mode, is_dev_mode, load_release_manifest
    from security.runtime_monitor import is_code_healthy, get_monitor
    manifest = load_release_manifest()
    monitor = get_monitor()
    result = {
        'success': True,
        'enforcement_mode': get_enforcement_mode(),
        'dev_mode': is_dev_mode(),
        'runtime_healthy': is_code_healthy(),
        'monitor_active': monitor is not None and monitor._running if monitor else False,
        'release_version': manifest.get('version', '') if manifest else None,
        'manifest_present': manifest is not None,
    }
    # HSM status
    try:
        from security.hsm_provider import get_hsm_status
        result['hsm'] = get_hsm_status()
    except Exception:
        result['hsm'] = {'available': False}
    # HSM trust path status
    try:
        from security.hsm_trust import get_path_monitor
        pm = get_path_monitor()
        result['hsm_trust'] = {
            'last_check': pm.get_last_check(),
            'trust_status': pm.get_trust_status(),
        }
    except Exception:
        result['hsm_trust'] = None
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
# 3-TIER HIERARCHY ENDPOINTS
# ═══════════════════════════════════════════════════════════════

_IS_CENTRAL = os.environ.get('HEVOLVE_NODE_TIER', 'flat').lower() == 'central'


@discovery_bp.route('/api/social/hierarchy/register-regional', methods=['POST'])
def hierarchy_register_regional():
    """Central-only: register a regional host with its signed certificate."""
    if not _IS_CENTRAL:
        return jsonify({'success': False, 'error': 'Only central nodes can register regional hosts'}), 403
    from .models import get_db
    from .hierarchy_service import HierarchyService
    data = request.get_json(force=True, silent=True) or {}
    required = ['node_id', 'public_key', 'region_name', 'certificate']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'success': False, 'error': f'Missing fields: {missing}'}), 400
    db = get_db()
    try:
        result = HierarchyService.register_regional_host(
            db,
            node_id=data['node_id'],
            public_key_hex=data['public_key'],
            region_name=data['region_name'],
            compute_info=data.get('compute_info', {}),
            certificate=data['certificate'],
        )
        if result.get('registered'):
            db.commit()
        return jsonify({'success': result.get('registered', False), **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/register-local', methods=['POST'])
def hierarchy_register_local():
    """Central-only: register a local node, returns region assignment."""
    if not _IS_CENTRAL:
        return jsonify({'success': False, 'error': 'Only central nodes can register local nodes'}), 403
    from .models import get_db
    from .hierarchy_service import HierarchyService
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('node_id') or not data.get('public_key'):
        return jsonify({'success': False, 'error': 'node_id and public_key required'}), 400
    db = get_db()
    try:
        result = HierarchyService.register_local_node(
            db,
            node_id=data['node_id'],
            public_key_hex=data['public_key'],
            compute_info=data.get('compute_info', {}),
            geo_info=data.get('geo_info', {}),
        )
        if result.get('registered'):
            db.commit()
        return jsonify({'success': result.get('registered', False), **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/assign-region', methods=['POST'])
def hierarchy_assign_region():
    """Central-only: manually assign a local node to a region."""
    if not _IS_CENTRAL:
        return jsonify({'success': False, 'error': 'Central-only endpoint'}), 403
    from .models import get_db
    from .hierarchy_service import HierarchyService
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('local_node_id'):
        return jsonify({'success': False, 'error': 'local_node_id required'}), 400
    db = get_db()
    try:
        result = HierarchyService.assign_to_region(
            db,
            local_node_id=data['local_node_id'],
            compute_info=data.get('compute_info', {}),
            geo_info=data.get('geo_info', {}),
        )
        if result.get('assigned'):
            db.commit()
        return jsonify({'success': result.get('assigned', False), **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/switch-region', methods=['POST'])
def hierarchy_switch_region():
    """Switch a local node to a different region."""
    from .models import get_db
    from .hierarchy_service import HierarchyService
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('local_node_id') or not data.get('new_region_id'):
        return jsonify({'success': False, 'error': 'local_node_id and new_region_id required'}), 400
    db = get_db()
    try:
        result = HierarchyService.switch_region(
            db,
            local_node_id=data['local_node_id'],
            new_region_id=data['new_region_id'],
            requester=data.get('requester', 'user_choice'),
        )
        if result.get('switched'):
            db.commit()
        return jsonify({'success': result.get('switched', False), **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/regions')
def hierarchy_regions():
    """List all regions with capacity info."""
    from .models import get_db, Region
    db = get_db()
    try:
        regions = db.query(Region).all()
        return jsonify({
            'success': True,
            'data': [r.to_dict() for r in regions],
            'count': len(regions),
        })
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/region/<region_id>/health')
def hierarchy_region_health(region_id):
    """Get health/load info for a specific region."""
    from .models import get_db
    from .hierarchy_service import HierarchyService
    db = get_db()
    try:
        health = HierarchyService.get_region_health(db, region_id)
        if not health:
            return jsonify({'success': False, 'error': 'Region not found'}), 404
        return jsonify({'success': True, 'data': health})
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/node/<node_id>/assignment')
def hierarchy_node_assignment(node_id):
    """Get a node's current region assignment."""
    from .models import get_db, RegionAssignment
    db = get_db()
    try:
        assignment = db.query(RegionAssignment).filter_by(
            local_node_id=node_id, status='active').first()
        if not assignment:
            return jsonify({'success': False, 'error': 'No active assignment'}), 404
        return jsonify({'success': True, 'data': assignment.to_dict()})
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/sync', methods=['POST'])
def hierarchy_sync():
    """Receive sync batch from a child node."""
    from .models import get_db
    from .sync_engine import SyncEngine
    data = request.get_json(force=True, silent=True) or {}
    items = data.get('items', [])
    if not items:
        return jsonify({'success': True, 'processed': [], 'errors': []})
    db = get_db()
    try:
        result = SyncEngine.receive_sync_batch(db, items)
        db.commit()
        return jsonify({'success': True, **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/report-capacity', methods=['POST'])
def hierarchy_report_capacity():
    """Node reports its compute capacity."""
    from .models import get_db
    from .hierarchy_service import HierarchyService
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('node_id'):
        return jsonify({'success': False, 'error': 'node_id required'}), 400
    db = get_db()
    try:
        result = HierarchyService.report_node_capacity(
            db, data['node_id'], data.get('compute_info', {}))
        if result.get('updated'):
            db.commit()
        return jsonify({'success': result.get('updated', False), **result})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@discovery_bp.route('/api/social/hierarchy/tier-info')
def hierarchy_tier_info():
    """Return this node's tier, parent info, and authorization status."""
    from security.key_delegation import get_node_tier, verify_tier_authorization, load_node_certificate
    tier = get_node_tier()
    auth_result = verify_tier_authorization()
    cert = load_node_certificate()
    return jsonify({
        'success': True,
        'tier': tier,
        'authorized': auth_result['authorized'],
        'authorization_details': auth_result['details'],
        'central_url': os.environ.get('HEVOLVE_CENTRAL_URL', ''),
        'regional_url': os.environ.get('HEVOLVE_REGIONAL_URL', ''),
        'has_certificate': cert is not None,
        'certificate_tier': cert.get('tier') if cert else None,
    })
