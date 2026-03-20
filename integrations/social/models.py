"""
HevolveSocial — Slim imports file.

All table classes (schema + behavior) are defined in hevolve-database (sql.models).
This file provides:
  1. HARTOS-specific DB infrastructure (URL resolution, engine, sessions)
  2. Re-exports of all model classes so existing HARTOS imports keep working

Usage (unchanged):
    from integrations.social.models import db_session, User, Post, Community
"""
import os
import uuid
from datetime import datetime

from sqlalchemy import (
    create_engine, event, Column, String, Text, Integer, Float, Boolean,
    DateTime, JSON, ForeignKey, UniqueConstraint, Index, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

# Import shared Base from hevolve-database (single metadata registry).
# Fallback to local Base if hevolve-database is not installed (e.g., dev without pip install).
try:
    from sql.database import Base
except ImportError:
    Base = declarative_base()

# Security: HTML sanitization for user-generated content (XSS prevention)
try:
    from security.sanitize import sanitize_html as _sanitize_html
except ImportError:
    import html as _html_module
    def _sanitize_html(text):
        """Minimal fallback: escape HTML entities to prevent XSS."""
        if not isinstance(text, str):
            return text
        return _html_module.escape(text)

# ── Database URL resolution ──────────────────────────────────
# Priority: HEVOLVE_DB_URL > DATABASE_URL > HEVOLVE_DB_PATH > SOCIAL_DB_PATH > auto-detect
#
# Cloud/Docker deployments MUST set HEVOLVE_DB_URL (or DATABASE_URL) pointing to
# the remote MySQL/PostgreSQL instance. SQLite fallback is only for local dev/standalone.
# Detection: /.dockerenv file, DOCKER_CONTAINER env, or HEVOLVE_CLOUD_MODE env.
_DB_URL_ENV = os.environ.get('HEVOLVE_DB_URL') or os.environ.get('DATABASE_URL')
_DB_PATH_ENV = os.environ.get('HEVOLVE_DB_PATH') or os.environ.get('SOCIAL_DB_PATH')

_IS_DOCKER = (
    os.path.exists('/.dockerenv')
    or os.environ.get('DOCKER_CONTAINER') == 'true'
    or os.environ.get('HEVOLVE_CLOUD_MODE') == 'true'
)

if _DB_URL_ENV:
    # Full URL override (MySQL, PostgreSQL, or SQLite) — aligns with Hevolve_Database
    DB_URL = _DB_URL_ENV
    DB_PATH = ':memory:' if DB_URL == 'sqlite://' else None
elif _IS_DOCKER and not _DB_PATH_ENV:
    # Cloud/Docker mode WITHOUT a DB URL configured — this is a misconfiguration.
    # Refuse to silently fall back to SQLite; log a loud warning and use in-memory
    # so the server starts (health checks pass) but makes the problem obvious.
    import logging as _logging_models
    _logging_models.getLogger(__name__).critical(
        'CLOUD/DOCKER MODE: HEVOLVE_DB_URL or DATABASE_URL not set! '
        'Set HEVOLVE_DB_URL=mysql+pymysql://user:pass@host/db to connect to cloud DB. '
        'Falling back to in-memory SQLite — NO DATA WILL PERSIST.'
    )
    DB_PATH = ':memory:'
    DB_URL = 'sqlite://'
elif _DB_PATH_ENV == ':memory:':
    DB_PATH = ':memory:'
    DB_URL = 'sqlite://'
else:
    import sys as _sys_models
    if _DB_PATH_ENV:
        DB_PATH = _DB_PATH_ENV
    elif os.environ.get('NUNBA_BUNDLED') or getattr(_sys_models, 'frozen', False):
        # Bundled mode: cross-platform writable data dir
        try:
            from core.platform_paths import get_db_path as _get_db_path
            DB_PATH = _get_db_path('hevolve_database.db')
        except ImportError:
            DB_PATH = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'hevolve_database.db')
    else:
        DB_PATH = os.path.join(
            os.path.dirname(__file__), '..', '..', 'agent_data', 'hevolve_database.db')
    DB_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"

import threading as _threading_models

_engine = None
_SessionLocal = None
_engine_lock = _threading_models.Lock()
_session_lock = _threading_models.Lock()


