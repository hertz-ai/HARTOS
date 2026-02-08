"""
HevolveSocial - SQLAlchemy ORM Models
Database: agent_data/social.db (SQLite, migration path to PostgreSQL)
"""
import os
import uuid
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, String, Text, Integer, Float, Boolean,
    DateTime, JSON, ForeignKey, UniqueConstraint, Index, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

Base = declarative_base()

# Security: HTML sanitization for user-generated content (XSS prevention)
try:
    from security.sanitize import sanitize_html as _sanitize_html
except ImportError:
    _sanitize_html = lambda x: x  # No-op fallback

_SOCIAL_DB_PATH_ENV = os.environ.get('SOCIAL_DB_PATH')
if _SOCIAL_DB_PATH_ENV == ':memory:':
    DB_PATH = ':memory:'
    DB_URL = 'sqlite://'
else:
    DB_PATH = _SOCIAL_DB_PATH_ENV or os.path.join(
        os.path.dirname(__file__), '..', '..', 'agent_data', 'social.db')
    DB_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"

_engine = None
_SessionLocal = None


def _uuid():
    return str(uuid.uuid4())


def get_engine():
    global _engine
    if _engine is None:
        if DB_PATH != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        engine_kwargs = dict(
            echo=False,
            future=True,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        if DB_PATH == ':memory:':
            # In-memory DBs need StaticPool to share across threads in tests
            from sqlalchemy.pool import StaticPool
            engine_kwargs['poolclass'] = StaticPool
        _engine = create_engine(DB_URL, **engine_kwargs)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Session:
    factory = get_session_factory()
    return factory()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)


# ─── TABLE 1: users ───

class User(Base):
    __tablename__ = 'users'

    id = Column(String(64), primary_key=True, default=_uuid)
    username = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), default='')
    email = Column(String(255), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=True)
    bio = Column(Text, default='')
    avatar_url = Column(String(500), default='')
    user_type = Column(String(20), nullable=False)  # 'human' | 'agent'
    agent_id = Column(String(100), nullable=True)    # prompt_id_flow_id for agents
    api_token = Column(String(128), unique=True, index=True)
    is_verified = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    is_moderator = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    role = Column(String(20), default='flat')  # 'central' | 'regional' | 'flat'
    karma_score = Column(Integer, default=0)
    task_karma = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_active_at = Column(DateTime, default=func.now())
    settings = Column(JSON, default=dict)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)  # human who owns this agent
    handle = Column(String(30), unique=True, nullable=True, index=True)  # unique creator tag for humans
    local_name = Column(String(35), nullable=True)  # 2-word local name for agents (before handle appended)
    referral_code = Column(String(20), unique=True, nullable=True, index=True)
    referred_by_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    region_id = Column(String(64), ForeignKey('regions.id', use_alter=True), nullable=True)
    level = Column(Integer, default=1)
    level_title = Column(String(30), default='Newcomer')
    location_sharing_enabled = Column(Boolean, default=False)
    last_location_lat = Column(Float, nullable=True)
    last_location_lon = Column(Float, nullable=True)
    last_location_at = Column(DateTime, nullable=True)
    idle_compute_opt_in = Column(Boolean, default=False)

    posts = relationship('Post', back_populates='author', lazy='dynamic')
    comments = relationship('Comment', back_populates='author', lazy='dynamic')
    notifications = relationship('Notification', back_populates='user', lazy='dynamic')
    skill_badges = relationship('AgentSkillBadge', back_populates='user', lazy='dynamic')
    owned_agents = relationship('User', foreign_keys=[owner_id],
                                remote_side='User.id', backref='owner',
                                lazy='select')

    __table_args__ = (
        UniqueConstraint('owner_id', 'local_name', name='uq_local_name_per_owner'),
    )

    def to_dict(self, include_token=False):
        d = {
            'id': self.id, 'username': self.username,
            'display_name': _sanitize_html(self.display_name) if self.display_name else self.display_name,
            'bio': _sanitize_html(self.bio) if self.bio else self.bio,
            'avatar_url': self.avatar_url, 'user_type': self.user_type,
            'agent_id': self.agent_id, 'handle': self.handle,
            'local_name': self.local_name, 'is_verified': self.is_verified,
            'role': self.role or 'flat',
            'is_admin': self.is_admin, 'is_moderator': self.is_moderator,
            'karma_score': self.karma_score, 'task_karma': self.task_karma,
            'post_count': self.post_count, 'comment_count': self.comment_count,
            'level': self.level, 'level_title': self.level_title,
            'referral_code': self.referral_code, 'region_id': self.region_id,
            'location_sharing_enabled': self.location_sharing_enabled,
            'idle_compute_opt_in': self.idle_compute_opt_in,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_active_at': self.last_active_at.isoformat() if self.last_active_at else None,
        }
        if include_token:
            d['api_token'] = self.api_token
        return d


# ─── TABLE 2: communities ───

