"""
Local model definitions fallback.
Used when sql package (Hevolve_Database) is not installed (e.g. standalone Docker).
Schema kept in sync with Hevolve_Database via consolidation verification.

This module is ONLY imported by models.py (line ~226) when sql.models is unavailable.
It imports Base, _uuid, _sanitize_html from models.py which are already defined by
the time this module is reached (no circular import issue).
"""
# noqa: E501
# ruff: noqa

from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean,
    DateTime, JSON, ForeignKey, UniqueConstraint, Index, func
)
from sqlalchemy.orm import relationship

# Import from parent models.py — these are already defined before models.py
# reaches the `from _models_local import ...` line, so partial-module import works.
from integrations.social.models import Base, _uuid, _sanitize_html


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
    # Thought Experiment fields
    intent_category = Column(String(30), nullable=True)   # community|environment|education|health|equity|technology
    hypothesis = Column(Text, nullable=True)               # "If we do X, then Y"
    expected_outcome = Column(Text, nullable=True)          # Expected net positive
    is_thought_experiment = Column(Boolean, default=False)
    dynamic_layout = Column(JSON, nullable=True)            # Liquid UI layout JSON
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
            'intent_category': self.intent_category,
            'hypothesis': self.hypothesis,
            'expected_outcome': self.expected_outcome,
            'is_thought_experiment': self.is_thought_experiment or False,
            'is_hidden': self.is_hidden or False,
            'dynamic_layout': self.dynamic_layout,
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
            'is_hidden': self.is_hidden or False,
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
    tier = Column(String(20), default='flat')  # TOPOLOGY MODE: central|regional|local|flat (NOT capability tier)
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
    # HART OS equilibrium: contribution tier + enabled features
    capability_tier = Column(String(20), nullable=True)       # CAPABILITY TIER: embedded|observer|lite|standard|full|compute_host
    enabled_features_json = Column(JSON, nullable=True)       # ["agent_engine", "tts", ...]
    # E2E encryption: X25519 public key for encrypted inter-node communication
    x25519_public = Column(String(64), nullable=True)         # Hex-encoded X25519 public key (32 bytes)
    # Fail2ban: progressive ban tracking
    ban_count = Column(Integer, default=0)                    # How many times this node has been banned
    ban_until = Column(DateTime, nullable=True)               # When current ban expires (None = no ban)
    # Usage tracking (cumulative, periodically aggregated by aggregate_compute_stats)
    gpu_hours_served = Column(Float, default=0.0)
    total_inferences = Column(Integer, default=0)
    energy_kwh_contributed = Column(Float, default=0.0)
    metered_api_costs_absorbed = Column(Float, default=0.0)  # USD of metered API used for hive
    # Provider identity (gossipped to network — single source of truth)
    electricity_rate_kwh = Column(Float, nullable=True)
    cause_alignment = Column(String(200), nullable=True)

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
            'capability_tier': self.capability_tier,
            'enabled_features': self.enabled_features_json,
            'x25519_public': self.x25519_public,
            'ban_count': self.ban_count,
            'ban_until': self.ban_until.isoformat() if self.ban_until else None,
            'gpu_hours_served': self.gpu_hours_served,
            'total_inferences': self.total_inferences,
            'energy_kwh_contributed': self.energy_kwh_contributed,
            'metered_api_costs_absorbed': self.metered_api_costs_absorbed,
            'electricity_rate_kwh': self.electricity_rate_kwh,
            'cause_alignment': self.cause_alignment,
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
    # Impression immutability - seal after witness attestation
    witness_node_id = Column(String(64), nullable=True)
    witness_signature = Column(String(256), nullable=True)
    sealed_hash = Column(String(64), nullable=True)
    sealed_at = Column(DateTime, nullable=True)

    ad = relationship('AdUnit', backref='impressions')

    __table_args__ = (
        Index('ix_ad_impressions_ad_user', 'ad_id', 'user_id', 'created_at'),
        Index('ix_ad_impressions_node', 'node_id', 'created_at'),
    )

    @property
    def compute_seal_hash(self) -> str:
        """SHA-256 of canonical impression data for tamper detection."""
        import hashlib
        import json as _json
        canonical = _json.dumps({
            'id': self.id, 'ad_id': self.ad_id, 'node_id': self.node_id,
            'user_id': self.user_id, 'impression_type': self.impression_type,
            'created_at': self.created_at.isoformat() if self.created_at else '',
            'witness_node_id': self.witness_node_id or '',
        }, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self):
        return {
            'id': self.id, 'ad_id': self.ad_id,
            'placement_id': self.placement_id,
            'node_id': self.node_id, 'region_id': self.region_id,
            'user_id': self.user_id,
            'impression_type': self.impression_type,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'witness_node_id': self.witness_node_id,
            'witness_signature': self.witness_signature,
            'sealed_hash': self.sealed_hash,
            'sealed_at': self.sealed_at.isoformat() if self.sealed_at else None,
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


# ─── TABLE 52: products ───

class Product(Base):
    """A product that can be marketed by the autonomous marketing agent."""
    __tablename__ = 'products'

    id = Column(String(64), primary_key=True, default=_uuid)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    name = Column(String(300), nullable=False)
    description = Column(Text, default='')
    tagline = Column(String(500), default='')
    product_url = Column(String(500), default='')
    logo_url = Column(String(500), default='')
    category = Column(String(50), default='general')  # saas|ecommerce|content|service|platform|general
    target_audience = Column(Text, default='')
    unique_value_prop = Column(Text, default='')
    keywords_json = Column(JSON, default=list)
    is_platform_product = Column(Boolean, default=False)
    status = Column(String(20), default='active')  # active|paused|archived
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'owner_id': self.owner_id,
            'name': self.name,
            'description': self.description,
            'tagline': self.tagline,
            'product_url': self.product_url,
            'logo_url': self.logo_url,
            'category': self.category,
            'target_audience': self.target_audience,
            'unique_value_prop': self.unique_value_prop,
            'keywords': self.keywords_json or [],
            'is_platform_product': self.is_platform_product,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ─── TABLE 53: agent_goals ───

class AgentGoal(Base):
    """Unified goal for any autonomous agent type (marketing, coding, analytics, etc.).

    The goal_type field determines which prompt builder and tools are used.
    config_json holds type-specific configuration. Adding a new agent type
    is just a new goal_type value + prompt builder registration.
    """
    __tablename__ = 'agent_goals'

    id = Column(String(64), primary_key=True, default=_uuid)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=True, index=True)
    goal_type = Column(String(50), nullable=False, index=True)  # marketing|coding|analytics|support|...
    title = Column(String(500), nullable=False)
    description = Column(Text, default='')
    status = Column(String(20), default='active', index=True)  # active|paused|completed|archived
    priority = Column(Integer, default=0)

    # Type-specific config (repo_url for coding, channels for marketing, etc.)
    config_json = Column(JSON, default=dict)

    # Marketing-specific (nullable for non-marketing goals)
    product_id = Column(String(64), ForeignKey('products.id'), nullable=True, index=True)

    # Budget
    spark_budget = Column(Integer, default=200)
    spark_spent = Column(Integer, default=0)

    # Tracking
    created_by = Column(String(64), nullable=True)
    prompt_id = Column(String(100), nullable=True)  # Links to prompts/{prompt_id}.json
    last_dispatched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    product = relationship('Product', backref='goals')

    __table_args__ = (
        Index('ix_agent_goal_type_status', 'goal_type', 'status'),
    )

    def to_dict(self):
        config = self.config_json or {}
        result = {
            'id': self.id,
            'owner_id': self.owner_id,
            'goal_type': self.goal_type,
            'title': self.title,
            'description': self.description,
            'status': self.status,
            'priority': self.priority,
            'product_id': self.product_id,
            'spark_budget': self.spark_budget,
            'spark_spent': self.spark_spent,
            'created_by': self.created_by,
            'prompt_id': self.prompt_id,
            'last_dispatched_at': self.last_dispatched_at.isoformat() if self.last_dispatched_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        # Merge type-specific config into result
        result.update(config)
        return result


# ─── TABLE 54: ip_patents ───

class IPPatent(Base):
    """Patent application tracking for autonomous IP protection agent."""
    __tablename__ = 'ip_patents'

    id = Column(String(64), primary_key=True, default=_uuid)
    title = Column(String(500), nullable=False)
    status = Column(String(30), default='draft', index=True)  # draft|filed|provisional|granted|rejected
    filing_type = Column(String(30), default='provisional')  # provisional|utility|pct

    # Patent content
    claims_json = Column(JSON, default=list)
    abstract = Column(Text, default='')
    description = Column(Text, default='')

    # Filing details
    filing_date = Column(DateTime, nullable=True)
    application_number = Column(String(50), nullable=True)
    patent_number = Column(String(50), nullable=True)

    # Verification evidence (loop health snapshot at filing time)
    verification_metrics = Column(JSON, default=dict)
    evidence_json = Column(JSON, default=list)

    # Tracking
    created_by = Column(String(64), nullable=True)
    goal_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'status': self.status,
            'filing_type': self.filing_type,
            'claims': self.claims_json or [],
            'abstract': self.abstract,
            'description': self.description,
            'filing_date': self.filing_date.isoformat() if self.filing_date else None,
            'application_number': self.application_number,
            'patent_number': self.patent_number,
            'verification_metrics': self.verification_metrics or {},
            'evidence': self.evidence_json or [],
            'created_by': self.created_by,
            'goal_id': self.goal_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ─── TABLE 55: ip_infringements ───

class IPInfringement(Base):
    """Tracked infringement cases for IP enforcement agent."""
    __tablename__ = 'ip_infringements'

    id = Column(String(64), primary_key=True, default=_uuid)
    patent_id = Column(String(64), ForeignKey('ip_patents.id'), nullable=True)

    infringer_name = Column(String(300), nullable=False)
    infringer_url = Column(String(1000), nullable=True)
    evidence_summary = Column(Text, default='')
    risk_level = Column(String(20), default='low')  # low|medium|high
    status = Column(String(30), default='detected', index=True)  # detected|reviewed|notice_sent|resolved|dismissed

    # Actions taken
    notice_sent_at = Column(DateTime, nullable=True)
    notice_type = Column(String(30), nullable=True)  # cease_desist|dmca|licensing_offer
    notice_text = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    patent = relationship('IPPatent', backref='infringements')

    def to_dict(self):
        return {
            'id': self.id,
            'patent_id': self.patent_id,
            'infringer_name': self.infringer_name,
            'infringer_url': self.infringer_url,
            'evidence_summary': self.evidence_summary,
            'risk_level': self.risk_level,
            'status': self.status,
            'notice_sent_at': self.notice_sent_at.isoformat() if self.notice_sent_at else None,
            'notice_type': self.notice_type,
            'notice_text': self.notice_text,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 56 - Defensive Publications (prior art proof, not patents)
# ═══════════════════════════════════════════════════════════════

class DefensivePublication(Base):
    """Timestamped proof-of-invention for legal prior art defence."""
    __tablename__ = 'defensive_publications'

    id = Column(String(64), primary_key=True, default=_uuid)
    title = Column(String(500), nullable=False)
    abstract = Column(Text, default='')
    content_hash = Column(String(64), nullable=False)       # SHA-256 of full content
    git_commit_hash = Column(String(40), nullable=True)
    code_snapshot_hash = Column(String(64), nullable=True)   # compute_code_hash() at time
    publication_date = Column(DateTime, default=func.now())
    signed_by_node_key = Column(String(128), nullable=True)
    signature_hex = Column(String(256), nullable=True)       # Ed25519 signature of content_hash
    moat_score_at_publication = Column(Float, default=0.0)
    verification_snapshot = Column(JSON, default=dict)       # verify_exponential_improvement() snapshot
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'abstract': self.abstract,
            'content_hash': self.content_hash,
            'git_commit_hash': self.git_commit_hash,
            'code_snapshot_hash': self.code_snapshot_hash,
            'publication_date': self.publication_date.isoformat() if self.publication_date else None,
            'signed_by_node_key': self.signed_by_node_key,
            'moat_score_at_publication': self.moat_score_at_publication,
            'verification_snapshot': self.verification_snapshot or {},
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 57 - Commercial API Keys
# ═══════════════════════════════════════════════════════════════

class CommercialAPIKey(Base):
    """API keys for paid intelligence-as-a-service."""
    __tablename__ = 'api_keys'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    key_hash = Column(String(128), nullable=False, unique=True, index=True)
    key_prefix = Column(String(12), nullable=False)  # first 8 chars for display
    name = Column(String(200), default='')
    tier = Column(String(20), default='free', index=True)  # free|starter|pro|enterprise
    rate_limit_per_day = Column(Integer, default=100)
    monthly_quota = Column(Integer, default=3000)
    usage_this_month = Column(Integer, default=0)
    usage_reset_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=True)

    user = relationship('User', backref='api_keys')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'key_prefix': self.key_prefix,
            'name': self.name,
            'tier': self.tier,
            'rate_limit_per_day': self.rate_limit_per_day,
            'monthly_quota': self.monthly_quota,
            'usage_this_month': self.usage_this_month,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 58 - API Usage Log
# ═══════════════════════════════════════════════════════════════

class APIUsageLog(Base):
    """Per-request usage logging for billing."""
    __tablename__ = 'api_usage_log'

    id = Column(String(64), primary_key=True, default=_uuid)
    api_key_id = Column(String(64), ForeignKey('api_keys.id'), nullable=False, index=True)
    endpoint = Column(String(200), nullable=False)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    compute_ms = Column(Integer, default=0)
    cost_credits = Column(Float, default=0.0)
    status_code = Column(Integer, default=200)
    created_at = Column(DateTime, default=func.now())

    api_key = relationship('CommercialAPIKey', backref='usage_logs')

    def to_dict(self):
        return {
            'id': self.id,
            'api_key_id': self.api_key_id,
            'endpoint': self.endpoint,
            'tokens_in': self.tokens_in,
            'tokens_out': self.tokens_out,
            'compute_ms': self.compute_ms,
            'cost_credits': self.cost_credits,
            'status_code': self.status_code,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 59 - Build Licenses
# ═══════════════════════════════════════════════════════════════

class BuildLicense(Base):
    """Licensed Linux build distribution gated by payment."""
    __tablename__ = 'build_licenses'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    license_key = Column(String(128), nullable=False, unique=True, index=True)
    build_type = Column(String(20), default='community')    # community|pro|enterprise
    platform = Column(String(30), default='linux_x64')       # linux_x64|linux_arm64
    payment_reference = Column(String(200), nullable=True)
    download_count = Column(Integer, default=0)
    max_downloads = Column(Integer, default=5)
    is_active = Column(Boolean, default=True)
    signed_by = Column(String(128), nullable=True)
    signature_hex = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=True)

    user = relationship('User', backref='build_licenses')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'license_key': self.license_key,
            'build_type': self.build_type,
            'platform': self.platform,
            'payment_reference': self.payment_reference,
            'download_count': self.download_count,
            'max_downloads': self.max_downloads,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 60 — Guest Recovery
# ═══════════════════════════════════════════════════════════════

class GuestRecovery(Base):
    """Recovery codes for guest users to restore identity across devices."""
    __tablename__ = 'guest_recovery'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    recovery_code_hash = Column(String(255), nullable=False)
    device_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=func.now())
    last_used_at = Column(DateTime, nullable=True)

    user = relationship('User', backref='guest_recoveries')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'device_id': self.device_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 61 — Device Bindings
