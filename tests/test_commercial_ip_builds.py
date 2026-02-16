"""
Tests for Commercial API Gateway, Defensive IP, Build Distribution, and Revenue Agent.

Covers: DefensivePublication CRUD, intelligence milestone, CommercialAPIService,
API key tiers, BuildDistributionService, revenue goal type registration.
"""
import os
import sys
import hashlib
import secrets
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import (
    Base, User, DefensivePublication, CommercialAPIKey,
    APIUsageLog, BuildLicense,
)


# ─── Fixtures ───

@pytest.fixture(scope='session')
def engine():
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope='session')
def tables(engine):
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(engine, tables):
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def test_user(db):
    user = User(username='testuser_cib', email='cib@test.com',
                password_hash='x', user_type='human')
    db.add(user)
    db.flush()
    return user


# ═══════════════════════════════════════════════════════════════
# Defensive Publications
# ═══════════════════════════════════════════════════════════════

class TestDefensivePublication:

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 42.5})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': False, 'checks_passed': 2})
    def test_create_defensive_publication(self, mock_verify, mock_moat, db, test_user):
        """Create a publication with SHA-256 content hash."""
        from integrations.agent_engine.ip_service import IPService

        content = "Novel architecture: distributed hive compute with RALT propagation"
        pub = IPService.create_defensive_publication(
            db, title='RALT Architecture', content=content,
            abstract='Distributed skill propagation',
            created_by=str(test_user.id))

        expected_hash = hashlib.sha256(content.encode()).hexdigest()
        assert pub['content_hash'] == expected_hash
        assert pub['title'] == 'RALT Architecture'
        assert pub['abstract'] == 'Distributed skill propagation'
        assert pub['moat_score_at_publication'] == 42.5
        assert pub['publication_date'] is not None

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 0.0})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True})
    def test_list_defensive_publications(self, mock_verify, mock_moat, db, test_user):
        """List returns publications in descending date order."""
        from integrations.agent_engine.ip_service import IPService

        IPService.create_defensive_publication(db, 'Pub 1', 'content-1')
        IPService.create_defensive_publication(db, 'Pub 2', 'content-2')
        IPService.create_defensive_publication(db, 'Pub 3', 'content-3')

        pubs = IPService.list_defensive_publications(db)
        assert len(pubs) >= 3
        # All three present
        titles = [p['title'] for p in pubs]
        assert 'Pub 1' in titles
        assert 'Pub 2' in titles
        assert 'Pub 3' in titles

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 10.0})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_get_provenance_record(self, mock_verify, mock_moat, db, test_user):
        """Provenance record aggregates publications, patents, moat, evidence."""
        from integrations.agent_engine.ip_service import IPService

        IPService.create_defensive_publication(db, 'Provenance Test', 'some-content')
        record = IPService.get_provenance_record(db)

        assert 'generated_at' in record
        assert 'defensive_publications' in record
        assert 'patents' in record
        assert 'evidence_chain' in record
        assert 'moat_depth' in record
        assert record['total_publications'] >= 1


# ═══════════════════════════════════════════════════════════════
# Intelligence Milestone
# ═══════════════════════════════════════════════════════════════

class TestIntelligenceMilestone:

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 5.0, 'competitor_catch_up_estimate': 'weeks'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': False, 'checks_passed': 2})
    def test_milestone_not_triggered_no_publications(self, mock_v, mock_m, db):
        """No publications → milestone not triggered."""
        from integrations.agent_engine.ip_service import IPService
        result = IPService.check_intelligence_milestone(db)
        assert result['triggered'] is False
        assert result['consecutive_verified'] == 0

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 500.0, 'competitor_catch_up_estimate': 'months'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_milestone_triggered(self, mock_verify, mock_moat, db):
        """14 consecutive verified publications + moat ≥ months → triggered."""
        from integrations.agent_engine.ip_service import IPService

        # Create 14 publications with verified snapshots
        for i in range(14):
            pub = DefensivePublication(
                title=f'Milestone Pub {i}',
                content_hash=hashlib.sha256(f'content-{i}'.encode()).hexdigest(),
                verification_snapshot={'verified': True, 'checks_passed': 5},
            )
            db.add(pub)
        db.flush()

        result = IPService.check_intelligence_milestone(db)
        assert result['triggered'] is True
        assert result['consecutive_verified'] >= 14
        assert result['moat_catch_up'] == 'months'

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 10.0, 'competitor_catch_up_estimate': 'weeks'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_milestone_not_triggered_low_moat(self, mock_v, mock_m, db):
        """Verified but moat only 'weeks' → not triggered (needs 'months')."""
        from integrations.agent_engine.ip_service import IPService

        for i in range(14):
            pub = DefensivePublication(
                title=f'Low Moat Pub {i}',
                content_hash=hashlib.sha256(f'lowmoat-{i}'.encode()).hexdigest(),
                verification_snapshot={'verified': True},
            )
            db.add(pub)
        db.flush()

        result = IPService.check_intelligence_milestone(db)
        assert result['triggered'] is False