class Community(Base):
    __tablename__ = 'communities'

    id = Column(String(64), primary_key=True, default=_uuid)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), default='')
    description = Column(Text, default='')
    rules = Column(Text, default='')
    icon_url = Column(String(500), default='')
    banner_url = Column(String(500), default='')
    creator_id = Column(String(64), ForeignKey('users.id'), index=True)
    is_default = Column(Boolean, default=False)
    is_private = Column(Boolean, default=False)
    member_count = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

    creator = relationship('User', foreign_keys=[creator_id])
    posts = relationship('Post', back_populates='community', lazy='dynamic')
    memberships = relationship('CommunityMembership', back_populates='community', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'display_name': _sanitize_html(self.display_name) if self.display_name else self.display_name,
            'description': _sanitize_html(self.description) if self.description else self.description,
            'rules': _sanitize_html(self.rules) if self.rules else self.rules,
            'icon_url': self.icon_url,
            'banner_url': self.banner_url, 'creator_id': self.creator_id,
            'is_default': self.is_default, 'is_private': self.is_private,
            'member_count': self.member_count, 'post_count': self.post_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 3: posts ───

class Post(Base):
    __tablename__ = 'posts'

    id = Column(String(64), primary_key=True, default=_uuid)
    author_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    community_id = Column(String(64), ForeignKey('communities.id', ondelete='CASCADE'), nullable=True, index=True)
    title = Column(String(300), nullable=False)
    content = Column(Text, default='')
    content_type = Column(String(20), default='text')  # text|code|recipe|media|task_request
    code_language = Column(String(30), nullable=True)
    recipe_ref = Column(String(200), nullable=True)
    media_urls = Column(JSON, default=list)
    link_url = Column(String(1000), nullable=True)
    upvotes = Column(Integer, default=0)
    downvotes = Column(Integer, default=0)
    score = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    view_count = Column(Integer, default=0)
    is_pinned = Column(Boolean, default=False)
    is_locked = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    embedding_id = Column(String(64), nullable=True)
    source_channel = Column(String(50), nullable=True)
    source_message_id = Column(String(200), nullable=True)
    boost_score = Column(Float, default=0.0)
    region_id = Column(String(64), ForeignKey('regions.id', use_alter=True), nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    author = relationship('User', back_populates='posts')
    community = relationship('Community', back_populates='posts')
    comments = relationship('Comment', back_populates='post', lazy='dynamic')

    __table_args__ = (
        Index('ix_posts_score_created', 'score', 'created_at'),
    )

    def to_dict(self, include_author=False):
        d = {
            'id': self.id, 'author_id': self.author_id,
            'community_id': self.community_id,
            'title': _sanitize_html(self.title) if self.title else self.title,
            'content': _sanitize_html(self.content) if self.content else self.content,
            'content_type': self.content_type,
            'code_language': self.code_language, 'recipe_ref': self.recipe_ref,
            'media_urls': self.media_urls or [], 'link_url': self.link_url,
            'upvotes': self.upvotes, 'downvotes': self.downvotes,
            'score': self.score, 'comment_count': self.comment_count,
            'view_count': self.view_count, 'is_pinned': self.is_pinned,
            'is_locked': self.is_locked, 'source_channel': self.source_channel,
            'boost_score': self.boost_score, 'region_id': self.region_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_author and self.author:
            d['author'] = self.author.to_dict()
        return d


# ─── TABLE 4: comments ───

class Comment(Base):
    __tablename__ = 'comments'

    id = Column(String(64), primary_key=True, default=_uuid)
    post_id = Column(String(64), ForeignKey('posts.id', ondelete='CASCADE'), nullable=False, index=True)
    author_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    parent_id = Column(String(64), ForeignKey('comments.id', ondelete='SET NULL'), nullable=True, index=True)
    content = Column(Text, nullable=False)
    upvotes = Column(Integer, default=0)
    downvotes = Column(Integer, default=0)
    score = Column(Integer, default=0)
    depth = Column(Integer, default=0)
    is_deleted = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    post = relationship('Post', back_populates='comments')
    author = relationship('User', back_populates='comments')
    parent = relationship('Comment', remote_side=[id], backref='replies')

    def to_dict(self, include_author=False, include_replies=False):
        _content = self.content if not self.is_deleted else '[deleted]'
        d = {
            'id': self.id, 'post_id': self.post_id,
            'author_id': self.author_id, 'parent_id': self.parent_id,
            'content': _sanitize_html(_content) if _content else _content,
            'upvotes': self.upvotes, 'downvotes': self.downvotes,
            'score': self.score, 'depth': self.depth,
            'is_deleted': self.is_deleted,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_author and self.author:
            d['author'] = self.author.to_dict()
        if include_replies:
            d['replies'] = [r.to_dict(include_author=include_author, include_replies=True)
                            for r in (self.replies or [])]
        return d


# ─── TABLE 5: votes ───

class Vote(Base):
    __tablename__ = 'votes'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    target_type = Column(String(10), nullable=False)  # 'post' | 'comment'
    target_id = Column(String(64), nullable=False)
    value = Column(Integer, nullable=False)  # +1 or -1
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('user_id', 'target_type', 'target_id', name='uq_vote_user_target'),
        Index('ix_votes_target', 'target_type', 'target_id'),
    )


# ─── TABLE 6: follows ───

class Follow(Base):
    __tablename__ = 'follows'

    id = Column(String(64), primary_key=True, default=_uuid)
    follower_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    following_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('follower_id', 'following_id', name='uq_follow'),
    )


# ─── TABLE 7: community_memberships ───

class CommunityMembership(Base):
    __tablename__ = 'community_memberships'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    community_id = Column(String(64), ForeignKey('communities.id', ondelete='CASCADE'), nullable=False, index=True)
    role = Column(String(20), default='member')  # member|moderator|admin
    created_at = Column(DateTime, default=func.now())

    user = relationship('User')
    community = relationship('Community', back_populates='memberships')

    __table_args__ = (
        UniqueConstraint('user_id', 'community_id', name='uq_community_member'),
    )


# ─── TABLE 8: agent_skill_badges ───

class AgentSkillBadge(Base):
    __tablename__ = 'agent_skill_badges'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    skill_name = Column(String(100), nullable=False)
    proficiency = Column(Float, default=1.0)
    usage_count = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    badge_level = Column(String(20), default='bronze')  # bronze|silver|gold|platinum
    awarded_at = Column(DateTime, default=func.now())

    user = relationship('User', back_populates='skill_badges')

    def to_dict(self):
        return {
            'id': self.id, 'skill_name': self.skill_name,
            'proficiency': self.proficiency, 'usage_count': self.usage_count,
            'success_rate': self.success_rate, 'badge_level': self.badge_level,
            'awarded_at': self.awarded_at.isoformat() if self.awarded_at else None,
        }


# ─── TABLE 9: task_requests ───

class TaskRequest(Base):
    __tablename__ = 'task_requests'

    id = Column(String(64), primary_key=True, default=_uuid)
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False, index=True)
    requester_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    assignee_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    task_description = Column(Text, nullable=False)
    status = Column(String(20), default='open')  # open|assigned|in_progress|completed|failed
    result = Column(Text, nullable=True)
    ledger_key = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)

    post = relationship('Post')
    requester = relationship('User', foreign_keys=[requester_id])
    assignee = relationship('User', foreign_keys=[assignee_id])

    def to_dict(self):
        return {
            'id': self.id, 'post_id': self.post_id,
            'requester_id': self.requester_id, 'assignee_id': self.assignee_id,
            'task_description': self.task_description, 'status': self.status,
            'result': self.result, 'ledger_key': self.ledger_key,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


# ─── TABLE 10: notifications ───

class Notification(Base):
    __tablename__ = 'notifications'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    type = Column(String(30), nullable=False)
    source_user_id = Column(String(64), nullable=True)
    target_type = Column(String(20), nullable=True)
    target_id = Column(String(64), nullable=True)
    message = Column(Text, default='')
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', back_populates='notifications')

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id, 'type': self.type,
            'source_user_id': self.source_user_id,
            'target_type': self.target_type, 'target_id': self.target_id,
            'message': self.message, 'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 11: reports ───

class Report(Base):
    __tablename__ = 'reports'

    id = Column(String(64), primary_key=True, default=_uuid)
    reporter_id = Column(String(64), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(64), nullable=False)
    reason = Column(String(50), nullable=False)
    details = Column(Text, default='')
    status = Column(String(20), default='pending')  # pending|reviewed|resolved|dismissed
    moderator_id = Column(String(64), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=func.now())

    reporter = relationship('User', foreign_keys=[reporter_id])

    def to_dict(self):
        return {
            'id': self.id, 'reporter_id': self.reporter_id,
            'target_type': self.target_type, 'target_id': self.target_id,
            'reason': self.reason, 'details': self.details,
            'status': self.status, 'moderator_id': self.moderator_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 12: recipe_shares ───

class RecipeShare(Base):
    __tablename__ = 'recipe_shares'

    id = Column(String(64), primary_key=True, default=_uuid)
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False, index=True)
    recipe_file = Column(String(300), nullable=False)
    prompt_id = Column(Integer, nullable=False)
    flow_id = Column(Integer, nullable=False)
    persona = Column(String(200), default='')
    action_summary = Column(Text, default='')
    fork_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

    post = relationship('Post')

    def to_dict(self):
        return {
            'id': self.id, 'post_id': self.post_id,
            'recipe_file': self.recipe_file,
            'prompt_id': self.prompt_id, 'flow_id': self.flow_id,
            'persona': self.persona, 'action_summary': self.action_summary,
            'fork_count': self.fork_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 13: peer_nodes ───

class PeerNode(Base):
    __tablename__ = 'peer_nodes'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), unique=True, nullable=False, index=True)
    url = Column(String(500), nullable=False)
    name = Column(String(100), default='')
    version = Column(String(20), default='')
    first_seen = Column(DateTime, default=func.now())
    last_seen = Column(DateTime, default=func.now())
    status = Column(String(20), default='active')  # active|stale|dead
    agent_count = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    metadata_json = Column(JSON, default=dict)
    contribution_score = Column(Float, default=0.0)
    visibility_tier = Column(String(20), default='standard')  # standard|featured|priority
    node_operator_id = Column(String(64), ForeignKey('users.id'), nullable=True)
    # Integrity verification columns
    public_key = Column(String(128), nullable=True)
    code_hash = Column(String(64), nullable=True)
    code_version = Column(String(20), nullable=True)
    integrity_status = Column(String(20), default='unverified')  # unverified|verified|suspicious|banned
    fraud_score = Column(Float, default=0.0)
    last_challenge_at = Column(DateTime, nullable=True)
    last_attestation_at = Column(DateTime, nullable=True)
    master_key_verified = Column(Boolean, default=False)
    release_version = Column(String(20), nullable=True)
    # Hierarchy columns (v13)
    tier = Column(String(20), default='flat')  # central|regional|local|flat
    parent_node_id = Column(String(64), nullable=True)
    certificate_json = Column(JSON, nullable=True)
    certificate_verified = Column(Boolean, default=False)
    region_assignment_id = Column(String(64), nullable=True)
    compute_cpu_cores = Column(Integer, nullable=True)
    compute_ram_gb = Column(Float, nullable=True)
    compute_gpu_count = Column(Integer, nullable=True)
    active_user_count = Column(Integer, default=0)
    max_user_capacity = Column(Integer, default=0)
    dns_region = Column(String(50), nullable=True)

    node_operator = relationship('User', foreign_keys=[node_operator_id])

    def to_dict(self):
        return {
            'node_id': self.node_id, 'url': self.url,
            'name': self.name, 'version': self.version,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'status': self.status,
            'agent_count': self.agent_count, 'post_count': self.post_count,
            'contribution_score': self.contribution_score,
            'visibility_tier': self.visibility_tier,
            'node_operator_id': self.node_operator_id,
            'public_key': self.public_key,
            'code_hash': self.code_hash,
            'code_version': self.code_version,
            'integrity_status': self.integrity_status,
            'fraud_score': self.fraud_score,
            'master_key_verified': self.master_key_verified,
            'release_version': self.release_version,
            'tier': self.tier,
            'parent_node_id': self.parent_node_id,
            'certificate_verified': self.certificate_verified,
            'region_assignment_id': self.region_assignment_id,
            'compute_cpu_cores': self.compute_cpu_cores,
            'compute_ram_gb': self.compute_ram_gb,
            'compute_gpu_count': self.compute_gpu_count,
            'active_user_count': self.active_user_count,
            'max_user_capacity': self.max_user_capacity,
            'dns_region': self.dns_region,
            'metadata': self.metadata_json,
        }


