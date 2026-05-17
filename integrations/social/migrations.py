"""
HevolveSocial - Schema Migrations
Version tracking and migration helpers.
"""
import logging
from sqlalchemy import text
from .models import get_engine, Base

logger = logging.getLogger('hevolve_social')

SCHEMA_VERSION = 51


# Tables that hold tenant-scoped user content. v40 adds a nullable
# `tenant_id` column to each. NULL on every row in flat/regional
# deploys (zero behavior change). Set on every row in central cloud
# (where the JWT 'tid' claim resolves the tenant). Polymorphic query
# filter in auth.py injects WHERE tenant_id = g.tenant_id when set.
#
# This list is conservative — system/admin/audit tables (e.g.
# fraud_alerts, integrity_challenges, federated_posts) are not
# tenant-scoped today; they can be added in a follow-up migration
# if cloud isolation needs them.
_V40_TENANT_TABLES = [
    'users', 'posts', 'comments', 'votes', 'follows',
    'communities', 'community_memberships',
    'notifications', 'reports',
    'agent_skill_badges', 'task_requests',
    'agent_evolution', 'agent_collaborations',
    'ratings', 'trust_scores',
    'referrals', 'referral_codes',
    'onboarding_progress',
    'encounters', 'discoverable_prefs', 'encounter_sightings',
    'missed_connections', 'missed_connection_responses',
    'location_pings', 'proximity_matches',
    'resonance_wallets', 'resonance_transactions',
    'user_achievements', 'user_challenges',
    'region_memberships', 'region_assignments',
    'boosts', 'campaigns', 'campaign_actions',
]