# ═══════════════════════════════════════════════════════════════

class DeviceBinding(Base):
    """Tracks devices linked to a user for sync purposes."""
    __tablename__ = 'device_bindings'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    device_id = Column(String(128), nullable=False)
    device_name = Column(String(100), default='')
    platform = Column(String(30), default='web')
    form_factor = Column(String(20), default='phone')       # phone|watch|tablet|desktop|embedded|tv
    capabilities_json = Column(Text, default='{}')           # {"tts":true,"mic":true,"speaker":true,...}
    linked_at = Column(DateTime, default=func.now())
    last_sync_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship('User', backref='device_bindings')

    __table_args__ = (
        UniqueConstraint('user_id', 'device_id', name='uq_user_device'),
    )

    @property
    def capabilities(self):
        import json as _json
        try:
            return _json.loads(self.capabilities_json or '{}')
        except (ValueError, TypeError):
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'device_id': self.device_id,
            'device_name': self.device_name,
            'platform': self.platform,
            'form_factor': self.form_factor,
            'capabilities': self.capabilities,
            'linked_at': self.linked_at.isoformat() if self.linked_at else None,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'is_active': self.is_active,
        }


# ═══════════════════════════════════════════════════════════════
# TABLE 62 — Backup Metadata
# ═══════════════════════════════════════════════════════════════