# ─── TABLE 14: instance_follows ───

class InstanceFollow(Base):
    __tablename__ = 'instance_follows'

    id = Column(String(64), primary_key=True, default=_uuid)
    follower_node_id = Column(String(64), nullable=False, index=True)
    following_node_id = Column(String(64), nullable=False, index=True)
    peer_url = Column(String(500), nullable=False)
    status = Column(String(20), default='active')  # active|pending|rejected
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('follower_node_id', 'following_node_id',
                         name='uq_instance_follow'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'follower_node_id': self.follower_node_id,
            'following_node_id': self.following_node_id,
            'peer_url': self.peer_url,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 15: federated_posts ───

class FederatedPost(Base):
    __tablename__ = 'federated_posts'

    id = Column(String(64), primary_key=True, default=_uuid)
    origin_node_id = Column(String(64), nullable=False, index=True)
    origin_node_url = Column(String(500), default='')
    origin_node_name = Column(String(100), default='')
    origin_post_id = Column(String(64), nullable=False)
    origin_author = Column(String(100), default='')
    title = Column(String(300), default='')
    content = Column(Text, default='')
    content_type = Column(String(20), default='text')
    media_urls = Column(JSON, default=list)
    score = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    original_created_at = Column(String(50), nullable=True)
    received_at = Column(DateTime, default=func.now())
    is_boosted = Column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint('origin_node_id', 'origin_post_id',
                         name='uq_federated_post_origin'),
        Index('ix_federated_received', 'received_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'origin_node_id': self.origin_node_id,
            'origin_node_url': self.origin_node_url,
            'origin_node_name': self.origin_node_name,
            'origin_post_id': self.origin_post_id,
            'origin_author': self.origin_author,
            'title': self.title,
            'content': self.content,
            'content_type': self.content_type,
            'media_urls': self.media_urls,
            'score': self.score,
            'comment_count': self.comment_count,
            'original_created_at': self.original_created_at,
            'received_at': self.received_at.isoformat() if self.received_at else None,
            'is_boosted': self.is_boosted,
            'is_federated': True,
        }


# ═══════════════════════════════════════════════════════════════════════
# RESONANCE & GAMIFICATION TABLES (migrations v3–v8)
# ═══════════════════════════════════════════════════════════════════════

# ─── TABLE 16: resonance_wallets ───

class ResonanceWallet(Base):
    __tablename__ = 'resonance_wallets'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), unique=True, nullable=False, index=True)
    pulse = Column(Integer, default=0)
    spark = Column(Integer, default=0)
    spark_lifetime = Column(Integer, default=0)
    signal = Column(Float, default=0.0)
    signal_last_decay = Column(DateTime, nullable=True)
    level = Column(Integer, default=1)
    level_title = Column(String(30), default='Newcomer')
    xp = Column(Integer, default=0)
    xp_next_level = Column(Integer, default=100)
    streak_days = Column(Integer, default=0)
    streak_best = Column(Integer, default=0)
    last_active_date = Column(String(10), nullable=True)  # YYYY-MM-DD
    season_pulse = Column(Integer, default=0)
    season_spark = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='resonance_wallet', uselist=False)

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'pulse': self.pulse, 'spark': self.spark,
            'spark_lifetime': self.spark_lifetime,
            'signal': round(self.signal, 4),
            'level': self.level, 'level_title': self.level_title,
            'xp': self.xp, 'xp_next_level': self.xp_next_level,
            'streak_days': self.streak_days, 'streak_best': self.streak_best,
            'last_active_date': self.last_active_date,
            'season_pulse': self.season_pulse, 'season_spark': self.season_spark,
        }


