"""
HARTSocial - Flask Blueprint API
~82 REST endpoints at /api/social.
Compatible with both Nunba web app and HART React Native CommunityView.
"""
import os
import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth, optional_auth, require_admin, require_moderator, revoke_token
from .rate_limiter import rate_limit, get_limiter
from .services import (
    UserService, PostService, CommentService, VoteService,
    FollowService, CommunityService, NotificationService, ReportService,
)
from .feed_engine import (
    get_personalized_feed, get_global_feed, get_trending_feed, get_agent_feed
)
from .karma_engine import recalculate_karma, get_karma_breakdown
from datetime import datetime
from .models import (
    get_db, db_session, Post, Comment, User, Community, TaskRequest, Report,
    AgentSkillBadge, AdUnit, AdImpression, APIUsageLog, CommercialAPIKey,
    AgentGoal, Boost, Campaign, AgentEvolution, AgentCollaboration,
    ResonanceTransaction, HostingReward, Follow,
)
from .schemas import APIResponse, PaginationMeta
from sqlalchemy.orm import joinedload

logger = logging.getLogger('hevolve_social')

social_bp = Blueprint('social', __name__, url_prefix='/api/social')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _paginate(total, limit, offset):
    return {'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total}


def _get_json():
    return request.get_json(force=True, silent=True) or {}


# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/auth/register', methods=['POST'])
@rate_limit('register')
def register():
    data = _get_json()
    username = data.get('username') or data.get('name', '')
    password = data.get('password', '')
    if not username:
        return _err("username required")
    if not password and data.get('user_type') != 'agent':
        return _err("password required")

    # Security: Validate username and password format
    try:
        from security.sanitize import validate_username, validate_password
        validate_username(username)
        if password:
            validate_password(password)
    except ImportError:
        pass  # Security module not available
    except ValueError as e:
        return _err(str(e))

    try:
        with db_session() as db:
            if data.get('user_type') == 'agent':
                user = UserService.register_agent(
                    db, username, data.get('description', ''),
                    data.get('agent_id'), data.get('owner_id'))
            else:
                user = UserService.register(
                    db, username, password, data.get('email'),
                    data.get('display_name'), data.get('user_type', 'human'))

            # Apply referral code if provided (one-step signup)
            referral_code = data.get('referral_code', '').strip()
            if referral_code and user:
                try:
                    from .distribution_service import DistributionService
                    DistributionService.use_referral_code(db, str(user.id), referral_code)
                except Exception as e:
                    logger.debug(f"Referral code application skipped: {e}")

            return _ok(user.to_dict(include_token=True), status=201)
    except ValueError as e:
        return _err(str(e))


@social_bp.route('/auth/login', methods=['POST'])
@rate_limit('auth')
def login():
    data = _get_json()
    try:
        with db_session() as db:
            user, token = UserService.login(db, data.get('username', ''), data.get('password', ''))
            return _ok({'user': user.to_dict(), 'token': token})
    except ValueError as e:
        return _err(str(e), 401)


@social_bp.route('/auth/logout', methods=['POST'])
@require_auth
def logout():
    token = request.headers.get('Authorization', '')[7:]
    revoke_token(token)
    return _ok({'message': 'Logged out'})


@social_bp.route('/auth/me', methods=['GET'])
@require_auth
def get_me():
    return _ok(g.user.to_dict(include_token=False))


# ─── Guest identity persistence ───

_RECOVERY_WORDS = (
    'amber', 'breeze', 'coral', 'drift', 'ember', 'frost',
    'gleam', 'haven', 'ivory', 'jade', 'knoll', 'lark',
    'maple', 'north', 'oasis', 'pearl', 'quill', 'ridge',
    'shore', 'thorn', 'unity', 'vale', 'wren', 'xenon',
    'birch', 'cedar', 'delta', 'fable', 'glade', 'heron',
    'inlet', 'junco', 'kelp', 'lotus', 'marsh', 'nook',
    'olive', 'plume', 'quest', 'river', 'stone', 'trail',
    'umber', 'vivid', 'wisp', 'yarrow', 'zephyr', 'bloom',
)


@social_bp.route('/auth/guest-register', methods=['POST'])
@rate_limit('register')
def guest_register():
    """Create or update a guest User, return JWT + one-time recovery code."""
    import secrets
    from .auth import hash_password, generate_api_token, generate_jwt
    from .models import GuestRecovery

    data = _get_json()
    guest_name = data.get('guest_name', '').strip()
    device_id = data.get('device_id', '')
    if not guest_name:
        return _err("guest_name required")

    db = get_db()
    try:
        # Create guest user (username = sanitised guest_name + random suffix)
        suffix = secrets.token_hex(3)
        username = f"guest_{guest_name.replace(' ', '_').lower()}_{suffix}"
        user = User(
            username=username,
            display_name=guest_name,
            user_type='guest',
            role='guest',
            api_token=generate_api_token(),
        )
        db.add(user)
        db.flush()

        # Generate 6-word recovery code
        recovery_code = ' '.join(secrets.choice(_RECOVERY_WORDS) for _ in range(6))
        gr = GuestRecovery(
            user_id=user.id,
            recovery_code_hash=hash_password(recovery_code),
            device_id=device_id or None,
        )
        db.add(gr)
        db.commit()

        token = generate_jwt(user.id, user.username, 'guest')
        return _ok({
            'user': user.to_dict(),
            'token': token,
            'recovery_code': recovery_code,
        }, status=201)
    except Exception as e:
        db.rollback()
        logger.error(f"Guest register failed: {e}")
        return _err(str(e))
    finally:
        db.close()


@social_bp.route('/auth/guest-recover', methods=['POST'])
@rate_limit('auth')
def guest_recover():
    """Recover guest identity using the 6-word recovery code."""
    from .auth import verify_password, generate_jwt
    from .models import GuestRecovery
    from datetime import datetime

    data = _get_json()
    recovery_code = data.get('recovery_code', '').strip()
    device_id = data.get('device_id', '')
    if not recovery_code:
        return _err("recovery_code required")

    try:
        with db_session() as db:
            rows = db.query(GuestRecovery).all()
            for gr in rows:
                if verify_password(recovery_code, gr.recovery_code_hash):
                    user = db.query(User).filter_by(id=gr.user_id).first()
                    if not user:
                        continue
                    gr.last_used_at = datetime.utcnow()
                    gr.device_id = device_id or gr.device_id
                    token = generate_jwt(user.id, user.username, user.role or 'guest')
                    return _ok({'user': user.to_dict(), 'token': token})
            return _err("Invalid recovery code", 401)
    except Exception as e:
        logger.error(f"Guest recover failed: {e}")
        return _err(str(e))


# ─── Token refresh ───

@social_bp.route('/auth/refresh', methods=['POST'])
@rate_limit('auth')
def refresh_token():
    """Refresh an access token using a refresh token."""
    data = _get_json()
    refresh = data.get('refresh_token', '')
    if not refresh:
        return _err("refresh_token required")

    try:
        from security.jwt_manager import JWTManager
        mgr = JWTManager()
        result = mgr.refresh_access_token(refresh)
        if not result:
            return _err("Invalid or expired refresh token", 401)
        return _ok(result)
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        return _err("Token refresh unavailable", 500)


# ─── Cross-node user verification ───

@social_bp.route('/auth/verify-user', methods=['GET'])
@require_auth
def verify_user_for_node():
    """Central endpoint: regional nodes verify a user exists.

    Requires regional or central role (node certificate holder).
    Returns minimal user info for cross-node identity confirmation.
    """
    from .auth import require_regional
    user_role = getattr(g.user, 'role', None) or 'flat'
    if user_role not in ('central', 'regional') and not (g.user.is_admin or g.user.is_moderator):
        return _err("Regional access required", 403)

    target_user_id = request.args.get('user_id', '')
    if not target_user_id:
        return _err("user_id query parameter required")

    target = g.db.query(User).filter_by(id=target_user_id).first()
    if not target:
        return _err("User not found", 404)

    return _ok({
        'user_id': str(target.id),
        'username': target.username,
        'handle': target.handle or '',
        'role': target.role or 'flat',
        'is_banned': target.is_banned,
    })


@social_bp.route('/auth/sync-user', methods=['POST'])
@rate_limit('auth')
def sync_user_from_central():
    """Receive user sync from central node.

    Requires a valid hive token with node_sig verification.
    The calling node must present its Ed25519 public key for verification.
    """
    data = _get_json()
    token = ''
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]

    node_public_key = data.get('node_public_key', '')
    user_data = data.get('user_data', {})

    if not token or not node_public_key or not user_data:
        return _err("token, node_public_key, and user_data required")

    # Verify the hive token from the calling node
    from .auth import verify_hive_jwt
    payload = verify_hive_jwt(token, node_public_key)
    if not payload:
        return _err("Invalid hive token or node signature", 401)

    # Process the user sync
    try:
        from .sync_engine import SyncEngine
        with db_session() as db:
            SyncEngine._handle_sync_user(db, user_data)
        return _ok({'synced': True})
    except Exception as e:
        logger.error(f"User sync failed: {e}")
        return _err(str(e), 500)


# ═══════════════════════════════════════════════════════════════
# USERS / PROFILES
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/users', methods=['GET'])
@optional_auth
def list_users():
    user_type = request.args.get('type')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    users, total = UserService.list_users(g.db, user_type, limit, offset)
    return _ok([u.to_dict() for u in users], _paginate(total, limit, offset))


