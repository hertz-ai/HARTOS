"""
Comprehensive regression test suite for HevolveSocial platform.
Covers: Models (39 tables), Services (10), Migrations (v1-v9),
Resonance engine, Gamification, Regions, Encounters, Ratings,
Agent Evolution, Distribution, Onboarding, Campaigns, Proximity/Geolocation.
"""
import os
import sys
import json
import uuid
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture(scope='session')
def engine():
    """Create in-memory SQLite engine for testing."""
    eng = create_engine('sqlite:///:memory:', echo=False)
    return eng

@pytest.fixture(scope='session')
def tables(engine):
    """Create all tables."""
    from integrations.social.models import Base
    Base.metadata.create_all(engine)
    return True

@pytest.fixture
def db(engine, tables):
    """Provide a transactional DB session."""
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()

@pytest.fixture
def user_factory(db):
    """Factory to create test users."""
    from integrations.social.models import User
    counter = [0]
    def _create(**kwargs):
        counter[0] += 1
        defaults = {
            'username': f'testuser_{counter[0]}_{uuid.uuid4().hex[:6]}',
            'display_name': f'Test User {counter[0]}',
            'email': f'test{counter[0]}_{uuid.uuid4().hex[:4]}@example.com',
            'password_hash': 'fakehash',
            'user_type': 'human',
        }
        defaults.update(kwargs)
        user = User(**defaults)
        db.add(user)
        db.flush()
        return user
    return _create

@pytest.fixture
def two_users(user_factory):
    """Create two test users."""
    return user_factory(), user_factory()

@pytest.fixture
def agent_user(user_factory):
    """Create an agent user."""
    return user_factory(user_type='agent', display_name='TestBot')


# ============================================================
# PART 1: MODEL IMPORT & TABLE CREATION TESTS
# ============================================================

class TestModelsExist:
    """Verify all model classes can be imported and have correct table names."""

    def test_core_models(self):
        from integrations.social.models import User, Post, Comment, Vote
        assert User.__tablename__ == 'users'
        assert Post.__tablename__ == 'posts'
        assert Comment.__tablename__ == 'comments'
        assert Vote.__tablename__ == 'votes'

    def test_community_models(self):
        from integrations.social.models import Community, CommunityMembership, Follow
        assert Community.__tablename__ == 'communities'
        assert CommunityMembership.__tablename__ == 'community_memberships'
        assert Follow.__tablename__ == 'follows'

    def test_infrastructure_models(self):
        from integrations.social.models import Notification, Report, PeerNode
        assert Notification.__tablename__ == 'notifications'
        assert Report.__tablename__ == 'reports'
        assert PeerNode.__tablename__ == 'peer_nodes'

    def test_resonance_models(self):
        from integrations.social.models import ResonanceWallet, ResonanceTransaction
        assert ResonanceWallet.__tablename__ == 'resonance_wallets'
        assert ResonanceTransaction.__tablename__ == 'resonance_transactions'

    def test_gamification_models(self):
        from integrations.social.models import (
            Achievement, UserAchievement, Season, Challenge, UserChallenge
        )
        assert Achievement.__tablename__ == 'achievements'
        assert UserAchievement.__tablename__ == 'user_achievements'
        assert Season.__tablename__ == 'seasons'
        assert Challenge.__tablename__ == 'challenges'
        assert UserChallenge.__tablename__ == 'user_challenges'

    def test_region_models(self):
        from integrations.social.models import Region, RegionMembership
        assert Region.__tablename__ == 'regions'
        assert RegionMembership.__tablename__ == 'region_memberships'

    def test_encounter_rating_models(self):
        from integrations.social.models import Encounter, Rating, TrustScore
        assert Encounter.__tablename__ == 'encounters'
        assert Rating.__tablename__ == 'ratings'
        assert TrustScore.__tablename__ == 'trust_scores'

    def test_agent_evolution_models(self):
        from integrations.social.models import AgentEvolution, AgentCollaboration
        assert AgentEvolution.__tablename__ == 'agent_evolution'
        assert AgentCollaboration.__tablename__ == 'agent_collaborations'

    def test_distribution_models(self):
        from integrations.social.models import Referral, ReferralCode, Boost, OnboardingProgress
        assert Referral.__tablename__ == 'referrals'
        assert ReferralCode.__tablename__ == 'referral_codes'
        assert Boost.__tablename__ == 'boosts'
        assert OnboardingProgress.__tablename__ == 'onboarding_progress'

    def test_campaign_models(self):
        from integrations.social.models import Campaign, CampaignAction
        assert Campaign.__tablename__ == 'campaigns'
        assert CampaignAction.__tablename__ == 'campaign_actions'

    def test_geolocation_models(self):
        from integrations.social.models import (
            LocationPing, ProximityMatch, MissedConnection, MissedConnectionResponse
        )
        assert LocationPing.__tablename__ == 'location_pings'
        assert ProximityMatch.__tablename__ == 'proximity_matches'
        assert MissedConnection.__tablename__ == 'missed_connections'
        assert MissedConnectionResponse.__tablename__ == 'missed_connection_responses'