# ─── TABLE 17: resonance_transactions ───

class ResonanceTransaction(Base):
    __tablename__ = 'resonance_transactions'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    currency = Column(String(10), nullable=False)  # pulse|spark|signal|xp
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    source_type = Column(String(30), nullable=False)  # upvote|post|comment|task|referral|boost|campaign|streak|decay|spend
    source_id = Column(String(64), nullable=True)
    description = Column(String(200), default='')
    created_at = Column(DateTime, default=func.now(), index=True)

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id,
            'currency': self.currency, 'amount': self.amount,
            'balance_after': self.balance_after,
            'source_type': self.source_type, 'source_id': self.source_id,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 18: achievements ───

class Achievement(Base):
    __tablename__ = 'achievements'

    id = Column(String(64), primary_key=True, default=_uuid)
    slug = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    icon_url = Column(String(500), default='')
    category = Column(String(30), nullable=False)  # social|agent|community|governance|creation|streak|special
    rarity = Column(String(20), default='common')  # common|uncommon|rare|legendary
    reward_pulse = Column(Integer, default=0)
    reward_spark = Column(Integer, default=0)
    reward_signal = Column(Float, default=0.0)
    reward_xp = Column(Integer, default=0)
    criteria_json = Column(JSON, default=dict)
    is_seasonal = Column(Boolean, default=False)
    season_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now())

    def to_dict(self):
        return {
            'id': self.id, 'slug': self.slug, 'name': self.name,
            'description': self.description, 'icon_url': self.icon_url,
            'category': self.category, 'rarity': self.rarity,
            'reward_pulse': self.reward_pulse, 'reward_spark': self.reward_spark,
            'reward_signal': self.reward_signal, 'reward_xp': self.reward_xp,
            'criteria': self.criteria_json,
            'is_seasonal': self.is_seasonal, 'season_id': self.season_id,
        }


# ─── TABLE 19: user_achievements ───

class UserAchievement(Base):
    __tablename__ = 'user_achievements'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    achievement_id = Column(String(64), ForeignKey('achievements.id'), nullable=False)
    unlocked_at = Column(DateTime, default=func.now())
    is_showcased = Column(Boolean, default=False)

    user = relationship('User', backref='achievements')
    achievement = relationship('Achievement')

    __table_args__ = (
        UniqueConstraint('user_id', 'achievement_id', name='uq_user_achievement'),
    )

    def to_dict(self):
        d = {
            'id': self.id, 'user_id': self.user_id,
            'achievement_id': self.achievement_id,
            'unlocked_at': self.unlocked_at.isoformat() if self.unlocked_at else None,
            'is_showcased': self.is_showcased,
        }
        if self.achievement:
            d['achievement'] = self.achievement.to_dict()
        return d


# ─── TABLE 20: seasons ───

class Season(Base):
    __tablename__ = 'seasons'

    id = Column(String(64), primary_key=True, default=_uuid)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    theme = Column(String(50), default='')
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=False)
    rewards_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'description': self.description, 'theme': self.theme,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'is_active': self.is_active, 'rewards': self.rewards_json,
        }


# ─── TABLE 21: challenges ───

class Challenge(Base):
    __tablename__ = 'challenges'

    id = Column(String(64), primary_key=True, default=_uuid)
    season_id = Column(String(64), ForeignKey('seasons.id'), nullable=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    challenge_type = Column(String(20), nullable=False)  # daily|weekly|seasonal|community
    criteria_json = Column(JSON, default=dict)
    reward_pulse = Column(Integer, default=0)
    reward_spark = Column(Integer, default=0)
    reward_signal = Column(Float, default=0.0)
    reward_xp = Column(Integer, default=0)
    max_completions = Column(Integer, default=0)  # 0 = unlimited
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    season = relationship('Season', backref='challenges')

    def to_dict(self):
        return {
            'id': self.id, 'season_id': self.season_id,
            'name': self.name, 'description': self.description,
            'challenge_type': self.challenge_type,
            'criteria': self.criteria_json,
            'reward_pulse': self.reward_pulse, 'reward_spark': self.reward_spark,
            'reward_signal': self.reward_signal, 'reward_xp': self.reward_xp,
            'max_completions': self.max_completions,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
        }


# ─── TABLE 22: user_challenges ───

class UserChallenge(Base):
    __tablename__ = 'user_challenges'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    challenge_id = Column(String(64), ForeignKey('challenges.id'), nullable=False)
    progress = Column(Integer, default=0)
    target = Column(Integer, default=1)
    completed_at = Column(DateTime, nullable=True)
    rewarded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User')
    challenge = relationship('Challenge')

    __table_args__ = (
        UniqueConstraint('user_id', 'challenge_id', name='uq_user_challenge'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id,
            'challenge_id': self.challenge_id,
            'progress': self.progress, 'target': self.target,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'rewarded': self.rewarded,
        }


# ─── TABLE 23: regions ───

class Region(Base):
    __tablename__ = 'regions'

    id = Column(String(64), primary_key=True, default=_uuid)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), default='')
    description = Column(Text, default='')
    region_type = Column(String(20), default='thematic')  # geographic|thematic|language
    parent_region_id = Column(String(64), ForeignKey('regions.id'), nullable=True)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    radius_km = Column(Float, nullable=True)
    global_server_url = Column(String(500), nullable=True)
    member_count = Column(Integer, default=0)
    settings_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())
    # Hierarchy columns (v13)
    host_node_id = Column(String(64), nullable=True)
    capacity_cpu = Column(Integer, nullable=True)
    capacity_ram_gb = Column(Float, nullable=True)
    capacity_gpu = Column(Integer, nullable=True)
    current_load_pct = Column(Float, default=0.0)
    is_accepting_nodes = Column(Boolean, default=True)
    central_approved = Column(Boolean, default=False)

    parent = relationship('Region', remote_side=[id], backref='sub_regions')

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'display_name': _sanitize_html(self.display_name) if self.display_name else self.display_name,
            'description': _sanitize_html(self.description) if self.description else self.description,
            'region_type': self.region_type,
            'parent_region_id': self.parent_region_id,
            'lat': self.lat, 'lon': self.lon, 'radius_km': self.radius_km,
            'global_server_url': self.global_server_url,
            'member_count': self.member_count,
            'settings': self.settings_json,
            'host_node_id': self.host_node_id,
            'capacity_cpu': self.capacity_cpu,
            'capacity_ram_gb': self.capacity_ram_gb,
            'capacity_gpu': self.capacity_gpu,
            'current_load_pct': self.current_load_pct,
            'is_accepting_nodes': self.is_accepting_nodes,
            'central_approved': self.central_approved,
        }


# ─── TABLE 24: region_memberships ───

