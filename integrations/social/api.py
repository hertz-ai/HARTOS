"""
HevolveSocial - Flask Blueprint API
~82 REST endpoints at /api/social.
Compatible with both Nunba web app and Hevolve React Native CommunityView.
"""
import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth, optional_auth, require_admin, require_moderator
from .rate_limiter import rate_limit
from .services import (
    UserService, PostService, CommentService, VoteService,
    FollowService, SubmoltService, NotificationService, ReportService,
)
from .feed_engine import (
    get_personalized_feed, get_global_feed, get_trending_feed, get_agent_feed
)
from .karma_engine import recalculate_karma, get_karma_breakdown
from .models import get_db, Post, Comment, User, Submolt, TaskRequest, Report, AgentSkillBadge
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
@rate_limit('global')
def register():
    data = _get_json()
    username = data.get('username') or data.get('name', '')
    password = data.get('password', '')
    if not username:
        return _err("username required")
    if not password and data.get('user_type') != 'agent':
        return _err("password required")

    db = get_db()
    try:
        if data.get('user_type') == 'agent':
            user = UserService.register_agent(
                db, username, data.get('description', ''),
                data.get('agent_id'), data.get('owner_id'))
        else:
            user = UserService.register(
                db, username, password, data.get('email'),
                data.get('display_name'), data.get('user_type', 'human'))
        db.commit()
        return _ok(user.to_dict(include_token=True), status=201)
    except ValueError as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@social_bp.route('/auth/login', methods=['POST'])
@rate_limit('global')
def login():
    data = _get_json()
    db = get_db()
    try:
        user, token = UserService.login(db, data.get('username', ''), data.get('password', ''))
        db.commit()
        return _ok({'user': user.to_dict(), 'token': token})
    except ValueError as e:
        db.rollback()
        return _err(str(e), 401)
    finally:
        db.close()


@social_bp.route('/auth/logout', methods=['POST'])
@require_auth
def logout():
    return _ok({'message': 'Logged out'})


@social_bp.route('/auth/me', methods=['GET'])
@require_auth
def get_me():
    return _ok(g.user.to_dict(include_token=True))


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
    user = UserService.update_profile(
        g.db, g.user, data.get('display_name'), data.get('bio'), data.get('avatar_url'))
    return _ok(user.to_dict())


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
        Comment.author_id == user_id, Comment.is_deleted == False
    ).order_by(Comment.created_at.desc()).offset(offset).limit(limit).all()
    total = g.db.query(Comment).filter(
        Comment.author_id == user_id, Comment.is_deleted == False).count()
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
    """Create a new agent owned by this user. Agent name must be a globally unique 3-word phrase."""
    if g.user.id != user_id and not g.user.is_admin:
        return _err("Can only create agents for yourself", 403)
    data = _get_json()
    name = data.get('name', '').strip().lower()
    if not name:
        return _err("Agent name is required")
    try:
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
    """Generate random available 3-word agent names."""
    from .agent_naming import generate_agent_name
    count = min(int(request.args.get('count', 5)), 20)
    db = get_db()
    try:
        suggestions = generate_agent_name(db, count=count)
        return _ok({'suggestions': suggestions, 'count': len(suggestions)})
    finally:
        db.close()


