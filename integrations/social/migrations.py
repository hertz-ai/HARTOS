"""
HevolveSocial - Schema Migrations
Version tracking and migration helpers.
"""
import logging
from sqlalchemy import text
from .models import get_engine, Base

logger = logging.getLogger('hevolve_social')

SCHEMA_VERSION = 35


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
            except Exception as e:
                logger.warning("v2 migration: ADD COLUMN handle skipped (may already exist): %s", e)
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN local_name VARCHAR(35)"))
            except Exception as e:
                logger.warning("v2 migration: ADD COLUMN local_name skipped (may already exist): %s", e)
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
                except Exception as e:
                    logger.warning("v3 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v5 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v8 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v9 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
            except Exception as e:
                logger.warning("v10 migration: ADD COLUMN node_operator_id skipped: %s", e)
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
                except Exception as e:
                    logger.warning("v11 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v12 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v13 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
                except Exception as e:
                    logger.warning("v13 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
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
            except Exception as e:
                logger.warning("v14 migration: ADD COLUMN idle_compute_opt_in skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 14)

    if current < 15:
        logger.info("HevolveSocial: migrating to v15 (User role field - central/regional/flat)")
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'flat'"))
            except Exception as e:
                logger.warning("v15 migration: ADD COLUMN role skipped (may already exist): %s", e)
            # Backfill: is_admin -> central, is_moderator (non-admin) -> regional, NULL -> flat
            try:
                conn.execute(text("UPDATE users SET role = 'central' WHERE is_admin = 1"))
                conn.execute(text("UPDATE users SET role = 'regional' WHERE is_moderator = 1 AND is_admin = 0"))
                conn.execute(text("UPDATE users SET role = 'flat' WHERE role IS NULL"))
            except Exception as e:
                logger.error("v15 migration: role backfill failed: %s", e)
            conn.commit()
        set_schema_version(engine, 15)

    if current < 16:
        logger.info("HevolveSocial: migrating to v16 (is_hidden column for posts & comments)")
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE posts ADD COLUMN is_hidden BOOLEAN DEFAULT 0"))
            except Exception as e:
                logger.warning("v16 migration: ADD COLUMN posts.is_hidden skipped (may already exist): %s", e)
            try:
                conn.execute(text("ALTER TABLE comments ADD COLUMN is_hidden BOOLEAN DEFAULT 0"))
            except Exception as e:
                logger.warning("v16 migration: ADD COLUMN comments.is_hidden skipped (may already exist): %s", e)
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
                except Exception as e:
                    logger.warning("v17 migration: rename skipped: %s — %s", stmt[:60], e)
            conn.commit()
        set_schema_version(engine, 17)

    if current < 18:
        logger.info("HevolveSocial: migrating to v18 (Unified Agent Engine + Products)")
        from .models import Product, AgentGoal
        for tbl in [Product.__table__, AgentGoal.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 18)

    if current < 19:
        logger.info("HevolveSocial: migrating to v19 (IP Protection Agent)")
        from .models import IPPatent, IPInfringement
        for tbl in [IPPatent.__table__, IPInfringement.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 19)

    if current < 20:
        logger.info("HevolveSocial: migrating to v20 (Thought Experiment fields on posts)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE posts ADD COLUMN intent_category VARCHAR(30)",
                "ALTER TABLE posts ADD COLUMN hypothesis TEXT",
                "ALTER TABLE posts ADD COLUMN expected_outcome TEXT",
                "ALTER TABLE posts ADD COLUMN is_thought_experiment BOOLEAN DEFAULT 0",
                "ALTER TABLE posts ADD COLUMN dynamic_layout JSON",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning("v20 migration: %s skipped (may already exist): %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
            conn.commit()
        set_schema_version(engine, 20)

    if current < 21:
        logger.info("HevolveSocial: migrating to v21 (Node capability tier - HART OS equilibrium)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN capability_tier VARCHAR(20)",
                "ALTER TABLE peer_nodes ADD COLUMN enabled_features_json JSON",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning("v21 migration: %s skipped (may already exist): %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
            conn.commit()
        set_schema_version(engine, 21)

    if current < 22:
        logger.info("HevolveSocial: migrating to v22 (Commercial API + Defensive IP + Build Licenses)")
        from .models import DefensivePublication, CommercialAPIKey, APIUsageLog, BuildLicense
        for tbl in [DefensivePublication.__table__, CommercialAPIKey.__table__,
                     APIUsageLog.__table__, BuildLicense.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 22)

    if current < 23:
        logger.info("HevolveSocial: migrating to v23 (Fail2ban: ban_count + ban_until on PeerNode)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE peer_nodes ADD COLUMN ban_count INTEGER DEFAULT 0",
                "ALTER TABLE peer_nodes ADD COLUMN ban_until DATETIME",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning("v23 migration: %s skipped (may already exist): %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
            conn.commit()
        set_schema_version(engine, 23)

    if current < 24:
        logger.info("HevolveSocial: migrating to v24 (Guest Recovery + Device Bindings + Backup Metadata)")
        from .models import GuestRecovery, DeviceBinding, BackupMetadata
        for tbl in [GuestRecovery.__table__, DeviceBinding.__table__, BackupMetadata.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 24)

    if current < 25:
        logger.info("HevolveSocial: migrating to v25 (Regional Host Requests)")
        from .models import RegionalHostRequest
        RegionalHostRequest.__table__.create(engine, checkfirst=True)
        set_schema_version(engine, 25)

    if current < 26:
        logger.info("HevolveSocial: migrating to v26 (Fleet Command - Queen Bee Authority)")
        from .models import FleetCommand
        FleetCommand.__table__.create(engine, checkfirst=True)
        set_schema_version(engine, 26)

    if current < 27:
        logger.info("HevolveSocial: migrating to v27 (Device form_factor + capabilities)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE device_bindings ADD COLUMN form_factor VARCHAR(20) DEFAULT 'phone'",
                "ALTER TABLE device_bindings ADD COLUMN capabilities_json TEXT DEFAULT '{}'",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning("v27 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
            conn.commit()
        set_schema_version(engine, 27)

    if current < 28:
        logger.info("HevolveSocial: migrating to v28 (Impression seal columns)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE ad_impressions ADD COLUMN witness_node_id VARCHAR(64)",
                "ALTER TABLE ad_impressions ADD COLUMN witness_signature VARCHAR(256)",
                "ALTER TABLE ad_impressions ADD COLUMN sealed_hash VARCHAR(64)",
                "ALTER TABLE ad_impressions ADD COLUMN sealed_at DATETIME",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning("v28 migration: %s skipped: %s", stmt.split("ADD COLUMN ")[-1].split()[0], e)
            conn.commit()
        set_schema_version(engine, 28)

    if current < 29:
        logger.info("HevolveSocial: migrating to v29 (ProvisionedNode table)")
        with engine.connect() as conn:
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS provisioned_nodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        target_host VARCHAR(256) NOT NULL,
                        ssh_user VARCHAR(64) DEFAULT 'root',
                        node_id VARCHAR(64),
                        peer_node_id INTEGER,
                        capability_tier VARCHAR(20),
                        status VARCHAR(20) DEFAULT 'pending',
                        installed_version VARCHAR(32),
                        last_health_check DATETIME,
                        provisioned_at DATETIME,
                        provisioned_by VARCHAR(64) NOT NULL DEFAULT 'system',
                        error_message TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            except Exception as e:
                logger.warning("v29 migration: CREATE TABLE provisioned_nodes skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_provisioned_nodes_target_host "
                    "ON provisioned_nodes (target_host)"))
            except Exception as e:
                logger.warning("v29 migration: CREATE INDEX target_host skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_provisioned_nodes_status "
                    "ON provisioned_nodes (status)"))
            except Exception as e:
                logger.warning("v29 migration: CREATE INDEX status skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 29)

    if current < 30:
        logger.info("HevolveSocial: migrating to v30 (ThoughtExperiment + ExperimentVote)")
        with engine.connect() as conn:
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS thought_experiments (
                        id VARCHAR(64) PRIMARY KEY,
                        post_id VARCHAR(64),
                        creator_id VARCHAR(64) NOT NULL,
                        title VARCHAR(200) NOT NULL,
                        hypothesis TEXT NOT NULL,
                        expected_outcome TEXT,
                        intent_category VARCHAR(30) DEFAULT 'technology',
                        status VARCHAR(20) DEFAULT 'proposed',
                        decision_type VARCHAR(20) DEFAULT 'weighted',
                        voting_opens_at DATETIME,
                        voting_closes_at DATETIME,
                        evaluation_deadline DATETIME,
                        decision_outcome TEXT,
                        decision_rationale JSON,
                        total_votes INTEGER DEFAULT 0,
                        agent_evaluations_json JSON,
                        is_core_ip BOOLEAN DEFAULT 0,
                        parent_experiment_id VARCHAR(64),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            except Exception as e:
                logger.warning("v30 migration: CREATE TABLE thought_experiments skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_thought_experiments_status "
                    "ON thought_experiments (status)"))
            except Exception as e:
                logger.warning("v30 migration: index on status skipped: %s", e)
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS experiment_votes (
                        id VARCHAR(64) PRIMARY KEY,
                        experiment_id VARCHAR(64) NOT NULL,
                        voter_id VARCHAR(64) NOT NULL,
                        voter_type VARCHAR(10) DEFAULT 'human',
                        vote_value INTEGER DEFAULT 0,
                        confidence FLOAT DEFAULT 1.0,
                        reasoning TEXT,
                        suggestion TEXT,
                        constitutional_check BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (experiment_id, voter_id)
                    )
                """))
            except Exception as e:
                logger.warning("v30 migration: CREATE TABLE experiment_votes skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_experiment_votes_experiment_id "
                    "ON experiment_votes (experiment_id)"))
            except Exception as e:
                logger.warning("v30 migration: index on experiment_id skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 30)

    if current < 31:
        logger.info("HevolveSocial: migrating to v31 (PeerNode x25519_public for E2E encryption)")
        with engine.connect() as conn:
            try:
                conn.execute(text(
                    "ALTER TABLE peer_nodes ADD COLUMN x25519_public VARCHAR(64)"))
            except Exception as e:
                logger.warning("v31 migration: ADD COLUMN x25519_public skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 31)

    if current < 32:
        logger.info("HevolveSocial: migrating to v32 (multiplayer game tables)")
        with engine.connect() as conn:
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS game_sessions (
                        id VARCHAR(64) PRIMARY KEY,
                        game_type VARCHAR(30) NOT NULL,
                        status VARCHAR(20) DEFAULT 'waiting',
                        host_user_id VARCHAR(64) NOT NULL REFERENCES users(id),
                        encounter_id VARCHAR(64),
                        community_id VARCHAR(64),
                        challenge_id VARCHAR(64),
                        max_players INTEGER DEFAULT 4,
                        current_round INTEGER DEFAULT 0,
                        total_rounds INTEGER DEFAULT 5,
                        game_state JSON,
                        config JSON,
                        started_at DATETIME,
                        ended_at DATETIME,
                        expires_at DATETIME NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            except Exception as e:
                logger.warning("v32 migration: CREATE TABLE game_sessions skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_game_sessions_status ON game_sessions (status)"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_game_sessions_host ON game_sessions (host_user_id)"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_game_sessions_type ON game_sessions (game_type)"))
            except Exception as e:
                logger.warning("v32 migration: game_sessions indexes skipped: %s", e)
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS game_participants (
                        id VARCHAR(64) PRIMARY KEY,
                        game_session_id VARCHAR(64) NOT NULL REFERENCES game_sessions(id),
                        user_id VARCHAR(64) NOT NULL REFERENCES users(id),
                        score INTEGER DEFAULT 0,
                        is_ready BOOLEAN DEFAULT 0,
                        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        finished_at DATETIME,
                        result VARCHAR(20),
                        spark_earned INTEGER DEFAULT 0,
                        xp_earned INTEGER DEFAULT 0,
                        UNIQUE (game_session_id, user_id)
                    )
                """))
            except Exception as e:
                logger.warning("v32 migration: CREATE TABLE game_participants skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_game_participants_session "
                    "ON game_participants (game_session_id)"))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_game_participants_user "
                    "ON game_participants (user_id)"))
            except Exception as e:
                logger.warning("v32 migration: game_participants indexes skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 32)

    if current < 33:
        logger.info("HevolveSocial: migrating to v33 (thought experiment discovery fields)")
        with engine.connect() as conn:
            for stmt in [
                "ALTER TABLE thought_experiments ADD COLUMN experiment_type VARCHAR(20) DEFAULT 'traditional'",
                "ALTER TABLE thought_experiments ADD COLUMN funding_total INTEGER DEFAULT 0",
                "ALTER TABLE thought_experiments ADD COLUMN contributor_count INTEGER DEFAULT 0",
                "ALTER TABLE thought_experiments ADD COLUMN camera_feed_url VARCHAR(500)",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    col = stmt.split("ADD COLUMN ")[-1].split()[0]
                    logger.warning("v33 migration: ADD COLUMN %s skipped: %s", col, e)
            conn.commit()
        set_schema_version(engine, 33)

    if current < 34:
        logger.info("HevolveSocial: migrating to v34 (Compute pledge extensions for thought experiments)")
        with engine.connect() as conn:
            # Extend compute_escrow for experiment-specific pledges
            for stmt in [
                "ALTER TABLE compute_escrow ADD COLUMN experiment_post_id VARCHAR(64)",
                "ALTER TABLE compute_escrow ADD COLUMN pledge_type VARCHAR(20)",
                "ALTER TABLE compute_escrow ADD COLUMN consumed REAL DEFAULT 0.0",
                "ALTER TABLE compute_escrow ADD COLUMN pledge_message TEXT",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    col = stmt.split("ADD COLUMN ")[-1].split()[0]
                    logger.warning("v34 migration: ADD COLUMN %s on compute_escrow skipped: %s", col, e)
            # Index for fast experiment pledge lookups
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_compute_escrow_experiment_post_id "
                    "ON compute_escrow (experiment_post_id)"))
            except Exception as e:
                logger.warning("v34 migration: index on experiment_post_id skipped: %s", e)
            # Extend metered_api_usage for consumption-to-escrow linking
            for stmt in [
                "ALTER TABLE metered_api_usage ADD COLUMN escrow_id INTEGER",
                "ALTER TABLE metered_api_usage ADD COLUMN experiment_post_id VARCHAR(64)",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    col = stmt.split("ADD COLUMN ")[-1].split()[0]
                    logger.warning("v34 migration: ADD COLUMN %s on metered_api_usage skipped: %s", col, e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_metered_api_usage_escrow_id "
                    "ON metered_api_usage (escrow_id)"))
            except Exception as e:
                logger.warning("v34 migration: index on escrow_id skipped: %s", e)
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_metered_api_usage_experiment_post_id "
                    "ON metered_api_usage (experiment_post_id)"))
            except Exception as e:
                logger.warning("v34 migration: index on experiment_post_id skipped: %s", e)
            conn.commit()
        set_schema_version(engine, 34)

    if current < 35:
        logger.info("HevolveSocial: migrating to v35 (Compute Pledge + Consumption tables)")
        from .models import ComputePledge, PledgeConsumption
        for tbl in [ComputePledge.__table__, PledgeConsumption.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 35)