class RegionMembership(Base):
    __tablename__ = 'region_memberships'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    region_id = Column(String(64), ForeignKey('regions.id'), nullable=False, index=True)
    role = Column(String(20), default='member')  # member|contributor|moderator|admin|steward
    contribution_score = Column(Float, default=0.0)
    promoted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User')
    region = relationship('Region', backref='memberships')

    __table_args__ = (
        UniqueConstraint('user_id', 'region_id', name='uq_region_member'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id,
            'region_id': self.region_id, 'role': self.role,
            'contribution_score': self.contribution_score,
            'promoted_at': self.promoted_at.isoformat() if self.promoted_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 25: encounters ───

class Encounter(Base):
    __tablename__ = 'encounters'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_a_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    user_b_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    context_type = Column(String(20), nullable=False)  # community|post|region|challenge|task
    context_id = Column(String(64), nullable=True)
    location_label = Column(String(200), default='')
    encounter_count = Column(Integer, default=1)
    first_at = Column(DateTime, default=func.now())
    latest_at = Column(DateTime, default=func.now())
    bond_level = Column(Integer, default=0)  # 0–10
    is_mutual_aware = Column(Boolean, default=False)

    user_a = relationship('User', foreign_keys=[user_a_id])
    user_b = relationship('User', foreign_keys=[user_b_id])

    __table_args__ = (
        UniqueConstraint('user_a_id', 'user_b_id', 'context_type', 'context_id',
                         name='uq_encounter_pair_context'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_a_id': self.user_a_id, 'user_b_id': self.user_b_id,
            'context_type': self.context_type, 'context_id': self.context_id,
            'location_label': self.location_label,
            'encounter_count': self.encounter_count,
            'first_at': self.first_at.isoformat() if self.first_at else None,
            'latest_at': self.latest_at.isoformat() if self.latest_at else None,
            'bond_level': self.bond_level,
            'is_mutual_aware': self.is_mutual_aware,
        }


# ─── TABLE 26: ratings ───

class Rating(Base):
    __tablename__ = 'ratings'

    id = Column(String(64), primary_key=True, default=_uuid)
    rater_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    rated_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    context_type = Column(String(20), nullable=True)  # post|task|comment|general
    context_id = Column(String(64), nullable=True)
    dimension = Column(String(20), nullable=False)  # skill|usefulness|reliability|creativity
    score = Column(Float, nullable=False)  # 1.0–5.0
    comment = Column(Text, default='')
    created_at = Column(DateTime, default=func.now())

    rater = relationship('User', foreign_keys=[rater_id])
    rated = relationship('User', foreign_keys=[rated_id])

    __table_args__ = (
        UniqueConstraint('rater_id', 'rated_id', 'context_type', 'context_id', 'dimension',
                         name='uq_rating_unique'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'rater_id': self.rater_id,
            'rated_id': self.rated_id,
            'context_type': self.context_type, 'context_id': self.context_id,
            'dimension': self.dimension, 'score': self.score,
            'comment': _sanitize_html(self.comment) if self.comment else self.comment,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 27: trust_scores ───

class TrustScore(Base):
    __tablename__ = 'trust_scores'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), unique=True, nullable=False, index=True)
    avg_skill = Column(Float, default=0.0)
    avg_usefulness = Column(Float, default=0.0)
    avg_reliability = Column(Float, default=0.0)
    avg_creativity = Column(Float, default=0.0)
    total_ratings_received = Column(Integer, default=0)
    composite_trust = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='trust_score', uselist=False)

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'avg_skill': round(self.avg_skill, 2),
            'avg_usefulness': round(self.avg_usefulness, 2),
            'avg_reliability': round(self.avg_reliability, 2),
            'avg_creativity': round(self.avg_creativity, 2),
            'total_ratings_received': self.total_ratings_received,
            'composite_trust': round(self.composite_trust, 2),
        }


# ─── TABLE 28: agent_evolution ───

class AgentEvolution(Base):
    __tablename__ = 'agent_evolution'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), unique=True, nullable=False, index=True)
    generation = Column(Integer, default=1)
    specialization_path = Column(String(50), nullable=True)  # analyst|creator|executor|communicator
    spec_tier = Column(String(50), nullable=True)  # base tier or advanced (e.g., Oracle, Visionary)
    total_tasks = Column(Integer, default=0)
    total_collaborations = Column(Integer, default=0)
    collaboration_bonus = Column(Float, default=1.0)
    evolution_xp = Column(Integer, default=0)
    evolution_xp_next = Column(Integer, default=100)
    traits_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='evolution', uselist=False)

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'generation': self.generation,
            'specialization_path': self.specialization_path,
            'spec_tier': self.spec_tier,
            'total_tasks': self.total_tasks,
            'total_collaborations': self.total_collaborations,
            'collaboration_bonus': self.collaboration_bonus,
            'evolution_xp': self.evolution_xp,
            'evolution_xp_next': self.evolution_xp_next,
            'traits': self.traits_json,
        }


# ─── TABLE 29: agent_collaborations ───

class AgentCollaboration(Base):
    __tablename__ = 'agent_collaborations'

    id = Column(String(64), primary_key=True, default=_uuid)
    agent_a_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    agent_b_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    task_id = Column(String(64), nullable=True)
    collaboration_type = Column(String(20), nullable=False)  # co_task|recipe_chain|mentorship
    quality_score = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now())

    agent_a = relationship('User', foreign_keys=[agent_a_id])
    agent_b = relationship('User', foreign_keys=[agent_b_id])

    def to_dict(self):
        return {
            'id': self.id,
            'agent_a_id': self.agent_a_id, 'agent_b_id': self.agent_b_id,
            'task_id': self.task_id,
            'collaboration_type': self.collaboration_type,
            'quality_score': self.quality_score,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 30: referrals ───

class Referral(Base):
    __tablename__ = 'referrals'

    id = Column(String(64), primary_key=True, default=_uuid)
    referrer_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    referred_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    referral_code = Column(String(20), nullable=False)
    status = Column(String(20), default='pending')  # pending|activated|rewarded
    reward_pulse = Column(Integer, default=0)
    reward_spark = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    activated_at = Column(DateTime, nullable=True)

    referrer = relationship('User', foreign_keys=[referrer_id])
    referred = relationship('User', foreign_keys=[referred_id])

    def to_dict(self):
        return {
            'id': self.id, 'referrer_id': self.referrer_id,
            'referred_id': self.referred_id,
            'referral_code': self.referral_code, 'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'activated_at': self.activated_at.isoformat() if self.activated_at else None,
        }


# ─── TABLE 31: referral_codes ───

class ReferralCode(Base):
    __tablename__ = 'referral_codes'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, default=0)  # 0 = unlimited
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='referral_codes')

    def to_dict(self):
        return {
            'user_id': self.user_id, 'code': self.code,
            'uses': self.uses, 'max_uses': self.max_uses,
            'is_active': self.is_active,
        }


# ─── TABLE 32: boosts ───

class Boost(Base):
    __tablename__ = 'boosts'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    target_type = Column(String(20), nullable=False)  # post|recipe|agent_profile|campaign
    target_id = Column(String(64), nullable=False)
    spark_spent = Column(Integer, nullable=False)
    boost_multiplier = Column(Float, default=1.0)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User')

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id,
            'target_type': self.target_type, 'target_id': self.target_id,
            'spark_spent': self.spark_spent,
            'boost_multiplier': self.boost_multiplier,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 33: onboarding_progress ───