class BackupMetadata(Base):
    """Metadata for encrypted backups (blob stored on filesystem)."""
    __tablename__ = 'backup_metadata'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    device_id = Column(String(128), nullable=True)
    backup_version = Column(Integer, default=1)
    content_hash = Column(String(64), nullable=False)
    size_bytes = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='backups')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'device_id': self.device_id,
            'backup_version': self.backup_version,
            'content_hash': self.content_hash,
            'size_bytes': self.size_bytes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# Regional Host Request (v25)
# ═══════════════════════════════════════════════════════════════

class RegionalHostRequest(Base):
    """Tracks regional host applications through the hybrid approval flow."""
    __tablename__ = 'regional_host_requests'

    id = Column(String(64), primary_key=True, default=_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    node_id = Column(String(64), nullable=True)
    public_key_hex = Column(String(128), nullable=True)
    compute_tier = Column(String(20), nullable=True)
    compute_info_json = Column(Text, nullable=True)
    trust_score = Column(Float, default=0.0)
    status = Column(String(20), default='pending', index=True)
    region_name = Column(String(50), nullable=True)
    certificate_json = Column(Text, nullable=True)
    github_username = Column(String(100), nullable=True)
    github_invite_sent = Column(Boolean, default=False)
    requested_at = Column(DateTime, default=func.now())
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(64), nullable=True)
    rejected_reason = Column(Text, nullable=True)

    user = relationship('User', backref='regional_host_requests')

    def to_dict(self):
        import json as _json
        return {
            'id': self.id,
            'user_id': self.user_id,
            'node_id': self.node_id,
            'public_key_hex': self.public_key_hex,
            'compute_tier': self.compute_tier,
            'compute_info': _json.loads(self.compute_info_json)
                if self.compute_info_json else None,
            'trust_score': self.trust_score,
            'status': self.status,
            'region_name': self.region_name,
            'github_username': self.github_username,
            'github_invite_sent': self.github_invite_sent,
            'requested_at': self.requested_at.isoformat()
                if self.requested_at else None,
            'approved_at': self.approved_at.isoformat()
                if self.approved_at else None,
            'approved_by': self.approved_by,
            'rejected_reason': self.rejected_reason,
        }