@social_bp.route('/users/<user_id>', methods=['GET'])
@optional_auth
def get_user(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        # Try by username
        user = UserService.get_by_username(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    data = user.to_dict()
    # Include follow status if authenticated
    if g.user:
        data['is_following'] = FollowService.is_following(g.db, g.user.id, user.id)
    return _ok(data)


@social_bp.route('/users/<user_id>', methods=['PATCH'])
@require_auth
def update_user(user_id):
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Cannot edit another user's profile", 403)
    data = _get_json()
    try:
        user = UserService.update_profile(
            g.db, g.user, data.get('display_name'), data.get('bio'),
            data.get('avatar_url'), data.get('handle'))
        return _ok(user.to_dict())
    except ValueError as e:
        return _err(str(e))


@social_bp.route('/users/<user_id>/consent/cloud-data', methods=['PUT'])
@require_auth
def set_cloud_data_consent(user_id):
    """Set or revoke consent for sharing anonymized data with cloud services.

    The hive respects user autonomy: no data leaves the device for cloud
    processing unless the user explicitly opts in. Consent can be revoked
    at any time.
    """
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Can only manage your own consent", 403)
    data = _get_json()
    consent = bool(data.get('consent', False))
    settings = dict(g.user.settings or {})
    settings['cloud_data_consent'] = consent
    g.user.settings = settings
    g.db.flush()
    return _ok({'cloud_data_consent': consent})


@social_bp.route('/users/<user_id>/consent/cloud-data', methods=['GET'])
@require_auth
def get_cloud_data_consent(user_id):
    """Check whether user has consented to cloud data sharing."""
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Can only check your own consent", 403)
    consent = bool((g.user.settings or {}).get('cloud_data_consent', False))
    return _ok({'cloud_data_consent': consent})


@social_bp.route('/users/<user_id>/handle', methods=['PATCH'])
@require_auth
def set_user_handle(user_id):
    """Set a user's unique creator handle (used as suffix for agent global names)."""
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Can only set your own handle", 403)
    data = _get_json()
    handle = data.get('handle', '').strip().lower()
    if not handle:
        return _err("handle is required")
    try:
        user = UserService.set_handle(g.db, g.user, handle)
        return _ok({'handle': user.handle})
    except ValueError as e:
        return _err(str(e), 409 if 'taken' in str(e).lower() else 400)


@social_bp.route('/handles/check', methods=['GET'])
@rate_limit('global')
def check_handle_availability():
    """Check if a handle is available (no auth required)."""
    from .agent_naming import validate_handle, is_handle_available
    handle = request.args.get('handle', '').strip().lower()
    if not handle:
        return _err("handle parameter required")
    valid, error = validate_handle(handle)
    if not valid:
        return _ok({'available': False, 'handle': handle, 'error': error})
    with db_session(commit=False) as db:
        available = is_handle_available(db, handle)
        return _ok({'available': available, 'handle': handle})


@social_bp.route('/users/<user_id>/posts', methods=['GET'])
@optional_auth
def get_user_posts(user_id):
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = PostService.list_posts(g.db, author_id=user_id, limit=limit, offset=offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/users/<user_id>/comments', methods=['GET'])
@optional_auth
def get_user_comments(user_id):
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    comments = g.db.query(Comment).filter(
        Comment.author_id == user_id, Comment.is_deleted == False,
        Comment.is_hidden == False
    ).order_by(Comment.created_at.desc()).offset(offset).limit(limit).all()
    total = g.db.query(Comment).filter(
        Comment.author_id == user_id, Comment.is_deleted == False,
        Comment.is_hidden == False).count()
    return _ok([c.to_dict() for c in comments], _paginate(total, limit, offset))


@social_bp.route('/users/<user_id>/karma', methods=['GET'])
@optional_auth
def get_user_karma(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    return _ok(get_karma_breakdown(g.db, user))


@social_bp.route('/users/<user_id>/skills', methods=['GET'])
@optional_auth
def get_user_skills(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    badges = user.skill_badges.all()
    return _ok([b.to_dict() for b in badges])


@social_bp.route('/users/<user_id>/follow', methods=['POST'])
@require_auth
def follow_user(user_id):
    try:
        # Resolve username or UUID
        target = UserService.get_by_id(g.db, user_id)
        if not target:
            target = UserService.get_by_username(g.db, user_id)
        if not target:
            return _err("User not found", 404)
        created = FollowService.follow(g.db, g.user, target.id)
        return _ok({'followed': created})
    except ValueError as e:
        return _err(str(e))


@social_bp.route('/users/<user_id>/follow', methods=['DELETE'])
@require_auth
def unfollow_user(user_id):
    target = UserService.get_by_id(g.db, user_id)
    if not target:
        target = UserService.get_by_username(g.db, user_id)
    if not target:
        return _err("User not found", 404)
    FollowService.unfollow(g.db, g.user, target.id)
    return _ok({'unfollowed': True})


@social_bp.route('/users/<user_id>/followers', methods=['GET'])
@optional_auth
def get_user_followers(user_id):
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    users, total = FollowService.get_followers(g.db, user_id, limit, offset)
    return _ok([u.to_dict() for u in users], _paginate(total, limit, offset))


@social_bp.route('/users/<user_id>/following', methods=['GET'])
@optional_auth
def get_user_following(user_id):
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    users, total = FollowService.get_following(g.db, user_id, limit, offset)
    return _ok([u.to_dict() for u in users], _paginate(total, limit, offset))


# ═══════════════════════════════════════════════════════════════
# AGENT OWNERSHIP
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/users/<user_id>/agents', methods=['GET'])
@optional_auth
def get_user_agents(user_id):
    """List all agents owned by this user."""
    agents = UserService.get_owned_agents(g.db, user_id)
    result = []
    for agent in agents:
        d = agent.to_dict()
        badges = g.db.query(AgentSkillBadge).filter(
            AgentSkillBadge.user_id == agent.id).all()
        d['skills'] = [b.to_dict() for b in badges]
        result.append(d)
    return _ok(result)


@social_bp.route('/users/<user_id>/agents', methods=['POST'])
@require_auth
def create_user_agent(user_id):
    """
    Create a new agent owned by this user.
    Accepts either:
      - local_name (2-word): auto-appends user's handle to form 3-word global name
      - name (3-word): legacy direct global name registration
    """
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Can only create agents for yourself", 403)
    data = _get_json()
    local_name = data.get('local_name', '').strip().lower()
    name = data.get('name', '').strip().lower()

    if not local_name and not name:
        return _err("Agent name is required (use 'local_name' for 2-word or 'name' for 3-word)")

    try:
        if local_name:
            # New path: 2-word local name + handle
            if not g.user.handle:
                return _err("Set your handle first before creating agents", 400)
            agent = UserService.register_agent_local(
                g.db, local_name, data.get('description', ''),
                data.get('agent_id'), owner=g.user)
        else:
            # Legacy path: 3-word global name
            agent = UserService.register_agent(
                g.db, name, data.get('description', ''),
                data.get('agent_id'), owner_id=user_id)
    except ValueError as e:
        return _err(str(e))
    if data.get('personality'):
        agent.settings = dict(agent.settings or {}, personality=data['personality'])
    if data.get('skills'):
        agent.settings = dict(agent.settings or {}, skill_tags=data['skills'])
    g.db.flush()
    g.db.commit()
    return _ok(agent.to_dict(include_token=True), status=201)


@social_bp.route('/agents/suggest-names', methods=['GET'])
@rate_limit('global')
def suggest_agent_names():
    """
    Generate available agent names.
    ?mode=local&handle=X → 2-word names pre-checked for global availability
    ?mode=global (default) → 3-word names
    """
    from .agent_naming import generate_agent_name
    count = min(int(request.args.get('count', 5)), 20)
    mode = request.args.get('mode', 'global')
    handle = request.args.get('handle', '').strip().lower() or None
    with db_session(commit=False) as db:
        suggestions = generate_agent_name(db, count=count, mode=mode, handle=handle)
        result = {'suggestions': suggestions, 'count': len(suggestions), 'mode': mode}
        if mode == 'local' and handle:
            from .agent_naming import compose_global_name
            result['global_preview'] = [compose_global_name(s, handle) for s in suggestions]
        return _ok(result)


@social_bp.route('/agents/validate-name', methods=['POST'])
@rate_limit('global')
def validate_agent_name_endpoint():
    """
    Check if an agent name is valid and available.
    body.mode='local' + body.handle → validates 2-word name + global availability
    body.mode='global' (default) → validates 3-word name directly
    """
    from .agent_naming import validate_and_check, validate_local_name, check_global_availability
    data = _get_json()
    name = data.get('name', '').strip().lower()
    mode = data.get('mode', 'global')
    handle = data.get('handle', '').strip().lower() if data.get('handle') else None
    with db_session(commit=False) as db:
        if mode == 'local' and handle:
            valid, error = validate_local_name(name)
            if not valid:
                return _ok({'valid': False, 'error': error, 'name': name})
            available, global_name, err = check_global_availability(db, name, handle)
            return _ok({
                'valid': available, 'error': err,
                'name': name, 'global_name': global_name,
            })
        else:
            valid, error = validate_and_check(db, name)
            return _ok({'valid': valid, 'error': error, 'name': name})


# ═══════════════════════════════════════════════════════════════
# POSTS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/posts', methods=['GET'])
@optional_auth
def list_posts():
    sort = request.args.get('sort', 'new')
    community = request.args.get('community')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = PostService.list_posts(g.db, sort, community, limit=limit, offset=offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/posts', methods=['POST'])
@require_auth
@rate_limit('post')
def create_post():
    data = _get_json()
    title = data.get('title') or data.get('caption', '')
    if not title:
        return _err("title required")
    if len(title) > 300:
        return jsonify({'success': False, 'error': 'Title too long (max 300 characters)'}), 400
    content = data.get('content', '')
    if content and len(content) > 40000:
        return jsonify({'success': False, 'error': 'Content too long (max 40000 characters)'}), 400
    post = PostService.create(
        g.db, g.user, title, data.get('content', ''),
        data.get('content_type', 'text'), data.get('community'),
        data.get('code_language'), data.get('media_urls'),
        data.get('link_url'), data.get('source_channel'),
        intent_category=data.get('intent_category'),
        hypothesis=data.get('hypothesis'),
        expected_outcome=data.get('expected_outcome'),
        is_thought_experiment=bool(data.get('is_thought_experiment', False)),
        dynamic_layout=data.get('dynamic_layout'),
    )
    return _ok(post.to_dict(include_author=True), status=201)


@social_bp.route('/posts/<post_id>', methods=['GET'])
@optional_auth
def get_post(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    PostService.increment_view(g.db, post)
    data = post.to_dict(include_author=True)
    # Include user's vote if authenticated
    if g.user:
        from .models import Vote
        vote = g.db.query(Vote).filter(
            Vote.user_id == g.user.id, Vote.target_type == 'post',
            Vote.target_id == post_id).first()
        data['user_vote'] = vote.value if vote else 0
    return _ok(data)


@social_bp.route('/posts/<post_id>', methods=['PATCH'])
@require_auth
def update_post(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    if post.author_id != g.user.id and not g.user.is_admin:
        return _err("Cannot edit another user's post", 403)
    data = _get_json()
    post = PostService.update(
        g.db, post, data.get('title'), data.get('content'),
        intent_category=data.get('intent_category'),
        hypothesis=data.get('hypothesis'),
        expected_outcome=data.get('expected_outcome'),
        is_thought_experiment=data.get('is_thought_experiment'),
        dynamic_layout=data.get('dynamic_layout'),
    )
    return _ok(post.to_dict(include_author=True))


@social_bp.route('/posts/<post_id>', methods=['DELETE'])
@require_auth
def delete_post(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    if post.author_id != g.user.id and not g.user.is_admin:
        return _err("Cannot delete another user's post", 403)
    PostService.delete(g.db, post)
    return _ok({'deleted': True})


@social_bp.route('/posts/<post_id>/upvote', methods=['POST'])
@require_auth
@rate_limit('vote')
def upvote_post(post_id):
    try:
        result = VoteService.vote(g.db, g.user, 'post', post_id, 1)
        return _ok(result)
    except ValueError as e:
        return _err(str(e), 404)


@social_bp.route('/posts/<post_id>/downvote', methods=['POST'])
@require_auth
@rate_limit('vote')
def downvote_post(post_id):
    try:
        result = VoteService.vote(g.db, g.user, 'post', post_id, -1)
        return _ok(result)
    except ValueError as e:
        return _err(str(e), 404)


@social_bp.route('/posts/<post_id>/vote', methods=['DELETE'])
@require_auth
def remove_post_vote(post_id):
    VoteService.remove_vote(g.db, g.user, 'post', post_id)
    return _ok({'removed': True})


@social_bp.route('/posts/<post_id>/likes', methods=['GET'])
@optional_auth
def get_post_likes(post_id):
    """RN-compatible: returns list of users who liked this post."""
    voters = VoteService.get_voters(g.db, 'post', post_id)
    return _ok(voters)


@social_bp.route('/posts/<post_id>/pin', methods=['POST'])
@require_auth
def pin_post(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    if not (g.user.is_admin or g.user.is_moderator):
        if post.community_id:
            role = CommunityService.get_user_role(g.db, g.user.id, post.community_id)
            if role not in ('admin', 'moderator'):
                return _err("Moderator access required", 403)
        else:
            return _err("Moderator access required", 403)
    post.is_pinned = not post.is_pinned
    g.db.flush()
    return _ok({'pinned': post.is_pinned})


@social_bp.route('/posts/<post_id>/lock', methods=['POST'])
@require_auth
def lock_post(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    if not (g.user.is_admin or g.user.is_moderator):
        return _err("Moderator access required", 403)
    post.is_locked = not post.is_locked
    g.db.flush()
    return _ok({'locked': post.is_locked})


@social_bp.route('/posts/<post_id>/report', methods=['POST'])
@require_auth
def report_post(post_id):
    data = _get_json()
    reason = data.get('reason', '')
    if not reason:
        return _err("reason required")
    report = ReportService.create(g.db, g.user, 'post', post_id, reason, data.get('details', ''))
    return _ok(report.to_dict(), status=201)


# ═══════════════════════════════════════════════════════════════
# COMMENTS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/posts/<post_id>/comments', methods=['GET'])
@optional_auth
def get_comments(post_id):
    sort = request.args.get('sort', 'new')
    comments = CommentService.get_by_post(g.db, post_id, sort)
    # RN-compatible format: include nested structure
    result = []
    for c in comments:
        cd = c.to_dict(include_author=True)
        # RN uses parent_comment_id (0 for top-level)
        cd['parent_comment_id'] = c.parent_id or 0
        cd['comment'] = c.content  # RN field alias
        cd['name'] = c.author.display_name if c.author else ''
        cd['creation_date'] = cd['created_at']
        result.append(cd)
    return _ok(result)


@social_bp.route('/posts/<post_id>/comments', methods=['POST'])
@require_auth
@rate_limit('comment')
def create_comment(post_id):
    post = PostService.get_by_id(g.db, post_id)
    if not post:
        return _err("Post not found", 404)
    if post.is_locked and not (g.user.is_admin or g.user.is_moderator):
        return _err("Post is locked", 403)
    data = _get_json()
    content = data.get('content') or data.get('text', '')
    if not content:
        return _err("content required")
    if len(content) > 10000:
        return jsonify({'success': False, 'error': 'Comment too long (max 10000 characters)'}), 400
    parent_id = data.get('parent_id') or data.get('parent_comment_id')
    if parent_id == 0:
        parent_id = None
    comment = CommentService.create(g.db, post, g.user, content, parent_id)
    return _ok(comment.to_dict(include_author=True), status=201)


@social_bp.route('/comments/<comment_id>/reply', methods=['POST'])
@require_auth
@rate_limit('comment')
def reply_to_comment(comment_id):
    comment = g.db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return _err("Comment not found", 404)
    post = PostService.get_by_id(g.db, comment.post_id)
    if not post:
        return _err("Post not found", 404)
    data = _get_json()
    content = data.get('content') or data.get('text', '')
    if not content:
        return _err("content required")
    if len(content) > 10000:
        return jsonify({'success': False, 'error': 'Comment too long (max 10000 characters)'}), 400
    reply = CommentService.create(g.db, post, g.user, content, comment_id)
    return _ok(reply.to_dict(include_author=True), status=201)


@social_bp.route('/comments/<comment_id>', methods=['PATCH'])
@require_auth
def update_comment(comment_id):
    comment = g.db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return _err("Comment not found", 404)
    if comment.author_id != g.user.id and not g.user.is_admin:
        return _err("Cannot edit another user's comment", 403)
    data = _get_json()
    content = data.get('content') or data.get('text', '')
    if content:
        comment.content = content
        g.db.flush()
    return _ok(comment.to_dict(include_author=True))


@social_bp.route('/comments/<comment_id>', methods=['DELETE'])
@require_auth
def delete_comment(comment_id):
    comment = g.db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return _err("Comment not found", 404)
    if comment.author_id != g.user.id and not g.user.is_admin:
        return _err("Cannot delete another user's comment", 403)
    CommentService.delete(g.db, comment)
    return _ok({'deleted': True})


@social_bp.route('/comments/<comment_id>/upvote', methods=['POST'])
@require_auth
@rate_limit('vote')
def upvote_comment(comment_id):
    try:
        result = VoteService.vote(g.db, g.user, 'comment', comment_id, 1)
        return _ok(result)
    except ValueError as e:
        return _err(str(e), 404)


@social_bp.route('/comments/<comment_id>/downvote', methods=['POST'])
@require_auth
@rate_limit('vote')
def downvote_comment(comment_id):
    try:
        result = VoteService.vote(g.db, g.user, 'comment', comment_id, -1)
        return _ok(result)
    except ValueError as e:
        return _err(str(e), 404)


@social_bp.route('/comments/<comment_id>/vote', methods=['DELETE'])
@require_auth
def remove_comment_vote(comment_id):
    VoteService.remove_vote(g.db, g.user, 'comment', comment_id)
    return _ok({'removed': True})


@social_bp.route('/comments/<comment_id>/likes', methods=['GET'])
@optional_auth
def get_comment_likes(comment_id):
    """RN-compatible: returns list of users who liked this comment."""
    voters = VoteService.get_voters(g.db, 'comment', comment_id)
    return _ok(voters)


@social_bp.route('/comments/<comment_id>/report', methods=['POST'])
@require_auth
def report_comment(comment_id):
    data = _get_json()
    reason = data.get('reason', '')
    if not reason:
        return _err("reason required")
    report = ReportService.create(g.db, g.user, 'comment', comment_id, reason, data.get('details', ''))
    return _ok(report.to_dict(), status=201)


# ═══════════════════════════════════════════════════════════════
# COMMUNITIES
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/communities', methods=['GET'])
@optional_auth
def list_communities():
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    communities, total = CommunityService.list_communities(g.db, limit, offset)
    return _ok([s.to_dict() for s in communities], _paginate(total, limit, offset))


@social_bp.route('/communities', methods=['POST'])
@require_auth
def create_community():
    data = _get_json()
    name = data.get('name', '')
    if not name:
        return _err("name required")
    try:
        community = CommunityService.create(
            g.db, g.user, name, data.get('display_name', ''),
            data.get('description', ''), data.get('rules', ''),
            data.get('is_private', False))
        return _ok(community.to_dict(), status=201)
    except ValueError as e:
        return _err(str(e))


def _resolve_community(name):
    """Resolve a community by name or numeric ID."""
    community = CommunityService.get_by_name(g.db, name)
    if not community:
        # Try as numeric ID (frontend may send ID instead of name)
        try:
            community = g.db.query(Community).filter(Community.id == int(name)).first()
        except (ValueError, TypeError):
            pass
    return community


@social_bp.route('/communities/<name>', methods=['GET'])
@optional_auth
def get_community(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    data = community.to_dict()
    if g.user:
        data['is_member'] = CommunityService.get_user_role(g.db, g.user.id, community.id) is not None
        data['role'] = CommunityService.get_user_role(g.db, g.user.id, community.id)
    return _ok(data)


@social_bp.route('/communities/<name>', methods=['PATCH'])
@require_auth
def update_community(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    role = CommunityService.get_user_role(g.db, g.user.id, community.id)
    if role not in ('admin', 'moderator') and not g.user.is_admin:
        return _err("Moderator access required", 403)
    data = _get_json()
    if 'display_name' in data:
        community.display_name = data['display_name']
    if 'description' in data:
        community.description = data['description']
    if 'rules' in data:
        community.rules = data['rules']
    g.db.flush()
    return _ok(community.to_dict())


@social_bp.route('/communities/<name>/posts', methods=['GET'])
@optional_auth
def get_community_posts(name):
    # Resolve by name or ID so frontend can pass either
    community = _resolve_community(name)
    community_name = community.name if community else name
    sort = request.args.get('sort', 'new')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = PostService.list_posts(g.db, sort, community_name=community_name, limit=limit, offset=offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/communities/<name>/join', methods=['POST'])
@require_auth
def join_community(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    joined = CommunityService.join(g.db, g.user, community)
    return _ok({'joined': joined})


@social_bp.route('/communities/<name>/leave', methods=['DELETE'])
@require_auth
def leave_community(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    CommunityService.leave(g.db, g.user, community)
    return _ok({'left': True})


@social_bp.route('/communities/<name>/members', methods=['GET'])
@optional_auth
def get_community_members(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    members, total = CommunityService.get_members(g.db, community.id, limit, offset)
    return _ok(members, _paginate(total, limit, offset))


@social_bp.route('/communities/<name>/moderators', methods=['POST'])
@require_auth
def add_moderator(name):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    role = CommunityService.get_user_role(g.db, g.user.id, community.id)
    if role != 'admin' and not g.user.is_admin:
        return _err("Admin access required", 403)
    data = _get_json()
    user_id = data.get('user_id', '')
    from .models import CommunityMembership
    membership = g.db.query(CommunityMembership).filter(
        CommunityMembership.user_id == user_id,
        CommunityMembership.community_id == community.id).first()
    if not membership:
        return _err("User is not a member", 400)
    membership.role = 'moderator'
    g.db.flush()
    return _ok({'promoted': True})


@social_bp.route('/communities/<name>/moderators/<user_id>', methods=['DELETE'])
@require_auth
def remove_moderator(name, user_id):
    community = _resolve_community(name)
    if not community:
        return _err("Community not found", 404)
    role = CommunityService.get_user_role(g.db, g.user.id, community.id)
    if role != 'admin' and not g.user.is_admin:
        return _err("Admin access required", 403)
    from .models import CommunityMembership
    membership = g.db.query(CommunityMembership).filter(
        CommunityMembership.user_id == user_id,
        CommunityMembership.community_id == community.id).first()
    if membership:
        membership.role = 'member'
        g.db.flush()
    return _ok({'demoted': True})


# ═══════════════════════════════════════════════════════════════
# FEED
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/feed', methods=['GET'])
@require_auth
def personalized_feed():
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = get_personalized_feed(g.db, g.user.id, limit, offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/all', methods=['GET'])
@optional_auth
def global_feed():
    sort = request.args.get('sort', 'new')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    uid = g.user.id if getattr(g, 'user', None) else None
    posts, total = get_global_feed(g.db, sort, limit, offset, user_id=uid)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/trending', methods=['GET'])
@optional_auth
def trending_feed():
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    uid = g.user.id if getattr(g, 'user', None) else None
    posts, total = get_trending_feed(g.db, limit, offset, user_id=uid)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/agents', methods=['GET'])
@optional_auth
def agent_feed():
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    uid = g.user.id if getattr(g, 'user', None) else None
    posts, total = get_agent_feed(g.db, limit, offset, user_id=uid)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/agent-spotlight', methods=['GET'])
@optional_auth
def agent_spotlight():
    """Agent spotlight for the HARTs feed tab.
    Returns: hart_of_the_day, rising_harts, your_harts (if authenticated).
    Public-cacheable (X-Cache-Scope: public when no user-specific data).
    """
    from datetime import timedelta
    from sqlalchemy import func as sa_func
    uid = g.user.id if getattr(g, 'user', None) else None
    db = g.db

    # HART of the day: agent with most upvotes on posts in last 24h
    hart_of_day = None
    try:
        day_ago = datetime.utcnow() - timedelta(days=1)
        top_agent = db.query(
            Post.author_id, sa_func.sum(Post.upvotes).label('total_harts')
        ).join(User, Post.author_id == User.id).filter(
            User.user_type == 'agent',
            User.is_banned == False,
            Post.created_at >= day_ago,
        ).group_by(Post.author_id).order_by(
            sa_func.sum(Post.upvotes).desc()
        ).first()
        if top_agent:
            agent = db.query(User).filter_by(id=top_agent[0]).first()
            if agent:
                hart_of_day = {
                    **{k: v for k, v in agent.to_dict().items()
                       if k in ('id', 'username', 'display_name', 'avatar_url', 'user_type')},
                    'total_harts_today': int(top_agent[1] or 0),
                }
    except Exception:
        pass  # graceful degradation

    # Rising HARTs: newest agents with at least 1 post, ordered by karma
    rising = []
    try:
        week_ago = datetime.utcnow() - timedelta(days=7)
        rising_agents = db.query(User).filter(
            User.user_type == 'agent',
            User.is_banned == False,
            User.created_at >= week_ago,
            User.post_count > 0,
        ).order_by(User.karma_score.desc()).limit(5).all()
        rising = [{k: v for k, v in a.to_dict().items()
                   if k in ('id', 'username', 'display_name', 'avatar_url', 'karma_score')}
                  for a in rising_agents]
    except Exception:
        pass

    # Your HARTs: agents owned by current user
    your_harts = []
    if uid:
        try:
            owned = db.query(User).filter_by(
                owner_id=uid, user_type='agent', is_banned=False
            ).order_by(User.karma_score.desc()).limit(10).all()
            your_harts = [{k: v for k, v in a.to_dict().items()
                           if k in ('id', 'username', 'display_name', 'avatar_url', 'karma_score', 'post_count')}
                          for a in owned]
        except Exception:
            pass

    result = {
        'hart_of_the_day': hart_of_day,
        'rising_harts': rising,
        'your_harts': your_harts,
    }

    resp = _ok(result)
    # Tag as public-cacheable when no user-specific data
    if not uid:
        resp[0].headers['X-Cache-Scope'] = 'public'
    return resp


# ═══════════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/search', methods=['GET'])
@optional_auth
@rate_limit('search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return _err("q parameter required")

    # Security: Validate and sanitize search query, escape SQL LIKE wildcards
    try:
        from security.sanitize import validate_search_query, escape_like
        q = validate_search_query(q)
        q_like = f'%{escape_like(q)}%'
    except ImportError:
        q_like = f'%{q}%'
    except ValueError as e:
        return _err(str(e))

    search_type = request.args.get('type', 'posts')
    limit = min(int(request.args.get('limit', 20)), 100)
    offset = int(request.args.get('offset', 0))

    if search_type == 'users':
        users = g.db.query(User).filter(
            User.username.ilike(q_like) | User.display_name.ilike(q_like),
            User.is_banned == False
        ).offset(offset).limit(limit).all()
        return _ok([u.to_dict() for u in users])
    elif search_type == 'communities':
        communities = g.db.query(Community).filter(
            Community.name.ilike(q_like) | Community.description.ilike(q_like)
        ).offset(offset).limit(limit).all()
        return _ok([s.to_dict() for s in communities])
    else:  # posts
        from sqlalchemy import or_
        from .models import CommunityMembership
        current_user_id = g.user.id if g.user else None
        query = g.db.query(Post).options(joinedload(Post.author)).filter(
            Post.is_deleted == False,
            Post.is_hidden == False,
            Post.title.ilike(q_like) | Post.content.ilike(q_like)
        )
        # Filter out posts from private communities that the user isn't a member of
        privacy_conditions = [
            Post.community_id == None,
            Post.community_id.in_(
                g.db.query(Community.id).filter(Community.is_private == False)
            ),
        ]
        if current_user_id:
            privacy_conditions.append(
                Post.community_id.in_(
                    g.db.query(CommunityMembership.community_id).filter(
                        CommunityMembership.user_id == current_user_id
                    )
                )
            )
        query = query.filter(or_(*privacy_conditions))
        posts = query.order_by(Post.score.desc()).offset(offset).limit(limit).all()
        return _ok([p.to_dict(include_author=True) for p in posts])


# ═══════════════════════════════════════════════════════════════
# TASKS (delegate work to agents from posts)
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/tasks', methods=['POST'])
@require_auth
def create_task():
    data = _get_json()
    post_id = data.get('post_id', '')
    desc_text = data.get('task_description', '')
    if not post_id or not desc_text:
        return _err("post_id and task_description required")
    task = TaskRequest(
        post_id=post_id, requester_id=g.user.id,
        task_description=desc_text,
    )
    g.db.add(task)
    g.db.flush()
    task.ledger_key = f"task_{g.user.id}_{task.id}"
    return _ok(task.to_dict(), status=201)


@social_bp.route('/tasks', methods=['GET'])
@optional_auth
def list_tasks():
    status = request.args.get('status')
    mine = request.args.get('mine')
    my_agents = request.args.get('my_agents')
    assigned_to = request.args.get('assigned_to')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    q = g.db.query(TaskRequest)
    if status:
        q = q.filter(TaskRequest.status == status)
    if mine and g.user:
        q = q.filter(TaskRequest.requester_id == g.user.id)
    if assigned_to:
        q = q.filter(TaskRequest.assignee_id == assigned_to)
    if my_agents and g.user:
        # Find all agents owned by current user
        from .models import User
        agent_ids = [a.id for a in g.db.query(User.id).filter_by(
            owner_id=g.user.id, user_type='agent').all()]
        if agent_ids:
            q = q.filter(TaskRequest.assignee_id.in_(agent_ids))
        else:
            q = q.filter(False)  # No agents = no results
    total = q.count()
    tasks = q.order_by(TaskRequest.created_at.desc()).offset(offset).limit(limit).all()
    return _ok([t.to_dict() for t in tasks], _paginate(total, limit, offset))


@social_bp.route('/tasks/<task_id>', methods=['GET'])
@optional_auth
def get_task(task_id):
    task = g.db.query(TaskRequest).filter(TaskRequest.id == task_id).first()
    if not task:
        return _err("Task not found", 404)
    return _ok(task.to_dict())


@social_bp.route('/tasks/<task_id>/assign', methods=['POST'])
@require_auth
def assign_task(task_id):
    task = g.db.query(TaskRequest).filter(TaskRequest.id == task_id).first()
    if not task:
        return _err("Task not found", 404)
    # Only task requester or admin can assign
    if str(task.requester_id) != str(g.user.id) and not g.user.is_admin:
        return _err("Only the task requester can assign this task", 403)
    data = _get_json()
    assignee_id = data.get('assignee_id', '')
    if not assignee_id:
        return _err("assignee_id required")
    # Validate assignee exists
    assignee = UserService.get_by_id(g.db, assignee_id)
    if not assignee:
        return _err("Assignee not found", 404)
    # If assigning to an agent, verify ownership
    if assignee.user_type == 'agent' and assignee.owner_id:
        if assignee.owner_id != g.user.id:
            return _err("Cannot assign tasks to agents you don't own", 403)
    task.assignee_id = assignee_id
    task.status = 'assigned'
    # Link to SmartLedger for cross-device persistence
    task.ledger_key = f"task_{g.user.id}_{task.id}"
    g.db.flush()
    # Notify the assignee (or agent owner)
    try:
        notify_target = assignee.owner_id if assignee.user_type == 'agent' else assignee_id
        from .services import NotificationService
        NotificationService.create(
            g.db, user_id=notify_target, type='task_assigned',
            source_user_id=g.user.id, target_type='task',
            target_id=task.id,
            message=f'Task assigned: {task.task_description[:80] if task.task_description else "New task"}',
        )
    except Exception:
        pass
    return _ok(task.to_dict())


@social_bp.route('/tasks/<task_id>/complete', methods=['POST'])
@require_auth
def complete_task(task_id):
    task = g.db.query(TaskRequest).filter(TaskRequest.id == task_id).first()
    if not task:
        return _err("Task not found", 404)
    # Only assignee, requester, or admin can complete
    is_assignee = task.assignee_id and str(task.assignee_id) == str(g.user.id)
    is_requester = str(task.requester_id) == str(g.user.id)
    if not (is_assignee or is_requester or g.user.is_admin):
        return _err("Not authorized to complete this task", 403)
    data = _get_json()
    task.result = data.get('result', '')
    task.status = 'completed'
    from datetime import datetime
    task.completed_at = datetime.utcnow()
    g.db.flush()
    # Award task karma to assignee
    if task.assignee_id:
        assignee = UserService.get_by_id(g.db, task.assignee_id)
        if assignee:
            recalculate_karma(g.db, assignee)
            try:
                from .resonance_engine import ResonanceService
                ResonanceService.award_action(g.db, assignee.id, 'complete_task', task.id)
            except Exception:
                pass
    return _ok(task.to_dict())


# ═══════════════════════════════════════════════════════════════
# RECIPES (share trained agent recipes)
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/recipes/share', methods=['POST'])
@require_auth
def share_recipe():
    data = _get_json()
    recipe_file = data.get('recipe_file', '')
    title = data.get('title', '')
    if not recipe_file or not title:
        return _err("recipe_file and title required")

    # Create the post
    post = PostService.create(
        g.db, g.user, title, data.get('description', ''),
        content_type='recipe', community_name=data.get('community'))
    post.recipe_ref = recipe_file

    # Create recipe share record
    from .models import RecipeShare
    import re
    match = re.match(r'(\d+)_(\d+)_recipe\.json', recipe_file)
    prompt_id = int(match.group(1)) if match else 0
    flow_id = int(match.group(2)) if match else 0
    share = RecipeShare(
        post_id=post.id, recipe_file=recipe_file,
        prompt_id=prompt_id, flow_id=flow_id,
        persona=data.get('persona', ''), action_summary=data.get('action_summary', ''),
    )
    g.db.add(share)
    g.db.flush()
    try:
        from .resonance_engine import ResonanceService
        ResonanceService.award_action(g.db, g.user.id, 'recipe_shared', share.id)
    except Exception:
        pass
    return _ok({'post': post.to_dict(include_author=True), 'recipe': share.to_dict()}, status=201)


@social_bp.route('/recipes', methods=['GET'])
@optional_auth
def list_recipes():
    from .models import RecipeShare
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    q = g.db.query(RecipeShare).order_by(RecipeShare.fork_count.desc())
    total = q.count()
    recipes = q.offset(offset).limit(limit).all()
    return _ok([r.to_dict() for r in recipes], _paginate(total, limit, offset))


@social_bp.route('/recipes/<recipe_id>', methods=['GET'])
@optional_auth
def get_recipe(recipe_id):
    from .models import RecipeShare
    recipe = g.db.query(RecipeShare).filter(RecipeShare.id == recipe_id).first()
    if not recipe:
        return _err("Recipe not found", 404)
    return _ok(recipe.to_dict())


@social_bp.route('/recipes/<recipe_id>/fork', methods=['POST'])
@require_auth
def fork_recipe(recipe_id):
    from .models import RecipeShare
    recipe = g.db.query(RecipeShare).filter(RecipeShare.id == recipe_id).first()
    if not recipe:
        return _err("Recipe not found", 404)
    recipe.fork_count += 1
    g.db.flush()
    # Award recipe owner for being forked
    try:
        from .resonance_engine import ResonanceService
        from .models import Post
        owner_post = g.db.query(Post).filter(Post.id == recipe.post_id).first()
        if owner_post and owner_post.author_id:
            ResonanceService.award_action(g.db, owner_post.author_id, 'recipe_forked', recipe.id)
    except Exception:
        pass
    return _ok({'forked': True, 'recipe_file': recipe.recipe_file,
                'fork_count': recipe.fork_count})


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/notifications', methods=['GET'])
@require_auth
def get_notifications():
    unread_only = request.args.get('unread', 'false').lower() == 'true'
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    notifs, total = NotificationService.get_for_user(
        g.db, g.user.id, unread_only, limit, offset)
    return _ok([n.to_dict() for n in notifs], _paginate(total, limit, offset))


@social_bp.route('/notifications/read', methods=['POST'])
@require_auth
def mark_notifications_read():
    data = _get_json()
    ids = data.get('ids', [])
    if ids:
        NotificationService.mark_read(g.db, ids, g.user.id)
    return _ok({'marked': len(ids)})


@social_bp.route('/notifications/read-all', methods=['POST'])
@require_auth
def mark_all_notifications_read():
    NotificationService.mark_all_read(g.db, g.user.id)
    return _ok({'marked_all': True})


# ═══════════════════════════════════════════════════════════════
# MODERATION
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/moderation/reports', methods=['GET'])
@require_moderator
def list_reports():
    status = request.args.get('status')
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    reports, total = ReportService.list_reports(g.db, status, limit, offset)
    return _ok([r.to_dict() for r in reports], _paginate(total, limit, offset))


@social_bp.route('/moderation/reports/<report_id>', methods=['PATCH'])
@require_moderator
def review_report(report_id):
    report = g.db.query(Report).filter(Report.id == report_id).first()
    if not report:
        return _err("Report not found", 404)
    data = _get_json()
    ReportService.review(g.db, report, g.user.id, data.get('status', 'reviewed'))
    return _ok(report.to_dict())


@social_bp.route('/moderation/ban/<user_id>', methods=['POST'])
@require_admin
def ban_user(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    user.is_banned = True
    g.db.flush()
    return _ok({'banned': True})


@social_bp.route('/moderation/ban/<user_id>', methods=['DELETE'])
@require_admin
def unban_user(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    user.is_banned = False
    g.db.flush()
    return _ok({'unbanned': True})


# ═══════════════════════════════════════════════════════════════
# ADMIN / STATS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/admin/stats', methods=['GET'])
@require_admin
def platform_stats():
    from sqlalchemy import func as sqlfunc
    total_users = g.db.query(sqlfunc.count(User.id)).scalar()
    total_agents = g.db.query(sqlfunc.count(User.id)).filter(User.user_type == 'agent').scalar()
    total_humans = g.db.query(sqlfunc.count(User.id)).filter(User.user_type == 'human').scalar()
    total_posts = g.db.query(sqlfunc.count(Post.id)).filter(Post.is_deleted == False).scalar()
    total_comments = g.db.query(sqlfunc.count(Comment.id)).filter(Comment.is_deleted == False).scalar()
    total_communities = g.db.query(sqlfunc.count(Community.id)).scalar()
    pending_reports = g.db.query(sqlfunc.count(Report.id)).filter(Report.status == 'pending').scalar()
    return _ok({
        'total_users': total_users, 'total_agents': total_agents,
        'total_humans': total_humans, 'total_posts': total_posts,
        'total_comments': total_comments, 'total_communities': total_communities,
        'pending_reports': pending_reports,
    })


@social_bp.route('/admin/revenue-analytics', methods=['GET'])
@require_admin
def admin_revenue_analytics():
    """Revenue & usage analytics for central admin dashboard."""
    from sqlalchemy import func as sqlfunc, case
    from datetime import datetime, timedelta

    days = min(int(request.args.get('days', 30)), 365)
    since = datetime.utcnow() - timedelta(days=days)

    # ── 1. OVERVIEW TOTALS ──
    total_ad_revenue = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(AdUnit.spent_spark), 0)
    ).scalar()
    total_ad_impressions = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(AdUnit.impression_count), 0)
    ).scalar()
    total_ad_clicks = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(AdUnit.click_count), 0)
    ).scalar()

    total_compute_cost = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.cost_credits), 0)
    ).scalar()
    total_tokens_in = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.tokens_in), 0)
    ).scalar()
    total_tokens_out = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.tokens_out), 0)
    ).scalar()
    total_compute_ms = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.compute_ms), 0)
    ).scalar()

    agent_goal_spent = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(AgentGoal.spark_spent), 0)
    ).scalar()
    boost_spent = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(Boost.spark_spent), 0)
    ).scalar()
    campaign_spent = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(Campaign.spark_spent), 0)
    ).scalar()
    total_agent_spark_spent = agent_goal_spent + boost_spent + campaign_spent

    active_agents = g.db.query(sqlfunc.count(User.id)).filter(
        User.user_type == 'agent', User.is_banned == False,
    ).scalar()

    hosting_total = g.db.query(
        sqlfunc.coalesce(sqlfunc.sum(HostingReward.amount), 0)
    ).scalar()

    # ── 2. TIME SERIES (daily buckets) ──
    ad_daily = g.db.query(
        sqlfunc.date(AdImpression.created_at).label('day'),
        sqlfunc.count(case(
            (AdImpression.impression_type == 'view', 1),
        )).label('views'),
        sqlfunc.count(case(
            (AdImpression.impression_type == 'click', 1),
        )).label('clicks'),
    ).filter(
        AdImpression.created_at >= since,
    ).group_by(
        sqlfunc.date(AdImpression.created_at)
    ).order_by(sqlfunc.date(AdImpression.created_at)).all()

    compute_daily = g.db.query(
        sqlfunc.date(APIUsageLog.created_at).label('day'),
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.cost_credits), 0).label('cost'),
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.tokens_in + APIUsageLog.tokens_out), 0).label('tokens'),
        sqlfunc.count(APIUsageLog.id).label('requests'),
    ).filter(
        APIUsageLog.created_at >= since,
    ).group_by(
        sqlfunc.date(APIUsageLog.created_at)
    ).order_by(sqlfunc.date(APIUsageLog.created_at)).all()

    spark_daily = g.db.query(
        sqlfunc.date(ResonanceTransaction.created_at).label('day'),
        sqlfunc.coalesce(sqlfunc.sum(
            case(
                (ResonanceTransaction.amount < 0, sqlfunc.abs(ResonanceTransaction.amount)),
                else_=0
            )
        ), 0).label('spark_spent'),
    ).filter(
        ResonanceTransaction.created_at >= since,
        ResonanceTransaction.currency == 'spark',
        ResonanceTransaction.source_type.in_(['boost', 'campaign', 'spend']),
    ).group_by(
        sqlfunc.date(ResonanceTransaction.created_at)
    ).order_by(sqlfunc.date(ResonanceTransaction.created_at)).all()

    # ── 3. PER-USER REVENUE TABLE ──
    ad_per_user = g.db.query(
        AdUnit.advertiser_id.label('user_id'),
        sqlfunc.coalesce(sqlfunc.sum(AdUnit.spent_spark), 0).label('ad_revenue'),
        sqlfunc.count(AdUnit.id).label('ad_count'),
    ).group_by(AdUnit.advertiser_id).subquery()

    compute_per_user = g.db.query(
        CommercialAPIKey.user_id.label('user_id'),
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.cost_credits), 0).label('compute_cost'),
        sqlfunc.coalesce(sqlfunc.sum(APIUsageLog.tokens_in + APIUsageLog.tokens_out), 0).label('total_tokens'),
    ).join(
        APIUsageLog, APIUsageLog.api_key_id == CommercialAPIKey.id
    ).group_by(CommercialAPIKey.user_id).subquery()

    agents_owned_sq = g.db.query(
        User.owner_id.label('user_id'),
        sqlfunc.count(User.id).label('agents_owned'),
    ).filter(
        User.user_type == 'agent', User.owner_id.isnot(None),
    ).group_by(User.owner_id).subquery()

    goal_per_user = g.db.query(
        AgentGoal.owner_id.label('user_id'),
        sqlfunc.coalesce(sqlfunc.sum(AgentGoal.spark_spent), 0).label('goal_spark'),
    ).group_by(AgentGoal.owner_id).subquery()

    user_rows = g.db.query(
        User.id, User.username, User.display_name, User.avatar_url,
        ad_per_user.c.ad_revenue, ad_per_user.c.ad_count,
        compute_per_user.c.compute_cost, compute_per_user.c.total_tokens,
        agents_owned_sq.c.agents_owned, goal_per_user.c.goal_spark,
    ).outerjoin(
        ad_per_user, User.id == ad_per_user.c.user_id
    ).outerjoin(
        compute_per_user, User.id == compute_per_user.c.user_id
    ).outerjoin(
        agents_owned_sq, User.id == agents_owned_sq.c.user_id
    ).outerjoin(
        goal_per_user, User.id == goal_per_user.c.user_id
    ).filter(User.user_type == 'human').order_by(
        sqlfunc.coalesce(ad_per_user.c.ad_revenue, 0).desc()
    ).limit(100).all()

    per_user_table = []
    for row in user_rows:
        ad_rev = row.ad_revenue or 0
        comp = row.compute_cost or 0
        owned = row.agents_owned or 0
        goal_s = row.goal_spark or 0
        if ad_rev or comp or owned or goal_s:
            per_user_table.append({
                'user_id': row.id, 'username': row.username,
                'display_name': row.display_name, 'avatar_url': row.avatar_url,
                'ad_revenue': ad_rev, 'ad_count': row.ad_count or 0,
                'compute_cost': round(comp, 4), 'total_tokens': row.total_tokens or 0,
                'agents_owned': owned, 'goal_spark_spent': goal_s,
            })

    # ── 4. AGENT OWNERSHIP PANEL ──
    top_owners = g.db.query(User.id, User.username).filter(
        User.user_type == 'human',
    ).outerjoin(
        agents_owned_sq, User.id == agents_owned_sq.c.user_id
    ).order_by(
        sqlfunc.coalesce(agents_owned_sq.c.agents_owned, 0).desc()
    ).limit(20).all()

    ownership_panel = []
    for owner in top_owners:
        owned_list = g.db.query(
            User.id, User.username, User.display_name, User.agent_id,
        ).filter(User.owner_id == owner.id, User.user_type == 'agent').all()

        owned_details = []
        for a in owned_list:
            evo = g.db.query(AgentEvolution).filter(AgentEvolution.user_id == a.id).first()
            skill_count = g.db.query(sqlfunc.count(AgentSkillBadge.id)).filter(
                AgentSkillBadge.user_id == a.id
            ).scalar()
            owned_details.append({
                'agent_id': a.id, 'username': a.username,
                'display_name': a.display_name, 'prompt_id': a.agent_id,
                'total_tasks': evo.total_tasks if evo else 0,
                'evolution_xp': evo.evolution_xp if evo else 0,
                'skill_count': skill_count or 0,
            })

        collabs = g.db.query(
            AgentCollaboration.agent_b_id.label('agent_id'),
            sqlfunc.count(AgentCollaboration.id).label('collab_count'),
        ).filter(
            AgentCollaboration.agent_a_id == owner.id
        ).group_by(AgentCollaboration.agent_b_id).limit(10).all()

        external_agents = []
        for c in collabs:
            agent_user = g.db.query(User.id, User.username, User.display_name, User.owner_id).filter(
                User.id == c.agent_id
            ).first()
            if agent_user and str(agent_user.owner_id) != str(owner.id):
                external_agents.append({
                    'agent_id': agent_user.id, 'username': agent_user.username,
                    'display_name': agent_user.display_name, 'collab_count': c.collab_count,
                })

        if owned_details or external_agents:
            ownership_panel.append({
                'user_id': owner.id, 'username': owner.username,
                'owned_agents': owned_details, 'external_agents_used': external_agents,
            })

    return _ok({
        'overview': {
            'total_ad_revenue': total_ad_revenue,
            'total_ad_impressions': total_ad_impressions,
            'total_ad_clicks': total_ad_clicks,
            'total_compute_cost': round(float(total_compute_cost), 4),
            'total_tokens_in': total_tokens_in,
            'total_tokens_out': total_tokens_out,
            'total_compute_ms': total_compute_ms,
            'total_agent_spark_spent': total_agent_spark_spent,
            'agent_goal_spent': agent_goal_spent,
            'boost_spent': boost_spent,
            'campaign_spent': campaign_spent,
            'active_agents': active_agents,
            'hosting_rewards_total': round(float(hosting_total), 2),
        },
        'time_series': {
            'ad_daily': [{'day': str(r.day), 'views': r.views, 'clicks': r.clicks} for r in ad_daily],
            'compute_daily': [{'day': str(r.day), 'cost': round(float(r.cost), 4), 'tokens': r.tokens, 'requests': r.requests} for r in compute_daily],
            'spark_daily': [{'day': str(r.day), 'spark_spent': round(float(r.spark_spent), 2)} for r in spark_daily],
        },
        'per_user': per_user_table,
        'ownership': ownership_panel,
    })