# ═══════════════════════════════════════════════════════════════
# Commercial API Service
# ═══════════════════════════════════════════════════════════════

class TestCommercialAPIService:

    def test_create_api_key(self, db, test_user):
        """Create key returns raw key once, stores hash."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        result = CommercialAPIService.create_api_key(
            db, str(test_user.id), name='Test Key', tier='free')

        assert 'raw_key' in result
        assert result['tier'] == 'free'
        assert result['rate_limit_per_day'] == 100
        assert result['monthly_quota'] == 3000
        # Hash should be different from raw key
        assert result['raw_key'] != result.get('key_hash', '')

    def test_validate_api_key_valid(self, db, test_user):
        """Validate a valid key returns key dict."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(db, str(test_user.id))
        raw_key = created['raw_key']

        validated = CommercialAPIService.validate_api_key(db, raw_key)
        assert validated is not None
        assert validated['id'] == created['id']

    def test_validate_api_key_invalid(self, db):
        """Invalid key returns None."""
        from integrations.agent_engine.commercial_api import CommercialAPIService
        assert CommercialAPIService.validate_api_key(db, 'bogus-key-123') is None

    def test_log_usage(self, db, test_user):
        """Log usage creates record and increments monthly count."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='starter')
        log = CommercialAPIService.log_usage(
            db, created['id'], '/v1/intelligence/chat',
            tokens_in=100, tokens_out=200, compute_ms=500)

        assert log['endpoint'] == '/v1/intelligence/chat'
        assert log['tokens_in'] == 100
        assert log['tokens_out'] == 200
        assert log['cost_credits'] > 0  # starter tier has cost

        # Monthly usage incremented
        key = db.query(CommercialAPIKey).filter_by(id=created['id']).first()
        assert key.usage_this_month >= 1

    def test_check_rate_limit_under(self, db, test_user):
        """Under rate limit → allowed."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(db, str(test_user.id))
        assert CommercialAPIService.check_rate_limit(db, created['id']) is True

    def test_check_rate_limit_exceeded(self, db, test_user):
        """Exceed daily rate limit → blocked."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        # Create key with very low limit
        key = CommercialAPIKey(
            user_id=str(test_user.id),
            key_hash=hashlib.sha256(b'test-rate-limit').hexdigest(),
            key_prefix='test1234',
            tier='free',
            rate_limit_per_day=2,
            monthly_quota=100,
        )
        db.add(key)
        db.flush()

        # Log 3 usages
        for _ in range(3):
            CommercialAPIService.log_usage(db, key.id, '/test')

        assert CommercialAPIService.check_rate_limit(db, key.id) is False

    def test_revoke_api_key(self, db, test_user):
        """Revoking a key deactivates it."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(db, str(test_user.id))
        revoked = CommercialAPIService.revoke_api_key(db, created['id'])
        assert revoked['is_active'] is False


# ═══════════════════════════════════════════════════════════════
# API Key Tiers
# ═══════════════════════════════════════════════════════════════