# ═══════════════════════════════════════════════════════════════
# Fleet Command (v26) — Queen Bee Authority
# ═══════════════════════════════════════════════════════════════

class FleetCommand(Base):
    """Commands pushed by central (queen bee) to fleet nodes.

    Central has instant, total authority. Commands are signed with the
    issuer's certificate and verified by the target before execution.
    """
    __tablename__ = 'fleet_commands'

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_node_id = Column(String(64), nullable=False, index=True)
    cmd_type = Column(String(30), nullable=False)
    params_json = Column(Text, nullable=True)
    issued_by = Column(String(64), nullable=False)
    signature = Column(Text, nullable=True)
    status = Column(String(20), default='pending', index=True)
    result_message = Column(Text, nullable=True)
    created_at = Column(Float, default=lambda: __import__('time').time())
    delivered_at = Column(Float, nullable=True)
    completed_at = Column(Float, nullable=True)

    def to_dict(self):
        import json as _json
        return {
            'id': self.id,
            'target_node_id': self.target_node_id,
            'cmd_type': self.cmd_type,
            'params': _json.loads(self.params_json) if self.params_json else {},
            'issued_by': self.issued_by,
            'signature': self.signature,
            'status': self.status,
            'result_message': self.result_message,
            'created_at': self.created_at,
            'delivered_at': self.delivered_at,
            'completed_at': self.completed_at,
        }


class ProvisionedNode(Base):
    """Tracks machines where HART OS was remotely provisioned via SSH.

    Created by NetworkProvisioner when an agent installs HART OS on
    a network machine. Used for fleet management, health monitoring,
    and remote updates.
    """
    __tablename__ = 'provisioned_nodes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_host = Column(String(256), nullable=False, index=True)
    ssh_user = Column(String(64), default='root')
    node_id = Column(String(64), nullable=True)
    peer_node_id = Column(Integer, nullable=True)
    capability_tier = Column(String(20), nullable=True)
    status = Column(String(20), default='pending', index=True)
    installed_version = Column(String(32), nullable=True)
    last_health_check = Column(DateTime, nullable=True)
    provisioned_at = Column(DateTime, nullable=True)
    provisioned_by = Column(String(64), nullable=False, default='system')
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())


# ─── TABLE: thought_experiments (v30) ───