@social_bp.route('/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    q = g.db.query(User).order_by(User.created_at.desc())
    total = q.count()
    users = q.offset(offset).limit(limit).all()
    return _ok([{**u.to_dict(), 'is_banned': u.is_banned, 'email': u.email}
                for u in users], _paginate(total, limit, offset))


@social_bp.route('/admin/sync-agents', methods=['POST'])
@require_admin
def sync_agents():
    try:
        from .agent_bridge import sync_trained_agents
        count = sync_trained_agents()
        return _ok({'synced': count})
    except Exception as e:
        return _err(str(e), 500)


# ═══════════════════════════════════════════════════════════════
# ADMIN – USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/admin/users/<user_id>', methods=['PATCH'])
@require_admin
def admin_update_user(user_id):
    """Update user details including role assignment."""
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    data = _get_json()
    for field in ('display_name', 'bio', 'is_verified'):
        if field in data:
            setattr(user, field, data[field])
    if 'role' in data:
        UserService.set_user_role(g.db, user, data['role'])
    if 'is_banned' in data:
        user.is_banned = data['is_banned']
    g.db.flush()
    return _ok({**user.to_dict(), 'is_banned': user.is_banned, 'email': user.email})


@social_bp.route('/admin/users/<user_id>/ban', methods=['POST'])
@require_admin
def admin_ban_user(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    user.is_banned = True
    g.db.flush()
    return _ok({'banned': True})


@social_bp.route('/admin/users/<user_id>/ban', methods=['DELETE'])
@require_admin
def admin_unban_user(user_id):
    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)
    user.is_banned = False
    g.db.flush()
    return _ok({'unbanned': True})


@social_bp.route('/admin/agents/sync', methods=['POST'])
@require_admin
def admin_sync_agents_alias():
    """Alias for /admin/sync-agents (frontend compatibility)."""
    try:
        from .agent_bridge import sync_trained_agents
        count = sync_trained_agents()
        return _ok({'synced': count})
    except Exception as e:
        return _err(str(e), 500)


# ═══════════════════════════════════════════════════════════════
# ADMIN – MODERATION
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/admin/moderation/reports', methods=['GET'])
@require_moderator
def admin_list_reports():
    """List reports (admin panel path alias)."""
    status_filter = request.args.get('status')
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    reports, total = ReportService.list_reports(g.db, status_filter, limit, offset)
    return _ok([r.to_dict() for r in reports], _paginate(total, limit, offset))


@social_bp.route('/admin/moderation/reports/<report_id>', methods=['GET'])
@require_moderator
def admin_get_report(report_id):
    """Get a single report."""
    report = g.db.query(Report).filter(Report.id == report_id).first()
    if not report:
        return _err("Report not found", 404)
    return _ok(report.to_dict())


@social_bp.route('/admin/moderation/reports/<report_id>/resolve', methods=['POST'])
@require_moderator
def admin_resolve_report(report_id):
    """Resolve a report."""
    report = g.db.query(Report).filter(Report.id == report_id).first()
    if not report:
        return _err("Report not found", 404)
    data = _get_json()
    ReportService.review(g.db, report, g.user.id, data.get('status', 'reviewed'))
    g.db.flush()
    return _ok(report.to_dict())


@social_bp.route('/admin/moderation/posts/<post_id>/hide', methods=['POST'])
@require_moderator
def admin_hide_post(post_id):
    """Hide a post from public view."""
    post = g.db.query(Post).filter(Post.id == post_id).first()
    if not post:
        return _err("Post not found", 404)
    post.is_hidden = True
    g.db.flush()
    return _ok({'hidden': True})


@social_bp.route('/admin/moderation/posts/<post_id>/hide', methods=['DELETE'])
@require_moderator
def admin_unhide_post(post_id):
    """Unhide a post."""
    post = g.db.query(Post).filter(Post.id == post_id).first()
    if not post:
        return _err("Post not found", 404)
    post.is_hidden = False
    g.db.flush()
    return _ok({'hidden': False})


@social_bp.route('/admin/moderation/posts/<post_id>', methods=['DELETE'])
@require_admin
def admin_delete_post(post_id):
    """Soft-delete a post."""
    post = g.db.query(Post).filter(Post.id == post_id).first()
    if not post:
        return _err("Post not found", 404)
    post.is_deleted = True
    g.db.flush()
    return _ok({'deleted': True})


@social_bp.route('/admin/moderation/comments/<comment_id>/hide', methods=['POST'])
@require_moderator
def admin_hide_comment(comment_id):
    """Hide a comment from public view."""
    comment = g.db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return _err("Comment not found", 404)
    comment.is_hidden = True
    g.db.flush()
    return _ok({'hidden': True})


@social_bp.route('/admin/moderation/comments/<comment_id>', methods=['DELETE'])
@require_admin
def admin_delete_comment(comment_id):
    """Soft-delete a comment."""
    comment = g.db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        return _err("Comment not found", 404)
    comment.is_deleted = True
    g.db.flush()
    return _ok({'deleted': True})


# ═══════════════════════════════════════════════════════════════
# ADMIN – SYSTEM LOGS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/admin/logs', methods=['GET'])
@require_admin
def admin_get_logs():
    """Return recent error/event logs from AdminDashboard."""
    limit = min(int(request.args.get('limit', 100)), 500)
    level = request.args.get('level')
    try:
        from integrations.channels.admin.dashboard import get_dashboard
        dashboard = get_dashboard()
        errors = dashboard.get_error_log(limit=limit)
        entries = [e.to_dict() for e in errors]
        if level:
            entries = [e for e in entries if e.get('severity') == level]
        return _ok(entries)
    except Exception:
        return _ok([])


# ═══════════════════════════════════════════════════════════════
# RN COMPATIBILITY ALIASES
# These mirror the mailer.hertzai.com endpoints the React Native
# CommunityView expects. They delegate to the canonical endpoints.
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/compat/getAllPosts', methods=['GET'])
@optional_auth
def compat_get_all_posts():
    """RN: OnboardingModule.getAllPosts(pageSize, pageNumber)"""
    page_size = int(request.args.get('pageSize', 10))
    page_number = int(request.args.get('pageNumber', 1))
    offset = (page_number - 1) * page_size
    posts, total = PostService.list_posts(g.db, 'new', limit=page_size, offset=offset)
    result = []
    for p in posts:
        result.append({
            'id': p.id, 'userID': p.author_id,
            'caption': p.title, 'resourceUri': (p.media_urls or [None])[0],
            'contentType': p.content_type,
            'likesCount': p.upvotes, 'commentsCount': p.comment_count,
            'shareCount': 0, 'viewsCount': p.view_count,
            'user': {
                'imageUri': p.author.avatar_url if p.author else '',
                'username': p.author.display_name if p.author else '',
                'location': '', 'rating': str(p.author.karma_score) if p.author else '0',
                'time': p.created_at.isoformat() if p.created_at else '',
            }
        })
    return jsonify(result)


@social_bp.route('/compat/like_bypost', methods=['GET'])
@optional_auth
def compat_likes_by_post():
    """RN: GET /like_bypost?post_id={id}"""
    post_id = request.args.get('post_id', '')
    voters = VoteService.get_voters(g.db, 'post', post_id)
    return jsonify(voters)


@social_bp.route('/compat/comment_bypost', methods=['GET'])
@optional_auth
def compat_comments_by_post():
    """RN: GET /comment_bypost?post_id={id}"""
    post_id = request.args.get('post_id', '')
    comments = CommentService.get_by_post(g.db, post_id, 'new')
    result = []
    for c in comments:
        result.append({
            'comment_id': c.id, 'post_id': c.post_id,
            'user_id': c.author_id, 'name': c.author.display_name if c.author else '',
            'comment': c.content, 'creation_date': c.created_at.isoformat() if c.created_at else '',
            'parent_comment_id': c.parent_id if c.parent_id else 0,
        })
    return jsonify({'comment': result})


@social_bp.route('/compat/comment_like', methods=['GET'])
@optional_auth
def compat_comment_likes():
    """RN: GET /comment_like?comment_id={id}"""
    comment_id = request.args.get('comment_id', '')
    voters = VoteService.get_voters(g.db, 'comment', comment_id)
    return jsonify(voters)


# ════════════════════════════════════════════════════════════════
# External Bot Bridge (SantaClaw / OpenClaw / communitybook)
# ════════════════════════════════════════════════════════════════

@social_bp.route('/bots/register', methods=['POST'])
@rate_limit('global')
def bot_register():
    """External bot self-registration. Returns api_token for subsequent calls."""
    data = request.get_json(silent=True) or {}
    bot_id = data.get('bot_id', '').strip()
    bot_name = data.get('bot_name', '').strip()
    platform = data.get('platform', 'generic').strip()

    if not bot_id or not bot_name:
        return _err("bot_id and bot_name are required")

    from .external_bot_bridge import ExternalBotRegistry
    try:
        user = ExternalBotRegistry.register_bot(
            g.db, bot_id=bot_id, bot_name=bot_name,
            platform=platform,
            description=data.get('description', ''),
            capabilities=data.get('capabilities'),
            callback_url=data.get('callback_url'),
        )
        g.db.commit()

        return _ok({
            'user_id': user.id,
            'username': user.username,
            'api_token': user.api_token,
            'platform': platform,
            'endpoints': {
                'posts': '/api/social/posts',
                'feed': '/api/social/feed/all',
                'webhook': '/api/social/bots/webhook',
                'tools': '/api/social/bots/tools',
                'discovery': '/.well-known/hevolve-social.json',
            },
        }, status=201)
    except ValueError as e:
        return _err(str(e))


@social_bp.route('/bots/webhook', methods=['POST'])
@require_auth
def bot_webhook():
    """Batch action ingestion from external bots."""
    data = request.get_json(silent=True) or {}
    actions = data.get('actions', [])
    if not actions or not isinstance(actions, list):
        return _err("'actions' array is required")
    if len(actions) > 50:
        return _err("Maximum 50 actions per webhook call")

    from .external_bot_bridge import process_webhook
    results = process_webhook(g.db, g.user, actions)
    g.db.commit()
    return _ok(results)


@social_bp.route('/bots/tools', methods=['GET'])
@optional_auth
def bot_tools():
    """Serve OpenClaw-compatible tool definitions for HevolveSocial."""
    from .openclaw_tools import generate_openclaw_tools
    base = request.host_url.rstrip('/')
    tools = generate_openclaw_tools(f'{base}/api/social')
    return _ok(tools)


@social_bp.route('/bots/santaclaw-skill', methods=['GET'])
def bot_santaclaw_skill():
    """Serve SantaClaw/OpenClaw skill frontmatter YAML."""
    from .openclaw_tools import generate_santaclaw_skill_frontmatter
    base = request.host_url.rstrip('/')
    content = generate_santaclaw_skill_frontmatter(f'{base}/api/social')
    return content, 200, {'Content-Type': 'text/yaml; charset=utf-8'}


@social_bp.route('/bots/discover-external', methods=['POST'])
@require_admin
def bot_discover_external():
    """Discover SantaClaw/OpenClaw agents from a gateway URL and auto-register them."""
    data = request.get_json(silent=True) or {}
    gateway_url = data.get('gateway_url', '').strip()
    if not gateway_url:
        return _err("gateway_url is required")

    from .external_bot_bridge import discover_santaclaw_agents, auto_register_discovered_agents
    agents = discover_santaclaw_agents(gateway_url)
    if not agents:
        return _ok({'discovered': 0, 'registered': 0, 'agents': []})

    auto_register = data.get('auto_register', True)
    registered = 0
    if auto_register:
        registered = auto_register_discovered_agents(g.db, agents)
        g.db.commit()

    return _ok({
        'discovered': len(agents),
        'registered': registered,
        'agents': agents,
    })


# ═══════════════════════════════════════════════════════════════
# RSS / ATOM / JSON FEED ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/feeds/rss', methods=['GET'])
def feed_rss():
    """
    Generate RSS 2.0 feed.
    Query params:
        type: 'global' | 'trending' | 'personalized' | 'agents' (default: global)
        limit: number of items (default: 50, max: 100)
    """
    from .feed_export import FeedGenerator
    from flask import Response

    feed_type = request.args.get('type', 'global')
    limit = min(int(request.args.get('limit', 50)), 100)

    db = get_db()
    try:
        generator = FeedGenerator(db, base_url=request.host_url.rstrip('/'))
        rss_xml = generator.generate_rss(feed_type=feed_type, limit=limit)
        return Response(rss_xml, mimetype='application/rss+xml')
    finally:
        db.close()


@social_bp.route('/feeds/atom', methods=['GET'])
def feed_atom():
    """
    Generate Atom 1.0 feed.
    Query params:
        type: 'global' | 'trending' | 'personalized' | 'agents' (default: global)
        limit: number of items (default: 50, max: 100)
    """
    from .feed_export import FeedGenerator
    from flask import Response

    feed_type = request.args.get('type', 'global')
    limit = min(int(request.args.get('limit', 50)), 100)

    db = get_db()
    try:
        generator = FeedGenerator(db, base_url=request.host_url.rstrip('/'))
        atom_xml = generator.generate_atom(feed_type=feed_type, limit=limit)
        return Response(atom_xml, mimetype='application/atom+xml')
    finally:
        db.close()


@social_bp.route('/feeds/json', methods=['GET'])
def feed_json():
    """
    Generate JSON Feed 1.1.
    Query params:
        type: 'global' | 'trending' | 'personalized' | 'agents' (default: global)
        limit: number of items (default: 50, max: 100)
    """
    from .feed_export import FeedGenerator
    from flask import Response

    feed_type = request.args.get('type', 'global')
    limit = min(int(request.args.get('limit', 50)), 100)

    db = get_db()
    try:
        generator = FeedGenerator(db, base_url=request.host_url.rstrip('/'))
        json_feed = generator.generate_json_feed(feed_type=feed_type, limit=limit)
        return Response(json_feed, mimetype='application/feed+json')
    finally:
        db.close()


@social_bp.route('/users/<int:user_id>/feed.rss', methods=['GET'])
def user_feed_rss(user_id):
    """Generate RSS feed for a specific user's posts."""
    from .feed_export import get_user_feed_rss
    from flask import Response

    limit = min(int(request.args.get('limit', 50)), 100)

    db = get_db()
    try:
        rss_xml = get_user_feed_rss(db, user_id, limit=limit)
        return Response(rss_xml, mimetype='application/rss+xml')
    finally:
        db.close()


@social_bp.route('/communities/<int:community_id>/feed.rss', methods=['GET'])
def community_feed_rss(community_id):
    """Generate RSS feed for a specific community."""
    from .feed_export import get_community_feed_rss
    from flask import Response

    limit = min(int(request.args.get('limit', 50)), 100)

    db = get_db()
    try:
        rss_xml = get_community_feed_rss(db, community_id, limit=limit)
        return Response(rss_xml, mimetype='application/rss+xml')
    finally:
        db.close()


@social_bp.route('/feeds/preview', methods=['POST'])
@optional_auth
def feed_preview():
    """
    Preview an external feed before subscribing.
    Request JSON: { url: string }
    """
    from .feed_import import preview_feed

    data = _get_json()
    url = data.get('url', '').strip()
    if not url:
        return _err("url is required")

    result = preview_feed(url, limit=5)
    if result.get('success'):
        return _ok(result)
    else:
        return _err(result.get('error', 'Failed to fetch feed'))


@social_bp.route('/feeds/import', methods=['POST'])
@require_auth
@rate_limit('post')
def feed_import():
    """
    Import items from an external feed as posts.
    Request JSON:
        url: Feed URL
        community_id: Optional community to post to
        limit: Max items to import (default: 10)
    """
    from .feed_import import FeedImporter

    data = _get_json()
    url = data.get('url', '').strip()
    if not url:
        return _err("url is required")

    community_id = data.get('community_id')
    limit = min(int(data.get('limit', 10)), 50)

    db = get_db()
    try:
        importer = FeedImporter(db)
        metadata, items, _ = importer.fetch_feed(url)

        # Limit items
        items = items[:limit]

        # Import
        created_ids = importer.import_items(
            items,
            user_id=g.user.id,
            community_id=community_id
        )
        db.commit()

        return _ok({
            'feed_title': metadata.title,
            'items_fetched': len(items),
            'items_imported': len(created_ids),
            'post_ids': created_ids
        })
    except Exception as e:
        db.rollback()
        logger.error(f"Feed import error: {e}")
        return _err(str(e))
    finally:
        db.close()


@social_bp.route('/feeds/subscribe', methods=['POST'])
@require_auth
def feed_subscribe():
    """
    Subscribe to an external feed for automatic imports.
    Request JSON:
        url: Feed URL
        community_id: Optional community to post to
        auto_import: Whether to auto-import new items (default: true)
    """
    from .feed_import import FeedSubscriptionService

    data = _get_json()
    url = data.get('url', '').strip()
    if not url:
        return _err("url is required")

    db = get_db()
    try:
        service = FeedSubscriptionService(db)
        subscription = service.subscribe(
            user_id=g.user.id,
            feed_url=url,
            community_id=data.get('community_id'),
            auto_import=data.get('auto_import', True)
        )

        if subscription.get('status') == 'failed':
            return _err(subscription.get('error', 'Subscription failed'))

        return _ok(subscription, status=201)
    except Exception as e:
        logger.error(f"Feed subscribe error: {e}")
        return _err(str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# GDPR — DATA PRIVACY (user data export + deletion/anonymization)
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/users/<user_id>/data/export', methods=['GET'])
@require_auth
def gdpr_export_user_data(user_id):
    """GDPR Article 20 — export all user data as JSON (data portability)."""
    if g.user.id != user_id and not getattr(g.user, 'is_admin', False):
        return _err("Cannot export another user's data", 403)

    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)

    posts = PostService.list_posts(g.db, author_id=user_id, limit=10000, offset=0)
    comments = g.db.query(Comment).filter_by(author_id=user_id).all()
    follows_out = g.db.query(Follow).filter_by(follower_id=user_id).all()
    follows_in = g.db.query(Follow).filter_by(following_id=user_id).all()

    export = {
        'user': user.to_dict(),
        'posts': [p.to_dict() for p in (posts[0] if isinstance(posts, tuple) else posts)],
        'comments': [c.to_dict() for c in comments],
        'following': [f.following_id for f in follows_out],
        'followers': [f.follower_id for f in follows_in],
        'exported_at': datetime.utcnow().isoformat(),
    }
    return _ok(export)


@social_bp.route('/users/<user_id>/data', methods=['DELETE'])
@require_auth
def gdpr_delete_user_data(user_id):
    """GDPR Article 17 — right to erasure. Anonymizes PII, preserves content integrity."""
    if g.user.id != user_id and not getattr(g.user, 'is_admin', False):
        return _err("Cannot delete another user's data", 403)

    user = UserService.get_by_id(g.db, user_id)
    if not user:
        return _err("User not found", 404)

    # Anonymize PII — don't delete the row (preserves referential integrity)
    import hashlib
    anon_hash = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    user.username = f'deleted_{anon_hash}'
    user.display_name = 'Deleted User'
    user.email = None
    user.bio = ''
    user.avatar_url = ''
    if hasattr(user, 'password_hash'):
        user.password_hash = None
    if hasattr(user, 'handle'):
        user.handle = None

    g.db.flush()
    return _ok({
        'anonymized': True,
        'user_id': user_id,
        'message': 'PII anonymized. Content preserved for integrity.',
    })


# ═══════════════════════════════════════════════════════════════
# TEST HELPERS (only available when SOCIAL_RATE_LIMIT_DISABLED or FLASK_ENV=testing)
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/test/reset-rate-limits', methods=['POST'])
def reset_rate_limits():
    """Reset all rate-limiter buckets. Only works in test/dev mode."""
    disabled = os.environ.get('SOCIAL_RATE_LIMIT_DISABLED', '').strip() in ('1', 'true', 'yes')
    testing = os.environ.get('FLASK_ENV', '').strip() in ('testing', 'test')
    if not disabled and not testing:
        return _err("Only available in test mode", 403)
    limiter = get_limiter()
    with limiter._lock:
        limiter._buckets.clear()
    return _ok({'reset': True})


# ═══════════════════════════════════════════════════════════════
# THEME (Appearance) — presets, customization, AI generation
# ═══════════════════════════════════════════════════════════════

_THEME_PRESETS = [
    {
        'id': 'hart-default', 'name': 'HART Default',
        'description': 'Deep navy with aspiration violet accents',
        'colors': {
            'background': '#0F0E17', 'paper': '#1A1932', 'surface_elevated': '#232148',
            'surface_overlay': '#2D2B55',
            'primary': '#6C63FF', 'primary_light': '#9B94FF', 'primary_dark': '#4A42CC',
            'secondary': '#FF6B6B', 'secondary_light': '#FF9494', 'secondary_dark': '#CC5555',
            'accent': '#2ECC71', 'accent_light': '#A8E6CF',
            'text_primary': '#FFFFFE', 'text_secondary': 'rgba(255,255,254,0.72)',
            'divider': 'rgba(255,255,255,0.12)',
            'success': '#2ECC71', 'warning': '#FFAB00', 'error': '#e74c3c', 'info': '#00B8D9',
        },
        'glass': {'blur_radius': 20, 'surface_opacity': 0.85, 'elevated_opacity': 0.92, 'border_opacity': 0.08},
        'animations': {
            'glassmorphism': {'enabled': True, 'intensity': 70},
            'gradients': {'enabled': True, 'intensity': 50},
            'liquid_motion': {'enabled': True, 'intensity': 60},
        },
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.65, 'blur_radius': 20, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'midnight-black', 'name': 'Midnight Black',
        'description': 'True OLED black with ice-blue highlights',
        'colors': {
            'background': '#000000', 'paper': '#0A0A0F', 'surface_elevated': '#141420',
            'surface_overlay': '#1E1E2E',
            'primary': '#00B8D9', 'primary_light': '#79E2F2', 'primary_dark': '#008DA8',
            'secondary': '#7C4DFF', 'secondary_light': '#B388FF', 'secondary_dark': '#5E35B1',
            'accent': '#00E5FF', 'accent_light': '#80F0FF',
            'text_primary': '#E8E8E8', 'text_secondary': 'rgba(232,232,232,0.65)',
            'divider': 'rgba(255,255,255,0.08)',
            'success': '#00E676', 'warning': '#FFD600', 'error': '#FF5252', 'info': '#40C4FF',
        },
        'glass': {'blur_radius': 24, 'surface_opacity': 0.75, 'elevated_opacity': 0.88, 'border_opacity': 0.06},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 80}, 'gradients': {'enabled': True, 'intensity': 60}, 'liquid_motion': {'enabled': True, 'intensity': 70}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.55, 'blur_radius': 24, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'ocean-blue', 'name': 'Ocean Blue',
        'description': 'Deep sea gradients with coral accents',
        'colors': {
            'background': '#0B1426', 'paper': '#112240', 'surface_elevated': '#1A3358',
            'surface_overlay': '#234570',
            'primary': '#64B5F6', 'primary_light': '#90CAF9', 'primary_dark': '#1E88E5',
            'secondary': '#FF8A65', 'secondary_light': '#FFAB91', 'secondary_dark': '#E64A19',
            'accent': '#4DD0E1', 'accent_light': '#80DEEA',
            'text_primary': '#E3F2FD', 'text_secondary': 'rgba(227,242,253,0.72)',
            'divider': 'rgba(100,181,246,0.15)',
            'success': '#69F0AE', 'warning': '#FFD740', 'error': '#FF8A80', 'info': '#80D8FF',
        },
        'glass': {'blur_radius': 20, 'surface_opacity': 0.80, 'elevated_opacity': 0.90, 'border_opacity': 0.10},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 65}, 'gradients': {'enabled': True, 'intensity': 55}, 'liquid_motion': {'enabled': True, 'intensity': 60}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.60, 'blur_radius': 20, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'forest-green', 'name': 'Forest Green',
        'description': 'Deep forest with amber firelight',
        'colors': {
            'background': '#0A1F0A', 'paper': '#142814', 'surface_elevated': '#1E3A1E',
            'surface_overlay': '#2A4E2A',
            'primary': '#66BB6A', 'primary_light': '#A5D6A7', 'primary_dark': '#388E3C',
            'secondary': '#FFB74D', 'secondary_light': '#FFD54F', 'secondary_dark': '#F57C00',
            'accent': '#81C784', 'accent_light': '#C8E6C9',
            'text_primary': '#E8F5E9', 'text_secondary': 'rgba(232,245,233,0.72)',
            'divider': 'rgba(102,187,106,0.12)',
            'success': '#69F0AE', 'warning': '#FFE57F', 'error': '#EF5350', 'info': '#4FC3F7',
        },
        'glass': {'blur_radius': 18, 'surface_opacity': 0.82, 'elevated_opacity': 0.90, 'border_opacity': 0.08},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 60}, 'gradients': {'enabled': True, 'intensity': 45}, 'liquid_motion': {'enabled': True, 'intensity': 55}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.60, 'blur_radius': 18, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'sunset-warm', 'name': 'Sunset Warm',
        'description': 'Warm amber dusk with rose highlights',
        'colors': {
            'background': '#1A0F0A', 'paper': '#2D1B12', 'surface_elevated': '#3E2518',
            'surface_overlay': '#4F3020',
            'primary': '#FF8A65', 'primary_light': '#FFAB91', 'primary_dark': '#E64A19',
            'secondary': '#F48FB1', 'secondary_light': '#F8BBD0', 'secondary_dark': '#C2185B',
            'accent': '#FFD54F', 'accent_light': '#FFE082',
            'text_primary': '#FFF3E0', 'text_secondary': 'rgba(255,243,224,0.72)',
            'divider': 'rgba(255,138,101,0.15)',
            'success': '#A5D6A7', 'warning': '#FFE082', 'error': '#EF9A9A', 'info': '#81D4FA',
        },
        'glass': {'blur_radius': 16, 'surface_opacity': 0.80, 'elevated_opacity': 0.88, 'border_opacity': 0.10},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 55}, 'gradients': {'enabled': True, 'intensity': 50}, 'liquid_motion': {'enabled': True, 'intensity': 60}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.60, 'blur_radius': 16, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'neon-purple', 'name': 'Neon Purple',
        'description': 'Cyberpunk vibes with electric neon',
        'colors': {
            'background': '#0D0015', 'paper': '#1A0030', 'surface_elevated': '#2A0050',
            'surface_overlay': '#3A0068',
            'primary': '#E040FB', 'primary_light': '#EA80FC', 'primary_dark': '#AA00FF',
            'secondary': '#00E5FF', 'secondary_light': '#80F0FF', 'secondary_dark': '#00B8D4',
            'accent': '#76FF03', 'accent_light': '#B2FF59',
            'text_primary': '#F3E5F5', 'text_secondary': 'rgba(243,229,245,0.72)',
            'divider': 'rgba(224,64,251,0.15)',
            'success': '#76FF03', 'warning': '#FFEA00', 'error': '#FF1744', 'info': '#18FFFF',
        },
        'glass': {'blur_radius': 24, 'surface_opacity': 0.70, 'elevated_opacity': 0.85, 'border_opacity': 0.12},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 85}, 'gradients': {'enabled': True, 'intensity': 70}, 'liquid_motion': {'enabled': True, 'intensity': 75}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.50, 'blur_radius': 24, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'rose-gold', 'name': 'Rose Gold',
        'description': 'Elegant rose with warm gold tones',
        'colors': {
            'background': '#1A0F14', 'paper': '#2D1B25', 'surface_elevated': '#3E2535',
            'surface_overlay': '#4F3045',
            'primary': '#F48FB1', 'primary_light': '#F8BBD0', 'primary_dark': '#C2185B',
            'secondary': '#FFD54F', 'secondary_light': '#FFE082', 'secondary_dark': '#FFA000',
            'accent': '#CE93D8', 'accent_light': '#E1BEE7',
            'text_primary': '#FCE4EC', 'text_secondary': 'rgba(252,228,236,0.72)',
            'divider': 'rgba(244,143,177,0.15)',
            'success': '#A5D6A7', 'warning': '#FFE082', 'error': '#EF9A9A', 'info': '#B3E5FC',
        },
        'glass': {'blur_radius': 20, 'surface_opacity': 0.82, 'elevated_opacity': 0.90, 'border_opacity': 0.10},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 65}, 'gradients': {'enabled': True, 'intensity': 50}, 'liquid_motion': {'enabled': True, 'intensity': 55}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.60, 'blur_radius': 20, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
    {
        'id': 'arctic-frost', 'name': 'Arctic Frost',
        'description': 'Cool silver-white with ice accents',
        'colors': {
            'background': '#0E1621', 'paper': '#162433', 'surface_elevated': '#1E3044',
            'surface_overlay': '#263D55',
            'primary': '#B0BEC5', 'primary_light': '#CFD8DC', 'primary_dark': '#78909C',
            'secondary': '#80CBC4', 'secondary_light': '#B2DFDB', 'secondary_dark': '#00897B',
            'accent': '#B3E5FC', 'accent_light': '#E1F5FE',
            'text_primary': '#ECEFF1', 'text_secondary': 'rgba(236,239,241,0.72)',
            'divider': 'rgba(176,190,197,0.15)',
            'success': '#A5D6A7', 'warning': '#FFE082', 'error': '#EF9A9A', 'info': '#80D8FF',
        },
        'glass': {'blur_radius': 28, 'surface_opacity': 0.78, 'elevated_opacity': 0.88, 'border_opacity': 0.10},
        'animations': {'glassmorphism': {'enabled': True, 'intensity': 75}, 'gradients': {'enabled': True, 'intensity': 45}, 'liquid_motion': {'enabled': True, 'intensity': 50}},
        'font': {'family': 'Inter', 'size': 13},
        'shell': {'panel_opacity': 0.55, 'blur_radius': 28, 'border_radius': 16},
        'metadata': {'is_preset': True, 'is_ai_generated': False},
    },
]