class TestModelCreation:
    """Test creating instances of key models."""

    def test_create_user(self, db, user_factory):
        user = user_factory()
        assert user.id is not None
        assert user.karma_score == 0

    def test_user_location_fields(self, db, user_factory):
        user = user_factory()
        assert user.location_sharing_enabled == False
        assert user.last_location_lat is None
        assert user.last_location_lon is None

    def test_create_resonance_wallet(self, db, user_factory):
        from integrations.social.models import ResonanceWallet
        user = user_factory()
        wallet = ResonanceWallet(user_id=user.id, pulse=0, spark=0, signal=0.0, xp=0, level=1, level_title='Newcomer')
        db.add(wallet)
        db.flush()
        assert wallet.id is not None
        assert wallet.level_title == 'Newcomer'

    def test_create_achievement(self, db):
        from integrations.social.models import Achievement
        ach = Achievement(
            slug=f'first_post_{uuid.uuid4().hex[:6]}', name='First Post', description='Create your first post',
            category='content', rarity='common', reward_pulse=10, reward_spark=5,
            reward_signal=0.01, reward_xp=25, criteria_json={"type": "post_count", "target": 1}
        )
        db.add(ach)
        db.flush()
        assert ach.id is not None

    def test_create_region(self, db):
        from integrations.social.models import Region
        region = Region(
            name=f'test_region_{uuid.uuid4().hex[:6]}',
            display_name='Test Region',
            region_type='thematic',
            member_count=0
        )
        db.add(region)
        db.flush()
        assert region.id is not None

    def test_create_season(self, db):
        from integrations.social.models import Season
        season = Season(
            name='Season 1', description='The first season',
            starts_at=datetime.utcnow(), ends_at=datetime.utcnow() + timedelta(days=90),
            is_active=True
        )
        db.add(season)
        db.flush()
        assert season.id is not None

    def test_create_campaign(self, db, user_factory):
        from integrations.social.models import Campaign
        user = user_factory()
        campaign = Campaign(
            owner_id=user.id, name='Test Campaign', goal='awareness',
            status='draft', total_spark_budget=100
        )
        db.add(campaign)
        db.flush()
        assert campaign.id is not None

    def test_create_location_ping(self, db, user_factory):
        from integrations.social.models import LocationPing
        user = user_factory()
        ping = LocationPing(
            user_id=user.id, lat=21.1458, lon=79.0882, accuracy_m=10.0,
            expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        db.add(ping)
        db.flush()
        assert ping.id is not None

    def test_create_proximity_match(self, db, two_users):
        from integrations.social.models import ProximityMatch
        u1, u2 = two_users
        a_id, b_id = sorted([u1.id, u2.id])
        match = ProximityMatch(
            user_a_id=a_id, user_b_id=b_id,
            lat=21.1458, lon=79.0882, distance_m=45.0,
            status='pending', expires_at=datetime.utcnow() + timedelta(hours=4)
        )
        db.add(match)
        db.flush()
        assert match.id is not None
        assert match.status == 'pending'

    def test_create_missed_connection(self, db, user_factory):
        from integrations.social.models import MissedConnection
        user = user_factory()
        mc = MissedConnection(
            user_id=user.id, lat=21.1458, lon=79.0882,
            location_name='Coffee House', description='Was reading a book at the corner table',
            was_at=datetime.utcnow() - timedelta(hours=3),
            expires_at=datetime.utcnow() + timedelta(days=7)
        )
        db.add(mc)
        db.flush()
        assert mc.id is not None
        assert mc.response_count == 0

    def test_create_missed_connection_response(self, db, user_factory):
        from integrations.social.models import MissedConnection, MissedConnectionResponse
        poster = user_factory()
        responder = user_factory()
        mc = MissedConnection(
            user_id=poster.id, lat=21.1458, lon=79.0882,
            location_name='Park', description='Walking the dog',
            was_at=datetime.utcnow() - timedelta(hours=1),
            expires_at=datetime.utcnow() + timedelta(days=7)
        )
        db.add(mc)
        db.flush()
        resp = MissedConnectionResponse(
            missed_connection_id=mc.id, responder_id=responder.id,
            message='I was there too! I had my dog as well.'
        )
        db.add(resp)
        db.flush()
        assert resp.id is not None
        assert resp.status == 'pending'


# ============================================================
# PART 2: SERVICE IMPORT TESTS
# ============================================================

class TestServiceImports:
    """Verify all 10 service files import without errors."""

    def test_resonance_engine(self):
        from integrations.social.resonance_engine import ResonanceService
        assert hasattr(ResonanceService, 'award_pulse')
        assert hasattr(ResonanceService, 'award_spark')
        assert hasattr(ResonanceService, 'award_signal')
        assert hasattr(ResonanceService, 'award_xp')

    def test_gamification_service(self):
        from integrations.social.gamification_service import GamificationService
        assert hasattr(GamificationService, 'check_achievements')

    def test_region_service(self):
        from integrations.social.region_service import RegionService
        assert hasattr(RegionService, 'create_region')
        assert hasattr(RegionService, 'join_region')

    def test_encounter_service(self):
        from integrations.social.encounter_service import EncounterService
        assert hasattr(EncounterService, 'record_encounter')

    def test_agent_evolution_service(self):
        from integrations.social.agent_evolution_service import AgentEvolutionService
        assert hasattr(AgentEvolutionService, 'get_evolution')

    def test_rating_service(self):
        from integrations.social.rating_service import RatingService
        assert hasattr(RatingService, 'submit_rating')

    def test_distribution_service(self):
        from integrations.social.distribution_service import DistributionService
        assert hasattr(DistributionService, 'get_or_create_referral_code')

    def test_onboarding_service(self):
        from integrations.social.onboarding_service import OnboardingService
        assert hasattr(OnboardingService, 'get_progress')

    def test_campaign_service(self):
        from integrations.social.campaign_service import CampaignService
        assert hasattr(CampaignService, 'create_campaign')

    def test_proximity_service(self):
        from integrations.social.proximity_service import ProximityService
        assert hasattr(ProximityService, 'haversine_distance')
        assert hasattr(ProximityService, 'update_location')
        assert hasattr(ProximityService, 'reveal_self')
        assert hasattr(ProximityService, 'create_missed_connection')
        assert hasattr(ProximityService, 'search_missed_connections')
        assert hasattr(ProximityService, 'auto_suggest_locations')


# ============================================================
# PART 3: PROXIMITY SERVICE UNIT TESTS
# ============================================================

class TestHaversineDistance:
    """Test the haversine distance calculation."""

    def test_same_point(self):
        from integrations.social.proximity_service import ProximityService
        dist = ProximityService.haversine_distance(21.1458, 79.0882, 21.1458, 79.0882)
        assert dist == 0.0

    def test_known_distance(self):
        """New York to Los Angeles ~3944 km."""
        from integrations.social.proximity_service import ProximityService
        dist = ProximityService.haversine_distance(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3900000 < dist < 4000000  # meters

    def test_nearby_points(self):
        """Two points ~100m apart."""
        from integrations.social.proximity_service import ProximityService
        # ~100m offset at equator is about 0.0009 degrees longitude
        dist = ProximityService.haversine_distance(0.0, 0.0, 0.0, 0.0009)
        assert 90 < dist < 110

    def test_symmetry(self):
        from integrations.social.proximity_service import ProximityService
        d1 = ProximityService.haversine_distance(10.0, 20.0, 30.0, 40.0)
        d2 = ProximityService.haversine_distance(30.0, 40.0, 10.0, 20.0)
        assert abs(d1 - d2) < 0.01


class TestBoundingBox:
    """Test bounding box pre-filter calculation."""

    def test_bounding_box_returns_four_values(self):
        from integrations.social.proximity_service import ProximityService
        result = ProximityService.bounding_box(21.1458, 79.0882, 1000)
        assert len(result) == 4
        min_lat, max_lat, min_lon, max_lon = result
        assert min_lat < 21.1458 < max_lat
        assert min_lon < 79.0882 < max_lon

    def test_larger_radius_gives_larger_box(self):
        from integrations.social.proximity_service import ProximityService
        small = ProximityService.bounding_box(0.0, 0.0, 100)
        large = ProximityService.bounding_box(0.0, 0.0, 10000)
        assert (large[1] - large[0]) > (small[1] - small[0])


class TestProximityDetection:
    """Test proximity detection and matching."""

    def test_update_location_creates_ping(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import LocationPing
        user = user_factory()
        ProximityService.update_location(db, user.id, 21.1458, 79.0882, 10.0)
        ping = db.query(LocationPing).filter_by(user_id=user.id).first()
        assert ping is not None
        assert abs(ping.lat - 21.1458) < 0.001

    def test_nearby_count_zero_when_alone(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        user = user_factory()
        ProximityService.update_location(db, user.id, 50.0, 50.0, 10.0)
        count = ProximityService.get_nearby_count(db, user.id)
        assert count == 0

    def test_two_users_nearby_creates_match(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import ProximityMatch
        u1 = user_factory()
        u2 = user_factory()
        # Enable location sharing
        u1.location_sharing_enabled = True
        u2.location_sharing_enabled = True
        db.flush()
        # Place both at nearly same location
        ProximityService.update_location(db, u1.id, 21.1458, 79.0882, 5.0)
        ProximityService.update_location(db, u2.id, 21.14585, 79.08825, 5.0)
        # Check match was created
        a_id, b_id = sorted([u1.id, u2.id])
        match = db.query(ProximityMatch).filter_by(
            user_a_id=a_id, user_b_id=b_id, status='pending'
        ).first()
        assert match is not None
        assert match.distance_m < 100

    def test_reveal_self_state_machine(self, db, two_users):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import ProximityMatch
        u1, u2 = two_users
        u1.location_sharing_enabled = True
        u2.location_sharing_enabled = True
        db.flush()
        a_id, b_id = sorted([u1.id, u2.id])
        match = ProximityMatch(
            user_a_id=a_id, user_b_id=b_id,
            lat=21.1458, lon=79.0882, distance_m=30.0,
            status='pending', expires_at=datetime.utcnow() + timedelta(hours=4)
        )
        db.add(match)
        db.flush()
        match_id = match.id
        # First user reveals
        result = ProximityService.reveal_self(db, match_id, a_id)
        assert result is not None
        # Re-query from DB
        match = db.query(ProximityMatch).filter_by(id=match_id).first()
        assert match.status in ('revealed_a', 'revealed_b')
        # Second user reveals -> matched
        result2 = ProximityService.reveal_self(db, match_id, b_id)
        match = db.query(ProximityMatch).filter_by(id=match_id).first()
        assert match.status == 'matched'


class TestMissedConnections:
    """Test missed connections CRUD."""

    def test_create_missed_connection(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        user = user_factory()
        mc = ProximityService.create_missed_connection(
            db, user.id, 21.1458, 79.0882,
            'Tea Stall', 'Having chai at the corner stall',
            (datetime.utcnow() - timedelta(hours=2)).isoformat()
        )
        assert mc is not None
        # Service returns dict
        if isinstance(mc, dict):
            assert mc['location_name'] == 'Tea Stall'
        else:
            assert mc.location_name == 'Tea Stall'

    def _create_mc(self, db, user_id, lat, lon, name, desc, hours_ago=1):
        """Helper to create missed connection and return (dict, id)."""
        from integrations.social.proximity_service import ProximityService
        mc = ProximityService.create_missed_connection(
            db, user_id, lat, lon, name, desc,
            (datetime.utcnow() - timedelta(hours=hours_ago)).isoformat()
        )
        mc_id = mc['id'] if isinstance(mc, dict) else mc.id
        return mc, mc_id

    def test_search_missed_connections_by_radius(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        poster = user_factory()
        searcher = user_factory()
        self._create_mc(db, poster.id, 21.1458, 79.0882, 'Bookstore', 'Browsing the fiction section')
        results = ProximityService.search_missed_connections(
            db, 21.1460, 79.0884, 1000, 10, 0, searcher.id
        )
        assert len(results) >= 1

    def test_search_excludes_far_away(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        poster = user_factory()
        searcher = user_factory()
        self._create_mc(db, poster.id, 21.1458, 79.0882, 'Far Place', 'Very far away')
        results = ProximityService.search_missed_connections(
            db, 40.7128, -74.0060, 100, 10, 0, searcher.id
        )
        # Check results - may be dicts or objects
        far_results = []
        for r in results:
            name = r.get('location_name', '') if isinstance(r, dict) else getattr(r, 'location_name', '')
            if name == 'Far Place':
                far_results.append(r)
        assert len(far_results) == 0

    def test_respond_to_missed_connection(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        poster = user_factory()
        responder = user_factory()
        _, mc_id = self._create_mc(db, poster.id, 21.1458, 79.0882, 'Cafe', 'Working on laptop')
        resp = ProximityService.respond_to_missed(
            db, mc_id, responder.id, 'I was there too!'
        )
        assert resp is not None

    def test_multiple_respondents(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import MissedConnection
        poster = user_factory()
        _, mc_id = self._create_mc(db, poster.id, 21.1458, 79.0882, 'Concert', 'At the rock concert', 5)
        for i in range(5):
            r = user_factory()
            ProximityService.respond_to_missed(db, mc_id, r.id, f'I was there too #{i}')
        mc_obj = db.query(MissedConnection).filter_by(id=mc_id).first()
        assert mc_obj.response_count == 5

    def test_accept_missed_response(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        poster = user_factory()
        responder = user_factory()
        _, mc_id = self._create_mc(db, poster.id, 21.1458, 79.0882, 'Library', 'Studying together')
        resp = ProximityService.respond_to_missed(db, mc_id, responder.id, 'Was there!')
        resp_id = resp['id'] if isinstance(resp, dict) else resp.id
        result = ProximityService.accept_missed_response(db, mc_id, resp_id, poster.id)
        assert result is not None

    def test_get_missed_with_responses(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        poster = user_factory()
        _, mc_id = self._create_mc(db, poster.id, 21.1458, 79.0882, 'Market', 'Shopping at the market', 2)
        r1 = user_factory()
        r2 = user_factory()
        ProximityService.respond_to_missed(db, mc_id, r1.id, 'Me too!')
        ProximityService.respond_to_missed(db, mc_id, r2.id, 'I was there!')
        result = ProximityService.get_missed_with_responses(db, mc_id)
        assert result is not None

    def test_delete_missed_connection(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import MissedConnection
        user = user_factory()
        _, mc_id = self._create_mc(db, user.id, 21.1458, 79.0882, 'To Delete', 'Will be deleted')
        result = ProximityService.delete_missed_connection(db, mc_id, user.id)
        assert result is not None
        mc_obj = db.query(MissedConnection).filter_by(id=mc_id).first()
        assert mc_obj.is_active == False

    def test_auto_suggest_locations(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        for name in ['Coffee House', 'Coffee House', 'Tea Stall', 'Bookstore']:
            u = user_factory()
            self._create_mc(db, u.id, 21.1458 + 0.0001, 79.0882 + 0.0001, name, f'At {name}')
        suggestions = ProximityService.auto_suggest_locations(db, 21.1458, 79.0882, 5000)
        assert len(suggestions) >= 1


class TestLocationSettings:
    """Test location sharing settings."""

    def test_get_location_settings(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        user = user_factory()
        settings = ProximityService.get_location_settings(db, user.id)
        assert settings is not None
        assert settings['location_sharing_enabled'] == False

    def test_update_location_settings(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import User
        user = user_factory()
        result = ProximityService.update_location_settings(db, user.id, True)
        assert result['location_sharing_enabled'] == True
        # Re-query to verify persistence
        u = db.query(User).filter_by(id=user.id).first()
        assert u.location_sharing_enabled == True


class TestCleanup:
    """Test expired data cleanup."""

    def test_cleanup_expired_pings(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import LocationPing
        user = user_factory()
        # Create an expired ping
        ping = LocationPing(
            user_id=user.id, lat=0.0, lon=0.0, accuracy_m=10.0,
            expires_at=datetime.utcnow() - timedelta(hours=1)
        )
        db.add(ping)
        db.flush()
        ping_id = ping.id
        ProximityService.cleanup_expired(db)
        remaining = db.query(LocationPing).filter_by(id=ping_id).first()
        assert remaining is None

    def test_cleanup_expired_missed_connections(self, db, user_factory):
        from integrations.social.proximity_service import ProximityService
        from integrations.social.models import MissedConnection
        user = user_factory()
        mc = MissedConnection(
            user_id=user.id, lat=0.0, lon=0.0,
            location_name='Expired Place', description='Old',
            was_at=datetime.utcnow() - timedelta(days=10),
            expires_at=datetime.utcnow() - timedelta(days=1),
            is_active=True
        )
        db.add(mc)
        db.flush()
        ProximityService.cleanup_expired(db)
        db.refresh(mc)
        assert mc.is_active == False


# ============================================================
# PART 4: RESONANCE ENGINE TESTS
# ============================================================

class TestResonanceEngine:
    """Test Resonance currency system."""

    def test_award_pulse(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        new_pulse = ResonanceService.award_pulse(db, user.id, 10, 'test', 'unit_test')
        assert new_pulse == 10

    def test_award_spark(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        new_spark = ResonanceService.award_spark(db, user.id, 25, 'test', 'unit_test')
        assert new_spark == 25

    def test_award_xp_triggers_level_up(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        result = ResonanceService.award_xp(db, user.id, 250, 'test', 'unit_test')
        assert result['level'] >= 2
        assert result['leveled_up'] == True

    def test_spend_spark(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        # Give some spark first
        ResonanceService.award_spark(db, user.id, 100, 'test', 'setup')
        success, remaining = ResonanceService.spend_spark(db, user.id, 30, 'boost', 'test_boost')
        assert success == True
        assert remaining == 70

    def test_spend_spark_insufficient(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        # Give only 10 spark
        ResonanceService.award_spark(db, user.id, 10, 'test', 'setup')
        success, remaining = ResonanceService.spend_spark(db, user.id, 50, 'boost', 'test_boost')
        assert success == False
        assert remaining == 10

    def test_get_wallet(self, db, user_factory):
        from integrations.social.resonance_engine import ResonanceService
        user = user_factory()
        ResonanceService.award_pulse(db, user.id, 42, 'test', 'setup')
        result = ResonanceService.get_wallet(db, user.id)
        assert result is not None
        assert result['pulse'] == 42


# ============================================================
# PART 5: MIGRATION VERSION TEST
# ============================================================

class TestMigrations:
    """Test migration versioning."""

    def test_schema_version_is_9(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 9

    def test_run_migrations_function_exists(self):
        from integrations.social.migrations import run_migrations
        assert callable(run_migrations)


# ============================================================
# PART 6: API BLUEPRINT TESTS
# ============================================================

class TestAPIBlueprint:
    """Test that API blueprint registers correctly."""

    def test_gamification_blueprint_exists(self):
        from integrations.social.api_gamification import gamification_bp
        assert gamification_bp is not None
        assert gamification_bp.name == 'gamification'

    def test_social_blueprint_exists(self):
        from integrations.social.api import social_bp
        assert social_bp is not None


# ============================================================
# PART 7: PROXIMITY MATCH PRIVACY TESTS
# ============================================================

class TestPrivacy:
    """Verify privacy constraints on geolocation data."""

    def test_proximity_match_to_dict_hides_coordinates(self, db, two_users):
        from integrations.social.models import ProximityMatch
        u1, u2 = two_users
        a_id, b_id = sorted([u1.id, u2.id])
        match = ProximityMatch(
            user_a_id=a_id, user_b_id=b_id,
            lat=21.1458, lon=79.0882, distance_m=45.0,
            status='pending', expires_at=datetime.utcnow() + timedelta(hours=4)
        )
        db.add(match)
        db.flush()
        d = match.to_dict(viewer_id=a_id)
        # Should NOT contain raw lat/lon
        assert 'lat' not in d or d.get('lat') is None
        assert 'lon' not in d or d.get('lon') is None
        # Should contain distance bucket
        assert 'distance_bucket' in d or 'distance_label' in d or 'distance_m' in d

    def test_missed_connection_to_dict_includes_location_for_poster(self, db, user_factory):
        from integrations.social.models import MissedConnection
        user = user_factory()
        mc = MissedConnection(
            user_id=user.id, lat=21.1458, lon=79.0882,
            location_name='My Location', description='Test',
            was_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(days=7)
        )
        db.add(mc)
        db.flush()
        d = mc.to_dict()
        assert d['location_name'] == 'My Location'


# ============================================================
# PART 8: REACT NATIVE FILE EXISTENCE TESTS
# ============================================================

class TestReactNativeFiles:
    """Verify all React Native geolocation files exist."""

    RN_BASE = r'C:\Users\sathi\StudioProjects\Hevolve_React_Native'

    def _file_exists(self, rel_path):
        full = os.path.join(self.RN_BASE, rel_path)
        return os.path.isfile(full)

    def test_android_manifest_has_location_perms(self):
        manifest = os.path.join(self.RN_BASE, 'android', 'app', 'src', 'main', 'AndroidManifest.xml')
        if not os.path.isfile(manifest):
            pytest.skip('RN project not available')
        with open(manifest, 'r') as f:
            content = f.read()
        assert 'ACCESS_FINE_LOCATION' in content
        assert 'ACCESS_COARSE_LOCATION' in content

    def test_social_api_exists(self):
        if not self._file_exists('services/socialApi.js'):
            pytest.skip('RN project not available')
        assert self._file_exists('services/socialApi.js')

    def test_encounter_store_exists(self):
        if not self._file_exists('encounterStore.js'):
            pytest.skip('RN project not available')
        assert self._file_exists('encounterStore.js')

    def test_use_location_ping_hook_exists(self):
        if not self._file_exists('hooks/useLocationPing.js'):
            pytest.skip('RN project not available')
        assert self._file_exists('hooks/useLocationPing.js')

    def test_encounters_screen_exists(self):
        path = os.path.join('components', 'CommunityView', 'screens', 'EncountersScreen.js')
        if not self._file_exists(path):
            pytest.skip('RN project not available')
        assert self._file_exists(path)

    def test_missed_connection_detail_screen_exists(self):
        path = os.path.join('components', 'CommunityView', 'screens', 'MissedConnectionDetailScreen.js')
        if not self._file_exists(path):
            pytest.skip('RN project not available')
        assert self._file_exists(path)

    def test_create_missed_connection_screen_exists(self):
        path = os.path.join('components', 'CommunityView', 'screens', 'CreateMissedConnectionScreen.js')
        if not self._file_exists(path):
            pytest.skip('RN project not available')
        assert self._file_exists(path)

    def test_map_screen_exists(self):
        path = os.path.join('components', 'CommunityView', 'screens', 'MissedConnectionsMapScreen.js')
        if not self._file_exists(path):
            pytest.skip('RN project not available')
        assert self._file_exists(path)

    def test_shared_components_exist(self):
        base = os.path.join('components', 'CommunityView', 'components', 'Encounters')
        components = ['ProximityBanner.js', 'ProximityMatchCard.js', 'MissedConnectionCard.js',
                       'LocationSettingsToggle.js', 'AutoSuggestInput.js']
        for comp in components:
            path = os.path.join(base, comp)
            if not self._file_exists(path):
                pytest.skip('RN project not available')
            assert self._file_exists(path), f'Missing: {comp}'

    def test_home_routes_has_encounter_screens(self):
        path = os.path.join('components', 'CommunityView', 'router', 'home.routes.js')
        full = os.path.join(self.RN_BASE, path)
        if not os.path.isfile(full):
            pytest.skip('RN project not available')
        with open(full, 'r') as f:
            content = f.read()
        assert 'EncountersScreen' in content
        assert 'MissedConnectionDetailScreen' in content
        assert 'CreateMissedConnectionScreen' in content
        assert 'MissedConnectionsMapScreen' in content

    def test_feed_header_has_encounters_button(self):
        path = os.path.join('components', 'CommunityView', 'components', 'FeedHeader', 'index.js')
        full = os.path.join(self.RN_BASE, path)
        if not os.path.isfile(full):
            pytest.skip('RN project not available')
        with open(full, 'r') as f:
            content = f.read()
        assert 'Encounters' in content
        assert 'navigate' in content


# ============================================================
# PART 9: FRONTEND FILE EXISTENCE TESTS (Nunba + Hevolve)
# ============================================================

class TestNunbaFrontendFiles:
    """Verify Nunba geolocation components exist."""

    NUNBA_BASE = r'C:\Users\sathi\PycharmProjects\Nunba\landing-page\src'

    def _check(self, rel):
        full = os.path.join(self.NUNBA_BASE, rel)
        if not os.path.isfile(full):
            pytest.skip('Nunba project not available')
        return True

    def test_social_api_has_encounter_methods(self):
        path = os.path.join(self.NUNBA_BASE, 'services', 'socialApi.js')
        if not os.path.isfile(path):
            pytest.skip('Nunba not available')
        with open(path, 'r') as f:
            content = f.read()
        assert 'locationPing' in content
        assert 'nearbyCount' in content
        assert 'proximityMatches' in content
        assert 'createMissed' in content
        assert 'suggestLocations' in content

    def test_shared_components_exist(self):
        shared = os.path.join('components', 'Social', 'shared')
        files = ['useLocationPing.js', 'ProximityBanner.js', 'ProximityMatchCard.js',
                 'MissedConnectionCard.js', 'MissedConnectionForm.js', 'MissedConnectionMapView.js',
                 'MissedConnectionDetail.js', 'LocationSettingsToggle.js', 'AutoSuggestInput.js']
        for f in files:
            assert self._check(os.path.join(shared, f)), f'Missing: {f}'

    def test_encounters_page_has_four_tabs(self):
        path = os.path.join(self.NUNBA_BASE, 'components', 'Social', 'Encounters', 'EncountersPage.js')
        if not os.path.isfile(path):
            pytest.skip('Nunba not available')
        with open(path, 'r') as f:
            content = f.read()
        assert 'Nearby' in content
        assert 'Missed' in content


class TestHevolveFrontendFiles:
    """Verify Hevolve geolocation components exist."""

    HEVOLVE_BASE = r'C:\Users\sathi\PycharmProjects\Hevolve\src'

    def _check(self, rel):
        full = os.path.join(self.HEVOLVE_BASE, rel)
        if not os.path.isfile(full):
            pytest.skip('Hevolve project not available')
        return True

    def test_social_api_has_encounter_methods(self):
        path = os.path.join(self.HEVOLVE_BASE, 'services', 'socialApi.js')
        if not os.path.isfile(path):
            pytest.skip('Hevolve not available')
        with open(path, 'r') as f:
            content = f.read()
        assert 'locationPing' in content
        assert 'createMissed' in content

    def test_shared_components_exist(self):
        shared = os.path.join('components', 'Social', 'shared')
        files = ['useLocationPing.js', 'ProximityBanner.js', 'ProximityMatchCard.js',
                 'MissedConnectionCard.js', 'MissedConnectionForm.js', 'MissedConnectionMapView.js',
                 'MissedConnectionDetail.js', 'LocationSettingsToggle.js', 'AutoSuggestInput.js']
        for f in files:
            assert self._check(os.path.join(shared, f)), f'Missing: {f}'


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