class ThoughtExperiment(Base):
    """Constitutional thought experiment — public hypothesis with voting lifecycle.

    Full lifecycle: PROPOSE → DISCUSS → VOTE → EVALUATE → DECIDE → ARCHIVE
    Both humans and agents vote. ConstitutionalFilter gates all content.
    """
    __tablename__ = 'thought_experiments'

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String(64), ForeignKey('posts.id', use_alter=True), nullable=True)
    creator_id = Column(String(64), ForeignKey('users.id', use_alter=True), nullable=False)
    title = Column(String(200), nullable=False)
    hypothesis = Column(Text, nullable=False)
    expected_outcome = Column(Text, nullable=True)
    intent_category = Column(String(30), default='technology')
    status = Column(String(20), default='proposed', index=True)
    decision_type = Column(String(20), default='weighted')
    decision_context = Column(String(50), nullable=True)
    voting_opens_at = Column(DateTime, nullable=True)
    voting_closes_at = Column(DateTime, nullable=True)
    evaluation_deadline = Column(DateTime, nullable=True)
    decision_outcome = Column(Text, nullable=True)
    decision_rationale = Column(JSON, nullable=True)
    total_votes = Column(Integer, default=0)
    agent_evaluations_json = Column(JSON, nullable=True)
    is_core_ip = Column(Boolean, default=False)
    parent_experiment_id = Column(String(64), nullable=True)
    experiment_type = Column(String(20), default='traditional')   # physical_ai | software | traditional
    funding_total = Column(Integer, default=0)                    # Total Spark invested
    contributor_count = Column(Integer, default=0)                # Unique believers
    camera_feed_url = Column(String(500), nullable=True)          # WebSocket URL for physical_ai
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'post_id': self.post_id,
            'creator_id': self.creator_id,
            'title': self.title,
            'hypothesis': self.hypothesis,
            'expected_outcome': self.expected_outcome,
            'intent_category': self.intent_category,
            'status': self.status,
            'decision_type': self.decision_type,
            'decision_context': self.decision_context,
            'voting_opens_at': self.voting_opens_at.isoformat() if self.voting_opens_at else None,
            'voting_closes_at': self.voting_closes_at.isoformat() if self.voting_closes_at else None,
            'evaluation_deadline': self.evaluation_deadline.isoformat() if self.evaluation_deadline else None,
            'decision_outcome': self.decision_outcome,
            'decision_rationale': self.decision_rationale,
            'total_votes': self.total_votes or 0,
            'agent_evaluations_json': self.agent_evaluations_json,
            'is_core_ip': self.is_core_ip or False,
            'parent_experiment_id': self.parent_experiment_id,
            'experiment_type': self.experiment_type or 'traditional',
            'funding_total': self.funding_total or 0,
            'contributor_count': self.contributor_count or 0,
            'camera_feed_url': self.camera_feed_url,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ExperimentVote(Base):
    """Vote on a thought experiment — from human or agent."""
    __tablename__ = 'experiment_votes'

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    experiment_id = Column(String(64), ForeignKey('thought_experiments.id', use_alter=True),
                           nullable=False, index=True)
    voter_id = Column(String(64), ForeignKey('users.id', use_alter=True), nullable=False)
    voter_type = Column(String(10), default='human')
    vote_value = Column(Integer, default=0)
    confidence = Column(Float, default=1.0)
    reasoning = Column(Text, nullable=True)
    suggestion = Column(Text, nullable=True)
    constitutional_check = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('experiment_id', 'voter_id',
                         name='uq_experiment_voter'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'experiment_id': self.experiment_id,
            'voter_id': self.voter_id,
            'voter_type': self.voter_type,
            'vote_value': self.vote_value,
            'confidence': self.confidence,
            'reasoning': self.reasoning,
            'suggestion': self.suggestion,
            'constitutional_check': self.constitutional_check,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PaperPortfolio(Base):
    """Simulated trading portfolio for paper trading agents."""
    __tablename__ = 'paper_portfolios'

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(64), nullable=False, index=True)
    goal_id = Column(String(64), nullable=True)
    strategy = Column(String(30), default='long_term')
    initial_balance = Column(Float, default=10000.0)
    current_balance = Column(Float, default=10000.0)
    total_pnl = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    status = Column(String(20), default='active')
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'goal_id': self.goal_id,
            'strategy': self.strategy,
            'initial_balance': self.initial_balance,
            'current_balance': self.current_balance,
            'total_pnl': self.total_pnl,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': round(self.winning_trades / self.total_trades, 4) if self.total_trades else 0.0,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class PaperTrade(Base):
    """Individual paper trade record."""
    __tablename__ = 'paper_trades'

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    portfolio_id = Column(String(64), ForeignKey('paper_portfolios.id', use_alter=True),
                          nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    pnl = Column(Float, default=0.0)
    status = Column(String(20), default='open')
    opened_at = Column(DateTime, default=func.now())
    closed_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'portfolio_id': self.portfolio_id,
            'symbol': self.symbol,
            'side': self.side,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'stop_loss': self.stop_loss,
            'pnl': self.pnl,
            'status': self.status,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
        }


class ComputeEscrow(Base):
    """Persistent compute lending escrow — replaces in-memory _compute_debts.

    When experiment_post_id is set, this escrow is a pledge toward a specific
    thought experiment.  pledge_type distinguishes gpu_hours / cloud_credits /
    money pledges from the legacy spark-only escrow rows (where pledge_type is
    NULL).  consumed tracks how much of the pledged amount has been used.
    """
    __tablename__ = 'compute_escrow'

    id = Column(Integer, primary_key=True)
    debtor_node_id = Column(String(100), nullable=False, index=True)
    creditor_node_id = Column(String(100), nullable=False, index=True)
    request_id = Column(String(100), nullable=True)
    task_type = Column(String(50), default='general')
    spark_amount = Column(Integer, nullable=False)
    status = Column(String(20), default='pending', index=True)  # pending|settled|expired
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    # v34 — thought-experiment pledge extensions
    experiment_post_id = Column(String(64), nullable=True, index=True)
    pledge_type = Column(String(20), nullable=True)  # gpu_hours | cloud_credits | money (NULL = legacy spark)
    consumed = Column(Float, default=0.0)
    pledge_message = Column(Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'debtor_node_id': self.debtor_node_id,
            'creditor_node_id': self.creditor_node_id,
            'request_id': self.request_id,
            'task_type': self.task_type,
            'spark_amount': self.spark_amount,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'settled_at': self.settled_at.isoformat() if self.settled_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'experiment_post_id': self.experiment_post_id,
            'pledge_type': self.pledge_type,
            'consumed': self.consumed,
            'pledge_message': self.pledge_message,
        }