def _is_already_exists_error(exc: Exception) -> bool:
    """Pass-5 F14 helper: distinguish "already-exists" failures
    (idempotent migration safe-skip) from genuine SQL errors that
    should escalate.

    Existing migrations follow the warn-only pattern (try → except →
    log warning).  New migration steps SHOULD adopt this helper to
    differentiate:
      - Already-exists: log at INFO, continue (idempotent).
      - Other errors: log at ERROR, the migration step has a real
        problem the operator should investigate.

    Until the call-site rewire lands (large mechanical change), this
    helper is the canonical predicate.  A unit test in
    test_phase7c_reviewer_fixes.py locks the recognised signals.

    SQLite raises `sqlite3.OperationalError: duplicate column name: X`
    or `... already exists`.
    Postgres raises `psycopg2.errors.DuplicateColumn` / `DuplicateTable`
    or wrapped `ProgrammingError` with state codes 42701 / 42P07.
    MySQL raises `mysql.connector.errors.ProgrammingError` with
    errno 1060 (duplicate column) or 1050 (table already exists).

    Returns True iff the error message text contains a recognised
    "already exists" / "duplicate" signal.  Conservative — anything
    else is treated as a genuine error and the caller logs at ERROR
    instead of WARNING.
    """
    msg = str(exc).lower()
    signals = (
        'already exists',
        'duplicate column',
        'duplicate key',
        'duplicate entry',
        'duplicate table',
        # Postgres SQLSTATE codes embedded in psycopg2 messages
        '42701',  # duplicate_column
        '42p07',  # duplicate_table
        # MySQL errnos
        '1060',  # ER_DUP_FIELDNAME
        '1050',  # ER_TABLE_EXISTS_ERROR
    )
    return any(s in msg for s in signals)


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

    if current < 36:
        logger.info("HevolveSocial: migrating to v36 (Channel bindings, conversation entries, presence)")
        from .models import UserChannelBinding, ConversationEntry, ChannelPresence
        for tbl in [UserChannelBinding.__table__, ConversationEntry.__table__, ChannelPresence.__table__]:
            tbl.create(engine, checkfirst=True)
        set_schema_version(engine, 36)

    if current < 37:
        # v37: agent voice_profile — previously the POST /users/<id>/agents
        # endpoint accepted `voice_profile` in the request body but silently
        # dropped it because the User model had no column for it.  Adds a
        # JSON column (TEXT-backed on SQLite) so voice presets round-trip.
        logger.info("HevolveSocial: migrating to v37 (User.voice_profile column)")
        with engine.connect() as conn:
            try:
                # SQLAlchemy JSON maps to TEXT on SQLite / JSON on MySQL/PG.
                # ALTER TABLE ADD COLUMN TEXT is the portable form.
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN voice_profile TEXT"))
            except Exception as e:
                logger.warning(
                    "v37 migration: ADD COLUMN voice_profile on users "
                    "skipped (may already exist): %s", e)
            conn.commit()
        set_schema_version(engine, 37)

    if current < 38:
        # v38: cross-device chat mirroring (U1-U9 workstream, task #389).
        # Adds msg_id/request_id/device_id/lang/attachments to
        # conversation_entries so `/api/chat-sync/pull?since=<id>` and the
        # `chat.new` WAMP event carry everything a remote device needs
        # to reconstruct a turn — including attachments (U9, WhatsApp-style
        # file replication) and the originating device_id (U6, RN stamping).
        #
        # Each DDL statement runs in its own connection so a failure on one
        # (e.g., column already exists from a prior partial run) does not
        # abort the remaining ones under PostgreSQL's aborted-transaction
        # rule.  MySQL auto-commits DDL; SQLite tolerates reuse.
        logger.info("HevolveSocial: migrating to v38 (chat-sync ConversationEntry columns)")
        _v38_stmts = [
            ("ALTER TABLE conversation_entries ADD COLUMN msg_id VARCHAR(32)",
             "ADD COLUMN msg_id"),
            ("ALTER TABLE conversation_entries ADD COLUMN request_id VARCHAR(64)",
             "ADD COLUMN request_id"),
            ("ALTER TABLE conversation_entries ADD COLUMN device_id VARCHAR(64)",
             "ADD COLUMN device_id"),
            ("ALTER TABLE conversation_entries ADD COLUMN lang VARCHAR(10)",
             "ADD COLUMN lang"),
            ("ALTER TABLE conversation_entries ADD COLUMN attachments TEXT",
             "ADD COLUMN attachments"),
            ("CREATE UNIQUE INDEX ix_conversation_entries_msg_id "
             "ON conversation_entries (msg_id)",
             "CREATE UNIQUE INDEX msg_id"),
            ("CREATE INDEX ix_conversation_entries_request_id "
             "ON conversation_entries (request_id)",
             "CREATE INDEX request_id"),
            ("CREATE INDEX ix_conversation_entries_device_id "
             "ON conversation_entries (device_id)",
             "CREATE INDEX device_id"),
        ]
        for sql, label in _v38_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v38 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 38)

    if current < 39:
        # v39: BLE encounter persistence — replaces the in-memory
        # _EncounterStore in encounter_api.py with real DB tables.
        # Adds two new tables (discoverable_prefs, encounter_sightings)
        # and extends `encounters` with lat / lng / payload columns so
        # post-match BLE rows persist in the canonical encounter graph
        # (context_type='ble') with their map pin and per-side
        # icebreaker state.  Per-statement commit (PostgreSQL-safe).
        logger.info("HevolveSocial: migrating to v39 "
                    "(BLE encounter persistence — discoverable_prefs, "
                    "encounter_sightings, encounters.lat/lng/payload)")
        _v39_stmts = [
            ("""CREATE TABLE IF NOT EXISTS discoverable_prefs (
                user_id VARCHAR(64) PRIMARY KEY REFERENCES users(id),
                enabled BOOLEAN DEFAULT 0 NOT NULL,
                enabled_at DATETIME,
                expires_at DATETIME,
                age_claim_18 BOOLEAN DEFAULT 0 NOT NULL,
                face_visible BOOLEAN DEFAULT 0 NOT NULL,
                avatar_style VARCHAR(64) DEFAULT 'studio_ghibli',
                vibe_tags JSON,
                toggle_count_24h INTEGER DEFAULT 0,
                toggle_window_start DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_toggle_at DATETIME,
                current_pubkey VARCHAR(128),
                pubkey_registered_at DATETIME,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
             )""",
             "CREATE TABLE discoverable_prefs"),
            ("CREATE INDEX IF NOT EXISTS ix_discoverable_prefs_expires_at "
             "ON discoverable_prefs(expires_at)",
             "CREATE INDEX expires_at"),
            ("CREATE INDEX IF NOT EXISTS ix_discoverable_prefs_current_pubkey "
             "ON discoverable_prefs(current_pubkey)",
             "CREATE INDEX current_pubkey"),
            ("""CREATE TABLE IF NOT EXISTS encounter_sightings (
                id VARCHAR(64) PRIMARY KEY,
                owner_user_id VARCHAR(64) NOT NULL REFERENCES users(id),
                peer_user_id VARCHAR(64) REFERENCES users(id),
                peer_pubkey VARCHAR(128) NOT NULL,
                rssi_peak INTEGER,
                dwell_sec INTEGER,
                lat FLOAT,
                lng FLOAT,
                sighted_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                swipe_decision VARCHAR(10) DEFAULT 'pending',
                expires_at DATETIME NOT NULL
             )""",
             "CREATE TABLE encounter_sightings"),
            ("CREATE INDEX IF NOT EXISTS ix_encounter_sightings_owner_user_id "
             "ON encounter_sightings(owner_user_id)",
             "CREATE INDEX owner_user_id"),
            ("CREATE INDEX IF NOT EXISTS ix_encounter_sightings_peer_user_id "
             "ON encounter_sightings(peer_user_id)",
             "CREATE INDEX peer_user_id"),
            ("CREATE INDEX IF NOT EXISTS ix_encounter_sightings_owner_sighted "
             "ON encounter_sightings(owner_user_id, sighted_at)",
             "CREATE INDEX owner_sighted"),
            ("CREATE INDEX IF NOT EXISTS ix_encounter_sightings_peer_pubkey "
             "ON encounter_sightings(peer_pubkey)",
             "CREATE INDEX peer_pubkey"),
            ("ALTER TABLE encounters ADD COLUMN lat FLOAT",
             "ADD COLUMN encounters.lat"),
            ("ALTER TABLE encounters ADD COLUMN lng FLOAT",
             "ADD COLUMN encounters.lng"),
            ("ALTER TABLE encounters ADD COLUMN payload JSON",
             "ADD COLUMN encounters.payload"),
        ]
        for sql, label in _v39_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v39 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 39)

    if current < 40:
        # v40: tenancy substrate. Adds nullable `tenant_id` (+ index)
        # to every user-content table. NULL passes through unchanged
        # in flat/regional deploys; central cloud populates per row
        # via the JWT 'tid' claim resolver in auth.py. The plan
        # (sunny-gliding-eich.md, Part C.1) explains the full
        # tenancy decision: one column on every social table,
        # SQLAlchemy event listener filters by g.tenant_id when set.
        # Per-statement commit (PostgreSQL-safe). Idempotent — each
        # ALTER is wrapped in try/except so re-running a partial
        # migration is safe.
        logger.info(
            "HevolveSocial: migrating to v40 "
            "(tenancy substrate — nullable tenant_id on %d tables)",
            len(_V40_TENANT_TABLES))
        _v40_stmts = []
        for tbl in _V40_TENANT_TABLES:
            _v40_stmts.append((
                f"ALTER TABLE {tbl} ADD COLUMN tenant_id VARCHAR(64)",
                f"ADD COLUMN {tbl}.tenant_id"))
            _v40_stmts.append((
                f"CREATE INDEX IF NOT EXISTS ix_{tbl}_tenant_id "
                f"ON {tbl}(tenant_id)",
                f"CREATE INDEX {tbl}.tenant_id"))
        for sql, label in _v40_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v40 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 40)

    if current < 41:
        # v41: polymorphic Membership table. Replaces today's implicit
        # `community_memberships`-only roster with a single table that
        # supports communities AND conversations (DM/group chat) as
        # parents, and humans AND agents as members. Includes a
        # one-shot backfill from `community_memberships` so existing
        # community rosters are immediately visible via the new API
        # (community_members endpoint reads Membership first, falls
        # back to community_memberships during the dual-write window
        # — see plan Part P.2). New writes go to BOTH tables until
        # cut-over in Phase 7e.
        #
        # Plan reference: sunny-gliding-eich.md, Part C.2.
        logger.info(
            "HevolveSocial: migrating to v41 "
            "(polymorphic Membership table + backfill)")
        _v41_stmts = [
            ("""CREATE TABLE IF NOT EXISTS memberships (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                parent_kind VARCHAR(20) NOT NULL,
                parent_id VARCHAR(64) NOT NULL,
                member_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                agent_kind VARCHAR(20) DEFAULT 'human' NOT NULL,
                role VARCHAR(20) DEFAULT 'member' NOT NULL,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                muted_until DATETIME,
                notification_pref VARCHAR(20) DEFAULT 'all' NOT NULL,
                agent_grant_id VARCHAR(64)
             )""",
             "CREATE TABLE memberships"),
            ("CREATE INDEX IF NOT EXISTS ix_memberships_tenant_id "
             "ON memberships(tenant_id)",
             "CREATE INDEX memberships.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_memberships_parent "
             "ON memberships(parent_kind, parent_id)",
             "CREATE INDEX memberships.parent_kind+parent_id"),
            ("CREATE INDEX IF NOT EXISTS ix_memberships_member "
             "ON memberships(member_id, parent_kind)",
             "CREATE INDEX memberships.member_id+parent_kind"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_memberships_unique "
             "ON memberships(parent_kind, parent_id, member_id)",
             "UNIQUE INDEX memberships(parent_kind+parent_id+member_id)"),
        ]
        for sql, label in _v41_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v41 migration: %s skipped (may already exist): %s",
                    label, e)

        # One-shot backfill from community_memberships. Idempotent —
        # the UNIQUE INDEX above prevents duplicate inserts on re-run.
        # We synthesize a Membership.id by reusing the source row's id
        # (deterministic) so the backfill is idempotent.
        try:
            with engine.connect() as conn:
                result = conn.execute(text(
                    "SELECT id, user_id, community_id, role, created_at "
                    "FROM community_memberships"))
                rows = result.fetchall()
                inserted = 0
                for row in rows:
                    try:
                        conn.execute(text(
                            "INSERT OR IGNORE INTO memberships "
                            "(id, parent_kind, parent_id, member_id, "
                            " agent_kind, role, joined_at, notification_pref) "
                            "VALUES "
                            "(:id, 'community', :pid, :mid, "
                            " 'human', :role, :joined, 'all')"),
                            {
                                'id': row[0],
                                'pid': row[2],
                                'mid': row[1],
                                'role': row[3] or 'member',
                                'joined': row[4],
                            })
                        inserted += 1
                    except Exception as ie:
                        # PostgreSQL doesn't support INSERT OR IGNORE;
                        # use ON CONFLICT DO NOTHING fallback inline.
                        try:
                            conn.execute(text(
                                "INSERT INTO memberships "
                                "(id, parent_kind, parent_id, member_id, "
                                " agent_kind, role, joined_at, notification_pref) "
                                "VALUES "
                                "(:id, 'community', :pid, :mid, "
                                " 'human', :role, :joined, 'all') "
                                "ON CONFLICT (parent_kind, parent_id, member_id) "
                                "DO NOTHING"),
                                {
                                    'id': row[0],
                                    'pid': row[2],
                                    'mid': row[1],
                                    'role': row[3] or 'member',
                                    'joined': row[4],
                                })
                            inserted += 1
                        except Exception as pg_e:
                            logger.warning(
                                "v41 backfill: row %s skipped: %s",
                                row[0], pg_e)
                conn.commit()
                logger.info(
                    "v41 backfill: inserted %d memberships from "
                    "community_memberships",
                    inserted)
        except Exception as e:
            logger.warning(
                "v41 backfill: skipped (community_memberships missing?): %s",
                e)
        set_schema_version(engine, 41)

    if current < 42:
        # v42: first-class @-mention index. Phase 7b. Plan reference:
        # sunny-gliding-eich.md, Part C.2 + Part E.5.
        #
        # Each row records one (source, mentioned_user) pair created
        # when MentionService.parse_and_record() processes a post,
        # comment, or message. Composite index supports the two
        # primary read paths: (1) "show me my mentions" feed
        # (mentioned_user_id, created_at DESC), (2) "edit a post —
        # diff old vs new mentions" (source_kind, source_id).
        logger.info(
            "HevolveSocial: migrating to v42 "
            "(Mention table — first-class @-mention index)")
        _v42_stmts = [
            ("""CREATE TABLE IF NOT EXISTS mentions (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                source_kind VARCHAR(20) NOT NULL,
                source_id VARCHAR(64) NOT NULL,
                mentioned_user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mentioned_kind VARCHAR(20) DEFAULT 'human' NOT NULL,
                agent_owner_id VARCHAR(64),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                notified_at DATETIME
             )""",
             "CREATE TABLE mentions"),
            ("CREATE INDEX IF NOT EXISTS ix_mentions_tenant_id "
             "ON mentions(tenant_id)",
             "CREATE INDEX mentions.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_mentions_mentioned_user "
             "ON mentions(mentioned_user_id, created_at)",
             "CREATE INDEX mentions.mentioned_user+created_at"),
            ("CREATE INDEX IF NOT EXISTS ix_mentions_source "
             "ON mentions(source_kind, source_id)",
             "CREATE INDEX mentions.source_kind+source_id"),
        ]
        for sql, label in _v42_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v42 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 42)

    if current < 43:
        # v43: Friendship state machine + Block table. Phase 7c.1.
        # Plan reference: sunny-gliding-eich.md, Part C.2 + Part E.8.
        #
        # Coexists with the existing one-direction Follow table —
        # NEITHER replaces nor migrates it. Existing follows remain
        # untouched. Friendship is a new symmetric relationship with
        # state (pending / active / blocked / rejected); accept
        # auto-creates reciprocal Follow rows so downstream code that
        # reads the follow graph keeps working.
        #
        # Block is separate from Friendship to allow blocking users
        # who never were friends. Block is the authoritative source
        # for the PeerLink trust-ratchet teardown (plan Part R.5).
        logger.info(
            "HevolveSocial: migrating to v43 "
            "(Friendship state machine + Block table)")
        _v43_stmts = [
            ("""CREATE TABLE IF NOT EXISTS friendships (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                user_a_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user_b_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                initiator_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                accepted_at DATETIME,
                blocked_at DATETIME
             )""",
             "CREATE TABLE friendships"),
            ("CREATE INDEX IF NOT EXISTS ix_friendships_tenant_id "
             "ON friendships(tenant_id)",
             "CREATE INDEX friendships.tenant_id"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_friendships_pair "
             "ON friendships(user_a_id, user_b_id)",
             "UNIQUE INDEX friendships(a,b)"),
            ("CREATE INDEX IF NOT EXISTS ix_friendships_user_a "
             "ON friendships(user_a_id, status)",
             "INDEX friendships.user_a+status"),
            ("CREATE INDEX IF NOT EXISTS ix_friendships_user_b "
             "ON friendships(user_b_id, status)",
             "INDEX friendships.user_b+status"),
            ("""CREATE TABLE IF NOT EXISTS blocks (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                blocker_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                blocked_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE blocks"),
            ("CREATE INDEX IF NOT EXISTS ix_blocks_tenant_id "
             "ON blocks(tenant_id)",
             "CREATE INDEX blocks.tenant_id"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_blocks_pair "
             "ON blocks(blocker_id, blocked_id)",
             "UNIQUE INDEX blocks(blocker,blocked)"),
            ("CREATE INDEX IF NOT EXISTS ix_blocks_blocker "
             "ON blocks(blocker_id)",
             "INDEX blocks.blocker"),
            ("CREATE INDEX IF NOT EXISTS ix_blocks_blocked "
             "ON blocks(blocked_id)",
             "INDEX blocks.blocked"),
        ]
        for sql, label in _v43_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v43 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 43)

    if current < 44:
        # v44: First-class Invite table (community + conversation).
        # Phase 7c.2. Plan reference: sunny-gliding-eich.md, Part C.2 +
        # Part E.9.
        #
        # Polymorphic by (parent_kind, parent_id) — one schema serves
        # both community invites and conversation invites once Phase
        # 7c.3 ships.  Three invite shapes:
        #
        #   1. Targeted user — invitee_id set, invitee_email NULL.
        #   2. Off-platform email — invitee_id NULL, invitee_email set.
        #   3. Shareable link — both NULL, invite_code is the token in
        #      the URL `/i/<code>`.  The first user to GET /invites/<code>
        #      and POST .../accept takes the slot (then status=accepted).
        #
        # Status state machine: pending → accepted | rejected | expired.
        # expires_at is checked at accept time and on incoming-list fetch.
        logger.info(
            "HevolveSocial: migrating to v44 (Invite table)")
        _v44_stmts = [
            ("""CREATE TABLE IF NOT EXISTS invites (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                parent_kind VARCHAR(20) NOT NULL,
                parent_id VARCHAR(64) NOT NULL,
                invitee_id VARCHAR(64),
                invitee_email VARCHAR(255),
                invite_code VARCHAR(64) NOT NULL UNIQUE,
                invited_by VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_offered VARCHAR(20) DEFAULT 'member' NOT NULL,
                status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                expires_at DATETIME,
                responded_at DATETIME
             )""",
             "CREATE TABLE invites"),
            ("CREATE INDEX IF NOT EXISTS ix_invites_tenant_id "
             "ON invites(tenant_id)",
             "CREATE INDEX invites.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_invites_parent "
             "ON invites(parent_kind, parent_id)",
             "CREATE INDEX invites.parent_kind+parent_id"),
            ("CREATE INDEX IF NOT EXISTS ix_invites_invitee "
             "ON invites(invitee_id, status)",
             "CREATE INDEX invites.invitee_id+status"),
            ("CREATE INDEX IF NOT EXISTS ix_invites_invited_by "
             "ON invites(invited_by, status)",
             "CREATE INDEX invites.invited_by+status"),
        ]
        for sql, label in _v44_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v44 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 44)

    if current < 45:
        # v45: Conversations (DM/group) + unified Message table.
        # Phase 7c.3.  Plan reference: sunny-gliding-eich.md, Part C.2 +
        # Part E.3.
        #
        # Naming: `conversations` (NEW) is the internal DM/group chat
        # surface — distinct from the legacy `conversation_entries`
        # (HARTOS 31-channel external adapter — Telegram, WhatsApp,
        # Discord, etc.) and from `conversation` (legacy single-user
        # Q/A history).  All three coexist; readers of the legacy
        # tables are untouched.
        #
        # Design notes:
        #   - DM dedup uses `member_hash` (sorted member IDs hashed)
        #     so re-creating a DM between A↔B returns the existing
        #     row instead of a duplicate.  App-level SELECT-then-INSERT;
        #     a UNIQUE INDEX would need a partial expression that's
        #     not portable across MySQL — Phase 9 hardening can add
        #     a per-dialect unique constraint if the race window
        #     becomes a problem.
        #   - Membership in a conversation lives in the polymorphic
        #     `memberships` table (v41) with parent_kind='conversation'.
        #     One source of truth across communities + conversations.
        #   - Messages.parent_kind covers 'conversation' and (later)
        #     'community' / 'post' so reactions, mentions, and
        #     moderation can run a single unified pipeline.
        logger.info(
            "HevolveSocial: migrating to v45 "
            "(Conversations + unified Messages)")
        _v45_stmts = [
            ("""CREATE TABLE IF NOT EXISTS conversations (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                kind VARCHAR(20) NOT NULL,
                title VARCHAR(200),
                created_by VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                member_hash VARCHAR(128),
                last_message_at DATETIME,
                is_locked BOOLEAN DEFAULT 0 NOT NULL,
                is_archived BOOLEAN DEFAULT 0 NOT NULL,
                settings TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE conversations"),
            ("CREATE INDEX IF NOT EXISTS ix_conversations_tenant_id "
             "ON conversations(tenant_id)",
             "INDEX conversations.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_conversations_created_by "
             "ON conversations(created_by)",
             "INDEX conversations.created_by"),
            ("CREATE INDEX IF NOT EXISTS ix_conversations_member_hash "
             "ON conversations(kind, member_hash)",
             "INDEX conversations.kind+member_hash"),
            ("""CREATE TABLE IF NOT EXISTS messages (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                parent_kind VARCHAR(20) NOT NULL,
                parent_id VARCHAR(64) NOT NULL,
                thread_root_id VARCHAR(64),
                author_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                agent_kind VARCHAR(20) DEFAULT 'human' NOT NULL,
                content TEXT NOT NULL,
                content_html TEXT,
                depth INTEGER DEFAULT 0 NOT NULL,
                edited_at DATETIME,
                is_deleted BOOLEAN DEFAULT 0 NOT NULL,
                metadata_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE messages"),
            ("CREATE INDEX IF NOT EXISTS ix_messages_tenant_id "
             "ON messages(tenant_id)",
             "INDEX messages.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_messages_parent "
             "ON messages(parent_kind, parent_id, created_at)",
             "INDEX messages.parent+created_at"),
            ("CREATE INDEX IF NOT EXISTS ix_messages_thread_root "
             "ON messages(thread_root_id)",
             "INDEX messages.thread_root"),
            ("CREATE INDEX IF NOT EXISTS ix_messages_author "
             "ON messages(author_id)",
             "INDEX messages.author"),
        ]
        for sql, label in _v45_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v45 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 45)

    if current < 46:
        # v46: per-conversation read tracking on memberships.
        # Phase 7c.7. Plan reference: sunny-gliding-eich.md, Part E.3.
        #
        # Two columns added to the existing memberships table — chosen
        # so we don't create a separate `read_receipts` table when the
        # state is already per-(conversation, member). Storing it on
        # the existing membership row keeps the read path a single
        # JOIN and makes deletions cascade with member-leave.
        #
        # Typing is intentionally NOT persisted — it's a pure WAMP emit
        # with a 5s TTL on the receiver side.  Persisting typing state
        # would require constant DB churn for an ephemeral signal.
        logger.info(
            "HevolveSocial: migrating to v46 "
            "(read receipts on memberships)")
        _v46_stmts = [
            ("ALTER TABLE memberships ADD COLUMN last_read_message_id "
             "VARCHAR(64)",
             "ADD COLUMN memberships.last_read_message_id"),
            ("ALTER TABLE memberships ADD COLUMN last_read_at DATETIME",
             "ADD COLUMN memberships.last_read_at"),
        ]
        for sql, label in _v46_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v46 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 46)

    if current < 47:
        # v47: emoji reactions on posts / comments / messages.
        # Phase 7c.4. Plan reference: sunny-gliding-eich.md, Part E.6.
        #
        # Polymorphic by (source_kind, source_id) so the same table
        # serves all three reactable surfaces.  Coexists with the
        # existing binary VoteService — votes are aggregate karma,
        # reactions are emoji.
        #
        # UNIQUE(source_kind, source_id, user_id, emoji) prevents the
        # same user adding the same emoji twice and gives the toggle
        # semantics a deterministic resolution.
        logger.info(
            "HevolveSocial: migrating to v47 (Reactions table)")
        _v47_stmts = [
            ("""CREATE TABLE IF NOT EXISTS reactions (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                source_kind VARCHAR(20) NOT NULL,
                source_id VARCHAR(64) NOT NULL,
                user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                emoji VARCHAR(16) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE reactions"),
            ("CREATE INDEX IF NOT EXISTS ix_reactions_tenant_id "
             "ON reactions(tenant_id)",
             "INDEX reactions.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_reactions_source "
             "ON reactions(source_kind, source_id)",
             "INDEX reactions.source"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_reactions_unique "
             "ON reactions(source_kind, source_id, user_id, emoji)",
             "UNIQUE INDEX reactions(source_kind+source_id+user_id+emoji)"),
            ("CREATE INDEX IF NOT EXISTS ix_reactions_user "
             "ON reactions(user_id)",
             "INDEX reactions.user"),
        ]
        for sql, label in _v47_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v47 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 47)

    if current < 48:
        # v48: per-post privacy column.
        # Phase 7c.5. Plan reference: sunny-gliding-eich.md, Part E.10.
        #
        # Nullable; legacy rows stay NULL and are normalised to 'public'
        # by integrations.social.privacy._normalize so existing posts
        # retain their pre-migration visibility exactly. New writes set
        # one of {public, friends, community, private}; the gate is
        # flag-gated behind 'post_privacy' so flat/regional deploys
        # without the flag on never read or write the column.
        logger.info(
            "HevolveSocial: migrating to v48 (post privacy column)")
        _v48_stmts = [
            ("ALTER TABLE posts ADD COLUMN privacy VARCHAR(16)",
             "ADD COLUMN posts.privacy"),
            ("CREATE INDEX IF NOT EXISTS ix_posts_privacy "
             "ON posts(privacy)",
             "INDEX posts.privacy"),
        ]
        for sql, label in _v48_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v48 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 48)

    if current < 49:
        # v49: Phase 7d — voice/video/screen-share rooms.
        # Plan reference: sunny-gliding-eich.md, Part C.2 + Part E.4 + E.7.
        #
        # Three tables ship together because they form a single
        # consistent surface area:
        #
        #   1. call_sessions    — one row per active or ended room.
        #      `id` doubles as the LiveKit room name when LiveKit is
        #      the transport (central deploy).  For flat / regional
        #      with WebRTC P2P mesh, `id` is just a UUID.
        #
        #   2. call_participants — per-track state.  device_kind
        #      includes 'agent_bridge' for AgentVoiceBridge participants
        #      (Plan E.12 — agents never hold a media socket; the
        #      bridge does, on the user's local node).
        #
        #   3. agent_join_grants — per-parent owner-issued consent for
        #      an agent to join a community / conversation / call.
        #      can_voice / can_screen scopes gate AgentVoiceBridge.
        #
        # All three tables are nullable on tenant_id (consistent with
        # the v40 tenancy invariant) and additive — no existing column
        # is touched.  Flag-gated by `calls_v1` server-side; off →
        # endpoints return 503 via requires_flag.
        logger.info(
            "HevolveSocial: migrating to v49 "
            "(call sessions + participants + agent join grants)")
        _v49_stmts = [
            ("""CREATE TABLE IF NOT EXISTS call_sessions (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                parent_kind VARCHAR(20) NOT NULL,
                parent_id VARCHAR(64) NOT NULL,
                title VARCHAR(200),
                kind VARCHAR(20) DEFAULT 'voice' NOT NULL,
                started_by VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                ended_at DATETIME,
                livekit_room_sid VARCHAR(200),
                max_participants INTEGER DEFAULT 32,
                settings TEXT
             )""",
             "CREATE TABLE call_sessions"),
            ("CREATE INDEX IF NOT EXISTS ix_call_sessions_tenant_id "
             "ON call_sessions(tenant_id)",
             "INDEX call_sessions.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_call_sessions_parent "
             "ON call_sessions(parent_kind, parent_id)",
             "INDEX call_sessions.parent"),
            ("CREATE INDEX IF NOT EXISTS ix_call_sessions_active "
             "ON call_sessions(ended_at)",
             "INDEX call_sessions.active"),
            ("""CREATE TABLE IF NOT EXISTS call_participants (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                call_id VARCHAR(64) NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
                user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                agent_kind VARCHAR(20) DEFAULT 'human' NOT NULL,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                left_at DATETIME,
                is_muted INTEGER DEFAULT 0 NOT NULL,
                is_video_on INTEGER DEFAULT 0 NOT NULL,
                is_screen_sharing INTEGER DEFAULT 0 NOT NULL,
                device_kind VARCHAR(20) DEFAULT 'mobile' NOT NULL
             )""",
             "CREATE TABLE call_participants"),
            ("CREATE INDEX IF NOT EXISTS ix_call_participants_tenant_id "
             "ON call_participants(tenant_id)",
             "INDEX call_participants.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_call_participants_call "
             "ON call_participants(call_id)",
             "INDEX call_participants.call"),
            ("CREATE INDEX IF NOT EXISTS ix_call_participants_user "
             "ON call_participants(user_id, left_at)",
             "INDEX call_participants.user_active"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_call_participants_active "
             "ON call_participants(call_id, user_id) "
             "WHERE left_at IS NULL",
             "UNIQUE INDEX call_participants.active"),
            ("""CREATE TABLE IF NOT EXISTS agent_join_grants (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                agent_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                owner_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parent_kind VARCHAR(20) NOT NULL,
                parent_id VARCHAR(64) NOT NULL,
                scope TEXT NOT NULL,
                granted_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                revoked_at DATETIME,
                source VARCHAR(40) DEFAULT 'user_explicit' NOT NULL
             )""",
             "CREATE TABLE agent_join_grants"),
            ("CREATE INDEX IF NOT EXISTS ix_agent_join_grants_tenant_id "
             "ON agent_join_grants(tenant_id)",
             "INDEX agent_join_grants.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_agent_join_grants_parent "
             "ON agent_join_grants(parent_kind, parent_id)",
             "INDEX agent_join_grants.parent"),
            ("CREATE INDEX IF NOT EXISTS ix_agent_join_grants_agent "
             "ON agent_join_grants(agent_id, revoked_at)",
             "INDEX agent_join_grants.agent_active"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_join_grants_active "
             "ON agent_join_grants(agent_id, parent_kind, parent_id) "
             "WHERE revoked_at IS NULL",
             "UNIQUE INDEX agent_join_grants.active"),
        ]
        for sql, label in _v49_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v49 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 49)

    if current < 50:
        # v50: Phase 7e — AI moderation default-on (post-DLP soft signal).
        # Plan reference: sunny-gliding-eich.md, Part E.11 + Part M.
        #
        # ContentModerationDecision rows are append-only audit records of
        # the classifier's verdict on a post / comment / message.
        # Polymorphic via (source_kind, source_id), same shape as the
        # Mention + Reaction tables.
        #
        # `decision` enum: 'allow' | 'quarantine' | 'block'.
        # `decision = 'block'` AND high confidence → caller flips
        #     Post.is_hidden = True (existing column from v1).
        # `decision = 'quarantine'` → caller flips the new
        #     posts.is_quarantined column added below; mods see it in
        #     the Report queue with `Report.auto_action='hide'` set.
        #
        # `human_reviewer_id` + `human_decision` are nullable so a mod
        # can later overrule the AI verdict.  Append-only — overrules
        # write a new row; never mutate the original.
        #
        # Flag-gated by `moderation_v2` server-side.  Off → no rows
        # written, no quarantine flips, no notifications.  Existing
        # DLPEngine pre-publish path is UNCHANGED — this is a SECOND
        # layer below it (Plan B.3 + Part M).
        logger.info(
            "HevolveSocial: migrating to v50 "
            "(ContentModerationDecision + posts.is_quarantined)")
        _v50_stmts = [
            ("""CREATE TABLE IF NOT EXISTS content_moderation_decisions (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                source_kind VARCHAR(20) NOT NULL,
                source_id VARCHAR(64) NOT NULL,
                classifier_model VARCHAR(80),
                classifications TEXT NOT NULL,
                decision VARCHAR(20) NOT NULL,
                confidence REAL NOT NULL,
                human_reviewer_id VARCHAR(64) REFERENCES users(id) ON DELETE SET NULL,
                human_decision VARCHAR(20),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                reviewed_at DATETIME
             )""",
             "CREATE TABLE content_moderation_decisions"),
            ("CREATE INDEX IF NOT EXISTS ix_cmd_tenant_id "
             "ON content_moderation_decisions(tenant_id)",
             "INDEX cmd.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_cmd_source "
             "ON content_moderation_decisions(source_kind, source_id)",
             "INDEX cmd.source"),
            ("CREATE INDEX IF NOT EXISTS ix_cmd_decision "
             "ON content_moderation_decisions(decision, created_at)",
             "INDEX cmd.decision_queue"),
            ("ALTER TABLE posts ADD COLUMN is_quarantined INTEGER DEFAULT 0",
             "ADD COLUMN posts.is_quarantined"),
        ]
        for sql, label in _v50_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v50 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 50)

    if current < 51:
        # v51: Phase 9 — optional E2E DM key envelope schema.
        # Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.
        #
        # E2E DMs are an OPT-IN Phase 9 feature.  When
        # `Conversation.settings.e2e_enabled = True` AND the
        # `e2e_dms` server flag is on, message bodies are stored as
        # ciphertext with one envelope per recipient member.  The
        # actual libsignal-style double-ratchet implementation lives
        # in a follow-up; this migration ships ONLY the key tables so
        # the schema is ready when the crypto lands.
        #
        # Two tables:
        #   1. conversation_keys — current public identity key per
        #      (user, conversation).  One row per member of an
        #      e2e_enabled conversation.  Used for envelope
        #      generation when a sender encrypts a new message.
        #
        #   2. message_envelopes — per-recipient encrypted payload.
        #      Append-only.  Each Message that's encrypted has one
        #      envelope row per active member.  When a member is
        #      removed mid-conversation, future Messages skip them
        #      (no envelope written), so they can't decrypt.
        #
        # Both tables are nullable on tenant_id consistent with v40.
        # All cryptography fields stored as base64-encoded TEXT for
        # dialect portability — production may upgrade to BLOB
        # columns in a follow-up.
        #
        # NOT in scope here: ratchet state machine, key rotation
        # cadence, key escrow / backup.  Plan E.X (Phase 9 follow-up).
        logger.info(
            "HevolveSocial: migrating to v51 "
            "(E2E DM key envelopes — schema only, crypto deferred)")
        _v51_stmts = [
            ("""CREATE TABLE IF NOT EXISTS conversation_keys (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                conversation_id VARCHAR(64) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                identity_key_b64 TEXT NOT NULL,
                signed_prekey_b64 TEXT,
                signed_prekey_signature_b64 TEXT,
                key_algorithm VARCHAR(40) DEFAULT 'x25519-ed25519' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                rotated_at DATETIME
             )""",
             "CREATE TABLE conversation_keys"),
            ("CREATE INDEX IF NOT EXISTS ix_conv_keys_tenant_id "
             "ON conversation_keys(tenant_id)",
             "INDEX conversation_keys.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_conv_keys_conv "
             "ON conversation_keys(conversation_id)",
             "INDEX conversation_keys.conv"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_conv_keys_active "
             "ON conversation_keys(conversation_id, user_id) "
             "WHERE rotated_at IS NULL",
             "UNIQUE INDEX conversation_keys.active"),
            ("""CREATE TABLE IF NOT EXISTS message_envelopes (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                message_id VARCHAR(64) NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                recipient_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ciphertext_b64 TEXT NOT NULL,
                ratchet_header_b64 TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE message_envelopes"),
            ("CREATE INDEX IF NOT EXISTS ix_msg_env_tenant_id "
             "ON message_envelopes(tenant_id)",
             "INDEX message_envelopes.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_msg_env_message "
             "ON message_envelopes(message_id)",
             "INDEX message_envelopes.message"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_env_pair "
             "ON message_envelopes(message_id, recipient_id)",
             "UNIQUE INDEX message_envelopes(message,recipient)"),
        ]
        for sql, label in _v51_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v51 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 51)

    if current < 52:
        # v52: Phase 9.B Double Ratchet state persistence.
        # Plan reference: sunny-gliding-eich.md, Part K.4.
        #
        # The ratchet primitives (e2e_ratchet.py) are stateless
        # functions that take + return a RatchetState NamedTuple.
        # This migration adds the table that persists that state
        # between message sends so Alice's send_message at t=1 and
        # at t=2 share the same advancing chain.
        #
        # Per-pair state: one row per (conversation, user, peer).
        # For DMs that's exactly two rows (Alice's view of Bob,
        # Bob's view of Alice).  For groups, an N-member conversation
        # has N*(N-1) rows (each member tracks every peer separately).
        #
        # state_json holds the serialized RatchetState — root_key,
        # sending/receiving chain keys, our_dh_priv/pub, their_dh_pub,
        # skipped_keys cache.  All bytes are base64-encoded for
        # dialect portability (same convention as conversation_keys).
        #
        # On Conversation deletion the rows cascade out (no audit
        # value — without the ratchet state the envelopes are
        # un-decryptable anyway).  When a member is removed mid-
        # conversation the rows for that pair go stale; future
        # messages skip them so they can't decrypt new envelopes.
        logger.info(
            "HevolveSocial: migrating to v52 "
            "(Double Ratchet state persistence — Phase 9.B integration)")
        _v52_stmts = [
            ("""CREATE TABLE IF NOT EXISTS ratchet_states (
                id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64),
                conversation_id VARCHAR(64) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id VARCHAR(64) NOT NULL,
                peer_id VARCHAR(64) NOT NULL,
                state_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
             )""",
             "CREATE TABLE ratchet_states"),
            ("CREATE INDEX IF NOT EXISTS ix_ratchet_states_tenant_id "
             "ON ratchet_states(tenant_id)",
             "INDEX ratchet_states.tenant_id"),
            ("CREATE INDEX IF NOT EXISTS ix_ratchet_states_conv "
             "ON ratchet_states(conversation_id)",
             "INDEX ratchet_states.conv"),
            ("CREATE UNIQUE INDEX IF NOT EXISTS ux_ratchet_states_triple "
             "ON ratchet_states(conversation_id, user_id, peer_id)",
             "UNIQUE INDEX ratchet_states(conv,user,peer)"),
        ]
        for sql, label in _v52_stmts:
            try:
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()
            except Exception as e:
                logger.warning(
                    "v52 migration: %s skipped (may already exist): %s",
                    label, e)
        set_schema_version(engine, 52)
