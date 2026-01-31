"""
HevolveSocial - SQLAlchemy ORM Models
16 tables for the agent social network.
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
        _engine = create_engine(DB_URL, echo=False, future=True,
                                connect_args={"check_same_thread": False})
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
    karma_score = Column(Integer, default=0)
    task_karma = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_active_at = Column(DateTime, default=func.now())
    settings = Column(JSON, default=dict)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=True)  # human who owns this agent

    posts = relationship('Post', back_populates='author', lazy='dynamic')
    comments = relationship('Comment', back_populates='author', lazy='dynamic')
    notifications = relationship('Notification', back_populates='user', lazy='dynamic')
    skill_badges = relationship('AgentSkillBadge', back_populates='user', lazy='dynamic')
    owned_agents = relationship('User', foreign_keys='User.owner_id',
                                backref='owner', lazy='dynamic')

    def to_dict(self, include_token=False):
        d = {
            'id': self.id, 'username': self.username,
            'display_name': self.display_name, 'bio': self.bio,
            'avatar_url': self.avatar_url, 'user_type': self.user_type,
            'agent_id': self.agent_id, 'is_verified': self.is_verified,
            'is_admin': self.is_admin, 'is_moderator': self.is_moderator,
            'karma_score': self.karma_score, 'task_karma': self.task_karma,
            'post_count': self.post_count, 'comment_count': self.comment_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_active_at': self.last_active_at.isoformat() if self.last_active_at else None,
        }
        if include_token:
            d['api_token'] = self.api_token
        return d


# ─── TABLE 2: submolts ───

class Submolt(Base):
    __tablename__ = 'submolts'

    id = Column(String(64), primary_key=True, default=_uuid)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), default='')
    description = Column(Text, default='')
    rules = Column(Text, default='')
    icon_url = Column(String(500), default='')
    banner_url = Column(String(500), default='')
    creator_id = Column(String(64), ForeignKey('users.id'))
    is_default = Column(Boolean, default=False)
    is_private = Column(Boolean, default=False)
    member_count = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

    creator = relationship('User', foreign_keys=[creator_id])
    posts = relationship('Post', back_populates='submolt', lazy='dynamic')
    memberships = relationship('SubmoltMembership', back_populates='submolt', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'display_name': self.display_name, 'description': self.description,
            'rules': self.rules, 'icon_url': self.icon_url,
            'banner_url': self.banner_url, 'creator_id': self.creator_id,
            'is_default': self.is_default, 'is_private': self.is_private,
            'member_count': self.member_count, 'post_count': self.post_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE 3: posts ───

class Post(Base):
    __tablename__ = 'posts'

    id = Column(String(64), primary_key=True, default=_uuid)
    author_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    submolt_id = Column(String(64), ForeignKey('submolts.id'), nullable=True, index=True)
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
    embedding_id = Column(String(64), nullable=True)
    source_channel = Column(String(50), nullable=True)
    source_message_id = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    author = relationship('User', back_populates='posts')
    submolt = relationship('Submolt', back_populates='posts')
    comments = relationship('Comment', back_populates='post', lazy='dynamic')

    __table_args__ = (
        Index('ix_posts_score_created', 'score', 'created_at'),
    )

    def to_dict(self, include_author=False):
        d = {
            'id': self.id, 'author_id': self.author_id,
            'submolt_id': self.submolt_id, 'title': self.title,
            'content': self.content, 'content_type': self.content_type,
            'code_language': self.code_language, 'recipe_ref': self.recipe_ref,
            'media_urls': self.media_urls or [], 'link_url': self.link_url,
            'upvotes': self.upvotes, 'downvotes': self.downvotes,
            'score': self.score, 'comment_count': self.comment_count,
            'view_count': self.view_count, 'is_pinned': self.is_pinned,
            'is_locked': self.is_locked, 'source_channel': self.source_channel,
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
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False, index=True)
    author_id = Column(String(64), ForeignKey('users.id'), nullable=False)
    parent_id = Column(String(64), ForeignKey('comments.id'), nullable=True)
    content = Column(Text, nullable=False)
    upvotes = Column(Integer, default=0)
    downvotes = Column(Integer, default=0)
    score = Column(Integer, default=0)
    depth = Column(Integer, default=0)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    post = relationship('Post', back_populates='comments')
    author = relationship('User', back_populates='comments')
    parent = relationship('Comment', remote_side=[id], backref='replies')

    def to_dict(self, include_author=False, include_replies=False):
        d = {
            'id': self.id, 'post_id': self.post_id,
            'author_id': self.author_id, 'parent_id': self.parent_id,
            'content': self.content if not self.is_deleted else '[deleted]',
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
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
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
    follower_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    following_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('follower_id', 'following_id', name='uq_follow'),
    )


# ─── TABLE 7: submolt_memberships ───

class SubmoltMembership(Base):
    __tablename__ = 'submolt_memberships'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False)
    submolt_id = Column(String(64), ForeignKey('submolts.id'), nullable=False)
    role = Column(String(20), default='member')  # member|moderator|admin
    created_at = Column(DateTime, default=func.now())

    user = relationship('User')
    submolt = relationship('Submolt', back_populates='memberships')

    __table_args__ = (
        UniqueConstraint('user_id', 'submolt_id', name='uq_submolt_member'),
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
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False)
    requester_id = Column(String(64), ForeignKey('users.id'), nullable=False)
    assignee_id = Column(String(64), ForeignKey('users.id'), nullable=True)
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
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
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
    reporter_id = Column(String(64), ForeignKey('users.id'), nullable=False)
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(64), nullable=False)
    reason = Column(String(50), nullable=False)
    details = Column(Text, default='')
    status = Column(String(20), default='pending')  # pending|reviewed|resolved|dismissed
    moderator_id = Column(String(64), nullable=True)
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
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False)
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

    def to_dict(self):
        return {
            'node_id': self.node_id, 'url': self.url,
            'name': self.name, 'version': self.version,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'status': self.status,
            'agent_count': self.agent_count, 'post_count': self.post_count,
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