class MeteredAPIUsage(Base):
    """Per-call record of metered API consumption for cost recovery.

    Tracks when hive/idle tasks consume a node operator's paid API credits
    (GPT-4, Claude, Groq paid tier). Distinct from APIUsageLog which tracks
    external commercial billing (customers paying us).
    """
    __tablename__ = 'metered_api_usage'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), nullable=False, index=True)
    operator_id = Column(String(64), nullable=True, index=True)
    model_id = Column(String(100), nullable=False)
    task_source = Column(String(30), nullable=False)                # own | hive | idle
    goal_id = Column(String(64), nullable=True, index=True)
    requester_node_id = Column(String(64), nullable=True)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    cost_per_1k_tokens = Column(Float, default=0.0)
    estimated_spark_cost = Column(Integer, default=0)
    actual_usd_cost = Column(Float, default=0.0)
    settlement_status = Column(String(20), default='pending', index=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    # v34 — thought-experiment consumption tracking
    escrow_id = Column(Integer, nullable=True, index=True)
    experiment_post_id = Column(String(64), nullable=True, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'node_id': self.node_id,
            'operator_id': self.operator_id,
            'model_id': self.model_id,
            'task_source': self.task_source,
            'goal_id': self.goal_id,
            'requester_node_id': self.requester_node_id,
            'tokens_in': self.tokens_in,
            'tokens_out': self.tokens_out,
            'cost_per_1k_tokens': self.cost_per_1k_tokens,
            'estimated_spark_cost': self.estimated_spark_cost,
            'actual_usd_cost': self.actual_usd_cost,
            'settlement_status': self.settlement_status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'escrow_id': self.escrow_id,
            'experiment_post_id': self.experiment_post_id,
        }


class NodeComputeConfig(Base):
    """Per-node LOCAL policy settings (not gossipped).

    Controls how this node behaves: model routing, metered API opt-in,
    feature flags, settlement. Provider identity fields (cause_alignment,
    electricity_rate_kwh) live on PeerNode only — single source of truth.
    """
    __tablename__ = 'node_compute_config'

    id = Column(String(64), primary_key=True, default=_uuid)
    node_id = Column(String(64), unique=True, nullable=False, index=True)
    # Model routing (local policy)
    compute_policy = Column(String(20), default='local_preferred')
    hive_compute_policy = Column(String(20), default='local_preferred')
    max_hive_gpu_pct = Column(Integer, default=50)
    # Metered API opt-in (local policy)
    allow_metered_for_hive = Column(Boolean, default=False)
    metered_daily_limit_usd = Column(Float, default=0.0)
    # Compute offer (local declaration)
    offered_gpu_hours_per_day = Column(Float, default=0.0)
    # Feature flags (local policy)
    accept_thought_experiments = Column(Boolean, default=True)
    accept_frontier_training = Column(Boolean, default=False)
    # Settlement (local policy)
    auto_settle = Column(Boolean, default=True)
    min_settlement_spark = Column(Integer, default=10)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'node_id': self.node_id,
            'compute_policy': self.compute_policy,
            'hive_compute_policy': self.hive_compute_policy,
            'max_hive_gpu_pct': self.max_hive_gpu_pct,
            'allow_metered_for_hive': self.allow_metered_for_hive,
            'metered_daily_limit_usd': self.metered_daily_limit_usd,
            'offered_gpu_hours_per_day': self.offered_gpu_hours_per_day,
            'accept_thought_experiments': self.accept_thought_experiments,
            'accept_frontier_training': self.accept_frontier_training,
            'auto_settle': self.auto_settle,
            'min_settlement_spark': self.min_settlement_spark,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AuditLogEntry(Base):
    """Immutable audit log with hash-chain integrity (see security/immutable_audit_log.py)."""
    __tablename__ = 'audit_log_entries'

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False, index=True)
    actor_id = Column(String(100), nullable=False, index=True)
    target_id = Column(String(100), nullable=True)
    action = Column(Text, nullable=False)
    detail_json = Column(Text, nullable=True)
    prev_hash = Column(String(64), nullable=False)
    entry_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ─── Multiplayer Games ───

class GameSession(Base):
    """Multiplayer game session (trivia, word chain, collab puzzle, compute challenge)."""
    __tablename__ = 'game_sessions'

    id = Column(String(64), primary_key=True, default=_uuid)
    game_type = Column(String(30), nullable=False, index=True)       # trivia|word_chain|collab_puzzle|compute_challenge|quick_match
    status = Column(String(20), default='waiting', index=True)       # waiting|active|completed|expired|cancelled
    host_user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    encounter_id = Column(String(64), nullable=True, index=True)     # if born from encounter
    community_id = Column(String(64), nullable=True)                 # scoped to community
    challenge_id = Column(String(64), nullable=True)                 # linked challenge
    max_players = Column(Integer, default=4)
    current_round = Column(Integer, default=0)
    total_rounds = Column(Integer, default=5)
    game_state = Column(JSON, default=dict)                          # game-type-specific state
    config = Column(JSON, default=dict)                              # difficulty, categories, etc.
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)                    # auto-cleanup
    created_at = Column(DateTime, default=datetime.utcnow)

    participants = relationship('GameParticipant', back_populates='session',
                                cascade='all, delete-orphan', lazy='joined')

    def to_dict(self):
        return {
            'id': self.id,
            'game_type': self.game_type,
            'status': self.status,
            'host_user_id': self.host_user_id,
            'encounter_id': self.encounter_id,
            'community_id': self.community_id,
            'challenge_id': self.challenge_id,
            'max_players': self.max_players,
            'current_round': self.current_round,
            'total_rounds': self.total_rounds,
            'game_state': self.game_state,
            'config': self.config,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'participants': [p.to_dict() for p in (self.participants or [])],
            'player_count': len(self.participants or []),
        }


