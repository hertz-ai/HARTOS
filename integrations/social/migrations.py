"""
HevolveSocial - Schema Migrations
Version tracking and migration helpers.
"""
import logging
from sqlalchemy import text
from .models import get_engine, Base

logger = logging.getLogger('hevolve_social')

SCHEMA_VERSION = 17


def get_schema_version(engine) -> int:
    """Get current schema version from DB."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT value FROM social_meta WHERE key = 'schema_version'"))
            row = result.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def set_schema_version(engine, version: int):
    """Set schema version in DB."""
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS social_meta (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(text(
            "INSERT OR REPLACE INTO social_meta (key, value) VALUES ('schema_version', :v)"),
            {'v': str(version)})
        conn.commit()


def run_migrations():
    """Run any pending migrations."""
    engine = get_engine()
    current = get_schema_version(engine)

    if current < 1:
        logger.info("HevolveSocial: creating initial schema (v1)")
        Base.metadata.create_all(engine)
        set_schema_version(engine, 1)

    if current < 2:
        logger.info("HevolveSocial: migrating to v2 (handle + local_name columns)")
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN handle VARCHAR(30) UNIQUE"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN local_name VARCHAR(35)"))
            except Exception:
                pass
            conn.commit()
        set_schema_version(engine, 2)

    if current < 3:
        logger.info("HevolveSocial: migrating to v3 (Resonance core)")
        # Create new tables via metadata
        from .models import ResonanceWallet, ResonanceTransaction
        for tbl in [ResonanceWallet.__table__, ResonanceTransaction.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add new columns to users
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE users ADD COLUMN referral_code VARCHAR(20) UNIQUE",
                "ALTER TABLE users ADD COLUMN referred_by_id VARCHAR(64) REFERENCES users(id)",
                "ALTER TABLE users ADD COLUMN region_id VARCHAR(64)",
                "ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 1",
                "ALTER TABLE users ADD COLUMN level_title VARCHAR(30) DEFAULT 'Newcomer'",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        # Bootstrap existing users: create wallets
        from .models import get_db, User
        db = get_db()
        try:
            users = db.query(User).all()
            for u in users:
                existing = db.query(ResonanceWallet).filter_by(user_id=u.id).first()
                if not existing:
                    w = ResonanceWallet(
                        user_id=u.id,
                        pulse=u.karma_score or 0,
                        spark=(u.task_karma or 0) * 2,
                        spark_lifetime=(u.task_karma or 0) * 2,
                    )
                    # Estimate signal from account age
                    if u.created_at:
                        from datetime import datetime
                        age_days = (datetime.utcnow() - u.created_at).days
                        w.signal = age_days * 0.01
                    db.add(w)
            db.commit()
            logger.info(f"Bootstrapped {len(users)} resonance wallets")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to bootstrap wallets: {e}")
        finally:
            db.close()
        set_schema_version(engine, 3)

    if current < 4:
        logger.info("HevolveSocial: migrating to v4 (Gamification)")
        from .models import Achievement, UserAchievement, Season, Challenge, UserChallenge
        for tbl in [Achievement.__table__, UserAchievement.__table__,
                     Season.__table__, Challenge.__table__, UserChallenge.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 4)

    if current < 5:
        logger.info("HevolveSocial: migrating to v5 (Regions & Governance)")
        from .models import Region, RegionMembership
        for tbl in [Region.__table__, RegionMembership.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add columns to posts
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE posts ADD COLUMN boost_score REAL DEFAULT 0.0",
                "ALTER TABLE posts ADD COLUMN region_id VARCHAR(64)",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 5)

    if current < 6:
        logger.info("HevolveSocial: migrating to v6 (Encounters, Ratings, Agent Evolution)")
        from .models import (Encounter, Rating, TrustScore,
                             AgentEvolution, AgentCollaboration)
        for tbl in [Encounter.__table__, Rating.__table__, TrustScore.__table__,
                     AgentEvolution.__table__, AgentCollaboration.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 6)

    if current < 7:
        logger.info("HevolveSocial: migrating to v7 (Distribution & Growth)")
        from .models import Referral, ReferralCode, Boost, OnboardingProgress
        for tbl in [Referral.__table__, ReferralCode.__table__,
                     Boost.__table__, OnboardingProgress.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 7)

    if current < 8:
        logger.info("HevolveSocial: migrating to v8 (Campaign Studio)")
        from .models import Campaign, CampaignAction
        for tbl in [Campaign.__table__, CampaignAction.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add columns to peer_nodes
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN contribution_score REAL DEFAULT 0.0",
                "ALTER TABLE peer_nodes ADD COLUMN visibility_tier VARCHAR(20) DEFAULT 'standard'",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 8)

    if current < 9:
        logger.info("HevolveSocial: migrating to v9 (Proximity & Missed Connections)")
        from .models import LocationPing, ProximityMatch, MissedConnection, MissedConnectionResponse
        for tbl in [LocationPing.__table__, ProximityMatch.__table__,
                     MissedConnection.__table__, MissedConnectionResponse.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add columns to users
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE users ADD COLUMN location_sharing_enabled BOOLEAN DEFAULT 0",
                "ALTER TABLE users ADD COLUMN last_location_lat REAL",
                "ALTER TABLE users ADD COLUMN last_location_lon REAL",
                "ALTER TABLE users ADD COLUMN last_location_at TIMESTAMP",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 9)

    if current < 10:
        logger.info("HevolveSocial: migrating to v10 (Ads & Hosting Rewards)")
        from .models import AdUnit, AdPlacement, AdImpression, HostingReward
        for tbl in [AdUnit.__table__, AdPlacement.__table__,
                     AdImpression.__table__, HostingReward.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add node_operator_id to peer_nodes
        with engine.connect() as conn:
            try:
                conn.execute(text(
                    "ALTER TABLE peer_nodes ADD COLUMN node_operator_id VARCHAR(64) REFERENCES users(id)"))
            except Exception:
                pass
            conn.commit()
        # Backfill contribution_score for existing active nodes
        from .models import get_db, PeerNode
        db = get_db()
        try:
            peers = db.query(PeerNode).filter(
                PeerNode.status.in_(['active', 'stale'])).all()
            for p in peers:
                score = (p.agent_count or 0) * 2.0 + (p.post_count or 0) * 0.5
                if p.status == 'active':
                    score += 100.0
                elif p.status == 'stale':
                    score += 50.0
                p.contribution_score = round(score, 2)
                if score >= 500:
                    p.visibility_tier = 'priority'
                elif score >= 100:
                    p.visibility_tier = 'featured'
                else:
                    p.visibility_tier = 'standard'
            db.commit()
            logger.info(f"Backfilled contribution scores for {len(peers)} peer nodes")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to backfill contribution scores: {e}")
        finally:
            db.close()
        # Seed default ad placements
        try:
            from .ad_service import AdService
            db2 = get_db()
            count = AdService.seed_placements(db2)
            if count > 0:
                db2.commit()
                logger.info(f"Seeded {count} ad placements")
            db2.close()
        except Exception as e:
            logger.debug(f"Ad placement seeding skipped: {e}")
        set_schema_version(engine, 10)

    if current < 11:
        logger.info("HevolveSocial: migrating to v11 (Node Integrity & Anti-Fraud)")
        from .models import NodeAttestation, IntegrityChallenge, FraudAlert
        for tbl in [NodeAttestation.__table__, IntegrityChallenge.__table__,
                     FraudAlert.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add integrity columns to peer_nodes
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN public_key VARCHAR(128)",
                "ALTER TABLE peer_nodes ADD COLUMN code_hash VARCHAR(64)",
                "ALTER TABLE peer_nodes ADD COLUMN code_version VARCHAR(20)",
                "ALTER TABLE peer_nodes ADD COLUMN integrity_status VARCHAR(20) DEFAULT 'unverified'",
                "ALTER TABLE peer_nodes ADD COLUMN fraud_score REAL DEFAULT 0.0",
                "ALTER TABLE peer_nodes ADD COLUMN last_challenge_at TIMESTAMP",
                "ALTER TABLE peer_nodes ADD COLUMN last_attestation_at TIMESTAMP",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 11)

    if current < 12:
        logger.info("HevolveSocial: migrating to v12 (Master Key Verification)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN master_key_verified BOOLEAN DEFAULT 0",
                "ALTER TABLE peer_nodes ADD COLUMN release_version VARCHAR(20)",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 12)

    if current < 13:
        logger.info("HevolveSocial: migrating to v13 (3-Tier Hierarchy)")
        from .models import RegionAssignment, SyncQueue
        for tbl in [RegionAssignment.__table__, SyncQueue.__table__]:
            tbl.create(engine, checkfirst=True)
        # Add hierarchy columns to peer_nodes
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN tier VARCHAR(20) DEFAULT 'flat'",
                "ALTER TABLE peer_nodes ADD COLUMN parent_node_id VARCHAR(64)",
                "ALTER TABLE peer_nodes ADD COLUMN certificate_json JSON",
                "ALTER TABLE peer_nodes ADD COLUMN certificate_verified BOOLEAN DEFAULT 0",
                "ALTER TABLE peer_nodes ADD COLUMN region_assignment_id VARCHAR(64)",
                "ALTER TABLE peer_nodes ADD COLUMN compute_cpu_cores INTEGER",
                "ALTER TABLE peer_nodes ADD COLUMN compute_ram_gb REAL",
                "ALTER TABLE peer_nodes ADD COLUMN compute_gpu_count INTEGER",
                "ALTER TABLE peer_nodes ADD COLUMN active_user_count INTEGER DEFAULT 0",
                "ALTER TABLE peer_nodes ADD COLUMN max_user_capacity INTEGER DEFAULT 0",
                "ALTER TABLE peer_nodes ADD COLUMN dns_region VARCHAR(50)",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            # Add hierarchy columns to regions
            for stmt in [
                "ALTER TABLE regions ADD COLUMN host_node_id VARCHAR(64)",
                "ALTER TABLE regions ADD COLUMN capacity_cpu INTEGER",
                "ALTER TABLE regions ADD COLUMN capacity_ram_gb REAL",
                "ALTER TABLE regions ADD COLUMN capacity_gpu INTEGER",
                "ALTER TABLE regions ADD COLUMN current_load_pct REAL DEFAULT 0.0",
                "ALTER TABLE regions ADD COLUMN is_accepting_nodes BOOLEAN DEFAULT 1",
                "ALTER TABLE regions ADD COLUMN central_approved BOOLEAN DEFAULT 0",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 13)

    if current < 14:
        logger.info("HevolveSocial: migrating to v14 (Distributed Coding Agent)")
        from .models import CodingGoal, CodingTask, CodingSubmission
        for tbl in [CodingGoal.__table__, CodingTask.__table__, CodingSubmission.__table__]:
            tbl.create(engine, checkfirst=True)
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN idle_compute_opt_in BOOLEAN DEFAULT 0"))
            except Exception:
                pass
            conn.commit()
        set_schema_version(engine, 14)

    if current < 15:
        logger.info("HevolveSocial: migrating to v15 (User role field - central/regional/flat)")
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'flat'"))
            except Exception:
                pass  # Column may already exist
            # Backfill: is_admin -> central, is_moderator (non-admin) -> regional, NULL -> flat
            try:
                conn.execute(text("UPDATE users SET role = 'central' WHERE is_admin = 1"))
                conn.execute(text("UPDATE users SET role = 'regional' WHERE is_moderator = 1 AND is_admin = 0"))
                conn.execute(text("UPDATE users SET role = 'flat' WHERE role IS NULL"))
            except Exception:
                pass
            conn.commit()
        set_schema_version(engine, 15)

    if current < 16:
        logger.info("HevolveSocial: migrating to v16 (is_hidden column for posts & comments)")
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE posts ADD COLUMN is_hidden BOOLEAN DEFAULT 0"))
            except Exception:
                pass  # Column may already exist
            try:
                conn.execute(text("ALTER TABLE comments ADD COLUMN is_hidden BOOLEAN DEFAULT 0"))
            except Exception:
                pass  # Column may already exist
            conn.commit()
        set_schema_version(engine, 16)

    if current < 17:
        logger.info("HevolveSocial: migrating to v17 (submolt -> community rename)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE submolts RENAME TO communities",
                "ALTER TABLE submolt_memberships RENAME TO community_memberships",
                "ALTER TABLE posts RENAME COLUMN submolt_id TO community_id",
                "ALTER TABLE campaigns RENAME COLUMN target_submolts TO target_communities",
                "ALTER TABLE onboarding_progress RENAME COLUMN first_submolt_join_at TO first_community_join_at",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
        set_schema_version(engine, 17)