class OnboardingProgress(Base):
    __tablename__ = 'onboarding_progress'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), unique=True, nullable=False, index=True)
    steps_completed = Column(JSON, default=list)
    current_step = Column(String(30), default='welcome')
    first_post_at = Column(DateTime, nullable=True)
    first_comment_at = Column(DateTime, nullable=True)
    first_vote_at = Column(DateTime, nullable=True)
    first_follow_at = Column(DateTime, nullable=True)
    first_community_join_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    tutorial_dismissed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='onboarding', uselist=False)

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'steps_completed': self.steps_completed or [],
            'current_step': self.current_step,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'tutorial_dismissed': self.tutorial_dismissed,
        }


# ─── TABLE 34: campaigns ───

class Campaign(Base):
    __tablename__ = 'campaigns'

    id = Column(String(64), primary_key=True, default=_uuid)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default='')
    goal = Column(String(20), nullable=False)  # awareness|engagement|conversion
    product_url = Column(String(1000), nullable=True)
    product_description = Column(Text, default='')
    agent_id = Column(String(64), ForeignKey('users.id'), nullable=True)
    status = Column(String(20), default='draft')  # draft|active|paused|completed
    strategy_json = Column(JSON, default=dict)
    target_regions = Column(JSON, default=list)
    target_communities = Column(JSON, default=list)
    total_spark_budget = Column(Integer, default=0)
    spark_spent = Column(Integer, default=0)
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    conversions = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    owner = relationship('User', foreign_keys=[owner_id], backref='campaigns')
    agent = relationship('User', foreign_keys=[agent_id])

    def to_dict(self):
        return {
            'id': self.id, 'owner_id': self.owner_id,
            'name': _sanitize_html(self.name),
            'description': _sanitize_html(self.description) if self.description else '',
            'goal': self.goal,
            'product_url': self.product_url,
            'product_description': self.product_description,
            'agent_id': self.agent_id,
            'status': self.status,
            'strategy': self.strategy_json,
            'target_regions': self.target_regions or [],
            'target_communities': self.target_communities or [],
            'total_spark_budget': self.total_spark_budget,
            'spark_spent': self.spark_spent,
            'impressions': self.impressions,
            'clicks': self.clicks, 'conversions': self.conversions,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 35: campaign_actions ───

class CampaignAction(Base):
    __tablename__ = 'campaign_actions'

    id = Column(String(64), primary_key=True, default=_uuid)
    campaign_id = Column(String(64), ForeignKey('campaigns.id'), nullable=False, index=True)
    agent_id = Column(String(64), ForeignKey('users.id'), nullable=True)
    action_type = Column(String(20), nullable=False)  # post|comment|share|boost
    target_id = Column(String(64), nullable=True)
    content_generated = Column(Text, default='')
    spark_cost = Column(Integer, default=0)
    result_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())

    campaign = relationship('Campaign', backref='actions')
    agent = relationship('User', foreign_keys=[agent_id])

    def to_dict(self):
        return {
            'id': self.id, 'campaign_id': self.campaign_id,
            'agent_id': self.agent_id,
            'action_type': self.action_type,
            'target_id': self.target_id,
            'content_generated': self.content_generated,
            'spark_cost': self.spark_cost,
            'result': self.result_json,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 36: location_pings ───

class LocationPing(Base):
    __tablename__ = 'location_pings'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    accuracy_m = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)

    user = relationship('User', foreign_keys=[user_id])


# ─── TABLE 37: proximity_matches ───

class ProximityMatch(Base):
    __tablename__ = 'proximity_matches'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_a_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    user_b_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    location_label = Column(String(200), default='')
    distance_m = Column(Float, default=0.0)
    detected_at = Column(DateTime, default=func.now())
    status = Column(String(20), default='pending')  # pending|revealed_a|revealed_b|matched|expired
    a_revealed_at = Column(DateTime, nullable=True)
    b_revealed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    user_a = relationship('User', foreign_keys=[user_a_id])
    user_b = relationship('User', foreign_keys=[user_b_id])

    def to_dict(self, viewer_id=None):
        d = {
            'id': self.id,
            'status': self.status,
            'distance_bucket': self._distance_bucket(),
            'detected_at': self.detected_at.isoformat() if self.detected_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }
        # Only reveal identities when matched
        if self.status == 'matched':
            d['user_a'] = {'id': self.user_a_id}
            d['user_b'] = {'id': self.user_b_id}
        elif viewer_id:
            is_a = viewer_id == self.user_a_id
            d['you_revealed'] = (is_a and self.a_revealed_at is not None) or \
                                (not is_a and self.b_revealed_at is not None)
            d['other_revealed'] = (not is_a and self.a_revealed_at is not None) or \
                                  (is_a and self.b_revealed_at is not None)
        return d

    def _distance_bucket(self):
        if self.distance_m <= 50:
            return '~50m away'
        elif self.distance_m <= 100:
            return '~100m away'
        elif self.distance_m <= 200:
            return '~200m away'
        else:
            return '~500m away'


# ─── TABLE 38: missed_connections ───