class GameParticipant(Base):
    """Player in a game session, tracks score and result."""
    __tablename__ = 'game_participants'

    id = Column(String(64), primary_key=True, default=_uuid)
    game_session_id = Column(String(64), ForeignKey('game_sessions.id'), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    score = Column(Integer, default=0)
    is_ready = Column(Boolean, default=False)
    joined_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    result = Column(String(20), nullable=True)                       # win|loss|draw|abandoned
    spark_earned = Column(Integer, default=0)
    xp_earned = Column(Integer, default=0)

    session = relationship('GameSession', back_populates='participants')

    __table_args__ = (
        UniqueConstraint('game_session_id', 'user_id', name='uq_game_participant'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'game_session_id': self.game_session_id,
            'user_id': self.user_id,
            'score': self.score,
            'is_ready': self.is_ready,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'result': self.result,
            'spark_earned': self.spark_earned,
            'xp_earned': self.xp_earned,
        }


# ─── TABLE: shareable_links ───

class ShareableLink(Base):
    """Universal share token for any resource — posts, profiles, recipes, agents, etc."""
    __tablename__ = 'shareable_links'

    id = Column(String(64), primary_key=True, default=_uuid)
    token = Column(String(12), unique=True, nullable=False, index=True)
    resource_type = Column(String(30), nullable=False)
    resource_id = Column(String(64), nullable=False)
    created_by = Column(String(64), ForeignKey('users.id'), nullable=True)
    referral_code = Column(String(20), nullable=True)
    is_private = Column(Boolean, default=False)
    consent_token = Column(String(32), nullable=True)
    view_count = Column(Integer, default=0)
    share_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=True)
    metadata_json = Column(Text, nullable=True)

    creator = relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        Index('ix_share_resource', 'resource_type', 'resource_id', 'created_by'),
    )

    def to_dict(self):
        import json as _json
        og = {}
        if self.metadata_json:
            try:
                og = _json.loads(self.metadata_json)
            except Exception:
                pass
        return {
            'id': self.id,
            'token': self.token,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'referral_code': self.referral_code,
            'is_private': self.is_private,
            'view_count': self.view_count,
            'share_count': self.share_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'og': og,
        }


# ─── TABLE: share_events ───

class ShareEvent(Base):
    """Track share views, clicks, and consent grants."""
    __tablename__ = 'share_events'

    id = Column(String(64), primary_key=True, default=_uuid)
    link_id = Column(String(64), ForeignKey('shareable_links.id'), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # view|share|consent
    viewer_id = Column(String(64), ForeignKey('users.id'), nullable=True)
    ip_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now())

    link = relationship('ShareableLink')

    def to_dict(self):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'event_type': self.event_type,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── TABLE: user_consents ───