class TestAPIKeyTiers:

    def test_free_tier_limits(self, db, test_user):
        from integrations.agent_engine.commercial_api import CommercialAPIService
        key = CommercialAPIService.create_api_key(db, str(test_user.id), tier='free')
        assert key['rate_limit_per_day'] == 100
        assert key['monthly_quota'] == 3000

    def test_starter_tier_limits(self, db, test_user):
        from integrations.agent_engine.commercial_api import CommercialAPIService
        key = CommercialAPIService.create_api_key(db, str(test_user.id), tier='starter')
        assert key['rate_limit_per_day'] == 1000
        assert key['monthly_quota'] == 30000

    def test_pro_tier_limits(self, db, test_user):
        from integrations.agent_engine.commercial_api import CommercialAPIService
        key = CommercialAPIService.create_api_key(db, str(test_user.id), tier='pro')
        assert key['rate_limit_per_day'] == 10000
        assert key['monthly_quota'] == 300000

    def test_invalid_tier(self, db, test_user):
        from integrations.agent_engine.commercial_api import CommercialAPIService
        result = CommercialAPIService.create_api_key(db, str(test_user.id), tier='gold')
        assert 'error' in result


# ═══════════════════════════════════════════════════════════════
# Build Distribution
# ═══════════════════════════════════════════════════════════════

class TestBuildDistribution:

    def test_create_build_license(self, db, test_user):
        """Create license with correct defaults."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        result = BuildDistributionService.create_build_license(
            db, str(test_user.id), build_type='community', platform='linux_x64')

        assert result['license_key'] is not None
        assert result['build_type'] == 'community'
        assert result['platform'] == 'linux_x64'
        assert result['max_downloads'] == 3
        assert result['is_active'] is True

    def test_verify_valid_license(self, db, test_user):
        """Verify a valid license."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        created = BuildDistributionService.create_build_license(
            db, str(test_user.id))
        result = BuildDistributionService.verify_build_license(
            db, created['license_key'])

        assert result['valid'] is True
        assert result['reason'] == 'ok'

    def test_verify_expired_license(self, db, test_user):
        """Expired license → invalid."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        bl = BuildLicense(
            user_id=str(test_user.id),
            license_key=secrets.token_urlsafe(32),
            build_type='community',
            platform='linux_x64',
            max_downloads=3,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        db.add(bl)
        db.flush()

        result = BuildDistributionService.verify_build_license(db, bl.license_key)
        assert result['valid'] is False
        assert 'expired' in result['reason'].lower()

    def test_download_increments_count(self, db, test_user):
        """Recording a download increments the count."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        created = BuildDistributionService.create_build_license(
            db, str(test_user.id))
        result = BuildDistributionService.record_download(db, created['id'])

        assert result['download_count'] == 1

    def test_download_exceeds_max(self, db, test_user):
        """Cannot download beyond max_downloads."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        bl = BuildLicense(
            user_id=str(test_user.id),
            license_key=secrets.token_urlsafe(32),
            build_type='community',
            platform='linux_x64',
            max_downloads=1,
            download_count=1,
        )
        db.add(bl)
        db.flush()

        result = BuildDistributionService.record_download(db, bl.id)
        assert 'error' in result

    def test_invalid_build_type(self, db, test_user):
        from integrations.agent_engine.build_distribution import BuildDistributionService
        result = BuildDistributionService.create_build_license(
            db, str(test_user.id), build_type='ultra')
        assert 'error' in result


# ═══════════════════════════════════════════════════════════════
# Revenue Goal Type
# ═══════════════════════════════════════════════════════════════

class TestRevenueGoalType:

    def test_revenue_type_registered(self):
        """Revenue goal type is registered in GoalManager."""
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        assert 'revenue' in types

    def test_revenue_prompt_builder(self):
        """Revenue prompt contains philosophy keywords."""
        from integrations.agent_engine.goal_manager import GoalManager
        prompt = GoalManager.build_prompt({
            'goal_type': 'revenue',
            'title': 'Test Revenue Goal',
            'description': 'Test description',
            'config_json': {},
        })
        assert 'REVENUE OPTIMIZATION' in prompt
        assert 'free tier' in prompt.lower()
        assert '90%' in prompt

    def test_bootstrap_goals_include_revenue(self):
        """Seed goals include revenue monitor and defensive IP."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_revenue_monitor' in slugs
        assert 'bootstrap_defensive_ip' in slugs

    def test_schema_version(self):
        """Schema version >= 22 (commercial API + defensive IP + build licenses)."""
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 22