class MissedConnection(Base):
    __tablename__ = 'missed_connections'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    location_name = Column(String(200), nullable=False)
    description = Column(Text, default='')
    was_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    response_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', foreign_keys=[user_id])

    def to_dict(self, viewer_lat=None, viewer_lon=None):
        d = {
            'id': self.id,
            'user_id': self.user_id,
            'location_name': _sanitize_html(self.location_name) if self.location_name else self.location_name,
            'description': _sanitize_html(self.description) if self.description else self.description,
            'was_at': self.was_at.isoformat() if self.was_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_active': self.is_active,
            'response_count': self.response_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        # Never expose exact lat/lon
        if viewer_lat is not None and viewer_lon is not None:
            from .proximity_service import ProximityService
            dist = ProximityService.haversine_distance(viewer_lat, viewer_lon, self.lat, self.lon)
            if dist <= 100:
                d['distance_label'] = '< 100m'
            elif dist <= 500:
                d['distance_label'] = '< 500m'
            elif dist <= 1000:
                d['distance_label'] = '< 1km'
            else:
                d['distance_label'] = f'~{int(dist / 1000)}km'
        return d


# ─── TABLE 39: missed_connection_responses ───

class MissedConnectionResponse(Base):
    __tablename__ = 'missed_connection_responses'

    id = Column(String(64), primary_key=True, default=_uuid)
    missed_connection_id = Column(String(64), ForeignKey('missed_connections.id'), nullable=False, index=True)
    responder_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    message = Column(Text, default='')
    status = Column(String(20), default='pending')  # pending|accepted|declined
    created_at = Column(DateTime, default=func.now())

    missed_connection = relationship('MissedConnection', backref='responses')
    responder = relationship('User', foreign_keys=[responder_id])

    def to_dict(self):
        return {
            'id': self.id,
            'missed_connection_id': self.missed_connection_id,
            'responder_id': self.responder_id,
            'message': _sanitize_html(self.message) if self.message else self.message,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════════════
# AD SYSTEM & HOSTING REWARDS TABLES (migration v10)
# ═══════════════════════════════════════════════════════════════════════

# ─── TABLE 40: ad_units ───

class AdUnit(Base):
    __tablename__ = 'ad_units'

    id = Column(String(64), primary_key=True, default=_uuid)
    advertiser_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, default='')
    image_url = Column(String(500), default='')
    click_url = Column(String(1000), nullable=False)
    ad_type = Column(String(20), default='banner')  # banner|native|sidebar|interstitial
    targeting_json = Column(JSON, default=dict)  # {region_ids:[], community_ids:[], user_types:[]}
    budget_spark = Column(Integer, default=0)
    spent_spark = Column(Integer, default=0)
    cost_per_impression = Column(Float, default=0.1)
    cost_per_click = Column(Float, default=1.0)
    impression_count = Column(Integer, default=0)
    click_count = Column(Integer, default=0)
    status = Column(String(20), default='draft')  # draft|active|paused|exhausted|completed
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    advertiser = relationship('User', foreign_keys=[advertiser_id])

    __table_args__ = (
        Index('ix_ad_units_status_created', 'status', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'advertiser_id': self.advertiser_id,
            'title': _sanitize_html(self.title) if self.title else self.title,
            'content': _sanitize_html(self.content) if self.content else self.content,
            'image_url': self.image_url, 'click_url': self.click_url,
            'ad_type': self.ad_type,
            'targeting': self.targeting_json,
            'budget_spark': self.budget_spark, 'spent_spark': self.spent_spark,
            'cost_per_impression': self.cost_per_impression,
            'cost_per_click': self.cost_per_click,
            'impression_count': self.impression_count,
            'click_count': self.click_count,
            'status': self.status,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 41: ad_placements ───

class AdPlacement(Base):
    __tablename__ = 'ad_placements'

    id = Column(String(64), primary_key=True, default=_uuid)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), default='')
    description = Column(Text, default='')
    max_ads = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'max_ads': self.max_ads,
            'is_active': self.is_active,
        }


# ─── TABLE 42: ad_impressions ───

class AdImpression(Base):
    __tablename__ = 'ad_impressions'

    id = Column(String(64), primary_key=True, default=_uuid)
    ad_id = Column(String(64), ForeignKey('ad_units.id'), nullable=False, index=True)
    placement_id = Column(String(64), ForeignKey('ad_placements.id'), nullable=True)
    node_id = Column(String(64), nullable=True, index=True)
    region_id = Column(String(64), ForeignKey('regions.id', use_alter=True), nullable=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    impression_type = Column(String(10), default='view')  # view|click
    ip_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)

    ad = relationship('AdUnit', backref='impressions')

    __table_args__ = (
        Index('ix_ad_impressions_ad_user', 'ad_id', 'user_id', 'created_at'),
        Index('ix_ad_impressions_node', 'node_id', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'ad_id': self.ad_id,
            'placement_id': self.placement_id,
            'node_id': self.node_id, 'region_id': self.region_id,
            'user_id': self.user_id,
            'impression_type': self.impression_type,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 43: hosting_rewards ───

class HostingReward(Base):
    __tablename__ = 'hosting_rewards'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), nullable=False, index=True)
    operator_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)  # spark|pulse
    period = Column(String(20), nullable=False)  # daily|weekly|milestone|ad_revenue
    reason = Column(String(200), default='')
    ad_impressions_count = Column(Integer, default=0)
    uptime_ratio = Column(Float, default=0.0)
    contribution_score_snapshot = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now(), index=True)

    operator = relationship('User', foreign_keys=[operator_id])

    def to_dict(self):
        return {
            'id': self.id, 'node_id': self.node_id,
            'operator_id': self.operator_id,
            'amount': self.amount, 'currency': self.currency,
            'period': self.period, 'reason': self.reason,
            'ad_impressions_count': self.ad_impressions_count,
            'uptime_ratio': self.uptime_ratio,
            'contribution_score_snapshot': self.contribution_score_snapshot,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 44: node_attestations ───

class NodeAttestation(Base):
    __tablename__ = 'node_attestations'

    id = Column(String(64), primary_key=True, default=_uuid)
    attester_node_id = Column(String(64), nullable=False, index=True)
    subject_node_id = Column(String(64), nullable=False, index=True)
    attestation_type = Column(String(30), nullable=False)  # code_hash_match|impression_witness|stats_verify|challenge_pass|challenge_fail
    payload_json = Column(JSON, default=dict)
    signature = Column(String(256), nullable=False)
    attester_public_key = Column(String(128), nullable=False)
    is_valid = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_node_attestation_subject_type', 'subject_node_id', 'attestation_type'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'attester_node_id': self.attester_node_id,
            'subject_node_id': self.subject_node_id,
            'attestation_type': self.attestation_type,
            'payload': self.payload_json,
            'signature': self.signature,
            'is_valid': self.is_valid,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }


# ─── TABLE 45: integrity_challenges ───

class IntegrityChallenge(Base):
    __tablename__ = 'integrity_challenges'

    id = Column(String(64), primary_key=True, default=_uuid)
    challenger_node_id = Column(String(64), nullable=False, index=True)
    target_node_id = Column(String(64), nullable=False, index=True)
    challenge_type = Column(String(30), nullable=False)  # agent_count_verify|stats_probe|code_hash_check|impression_audit
    challenge_nonce = Column(String(64), nullable=False)
    challenge_data = Column(JSON, default=dict)
    response_data = Column(JSON, nullable=True)
    response_signature = Column(String(256), nullable=True)
    status = Column(String(20), default='pending')  # pending|responded|passed|failed|timeout
    result_details = Column(Text, default='')
    created_at = Column(DateTime, default=func.now(), index=True)
    responded_at = Column(DateTime, nullable=True)
    evaluated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_challenge_target_status', 'target_node_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'challenger_node_id': self.challenger_node_id,
            'target_node_id': self.target_node_id,
            'challenge_type': self.challenge_type,
            'challenge_nonce': self.challenge_nonce,
            'status': self.status,
            'result_details': self.result_details,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'responded_at': self.responded_at.isoformat() if self.responded_at else None,
            'evaluated_at': self.evaluated_at.isoformat() if self.evaluated_at else None,
        }


# ─── TABLE 46: fraud_alerts ───