class UserConsent(Base):
    """Track explicit user consent for data access, revenue sharing, and public exposure."""
    __tablename__ = 'user_consents'

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    agent_id = Column(String(64), nullable=True, index=True)
    consent_type = Column(String(30), nullable=False, index=True)  # data_access|revenue_share|public_exposure
    scope = Column(String(100), nullable=False, default='*')
    granted = Column(Boolean, default=False, nullable=False)
    granted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('user_id', 'agent_id', 'consent_type', 'scope',
                         name='uq_user_consent'),
        Index('ix_user_consent_lookup', 'user_id', 'consent_type', 'scope', 'granted'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'agent_id': self.agent_id,
            'consent_type': self.consent_type,
            'scope': self.scope,
            'granted': self.granted,
            'granted_at': self.granted_at.isoformat() if self.granted_at else None,
            'revoked_at': self.revoked_at.isoformat() if self.revoked_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ─── TABLE: marketplace_listings ───

class MarketplaceListing(Base):
    """HART agent service listing in the marketplace."""
    __tablename__ = 'marketplace_listings'

    id = Column(String(64), primary_key=True, default=_uuid)
    agent_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default='')
    category = Column(String(50), nullable=False, default='custom')
    price_spark = Column(Integer, default=0)
    rating_avg = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)
    hire_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    agent = relationship('User', backref='marketplace_listings')

    def to_dict(self):
        agent_data = None
        if self.agent:
            agent_data = {
                'id': self.agent.id,
                'username': self.agent.username,
                'display_name': self.agent.display_name,
                'avatar_url': self.agent.avatar_url,
                'user_type': self.agent.user_type,
            }
        return {
            'id': self.id,
            'agent_id': self.agent_id,
            'title': self.title,
            'description': self.description,
            'category': self.category,
            'price_spark': self.price_spark,
            'rating_avg': self.rating_avg,
            'review_count': self.review_count,
            'hire_count': self.hire_count,
            'is_active': self.is_active,
            'agent': agent_data,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ListingReview(Base):
    """Review for a marketplace listing."""
    __tablename__ = 'listing_reviews'

    id = Column(String(64), primary_key=True, default=_uuid)
    listing_id = Column(String(64), ForeignKey('marketplace_listings.id'), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    rating = Column(Integer, nullable=False)  # 1-5
    text = Column(Text, default='')
    created_at = Column(DateTime, default=func.now())

    listing = relationship('MarketplaceListing', backref='reviews')
    user = relationship('User')

    __table_args__ = (
        UniqueConstraint('listing_id', 'user_id', name='uq_listing_review'),
    )

    def to_dict(self):
        user_data = None
        if self.user:
            user_data = {
                'id': self.user.id,
                'username': self.user.username,
                'display_name': self.user.display_name,
                'avatar_url': self.user.avatar_url,
            }
        return {
            'id': self.id,
            'listing_id': self.listing_id,
            'user_id': self.user_id,
            'rating': self.rating,
            'text': self.text,
            'user': user_data,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class MCPServer(Base):
    """Registered MCP tool server."""
    __tablename__ = 'mcp_servers'

    id = Column(String(64), primary_key=True, default=_uuid)
    owner_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    url = Column(String(500), nullable=True)
    category = Column(String(50), default='general')
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    owner = relationship('User', backref='mcp_servers')

    def to_dict(self):
        owner_data = None
        if self.owner:
            owner_data = {
                'id': self.owner.id,
                'username': self.owner.username,
                'display_name': self.owner.display_name,
                'avatar_url': self.owner.avatar_url,
                'user_type': self.owner.user_type,
            }
        return {
            'id': self.id,
            'owner_id': self.owner_id,
            'name': self.name,
            'description': self.description,
            'url': self.url,
            'category': self.category,
            'is_active': self.is_active,
            'owner': owner_data,
            'tool_count': len(self.tools) if hasattr(self, 'tools') else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class MCPTool(Base):
    """A tool provided by an MCP server."""
    __tablename__ = 'mcp_tools'

    id = Column(String(64), primary_key=True, default=_uuid)
    server_id = Column(String(64), ForeignKey('mcp_servers.id'), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    input_schema = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())

    server = relationship('MCPServer', backref='tools')

    def to_dict(self):
        return {
            'id': self.id,
            'server_id': self.server_id,
            'name': self.name,
            'description': self.description,
            'input_schema': self.input_schema,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ─── COMPUTE PLEDGE SYSTEM (thought experiment resource commitment) ───

class ComputePledge(Base):
    """Pledge of compute resources (GPU hours, money, cloud credits) to a thought experiment.

    Users commit resources that agents deterministically consume.  The remaining
    field is denormalized (amount - consumed) for fast budget-check queries.
    Status lifecycle: pledged -> active -> consumed -> fulfilled | expired | refunded
    """
    __tablename__ = 'compute_pledges'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    post_id = Column(String(64), ForeignKey('posts.id'), nullable=False, index=True)

    # Contribution type: 'gpu_hours', 'cloud_credits', 'money'
    pledge_type = Column(String(20), nullable=False)

    # Amount and unit
    amount = Column(Float, nullable=False)           # e.g., 10.0
    unit = Column(String(20), nullable=False)         # e.g., 'hours', 'USD', 'credits'

    # Consumption tracking (deterministic enforcement)
    consumed = Column(Float, default=0.0)             # how much has been used
    remaining = Column(Float, default=0.0)            # amount - consumed (denormalized)

    # Status lifecycle
    status = Column(String(20), default='pledged', index=True)

    # Node verification (for gpu_hours type)
    node_id = Column(String(64), nullable=True)       # PeerNode providing compute
    node_tier = Column(String(20), nullable=True)      # 'flat', 'regional', 'central'
    verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)

    # Metadata
    message = Column(Text, nullable=True)             # optional supporter message
    anonymous = Column(Boolean, default=False)         # hide identity in public summary
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship('User', backref='compute_pledges')
    post = relationship('Post', backref='compute_pledges')

    __table_args__ = (
        Index('ix_pledge_post_type', 'post_id', 'pledge_type'),
        Index('ix_pledge_status_remaining', 'status', 'remaining'),
    )

    def to_dict(self, include_user=False):
        d = {
            'id': self.id,
            'user_id': self.user_id,
            'post_id': self.post_id,
            'pledge_type': self.pledge_type,
            'amount': self.amount,
            'unit': self.unit,
            'consumed': self.consumed,
            'remaining': self.remaining,
            'status': self.status,
            'node_id': self.node_id,
            'node_tier': self.node_tier,
            'verified': self.verified,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            'message': self.message,
            'anonymous': self.anonymous,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_user and self.user:
            d['user'] = {
                'id': self.user.id,
                'username': self.user.username,
                'display_name': self.user.display_name,
                'avatar_url': self.user.avatar_url,
            }
        return d


class PledgeConsumption(Base):
    """Audit log for every resource consumption against a pledge.

    Each row records a single draw from a pledge -- the agent system creates
    one PledgeConsumption per consumption request, possibly spanning multiple
    pledges (one row per pledge touched).
    """
    __tablename__ = 'pledge_consumptions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    pledge_id = Column(Integer, ForeignKey('compute_pledges.id'), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    task_description = Column(Text, nullable=True)
    agent_goal_id = Column(String(64), nullable=True, index=True)  # which AgentGoal consumed this
    consumed_at = Column(DateTime, default=func.now())

    pledge = relationship('ComputePledge', backref='consumptions')

    def to_dict(self):
        return {
            'id': self.id,
            'pledge_id': self.pledge_id,
            'amount': self.amount,
            'task_description': self.task_description,
            'agent_goal_id': self.agent_goal_id,
            'consumed_at': self.consumed_at.isoformat() if self.consumed_at else None,
        }