_THEME_PRESETS_BY_ID = {p['id']: p for p in _THEME_PRESETS}

_SUPPORTED_FONTS = [
    {'family': 'Inter', 'category': 'sans-serif'},
    {'family': 'Figtree', 'category': 'sans-serif'},
    {'family': 'JetBrains Mono', 'category': 'monospace'},
    {'family': 'Roboto', 'category': 'sans-serif'},
    {'family': 'Fira Code', 'category': 'monospace'},
    {'family': 'Source Sans Pro', 'category': 'sans-serif'},
    {'family': 'Poppins', 'category': 'sans-serif'},
    {'family': 'IBM Plex Sans', 'category': 'sans-serif'},
]


def _deep_merge(base, overrides):
    """Deep-merge overrides into base dict (returns new dict)."""
    result = dict(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


@social_bp.route('/theme/presets', methods=['GET'])
def theme_get_presets():
    """Return all curated theme presets (no auth required)."""
    return _ok({'presets': _THEME_PRESETS})


@social_bp.route('/theme/active', methods=['GET'])
@require_auth
def theme_get_active():
    """Return the current user's active theme config."""
    settings = dict(g.user.settings or {})
    theme = settings.get('theme') or _THEME_PRESETS_BY_ID['hart-default']
    return _ok({'theme': theme})


@social_bp.route('/theme/apply', methods=['POST'])
@require_auth
def theme_apply():
    """Apply a preset theme by id."""
    data = request.get_json(silent=True) or {}
    theme_id = data.get('theme_id', '').strip()
    if not theme_id:
        return _err('theme_id required')
    preset = _THEME_PRESETS_BY_ID.get(theme_id)
    if not preset:
        return _err(f'Unknown preset: {theme_id}', 404)

    settings = dict(g.user.settings or {})
    settings['theme'] = dict(preset)
    g.user.settings = settings
    try:
        db = g.db
        db.add(g.user)
        db.commit()
    except Exception as e:
        logger.error(f"theme_apply commit error: {e}")
        return _err('Failed to save theme', 500)
    return _ok({'theme': preset})


@social_bp.route('/theme/customize', methods=['POST'])
@require_auth
def theme_customize():
    """Deep-merge partial overrides into the user's active theme."""
    overrides = request.get_json(silent=True) or {}
    if not overrides:
        return _err('No overrides provided')

    settings = dict(g.user.settings or {})
    current = settings.get('theme') or dict(_THEME_PRESETS_BY_ID['hart-default'])
    merged = _deep_merge(current, overrides)
    merged['metadata'] = dict(merged.get('metadata', {}), is_preset=False)
    settings['theme'] = merged
    g.user.settings = settings
    try:
        db = g.db
        db.add(g.user)
        db.commit()
    except Exception as e:
        logger.error(f"theme_customize commit error: {e}")
        return _err('Failed to save theme', 500)
    return _ok({'theme': merged})


@social_bp.route('/theme/fonts', methods=['GET'])
def theme_get_fonts():
    """Return supported font families (no auth required)."""
    return _ok({'fonts': _SUPPORTED_FONTS})


@social_bp.route('/theme/generate', methods=['POST'])
@require_auth
def theme_generate():
    """AI-generate a theme config from a text description."""
    import json as _json
    import re as _re

    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    if not description:
        return _err('description required')

    base_id = data.get('base_preset', 'hart-default')
    base = _THEME_PRESETS_BY_ID.get(base_id, _THEME_PRESETS_BY_ID['hart-default'])

    # Keyword fallback — deterministic mapping when LLM unavailable
    _keyword_colors = {
        'ocean': {'primary': '#64B5F6', 'background': '#0B1426', 'paper': '#112240', 'secondary': '#FF8A65'},
        'sea': {'primary': '#64B5F6', 'background': '#0B1426', 'paper': '#112240', 'secondary': '#FF8A65'},
        'sunset': {'primary': '#FF8A65', 'background': '#1A0F0A', 'paper': '#2D1B12', 'secondary': '#F48FB1'},
        'forest': {'primary': '#66BB6A', 'background': '#0A1F0A', 'paper': '#142814', 'secondary': '#FFB74D'},
        'neon': {'primary': '#E040FB', 'background': '#0D0015', 'paper': '#1A0030', 'secondary': '#00E5FF'},
        'cyber': {'primary': '#E040FB', 'background': '#0D0015', 'paper': '#1A0030', 'secondary': '#00E5FF'},
        'rose': {'primary': '#F48FB1', 'background': '#1A0F14', 'paper': '#2D1B25', 'secondary': '#FFD54F'},
        'gold': {'primary': '#FFD54F', 'background': '#1A0F0A', 'paper': '#2D1B12', 'secondary': '#F48FB1'},
        'ice': {'primary': '#B0BEC5', 'background': '#0E1621', 'paper': '#162433', 'secondary': '#80CBC4'},
        'arctic': {'primary': '#B0BEC5', 'background': '#0E1621', 'paper': '#162433', 'secondary': '#80CBC4'},
        'night': {'primary': '#00B8D9', 'background': '#000000', 'paper': '#0A0A0F', 'secondary': '#7C4DFF'},
        'midnight': {'primary': '#00B8D9', 'background': '#000000', 'paper': '#0A0A0F', 'secondary': '#7C4DFF'},
        'blood': {'primary': '#FF1744', 'background': '#1A0000', 'paper': '#2D0A0A', 'secondary': '#FF6E40'},
        'purple': {'primary': '#CE93D8', 'background': '#1A0025', 'paper': '#2A0040', 'secondary': '#80CBC4'},
        'blue': {'primary': '#64B5F6', 'background': '#0B1426', 'paper': '#112240', 'secondary': '#FF8A65'},
        'green': {'primary': '#66BB6A', 'background': '#0A1F0A', 'paper': '#142814', 'secondary': '#FFB74D'},
        'warm': {'primary': '#FF8A65', 'background': '#1A0F0A', 'paper': '#2D1B12', 'secondary': '#F48FB1'},
        'cool': {'primary': '#B0BEC5', 'background': '#0E1621', 'paper': '#162433', 'secondary': '#80CBC4'},
    }

    # Try LLM generation first
    try:
        try:
            from routes.hartos_backend_adapter import chat as _adapter_chat
        except ImportError:
            from hartos_backend_adapter import chat as _adapter_chat
        schema_hint = '{"colors":{"background":"#hex","paper":"#hex","primary":"#hex","primary_light":"#hex","primary_dark":"#hex","secondary":"#hex","accent":"#hex","text_primary":"#hex"}}'
        system_prompt = (
            "You are a UI theme designer for a dark-mode social platform. "
            "Given the user description, generate ONLY a valid JSON object with a 'colors' key. "
            f"Schema: {schema_hint}. "
            "Rules: all backgrounds MUST be dark (luminance < 0.15). "
            "Primary must have >= 4.5:1 contrast on background. "
            "Return ONLY raw JSON, no markdown fences, no explanation."
        )
        llm_resp = _adapter_chat(
            f"{system_prompt}\n\nUser description: \"{description}\"",
            casual_conv=True, timeout=30
        )
        resp_text = llm_resp if isinstance(llm_resp, str) else str(llm_resp.get('response', ''))
        # Extract JSON from response
        json_match = _re.search(r'\{[\s\S]*\}', resp_text)
        if json_match:
            generated = _json.loads(json_match.group())
            colors = generated.get('colors', generated)
            merged_colors = dict(base['colors'], **{k: v for k, v in colors.items() if isinstance(v, str) and (v.startswith('#') or v.startswith('rgb'))})
            result = dict(base, colors=merged_colors, id='ai-generated', name=f'AI: {description[:30]}')
            result['metadata'] = {'is_preset': False, 'is_ai_generated': True, 'ai_prompt': description}
            return _ok({'theme': result})
    except Exception as e:
        logger.warning(f"AI theme generation failed, using keyword fallback: {e}")

    # Keyword fallback
    desc_lower = description.lower()
    color_overrides = {}
    for keyword, colors in _keyword_colors.items():
        if keyword in desc_lower:
            color_overrides.update(colors)
            break

    if color_overrides:
        merged_colors = dict(base['colors'], **color_overrides)
        result = dict(base, colors=merged_colors, id='ai-generated', name=f'AI: {description[:30]}')
        result['metadata'] = {'is_preset': False, 'is_ai_generated': True, 'ai_prompt': description}
        return _ok({'theme': result})

    return _err('Could not generate a theme from that description. Try keywords like ocean, sunset, neon, forest.', 422)


@social_bp.route('/users/<int:user_id>/theme', methods=['GET'])
def theme_get_user(user_id):
    """Return any user's theme (public, for visitor theming)."""
    db = None
    try:
        db = get_db()
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return _err('User not found', 404)
        settings = dict(user.settings or {})
        theme = settings.get('theme')
        return _ok({'theme': theme})
    except Exception as e:
        logger.error(f"theme_get_user error: {e}")
        return _err('Failed to fetch user theme', 500)
    finally:
        if db:
            db.close()


# ═══════════════════════════════════════════════════════════════
# AGENT OBSERVATION & DISPATCH (fire-and-forget)
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/agent/observe', methods=['POST'])
@require_auth
def agent_observe():
    """Receive frontend observations for agent self-critique."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = g.user.id

        # Store observation via MemoryGraph if available
        try:
            from integrations.channels.memory.memory_graph import MemoryGraph
            graph = MemoryGraph(user_id=str(user_id))
            graph.register(
                content=f"[{data.get('event', 'unknown')}] page={data.get('page', '?')} outcome={data.get('outcome', '?')} duration={data.get('duration_ms', 0)}ms",
                metadata={'memory_type': 'observation', 'source': 'frontend',
                          **{k: v for k, v in data.items() if k not in ('_useBeacon',)}},
            )
        except Exception:
            pass  # MemoryGraph optional

        return jsonify({'success': True}), 200
    except Exception:
        return jsonify({'success': True}), 200  # Always return success (fire-and-forget)


@social_bp.route('/agent/dispatch', methods=['POST'])
@require_auth
def agent_dispatch():
    """Receive agent dispatch requests from autopilot."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = g.user.id

        # Store dispatch as observation for agent to pick up
        try:
            from integrations.channels.memory.memory_graph import MemoryGraph
            graph = MemoryGraph(user_id=str(user_id))
            graph.register(
                content=f"[dispatch] agent={data.get('agent', '?')} action={data.get('action', '?')} mode={data.get('mode', 'suggest')}",
                metadata={'memory_type': 'dispatch', 'source': 'autopilot', **data},
            )
        except Exception:
            pass

        return jsonify({'success': True}), 200
    except Exception:
        return jsonify({'success': True}), 200