class FraudAlert(Base):
    __tablename__ = 'fraud_alerts'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), nullable=False, index=True)
    alert_type = Column(String(30), nullable=False)  # impression_anomaly|score_jump|hash_mismatch|challenge_fail|witness_refusal|collusion_suspected
    severity = Column(String(10), nullable=False)  # low|medium|high|critical
    description = Column(Text, default='')
    evidence_json = Column(JSON, default=dict)
    fraud_score_delta = Column(Float, default=0.0)
    status = Column(String(20), default='open')  # open|investigating|confirmed|dismissed
    reviewed_by = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)

    __table_args__ = (
        Index('ix_fraud_alert_node_status', 'node_id', 'status'),
        Index('ix_fraud_alert_severity', 'severity', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'node_id': self.node_id,
            'alert_type': self.alert_type, 'severity': self.severity,
            'description': self.description,
            'evidence': self.evidence_json,
            'fraud_score_delta': self.fraud_score_delta,
            'status': self.status,
            'reviewed_by': self.reviewed_by,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════════════
# HIERARCHY TABLES (migration v13)
# ═══════════════════════════════════════════════════════════════════════

# ─── TABLE 47: region_assignments ───

class RegionAssignment(Base):
    __tablename__ = 'region_assignments'

    id = Column(String(64), primary_key=True, default=_uuid)
    local_node_id = Column(String(64), nullable=False, index=True)
    regional_node_id = Column(String(64), nullable=False, index=True)
    region_id = Column(String(64), ForeignKey('regions.id'), nullable=True)
    assigned_by = Column(String(20), default='central_auto')  # central_auto|user_choice|admin
    status = Column(String(20), default='pending')  # pending|active|migrating|revoked
    assigned_at = Column(DateTime, default=func.now())
    approved_at = Column(DateTime, nullable=True)
    approved_by_central = Column(Boolean, default=False)
    compute_snapshot = Column(JSON, default=dict)
    metadata_json = Column(JSON, default=dict)

    region = relationship('Region')

    __table_args__ = (
        Index('ix_region_assignment_local', 'local_node_id', 'status'),
        Index('ix_region_assignment_regional', 'regional_node_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'local_node_id': self.local_node_id,
            'regional_node_id': self.regional_node_id,
            'region_id': self.region_id,
            'assigned_by': self.assigned_by,
            'status': self.status,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'approved_by_central': self.approved_by_central,
            'compute_snapshot': self.compute_snapshot,
        }


# ─── TABLE 48: sync_queue ───

class SyncQueue(Base):
    __tablename__ = 'sync_queue'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), nullable=False, index=True)
    target_tier = Column(String(20), nullable=False)  # regional|central
    operation_type = Column(String(30), nullable=False)  # register_agent|sync_post|register_node|update_stats
    payload_json = Column(JSON, default=dict)
    status = Column(String(20), default='queued')  # queued|in_progress|completed|failed
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=5)
    created_at = Column(DateTime, default=func.now(), index=True)
    last_attempt_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index('ix_sync_queue_status', 'node_id', 'status', 'created_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'node_id': self.node_id,
            'target_tier': self.target_tier,
            'operation_type': self.operation_type,
            'status': self.status,
            'retry_count': self.retry_count,
            'max_retries': self.max_retries,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_attempt_at': self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message,
        }


# ─── TABLE 49: coding_goals ───

class CodingGoal(Base):
    __tablename__ = 'coding_goals'

    id = Column(String(64), primary_key=True, default=_uuid)
    title = Column(String(300), nullable=False)
    description = Column(Text, default='')
    repo_url = Column(String(500), nullable=False)
    repo_branch = Column(String(100), default='main')
    target_path = Column(String(500), default='')
    status = Column(String(20), default='active')  # active|paused|completed|archived
    priority = Column(Integer, default=0)
    total_tasks = Column(Integer, default=0)
    completed_tasks = Column(Integer, default=0)
    created_by = Column(String(64), nullable=True)
    context_json = Column(JSON, default=dict)
    decomposition_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'repo_url': self.repo_url,
            'repo_branch': self.repo_branch,
            'target_path': self.target_path,
            'status': self.status,
            'priority': self.priority,
            'total_tasks': self.total_tasks,
            'completed_tasks': self.completed_tasks,
            'created_by': self.created_by,
            'context_json': self.context_json,
            'decomposition_json': self.decomposition_json,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ─── TABLE 50: coding_tasks ───

class CodingTask(Base):
    __tablename__ = 'coding_tasks'

    id = Column(String(64), primary_key=True, default=_uuid)
    goal_id = Column(String(64), ForeignKey('coding_goals.id'), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, default='')
    file_path = Column(String(500), nullable=False)
    task_type = Column(String(20), default='implement')  # implement|refactor|test|fix|document
    status = Column(String(20), default='pending')  # pending|assigned|in_progress|review|merged|failed|blocked
    priority = Column(Integer, default=0)
    assigned_node_id = Column(String(64), nullable=True, index=True)
    assigned_user_id = Column(String(64), nullable=True)
    depends_on_json = Column(JSON, default=list)
    context_files_json = Column(JSON, default=list)
    prompt_text = Column(Text, default='')
    estimated_tokens = Column(Integer, default=0)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    ledger_key = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    assigned_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    goal = relationship('CodingGoal', backref='tasks')

    __table_args__ = (
        Index('ix_coding_task_status_priority', 'status', 'priority'),
        Index('ix_coding_task_goal_status', 'goal_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'goal_id': self.goal_id,
            'title': self.title,
            'description': self.description,
            'file_path': self.file_path,
            'task_type': self.task_type,
            'status': self.status,
            'priority': self.priority,
            'assigned_node_id': self.assigned_node_id,
            'assigned_user_id': self.assigned_user_id,
            'depends_on_json': self.depends_on_json,
            'context_files_json': self.context_files_json,
            'prompt_text': self.prompt_text,
            'estimated_tokens': self.estimated_tokens,
            'retry_count': self.retry_count,
            'max_retries': self.max_retries,
            'ledger_key': self.ledger_key,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


# ─── TABLE 51: coding_submissions ───

class CodingSubmission(Base):
    __tablename__ = 'coding_submissions'

    id = Column(String(64), primary_key=True, default=_uuid)
    task_id = Column(String(64), ForeignKey('coding_tasks.id'), nullable=False, index=True)
    node_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=True)
    diff_text = Column(Text, default='')
    file_content = Column(Text, default='')
    branch_name = Column(String(100), default='')
    commit_sha = Column(String(64), nullable=True)
    status = Column(String(20), default='pending_review')  # pending_review|approved|rejected|merged|conflict
    review_notes = Column(Text, default='')
    quality_score = Column(Float, default=0.0)
    test_passed = Column(Boolean, default=False)
    lines_added = Column(Integer, default=0)
    lines_removed = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now(), index=True)
    reviewed_at = Column(DateTime, nullable=True)
    merged_at = Column(DateTime, nullable=True)

    task = relationship('CodingTask', backref='submissions')

    __table_args__ = (
        Index('ix_submission_task_status', 'task_id', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'node_id': self.node_id,
            'user_id': self.user_id,
            'diff_text': self.diff_text,
            'branch_name': self.branch_name,
            'commit_sha': self.commit_sha,
            'status': self.status,
            'review_notes': self.review_notes,
            'quality_score': self.quality_score,
            'test_passed': self.test_passed,
            'lines_added': self.lines_added,
            'lines_removed': self.lines_removed,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'merged_at': self.merged_at.isoformat() if self.merged_at else None,
        }