def _uuid():
    return str(uuid.uuid4())


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is not None:
                return _engine

            _is_sqlite = DB_URL.startswith('sqlite')
            _is_memory = DB_URL == 'sqlite://'

            # Ensure parent directory exists for file-based SQLite
            if _is_sqlite and not _is_memory and DB_PATH:
                os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

            if _is_sqlite:
                if _is_memory:
                    from sqlalchemy.pool import StaticPool
                    engine_kwargs = dict(
                        echo=False,
                        future=True,
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool,
                        pool_pre_ping=True,
                    )
                else:
                    # File-based SQLite: NullPool — each thread gets its own
                    # short-lived connection. QueuePool holds connections open
                    # across threads → "database is locked" under concurrent
                    # daemon writes.
                    from sqlalchemy.pool import NullPool
                    engine_kwargs = dict(
                        echo=False,
                        future=True,
                        connect_args={"check_same_thread": False},
                        poolclass=NullPool,
                    )
            else:
                # MySQL / PostgreSQL (via HEVOLVE_DB_URL)
                engine_kwargs = dict(
                    echo=False,
                    future=True,
                    pool_pre_ping=True,
                    pool_size=20,
                    max_overflow=0,
                )

            _engine = create_engine(DB_URL, **engine_kwargs)

            # Enable WAL mode for file-based SQLite
            # WAL allows concurrent reads + one writer without blocking readers.
            # busy_timeout=3000 (3s) — fail fast rather than blocking daemon threads
            # for 15-30s which triggers watchdog restarts.
            if _is_sqlite and not _is_memory:
                @event.listens_for(_engine, "connect")
                def _set_sqlite_wal(dbapi_connection, connection_record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA busy_timeout=3000")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.close()
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        with _session_lock:
            if _SessionLocal is None:
                _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Session:
    factory = get_session_factory()
    return factory()


def db_session(commit=True):
    """Context manager for database sessions with automatic commit/rollback/close.

    Usage:
        with db_session() as db:
            user = db.query(User).filter_by(id=uid).first()
            user.name = 'new'
        # auto-commits on clean exit, auto-rollbacks on exception, always closes
    """
    from contextlib import contextmanager

    @contextmanager
    def _session_cm():
        db = get_db()
        try:
            yield db
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return _session_cm()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)


# ═══════════════════════════════════════════════════════════════
# MODEL IMPORTS — Canonical source: hevolve-database (sql.models)
# Fallback: local definitions in _models_local.py (for Docker/standalone)
#
# The 3 collision classes (SocialUser, SocialPost, SocialComment) are aliased
# to User, Post, Comment so all existing HARTOS code keeps working.
# ═══════════════════════════════════════════════════════════════

try:
    from sql.models import (  # noqa: E402, F401
        # Collision classes — aliased for HARTOS backward compatibility
        SocialUser as User,
        SocialPost as Post,
        SocialComment as Comment,
        # Non-collision classes — direct import
        Region, Community, Vote, Follow, CommunityMembership,
        AgentSkillBadge, TaskRequest, Notification, Report, RecipeShare,
        PeerNode, InstanceFollow, FederatedPost, ResonanceWallet,
        ResonanceTransaction, Achievement, UserAchievement, Season,
        Challenge, UserChallenge, RegionMembership, Encounter, Rating,
        TrustScore, AgentEvolution, AgentCollaboration, Referral,
        ReferralCode, Boost, OnboardingProgress, Campaign, CampaignAction,
        LocationPing, ProximityMatch, MissedConnection,
        MissedConnectionResponse, AdUnit, AdPlacement, AdImpression,
        HostingReward, NodeAttestation, IntegrityChallenge, FraudAlert,
        RegionAssignment, SyncQueue, CodingGoal, CodingTask,
        CodingSubmission, Product, AgentGoal, IPPatent, IPInfringement,
        DefensivePublication, CommercialAPIKey, APIUsageLog, BuildLicense,
        GuestRecovery, DeviceBinding, BackupMetadata, RegionalHostRequest,
        FleetCommand, ProvisionedNode, ThoughtExperiment, ExperimentVote,
        PaperPortfolio, PaperTrade, ComputeEscrow, MeteredAPIUsage,
        NodeComputeConfig, AuditLogEntry, GameSession, GameParticipant,
        ShareableLink, ShareEvent, UserConsent, MarketplaceListing,
        ListingReview, MCPServer, MCPTool, ComputePledge, PledgeConsumption,
        UserChannelBinding, ConversationEntry, ChannelPresence,
    )
except ImportError:
    # Standalone/Docker mode: sql package not installed, use local definitions
    from integrations.social._models_local import (  # noqa: E402, F401
        User, Post, Comment, Region, Community, Vote, Follow,
        CommunityMembership, AgentSkillBadge, TaskRequest, Notification,
        Report, RecipeShare, PeerNode, InstanceFollow, FederatedPost,
        ResonanceWallet, ResonanceTransaction, Achievement, UserAchievement,
        Season, Challenge, UserChallenge, RegionMembership, Encounter,
        Rating, TrustScore, AgentEvolution, AgentCollaboration, Referral,
        ReferralCode, Boost, OnboardingProgress, Campaign, CampaignAction,
        LocationPing, ProximityMatch, MissedConnection,
        MissedConnectionResponse, AdUnit, AdPlacement, AdImpression,
        HostingReward, NodeAttestation, IntegrityChallenge, FraudAlert,
        RegionAssignment, SyncQueue, CodingGoal, CodingTask,
        CodingSubmission, Product, AgentGoal, IPPatent, IPInfringement,
        DefensivePublication, CommercialAPIKey, APIUsageLog, BuildLicense,
        GuestRecovery, DeviceBinding, BackupMetadata, RegionalHostRequest,
        FleetCommand, ProvisionedNode, ThoughtExperiment, ExperimentVote,
        PaperPortfolio, PaperTrade, ComputeEscrow, MeteredAPIUsage,
        NodeComputeConfig, AuditLogEntry, GameSession, GameParticipant,
        ShareableLink, ShareEvent, UserConsent, MarketplaceListing,
        ListingReview, MCPServer, MCPTool, ComputePledge, PledgeConsumption,
        UserChannelBinding, ConversationEntry, ChannelPresence,
    )