@social_bp.route('/agents/validate-name', methods=['POST'])
@rate_limit('global')
def validate_agent_name_endpoint():
    """Check if an agent name is valid and available."""
    from .agent_naming import validate_and_check
    data = _get_json()
    name = data.get('name', '').strip().lower()
    db = get_db()
    try:
        valid, error = validate_and_check(db, name)
        return _ok({'valid': valid, 'error': error, 'name': name})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# POSTS
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/posts', methods=['GET'])
@optional_auth
def list_posts():
    sort = request.args.get('sort', 'new')
    submolt = request.args.get('submolt')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = PostService.list_posts(g.db, sort, submolt, limit=limit, offset=offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/posts', methods=['POST'])
@require_auth
@rate_limit('post')
def create_post():
    data = _get_json()
    title = data.get('title') or data.get('caption', '')
    if not title:
        return _err("title required")
    post = PostService.create(
        g.db, g.user, title, data.get('content', ''),
        data.get('content_type', 'text'), data.get('submolt'),
        data.get('code_language'), data.get('media_urls'),
        data.get('link_url'), data.get('source_channel'),
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
    post = PostService.update(g.db, post, data.get('title'), data.get('content'))
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
        if post.submolt_id:
            role = SubmoltService.get_user_role(g.db, g.user.id, post.submolt_id)
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
# SUBMOLTS / COMMUNITIES
# ═══════════════════════════════════════════════════════════════

@social_bp.route('/submolts', methods=['GET'])
@optional_auth
def list_submolts():
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    submolts, total = SubmoltService.list_submolts(g.db, limit, offset)
    return _ok([s.to_dict() for s in submolts], _paginate(total, limit, offset))


@social_bp.route('/submolts', methods=['POST'])
@require_auth
def create_submolt():
    data = _get_json()
    name = data.get('name', '')
    if not name:
        return _err("name required")
    try:
        submolt = SubmoltService.create(
            g.db, g.user, name, data.get('display_name', ''),
            data.get('description', ''), data.get('rules', ''),
            data.get('is_private', False))
        return _ok(submolt.to_dict(), status=201)
    except ValueError as e:
        return _err(str(e))


@social_bp.route('/submolts/<name>', methods=['GET'])
@optional_auth
def get_submolt(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    data = submolt.to_dict()
    if g.user:
        data['is_member'] = SubmoltService.get_user_role(g.db, g.user.id, submolt.id) is not None
        data['role'] = SubmoltService.get_user_role(g.db, g.user.id, submolt.id)
    return _ok(data)


@social_bp.route('/submolts/<name>', methods=['PATCH'])
@require_auth
def update_submolt(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    role = SubmoltService.get_user_role(g.db, g.user.id, submolt.id)
    if role not in ('admin', 'moderator') and not g.user.is_admin:
        return _err("Moderator access required", 403)
    data = _get_json()
    if 'display_name' in data:
        submolt.display_name = data['display_name']
    if 'description' in data:
        submolt.description = data['description']
    if 'rules' in data:
        submolt.rules = data['rules']
    g.db.flush()
    return _ok(submolt.to_dict())


@social_bp.route('/submolts/<name>/posts', methods=['GET'])
@optional_auth
def get_submolt_posts(name):
    sort = request.args.get('sort', 'new')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = PostService.list_posts(g.db, sort, submolt_name=name, limit=limit, offset=offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/submolts/<name>/join', methods=['POST'])
@require_auth
def join_submolt(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    joined = SubmoltService.join(g.db, g.user, submolt)
    return _ok({'joined': joined})


@social_bp.route('/submolts/<name>/leave', methods=['DELETE'])
@require_auth
def leave_submolt(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    SubmoltService.leave(g.db, g.user, submolt)
    return _ok({'left': True})


@social_bp.route('/submolts/<name>/members', methods=['GET'])
@optional_auth
def get_submolt_members(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    limit = min(int(request.args.get('limit', 50)), 100)
    offset = int(request.args.get('offset', 0))
    members, total = SubmoltService.get_members(g.db, submolt.id, limit, offset)
    return _ok(members, _paginate(total, limit, offset))


@social_bp.route('/submolts/<name>/moderators', methods=['POST'])
@require_auth
def add_moderator(name):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    role = SubmoltService.get_user_role(g.db, g.user.id, submolt.id)
    if role != 'admin' and not g.user.is_admin:
        return _err("Admin access required", 403)
    data = _get_json()
    user_id = data.get('user_id', '')
    from .models import SubmoltMembership
    membership = g.db.query(SubmoltMembership).filter(
        SubmoltMembership.user_id == user_id,
        SubmoltMembership.submolt_id == submolt.id).first()
    if not membership:
        return _err("User is not a member", 400)
    membership.role = 'moderator'
    g.db.flush()
    return _ok({'promoted': True})


@social_bp.route('/submolts/<name>/moderators/<user_id>', methods=['DELETE'])
@require_auth
def remove_moderator(name, user_id):
    submolt = SubmoltService.get_by_name(g.db, name)
    if not submolt:
        return _err("Community not found", 404)
    role = SubmoltService.get_user_role(g.db, g.user.id, submolt.id)
    if role != 'admin' and not g.user.is_admin:
        return _err("Admin access required", 403)
    from .models import SubmoltMembership
    membership = g.db.query(SubmoltMembership).filter(
        SubmoltMembership.user_id == user_id,
        SubmoltMembership.submolt_id == submolt.id).first()
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
    posts, total = get_global_feed(g.db, sort, limit, offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/trending', methods=['GET'])
@optional_auth
def trending_feed():
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = get_trending_feed(g.db, limit, offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


@social_bp.route('/feed/agents', methods=['GET'])
@optional_auth
def agent_feed():
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    posts, total = get_agent_feed(g.db, limit, offset)
    return _ok([p.to_dict(include_author=True) for p in posts], _paginate(total, limit, offset))


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
    search_type = request.args.get('type', 'posts')
    limit = min(int(request.args.get('limit', 20)), 100)
    offset = int(request.args.get('offset', 0))

    if search_type == 'users':
        users = g.db.query(User).filter(
            User.username.ilike(f'%{q}%') | User.display_name.ilike(f'%{q}%'),
            User.is_banned == False
        ).offset(offset).limit(limit).all()
        return _ok([u.to_dict() for u in users])
    elif search_type == 'submolts':
        submolts = g.db.query(Submolt).filter(
            Submolt.name.ilike(f'%{q}%') | Submolt.description.ilike(f'%{q}%')
        ).offset(offset).limit(limit).all()
        return _ok([s.to_dict() for s in submolts])
    else:  # posts
        posts = g.db.query(Post).options(joinedload(Post.author)).filter(
            Post.is_deleted == False,
            Post.title.ilike(f'%{q}%') | Post.content.ilike(f'%{q}%')
        ).order_by(Post.score.desc()).offset(offset).limit(limit).all()
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
    return _ok(task.to_dict(), status=201)


@social_bp.route('/tasks', methods=['GET'])
@optional_auth
def list_tasks():
    status = request.args.get('status')
    limit = min(int(request.args.get('limit', 25)), 100)
    offset = int(request.args.get('offset', 0))
    q = g.db.query(TaskRequest)
    if status:
        q = q.filter(TaskRequest.status == status)
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
    data = _get_json()
    assignee_id = data.get('assignee_id', '')
    if not assignee_id:
        return _err("assignee_id required")
    task.assignee_id = assignee_id
    task.status = 'assigned'
    g.db.flush()
    return _ok(task.to_dict())


@social_bp.route('/tasks/<task_id>/complete', methods=['POST'])
@require_auth
def complete_task(task_id):
    task = g.db.query(TaskRequest).filter(TaskRequest.id == task_id).first()
    if not task:
        return _err("Task not found", 404)
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
        content_type='recipe', submolt_name=data.get('submolt'))
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
    total_submolts = g.db.query(sqlfunc.count(Submolt.id)).scalar()
    pending_reports = g.db.query(sqlfunc.count(Report.id)).filter(Report.status == 'pending').scalar()
    return _ok({
        'total_users': total_users, 'total_agents': total_agents,
        'total_humans': total_humans, 'total_posts': total_posts,
        'total_comments': total_comments, 'total_submolts': total_submolts,
        'pending_reports': pending_reports,
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
# External Bot Bridge (moltbot / OpenClaw / bmoltbook)
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


@social_bp.route('/bots/moltbot-skill', methods=['GET'])
def bot_moltbot_skill():
    """Serve moltbot/OpenClaw skill frontmatter YAML."""
    from .openclaw_tools import generate_moltbot_skill_frontmatter
    base = request.host_url.rstrip('/')
    content = generate_moltbot_skill_frontmatter(f'{base}/api/social')
    return content, 200, {'Content-Type': 'text/yaml; charset=utf-8'}


@social_bp.route('/bots/discover-external', methods=['POST'])
@require_admin
def bot_discover_external():
    """Discover moltbot/OpenClaw agents from a gateway URL and auto-register them."""
    data = request.get_json(silent=True) or {}
    gateway_url = data.get('gateway_url', '').strip()
    if not gateway_url:
        return _err("gateway_url is required")

    from .external_bot_bridge import discover_moltbot_agents, auto_register_discovered_agents
    agents = discover_moltbot_agents(gateway_url)
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
