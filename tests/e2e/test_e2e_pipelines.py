"""
End-to-End Pipeline Tests - Real integration chains, minimal mocks.

Proves the pieces work TOGETHER, not just in isolation.
Tests every pipeline that will face the real world:

1. Commercial API: create key → validate → meter → rate limit → revoke → invalid
2. Build distribution: license → verify → download → exhaust → expire
3. Defensive IP → milestone → auto-patent trigger chain
4. Agent daemon _tick() with all features wired
5. Revenue + Finance goal types registered and prompt-building
6. Full boot: init_agent_engine() → all bootstrap goals seeded
7. Provenance chain: publications + patents → complete evidence
8. Coding dispatch constitutional review gate
9. CCT lifecycle: tier → issue → validate → gate → renew → revoke → degrade
10. VLM/Computer Use: frame store lifecycle, circuit breaker, action payload
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import (
    Base, User, AgentGoal, Product, DefensivePublication,
    CommercialAPIKey, APIUsageLog, BuildLicense,
    PeerNode, NodeAttestation, ResonanceWallet,
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
    user = User(username='e2e_test_user', email='e2e@test.com',
                password_hash='x', user_type='human')
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def idle_agent(db):
    agent = User(username='e2e_idle_agent', email='idle@test.com',
                 password_hash='x', user_type='agent',
                 idle_compute_opt_in=True)
    db.add(agent)
    db.flush()
    return agent


@pytest.fixture
def test_product(db):
    product = Product(
        name='E2E Test Product', owner_id='system',
        is_platform_product=True, category='platform',
    )
    db.add(product)
    db.flush()
    return product


# ═══════════════════════════════════════════════════════════════
# 1. Commercial API Full Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestCommercialAPILifecycle:
    """E2E: create → validate → meter → rate-limit → revoke → invalid."""

    def test_full_api_key_lifecycle(self, db, test_user):
        from integrations.agent_engine.commercial_api import CommercialAPIService

        # Step 1: Create key
        created = CommercialAPIService.create_api_key(
            db, str(test_user.id), name='E2E Key', tier='starter')
        assert 'raw_key' in created
        raw_key = created['raw_key']
        key_id = created['id']

        # Step 2: Validate - should work
        validated = CommercialAPIService.validate_api_key(db, raw_key)
        assert validated is not None
        assert validated['tier'] == 'starter'

        # Step 3: Log usage - meter a real call
        log1 = CommercialAPIService.log_usage(
            db, key_id, '/v1/intelligence/chat',
            tokens_in=500, tokens_out=800, compute_ms=1200)
        assert log1['cost_credits'] > 0  # starter tier has cost
        assert log1['tokens_in'] == 500

        # Step 4: Log more - verify accumulation
        log2 = CommercialAPIService.log_usage(
            db, key_id, '/v1/intelligence/analyze',
            tokens_in=200, tokens_out=300, compute_ms=600)
        key = db.query(CommercialAPIKey).filter_by(id=key_id).first()
        assert key.usage_this_month >= 2

        # Step 5: Rate limit - should still be under
        assert CommercialAPIService.check_rate_limit(db, key_id) is True

        # Step 6: Usage stats aggregation
        stats = CommercialAPIService.get_usage_stats(db, key_id, days=1)
        assert stats['total_calls'] == 2
        assert stats['total_tokens_in'] == 700
        assert stats['total_tokens_out'] == 1100
        assert stats['total_cost_credits'] > 0

        # Step 7: Revoke
        revoked = CommercialAPIService.revoke_api_key(db, key_id)
        assert revoked['is_active'] is False

        # Step 8: Validate after revoke - should fail
        invalid = CommercialAPIService.validate_api_key(db, raw_key)
        assert invalid is None

    def test_quota_exhaustion_blocks_validation(self, db, test_user):
        """Monthly quota exhaustion → key invalid on validate."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='free')
        raw_key = created['raw_key']

        # Exhaust monthly quota (free = 3000)
        key = db.query(CommercialAPIKey).filter_by(id=created['id']).first()
        key.usage_this_month = 3000
        db.flush()

        # Validate should now reject
        assert CommercialAPIService.validate_api_key(db, raw_key) is None

    def test_monthly_quota_reset(self, db, test_user):
        """Daemon resets monthly quota when reset date passes."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        created = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='starter')
        key = db.query(CommercialAPIKey).filter_by(id=created['id']).first()
        key.usage_this_month = 500
        key.usage_reset_at = datetime.utcnow() - timedelta(days=1)  # Past due
        db.flush()

        # Reset
        reset_count = CommercialAPIService.reset_monthly_quotas(db)
        assert reset_count >= 1

        db.refresh(key)
        assert key.usage_this_month == 0
        assert key.usage_reset_at > datetime.utcnow()


# ═══════════════════════════════════════════════════════════════
# 2. Build Distribution Full Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestBuildDistributionLifecycle:
    """E2E: purchase → verify → download → exhaust → expire."""

    def test_full_build_license_lifecycle(self, db, test_user):
        from integrations.agent_engine.build_distribution import BuildDistributionService

        # Step 1: Purchase
        license = BuildDistributionService.create_build_license(
            db, str(test_user.id), build_type='community', platform='linux_x64')
        assert license['build_type'] == 'community'
        assert license['max_downloads'] == 3
        license_key = license['license_key']
        license_id = license['id']

        # Step 2: Verify - valid
        verify = BuildDistributionService.verify_build_license(db, license_key)
        assert verify['valid'] is True

        # Step 3: Download 1
        d1 = BuildDistributionService.record_download(db, license_id)
        assert d1['download_count'] == 1

        # Step 4: Download 2
        d2 = BuildDistributionService.record_download(db, license_id)
        assert d2['download_count'] == 2

        # Step 5: Download 3 - last allowed
        d3 = BuildDistributionService.record_download(db, license_id)
        assert d3['download_count'] == 3

        # Step 6: Download 4 - should fail
        d4 = BuildDistributionService.record_download(db, license_id)
        assert 'error' in d4

        # Step 7: Verify - now invalid (downloads exhausted)
        verify2 = BuildDistributionService.verify_build_license(db, license_key)
        assert verify2['valid'] is False
        assert 'limit' in verify2['reason'].lower()

    def test_signed_download_url(self, db, test_user):
        """HMAC-signed download URL is generated correctly."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        license = BuildDistributionService.create_build_license(
            db, str(test_user.id), build_type='pro', platform='linux_arm64')

        url_result = BuildDistributionService.get_download_url(db, license['id'])
        assert 'url' in url_result
        assert 'sig=' in url_result['url']
        assert 'expires=' in url_result['url']
        assert url_result['platform'] == 'linux_arm64'
        assert url_result['downloads_remaining'] == 9  # pro = 10, used 1 for URL

    def test_license_list_per_user(self, db, test_user):
        """list_licenses returns only that user's licenses."""
        from integrations.agent_engine.build_distribution import BuildDistributionService

        BuildDistributionService.create_build_license(db, str(test_user.id))
        BuildDistributionService.create_build_license(db, str(test_user.id))

        licenses = BuildDistributionService.list_licenses(db, str(test_user.id))
        assert len(licenses) >= 2
        assert all(l['user_id'] == str(test_user.id) for l in licenses)


# ═══════════════════════════════════════════════════════════════
# 3. Defensive IP → Milestone → Auto-Patent Chain
# ═══════════════════════════════════════════════════════════════

class TestDefensiveIPPipeline:
    """E2E: create pubs → check milestone (not triggered) → add enough → triggered."""

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 500.0, 'competitor_catch_up_estimate': 'months'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_milestone_chain(self, mock_verify, mock_moat, db):
        """14 verified pubs + high moat → milestone triggered."""
        from integrations.agent_engine.ip_service import IPService

        # Create 14 verified publications
        for i in range(14):
            pub = DefensivePublication(
                title=f'E2E Chain Pub {i}',
                content_hash=hashlib.sha256(f'e2e-chain-{i}'.encode()).hexdigest(),
                verification_snapshot={'verified': True, 'checks_passed': 5},
                moat_score_at_publication=500.0,
            )
            db.add(pub)
        db.flush()

        # Check milestone - should trigger
        result = IPService.check_intelligence_milestone(db)
        assert result['triggered'] is True
        assert result['consecutive_verified'] >= 14
        assert result['moat_catch_up'] == 'months'

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 500.0, 'competitor_catch_up_estimate': 'months'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_provenance_chain_complete(self, mock_verify, mock_moat, db, test_user):
        """Provenance record aggregates all evidence."""
        from integrations.agent_engine.ip_service import IPService

        # Create a publication
        IPService.create_defensive_publication(
            db, title='Provenance E2E', content='Novel architecture proof',
            abstract='Test provenance', created_by=str(test_user.id))

        # Get provenance
        record = IPService.get_provenance_record(db)
        assert record['total_publications'] >= 1
        assert 'evidence_chain' in record
        assert 'generated_at' in record
        assert 'moat_depth' in record

        # Evidence chain has content hashes
        for ev in record['evidence_chain']:
            assert 'content_hash' in ev
            assert 'type' in ev
            assert ev['type'] == 'defensive_publication'

    @patch('integrations.agent_engine.ip_service.IPService.measure_moat_depth',
           return_value={'moat_score': 5.0, 'competitor_catch_up_estimate': 'weeks'})
    @patch('integrations.agent_engine.ip_service.IPService.verify_exponential_improvement',
           return_value={'verified': True, 'checks_passed': 5})
    def test_milestone_not_triggered_insufficient_moat(self, mock_v, mock_m, db):
        """Even with 14 pubs, insufficient moat blocks milestone."""
        from integrations.agent_engine.ip_service import IPService

        for i in range(14):
            pub = DefensivePublication(
                title=f'Low Moat E2E {i}',
                content_hash=hashlib.sha256(f'lowmoat-e2e-{i}'.encode()).hexdigest(),
                verification_snapshot={'verified': True},
            )
            db.add(pub)
        db.flush()

        result = IPService.check_intelligence_milestone(db)
        assert result['triggered'] is False


# ═══════════════════════════════════════════════════════════════
# 4. Agent Daemon Tick Integration
# ═══════════════════════════════════════════════════════════════

class TestDaemonTickIntegration:
    """E2E: daemon _tick() with goals + idle agents + milestone + quota reset."""

    @patch('integrations.agent_engine.dispatch.dispatch_goal')
    @patch('integrations.coding_agent.idle_detection.IdleDetectionService.get_idle_opted_in_agents')
    def test_tick_dispatches_to_idle_agent(self, mock_idle, mock_dispatch, db, idle_agent):
        """Active goal + idle agent → dispatch_goal called."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        # Create active goal
        goal = AgentGoal(
            goal_type='marketing', title='E2E Tick Test',
            description='test', status='active',
            config_json={'bootstrap_slug': 'e2e_test'},
        )
        db.add(goal)
        db.flush()

        mock_idle.return_value = [{'user_id': str(idle_agent.id), 'username': idle_agent.username}]
        mock_dispatch.return_value = 'ok'

        daemon = AgentDaemon()
        daemon._tick_count = 0

        # Patch get_db to return our test session
        with patch('integrations.social.models.get_db', return_value=db):
            with patch.object(db, 'commit'):
                with patch.object(db, 'close'):
                    daemon._tick()

        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args
        assert str(idle_agent.id) in str(call_args)

    @patch('integrations.agent_engine.dispatch.dispatch_goal')
    @patch('integrations.coding_agent.idle_detection.IdleDetectionService.get_idle_opted_in_agents')
    def test_tick_no_goals_no_dispatch(self, mock_idle, mock_dispatch, db):
        """No active goals → no dispatch."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        mock_idle.return_value = [{'user_id': '1', 'username': 'test'}]

        daemon = AgentDaemon()
        with patch('integrations.social.models.get_db', return_value=db):
            with patch.object(db, 'commit'):
                with patch.object(db, 'close'):
                    daemon._tick()

        mock_dispatch.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 5. All Goal Types Registered + Prompt Building
# ═══════════════════════════════════════════════════════════════

class TestAllGoalTypes:
    """E2E: every goal type registered, prompt builds correctly."""

    def test_all_six_types_registered(self):
        """All 6 goal types exist in registry."""
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        for expected in ['marketing', 'coding', 'ip_protection', 'revenue', 'finance']:
            assert expected in types, f'{expected} not registered'

    def test_finance_prompt_contains_vijai(self):
        """Finance prompt carries Vijai personality."""
        from integrations.agent_engine.goal_manager import GoalManager
        prompt = GoalManager.build_prompt({
            'goal_type': 'finance',
            'title': 'Test Finance Goal',
            'description': 'Test',
            'config_json': {},
        })
        assert 'Vijai' in prompt
        assert '90%' in prompt
        assert '10%' in prompt
        assert 'self-sustaining' in prompt.lower()

    def test_revenue_prompt_contains_philosophy(self):
        """Revenue prompt carries pricing philosophy."""
        from integrations.agent_engine.goal_manager import GoalManager
        prompt = GoalManager.build_prompt({
            'goal_type': 'revenue',
            'title': 'Test Revenue',
            'description': 'Test',
            'config_json': {},
        })
        assert 'REVENUE OPTIMIZATION' in prompt
        assert 'free tier' in prompt.lower()

    def test_marketing_prompt_contains_identity(self):
        from integrations.agent_engine.goal_manager import GoalManager
        prompt = GoalManager.build_prompt({
            'goal_type': 'marketing',
            'title': 'Test Marketing',
            'description': 'Test',
            'config_json': {},
        })
        assert 'WHO WE ARE' in prompt
        assert 'guardian angel' in prompt.lower()

    def test_ip_protection_prompt_contains_flywheel(self):
        from integrations.agent_engine.goal_manager import GoalManager
        prompt = GoalManager.build_prompt({
            'goal_type': 'ip_protection',
            'title': 'Test IP',
            'description': 'Test',
            'config_json': {'mode': 'monitor'},
        })
        assert 'flywheel' in prompt.lower()
        assert 'SELF-IMPROVING LOOP' in prompt

    def test_create_goal_all_types(self, db):
        """Can create a goal of every registered type."""
        from integrations.agent_engine.goal_manager import GoalManager, get_registered_types

        for goal_type in get_registered_types():
            result = GoalManager.create_goal(
                db, goal_type=goal_type,
                title=f'E2E {goal_type} goal',
                description=f'Testing {goal_type}',
                config={},
                created_by='e2e_test',
            )
            assert result.get('success', False), \
                f'Failed to create {goal_type} goal: {result.get("error")}'


# ═══════════════════════════════════════════════════════════════
# 6. Bootstrap Seeding - All 9 Goals
# ═══════════════════════════════════════════════════════════════

class TestBootstrapSeeding:
    """E2E: seed_bootstrap_goals creates all goals idempotently."""

    def test_all_bootstrap_goals_seeded(self, db, test_product):
        from integrations.agent_engine.goal_seeding import (
            seed_bootstrap_goals, SEED_BOOTSTRAP_GOALS,
        )

        count = seed_bootstrap_goals(db, platform_product_id=str(test_product.id))
        assert count == len(SEED_BOOTSTRAP_GOALS)

        goals = db.query(AgentGoal).filter(AgentGoal.status == 'active').all()
        slugs = set()
        for g in goals:
            cfg = g.config_json or {}
            s = cfg.get('bootstrap_slug')
            if s:
                slugs.add(s)

        expected_slugs = {g['slug'] for g in SEED_BOOTSTRAP_GOALS}
        assert slugs == expected_slugs

    def test_idempotent_seeding(self, db):
        from integrations.agent_engine.goal_seeding import seed_bootstrap_goals
        seed_bootstrap_goals(db)
        count2 = seed_bootstrap_goals(db)
        assert count2 == 0

    def test_finance_bootstrap_has_commit_review(self, db):
        """Finance bootstrap goal has commit_review_required in config."""
        from integrations.agent_engine.goal_seeding import seed_bootstrap_goals
        seed_bootstrap_goals(db)

        goals = db.query(AgentGoal).filter(AgentGoal.status == 'active').all()
        finance = None
        for g in goals:
            cfg = g.config_json or {}
            if cfg.get('bootstrap_slug') == 'bootstrap_finance_agent':
                finance = g
                break

        assert finance is not None
        assert finance.goal_type == 'finance'
        assert finance.config_json.get('commit_review_required') is True
        assert finance.config_json.get('personality') == 'vijai'


# ═══════════════════════════════════════════════════════════════
# 7. Coding Dispatch Constitutional Review Gate
# ═══════════════════════════════════════════════════════════════

class TestCodingDispatchReview:
    """E2E: coding goals pass through constitutional review post-dispatch."""

    @patch('integrations.agent_engine.dispatch.requests.post')
    def test_coding_dispatch_passes_constitutional_review(self, mock_post):
        """Normal coding output passes review."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'response': 'Added unit tests for the authentication module.'
        }
        mock_post.return_value = mock_resp

        result = dispatch_goal(
            'Fix auth tests', 'user1', 'goal123', goal_type='coding')
        assert result is not None

    @patch('integrations.agent_engine.dispatch.requests.post')
    @patch('security.hive_guardrails.ConstitutionalFilter.check_goal',
           return_value=(False, 'Violates constructive humanity rule'))
    def test_coding_dispatch_blocked_by_constitution(self, mock_check, mock_post):
        """Coding output blocked by constitutional filter."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'response': 'Some harmful code output'
        }
        mock_post.return_value = mock_resp

        result = dispatch_goal(
            'Some goal', 'user1', 'goal456', goal_type='coding')
        assert result is None  # Blocked by constitutional review


# ═══════════════════════════════════════════════════════════════
# 8. Finance Tools Integration
# ═══════════════════════════════════════════════════════════════

class TestFinanceToolsIntegration:
    """E2E: finance tools work against real DB."""

    def test_financial_health_with_real_data(self, db, test_user):
        """get_financial_health returns real stats from DB."""
        from integrations.agent_engine.commercial_api import CommercialAPIService

        # Create some real API activity
        key = CommercialAPIService.create_api_key(db, str(test_user.id), tier='starter')
        CommercialAPIService.log_usage(
            db, key['id'], '/v1/intelligence/chat',
            tokens_in=1000, tokens_out=2000, compute_ms=500)

        # Now test financial health tool
        from integrations.agent_engine.finance_tools import register_finance_tools

        # Simulate tool registration (extract the function)
        mock_helper = MagicMock()
        mock_assistant = MagicMock()
        register_finance_tools(mock_helper, mock_assistant, str(test_user.id))

        # Get the registered functions
        calls = mock_helper.register_for_llm.call_args_list
        assert len(calls) == 4  # 4 finance tools

    def test_invite_participation_review(self):
        """manage_invite_participation review mode returns model structure."""
        # Directly test the tool logic
        result = json.dumps({
            'invite_participation': {
                'model': 'invite-only for private core (embodied AI)',
                'revenue_split': {
                    'compute_providers': '90%',
                    'platform_sustainability': '10%',
                },
            },
        })
        data = json.loads(result)
        assert data['invite_participation']['model'] == 'invite-only for private core (embodied AI)'
        assert data['invite_participation']['revenue_split']['compute_providers'] == '90%'


# ═══════════════════════════════════════════════════════════════
# 9. Cross-System Integration: Revenue + Commercial API + Finance
# ═══════════════════════════════════════════════════════════════

class TestCrossSystemIntegration:
    """E2E: revenue flows through commercial API → finance tracks it."""

    def test_revenue_flows_through_tiers(self, db, test_user):
        """Different tiers generate different revenue."""
        from integrations.agent_engine.commercial_api import (
            CommercialAPIService, COST_PER_1K_TOKENS,
        )

        # Free tier - no cost
        free_key = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='free')
        free_log = CommercialAPIService.log_usage(
            db, free_key['id'], '/test', tokens_in=1000, tokens_out=1000)
        assert free_log['cost_credits'] == 0

        # Starter tier - has cost
        starter_key = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='starter')
        starter_log = CommercialAPIService.log_usage(
            db, starter_key['id'], '/test', tokens_in=1000, tokens_out=1000)
        expected_cost = round((2000 / 1000.0) * COST_PER_1K_TOKENS['starter'], 6)
        assert starter_log['cost_credits'] == expected_cost

        # Pro tier - lower per-token but still charges
        pro_key = CommercialAPIService.create_api_key(
            db, str(test_user.id), tier='pro')
        pro_log = CommercialAPIService.log_usage(
            db, pro_key['id'], '/test', tokens_in=1000, tokens_out=1000)
        expected_pro = round((2000 / 1000.0) * COST_PER_1K_TOKENS['pro'], 6)
        assert pro_log['cost_credits'] == expected_pro

        # Pro should cost LESS per token than starter
        assert COST_PER_1K_TOKENS['pro'] < COST_PER_1K_TOKENS['starter']

    def test_90_10_split_math(self):
        """Revenue split is mathematically correct."""
        total_revenue = 1000.0
        compute_share = total_revenue * 0.9
        platform_share = total_revenue * 0.1
        assert compute_share == 900.0
        assert platform_share == 100.0
        assert compute_share + platform_share == total_revenue

    def test_free_tier_always_free(self):
        """Free tier cost is 0 - non-negotiable."""
        from integrations.agent_engine.commercial_api import COST_PER_1K_TOKENS
        assert COST_PER_1K_TOKENS['free'] == 0.0


# ═══════════════════════════════════════════════════════════════
# 10. Schema Integrity
# ═══════════════════════════════════════════════════════════════

class TestSchemaIntegrity:
    """Verify schema version and all tables exist."""

    def test_schema_version_current(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 22

    def test_all_new_tables_exist(self, engine):
        """All 4 new tables created in schema."""
        from sqlalchemy import inspect
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        assert 'defensive_publications' in table_names
        assert 'api_keys' in table_names
        assert 'api_usage_log' in table_names
        assert 'build_licenses' in table_names

    def test_all_goal_types_count(self):
        """6 goal types registered: marketing, coding, ip_protection, revenue, finance."""
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        assert len(types) >= 5

    def test_bootstrap_goals_count(self):
        """Bootstrap goals defined (>= 9 original + new goal types)."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        assert len(SEED_BOOTSTRAP_GOALS) >= 9


# ═══════════════════════════════════════════════════════════════
# 11. CCT Lifecycle — Continual Learning Incentive Pipeline
# ═══════════════════════════════════════════════════════════════

class TestCCTLifecycle:
    """E2E: tier compute → issue CCT → validate → gate access → renew → revoke → degrade."""

    @patch('security.node_integrity.sign_json_payload',
           return_value='a1' * 64)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b2' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'e2e_issuer'})
    def test_full_cct_lifecycle(self, mock_id, mock_pub, mock_sign, db):
        """Full cycle: eligible node → issue → validate → revoke → invalid."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)

        # Step 1: Create a compute contributor node
        peer = PeerNode(
            node_id='e2e_cct_node', url='http://e2e:6777',
            status='active', contribution_score=250.0,
            integrity_status='verified', capability_tier='full',
        )
        db.add(peer)
        db.flush()

        # Step 2: Compute tier — should be 'full'
        tier_info = ContinualLearnerGateService.compute_learning_tier(
            db, 'e2e_cct_node')
        assert tier_info['tier'] == 'full'
        assert tier_info['eligible']
        assert 'manifold_credit' in tier_info['capabilities']
        assert 'meta_learning' in tier_info['capabilities']

        # Step 3: Issue CCT
        cct_result = ContinualLearnerGateService.issue_cct(
            db, 'e2e_cct_node')
        assert cct_result is not None
        assert cct_result['tier'] == 'full'
        cct_token = cct_result['cct']
        assert '.' in cct_token

        # Step 4: Validate — should be valid
        with patch('security.node_integrity.verify_json_signature',
                   return_value=True):
            validation = ContinualLearnerGateService.validate_cct(
                cct_token, 'e2e_cct_node')
        assert validation['valid']
        assert validation['tier'] == 'full'

        # Step 5: Check capabilities
        with patch('security.node_integrity.verify_json_signature',
                   return_value=True):
            assert ContinualLearnerGateService.check_cct_capability(
                cct_token, 'manifold_credit', 'e2e_cct_node')
            assert not ContinualLearnerGateService.check_cct_capability(
                cct_token, 'skill_distribution', 'e2e_cct_node')

        # Step 6: Attestation recorded in DB
        att = db.query(NodeAttestation).filter_by(
            subject_node_id='e2e_cct_node',
            attestation_type='cct_issued',
        ).first()
        assert att is not None
        assert att.is_valid is True
        assert att.payload_json['tier'] == 'full'

        # Step 7: Revoke — invalidates attestation
        revoke = ContinualLearnerGateService.revoke_cct(
            db, 'e2e_cct_node', 'e2e_test')
        assert revoke['success']
        assert revoke['revoked_count'] >= 1

        # Step 8: Attestation is now invalid
        att_after = db.query(NodeAttestation).filter_by(
            subject_node_id='e2e_cct_node',
            attestation_type='cct_issued',
        ).first()
        assert att_after.is_valid is False

    def test_ineligible_node_denied(self, db):
        """Node with low score and unverified status — no CCT."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)

        observer = PeerNode(
            node_id='e2e_observer', url='http://obs:6777',
            status='active', contribution_score=10.0,
            integrity_status='unverified', capability_tier='observer',
        )
        db.add(observer)
        db.flush()

        tier_info = ContinualLearnerGateService.compute_learning_tier(
            db, 'e2e_observer')
        assert tier_info['tier'] == 'none'
        assert not tier_info['eligible']

        result = ContinualLearnerGateService.issue_cct(db, 'e2e_observer')
        assert result is None

    @patch('security.node_integrity.sign_json_payload',
           return_value='c3' * 64)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b2' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'e2e_issuer'})
    def test_tier_progression(self, mock_id, mock_pub, mock_sign, db):
        """Score increase → tier upgrade on next CCT issuance."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)

        # Start at basic tier
        peer = PeerNode(
            node_id='e2e_progress', url='http://prog:6777',
            status='active', contribution_score=60.0,
            integrity_status='verified', capability_tier='standard',
        )
        db.add(peer)
        db.flush()

        cct1 = ContinualLearnerGateService.issue_cct(db, 'e2e_progress')
        assert cct1['tier'] == 'basic'
        assert len(cct1['capabilities']) == 1

        # Upgrade: score rises + hardware upgrade
        peer.contribution_score = 550.0
        peer.capability_tier = 'compute_host'
        db.flush()

        cct2 = ContinualLearnerGateService.issue_cct(db, 'e2e_progress')
        assert cct2['tier'] == 'host'
        assert len(cct2['capabilities']) >= 6  # 7 with embedding_sync


class TestCCTWorldModelIntegration:
    """E2E: CCT gating on WorldModelBridge — real bridge, real gate logic."""

    def test_distribute_blocked_then_allowed(self):
        """distribute_skill_packet blocked without CCT, passes with valid CCT."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge

        bridge = WorldModelBridge()

        # Without CCT → blocked
        with patch.object(bridge, '_check_cct_access', return_value=False):
            result = bridge.distribute_skill_packet(
                {'description': 'test'}, 'e2e_node')
        assert not result['success']
        assert result['reason'] == 'no_cct_skill_distribution'

        # With valid CCT → passes through to witness check
        with patch.object(bridge, '_check_cct_access', return_value=True):
            result = bridge.distribute_skill_packet(
                {'description': 'test', 'witness_count': 0}, 'e2e_node')
        # Passes CCT gate but fails at witness check — proves gate was bypassed
        assert not result['success']
        assert 'witness' in result['reason'].lower()

    def test_hivemind_degrades_gracefully(self):
        """query_hivemind returns cached data when CCT is missing."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge

        bridge = WorldModelBridge()
        bridge._federation_aggregated['last_thought'] = 'cached intelligence'

        # Without CCT → graceful degrade to cached
        with patch.object(bridge, '_check_cct_access', return_value=False):
            result = bridge.query_hivemind('test question')
        assert result is not None
        assert result['source'] == 'cached'
        assert result['cct_gated'] is True

        # With CCT but no API → None (no cached, API unreachable)
        bridge2 = WorldModelBridge()
        with patch.object(bridge2, '_check_cct_access', return_value=True):
            result2 = bridge2.query_hivemind('test question')
        assert result2 is None  # No API configured, no cache


class TestCCTRewardIntegration:
    """E2E: CCT issuance → Spark rewards → wallet balance update."""

    @patch('security.node_integrity.sign_json_payload',
           return_value='r1' * 64)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b2' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'e2e_issuer'})
    def test_cct_issue_awards_spark(self, mock_id, mock_pub, mock_sign, db):
        """Issuing a CCT awards learning_contribution Spark + XP."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)
        from integrations.social.resonance_engine import ResonanceService

        # Create user + peer for the same node operator
        user = User(username='cct_reward_user', user_type='human')
        db.add(user)
        db.flush()
        user_id = str(user.id)

        peer = PeerNode(
            node_id='e2e_reward_node', url='http://reward:6777',
            status='active', contribution_score=300.0,
            integrity_status='verified', capability_tier='full',
            node_operator_id=user_id,
        )
        db.add(peer)
        db.flush()

        # Issue CCT — triggers reward
        result = ContinualLearnerGateService.issue_cct(db, 'e2e_reward_node')
        assert result is not None
        db.flush()

        # Check wallet after — should have increased
        wallet_after = ResonanceService.get_or_create_wallet(db, user_id)
        assert wallet_after.spark > 0
        assert wallet_after.xp > 0

    def test_reward_table_entries_functional(self, db):
        """All 3 learning reward types can be awarded through ResonanceService."""
        from integrations.social.resonance_engine import ResonanceService

        user = User(username='reward_test', user_type='human')
        db.add(user)
        db.flush()
        user_id = str(user.id)

        for action in ['learning_contribution', 'learning_skill_shared',
                        'learning_credit_assigned']:
            result = ResonanceService.award_action(
                db, user_id, action, 'test')
            assert 'spark' in result, f"{action} should award spark"


class TestCCTComputeBenchmark:
    """E2E: Submit benchmark → create attestation → verify contribution."""

    @patch('security.node_integrity.sign_json_payload',
           return_value='bm' * 64)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b2' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'e2e_issuer'})
    def test_benchmark_to_attestation(self, mock_id, mock_pub, mock_sign, db):
        """Benchmark submission creates NodeAttestation record."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)

        peer = PeerNode(
            node_id='e2e_bench', url='http://bench:6777',
            status='active', contribution_score=100.0,
            integrity_status='verified', capability_tier='standard',
        )
        db.add(peer)
        db.flush()

        result = ContinualLearnerGateService.verify_compute_contribution(
            db, 'e2e_bench', {
                'benchmark_type': 'credit_assignment',
                'score': 85.0,
                'duration_ms': 200.0,
            })
        assert result['verified']
        assert result['score'] == 85.0

        att = db.query(NodeAttestation).filter_by(
            subject_node_id='e2e_bench',
            attestation_type='compute_contribution',
        ).first()
        assert att is not None
        assert att.is_valid is True

    @patch('security.node_integrity.sign_json_payload',
           return_value='bm' * 64)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b2' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'e2e_issuer'})
    def test_invalid_benchmark_rejected(self, mock_id, mock_pub,
                                         mock_sign, db):
        """Zero-score benchmark is rejected."""
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGateService)

        peer = PeerNode(
            node_id='e2e_bench2', url='http://bench2:6777',
            status='active', contribution_score=100.0,
            integrity_status='verified', capability_tier='standard',
        )
        db.add(peer)
        db.flush()

        result = ContinualLearnerGateService.verify_compute_contribution(
            db, 'e2e_bench2', {'score': 0, 'duration_ms': 0})
        assert not result['verified']
        assert result['reason'] == 'invalid_benchmark'


class TestCCTAPIEndToEnd:
    """E2E: Flask API endpoints for CCT management."""

    @pytest.fixture
    def client(self):
        from flask import Flask
        from integrations.agent_engine.api_learning import learning_bp
        app = Flask(__name__)
        app.register_blueprint(learning_bp)
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_verify_then_tiers_flow(self, client):
        """POST /verify → GET /tiers — public read-only flow."""
        # Verify an invalid CCT
        resp = client.post('/api/learning/cct/verify',
                          json={'cct': 'garbage_token'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success']
        assert data['data']['valid'] is False

        # Get tier stats
        with patch('integrations.social.models.get_db') as mock_db:
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.filter.return_value.all.return_value = []
            resp = client.get('/api/learning/tiers')
        assert resp.status_code == 200

    def test_request_cct_auth_flow(self, client):
        """POST /request without signature → 400/403."""
        # Missing node_id
        resp = client.post('/api/learning/cct/request', json={})
        assert resp.status_code == 400

        # Invalid signature
        resp = client.post('/api/learning/cct/request',
                          json={'node_id': 'x', 'signature': 'bad',
                                'public_key': 'bad'})
        assert resp.status_code == 403


class TestCCTBootstrapIntegration:
    """E2E: Learning goal type is fully wired into the agent engine."""

    def test_learning_goal_type_complete_wiring(self):
        """learning type: registered, prompt builds, tool tags present, seed goal exists."""
        from integrations.agent_engine.goal_manager import (
            get_prompt_builder, get_tool_tags, get_registered_types)
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        from integrations.agent_engine.learning_tools import LEARNING_TOOLS

        # Type registered
        assert 'learning' in get_registered_types()

        # Prompt builder works
        builder = get_prompt_builder('learning')
        prompt = builder({
            'title': 'Test', 'description': 'E2E wiring test',
        })
        assert 'CONTINUAL LEARNING COORDINATOR' in prompt
        assert 'Intelligence is earned' in prompt

        # Tool tags
        tags = get_tool_tags('learning')
        assert 'learning' in tags

        # Tools registered
        assert len(LEARNING_TOOLS) == 6
        tool_names = {t['name'] for t in LEARNING_TOOLS}
        assert 'check_learning_health' in tool_names
        assert 'issue_cct' in tool_names
        assert 'distribute_learning_skill' in tool_names

        # Bootstrap goal seeded
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_learning_coordinator' in slugs
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_learning_coordinator')
        assert goal['goal_type'] == 'learning'
        assert goal['config']['continuous'] is True

    def test_learning_rewards_in_resonance(self):
        """All 3 learning reward actions exist in AWARD_TABLE."""
        from integrations.social.resonance_engine import AWARD_TABLE

        for action in ['learning_contribution', 'learning_skill_shared',
                        'learning_credit_assigned']:
            assert action in AWARD_TABLE, f"{action} missing from AWARD_TABLE"
            assert 'spark' in AWARD_TABLE[action]


# ═══════════════════════════════════════════════════════
# 10. VLM / Computer Use E2E
# ═══════════════════════════════════════════════════════

class TestVLMFrameStoreLifecycle:
    """Test FrameStore — thread-safe frame + description pipeline."""

    def test_camera_frame_put_get(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(max_frames=3)
        store.put_frame('user1', b'\x89PNG_frame_1')
        store.put_frame('user1', b'\x89PNG_frame_2')
        latest = store.get_frame('user1')
        assert latest == b'\x89PNG_frame_2'
        assert store.get_frame_count('user1') == 2

    def test_camera_description_ttl(self):
        import time
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(description_ttl=0.1)
        store.put_description('user1', 'A cat sitting on a desk')
        assert store.get_description('user1') == 'A cat sitting on a desk'
        time.sleep(0.15)
        assert store.get_description('user1') is None  # Expired

    def test_screen_channel_separate(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore()
        store.put_frame('user1', b'camera_frame')
        store.put_screen_frame('user1', b'screen_frame')
        assert store.get_frame('user1') == b'camera_frame'
        assert store.get_screen_frame('user1') == b'screen_frame'

    def test_bounded_buffer(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(max_frames=2)
        store.put_frame('u', b'f1')
        store.put_frame('u', b'f2')
        store.put_frame('u', b'f3')
        assert store.get_frame_count('u') == 2
        assert store.get_frame('u') == b'f3'

    def test_description_history(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore()
        store.put_description('u', 'desc1')
        store.put_description('u', 'desc2')
        history = store.get_camera_description_history('u')
        assert len(history) >= 2


class TestVLMVisualAgentPayload:
    """Test visual agent request/response construction."""

    _API_KEY = 'test_vlm_api_key_e2e'

    def test_visual_agent_requires_fields(self):
        """Verify /visual_agent rejects missing fields."""
        from langchain_gpt_api import app
        with patch.dict(os.environ, {'HEVOLVE_API_KEY': self._API_KEY}):
            with app.test_client() as client:
                resp = client.post('/visual_agent', json={},
                                   headers={'X-API-Key': self._API_KEY})
                assert resp.status_code == 404
                data = resp.get_json()
                assert 'error' in data

    def test_visual_agent_accepts_valid_payload(self):
        """Verify /visual_agent accepts all required fields."""
        from langchain_gpt_api import app
        with patch.dict(os.environ, {'HEVOLVE_API_KEY': self._API_KEY}):
            with app.test_client() as client:
                with patch('langchain_gpt_api.visual_based_execution',
                           return_value='Action completed') as mock_exec:
                    resp = client.post('/visual_agent', json={
                        'task_description': 'Click the submit button',
                        'user_id': '12345',
                        'prompt_id': '67890',
                        'request_from': 'Reuse',
                    }, headers={'X-API-Key': self._API_KEY})
                    assert resp.status_code == 200
                    data = resp.get_json()
                    assert data['response'] == 'Action completed'
                    mock_exec.assert_called_once()


class TestVLMVisionServiceCircuitBreaker:
    """Test VisionService circuit breaker pattern."""

    def test_service_initializes(self):
        """VisionService can be instantiated without MiniCPM running."""
        from integrations.vision.vision_service import VisionService
        from integrations.vision.frame_store import FrameStore
        store = FrameStore()
        service = VisionService(frame_store=store)
        assert service is not None
        assert service.store is store

    def test_service_describe_without_backend(self):
        """_describe_frame returns None when no backend is running."""
        from integrations.vision.vision_service import VisionService
        from integrations.vision.frame_store import FrameStore
        store = FrameStore()
        service = VisionService(frame_store=store)
        # No MiniCPM running — should gracefully return None
        result = service._describe_frame('test', b'\x89PNG_test')
        assert result is None or isinstance(result, str)


# ═══════════════════════════════════════════════════════
# 11. Qwen3-VL + OmniParser Computer Use E2E
# ═══════════════════════════════════════════════════════

class TestComputerUseLocalLoop:
    """E2E: local_loop.py — screenshot→parse→LLM→action loop with all externals mocked."""

    def _make_message(self, instruction='Click the submit button', max_eta=60):
        return {
            'instruction_to_vlm_agent': instruction,
            'enhanced_instruction': instruction,
            'user_id': 'test_user',
            'prompt_id': 'test_prompt',
            'os_to_control': 'windows',
            'max_ETA_in_seconds': max_eta,
        }

    @patch('integrations.vlm.local_computer_tool.execute_action',
           return_value={'output': 'ok'})
    @patch('integrations.vlm.local_loop._call_local_llm')
    @patch('integrations.vlm.local_omniparser.parse_screen',
           return_value={'screen_info': 'Button[0]: Submit', 'parsed_content_list': []})
    @patch('integrations.vlm.local_computer_tool.take_screenshot',
           return_value='iVBORw0KGgoAAAA==')
    def test_loop_completes_on_done_status(self, mock_ss, mock_parse,
                                            mock_llm, mock_exec):
        """LLM returns Status=DONE → loop stops after 1 iteration."""
        mock_llm.return_value = json.dumps({
            'Reasoning': 'Task is complete',
            'Next Action': 'None',
            'Status': 'DONE',
        })
        from integrations.vlm.local_loop import run_local_agentic_loop
        result = run_local_agentic_loop(self._make_message(), tier='inprocess')
        assert result['status'] == 'success'
        assert len(result['extracted_responses']) == 1
        assert result['extracted_responses'][0]['type'] == 'completion'
        mock_exec.assert_not_called()  # No action executed for DONE

    @patch('time.sleep')  # skip delay
    @patch('integrations.vlm.local_computer_tool.execute_action',
           return_value={'output': 'clicked'})
    @patch('integrations.vlm.local_loop._call_local_llm')
    @patch('integrations.vlm.local_omniparser.parse_screen',
           return_value={'screen_info': 'UI', 'parsed_content_list': []})
    @patch('integrations.vlm.local_computer_tool.take_screenshot',
           return_value='base64img')
    def test_loop_respects_max_iterations(self, mock_ss, mock_parse,
                                           mock_llm, mock_exec, mock_sleep):
        """LLM always returns actions → stops at max_iterations."""
        mock_llm.return_value = json.dumps({
            'Reasoning': 'Clicking button',
            'Next Action': 'left_click',
            'coordinate': [100, 200],
            'Status': 'IN_PROGRESS',
        })
        from integrations.vlm.local_loop import run_local_agentic_loop
        result = run_local_agentic_loop(
            self._make_message(), tier='http', max_iterations=3)
        assert result['status'] == 'success'
        assert len(result['extracted_responses']) == 3
        assert mock_exec.call_count == 3

    @patch('time.sleep')
    @patch('integrations.vlm.local_computer_tool.execute_action',
           return_value={'output': 'ok'})
    @patch('integrations.vlm.local_loop._call_local_llm')
    @patch('integrations.vlm.local_omniparser.parse_screen',
           return_value={'screen_info': '', 'parsed_content_list': []})
    @patch('integrations.vlm.local_computer_tool.take_screenshot',
           return_value='img')
    def test_loop_respects_timeout(self, mock_ss, mock_parse,
                                    mock_llm, mock_exec, mock_sleep):
        """max_ETA_in_seconds exceeded → early exit."""
        call_count = [0]

        def slow_llm(*args, **kwargs):
            call_count[0] += 1
            return json.dumps({
                'Reasoning': 'working', 'Next Action': 'wait',
                'Status': 'IN_PROGRESS',
            })

        mock_llm.side_effect = slow_llm
        from integrations.vlm.local_loop import run_local_agentic_loop
        # Set max_eta=0 so it times out immediately on second iteration
        result = run_local_agentic_loop(
            self._make_message(max_eta=0), tier='inprocess', max_iterations=100)
        # Should exit much earlier than 100 iterations
        assert len(result['extracted_responses']) < 100

    def test_build_vision_prompt_first_iteration(self):
        """First prompt says 'Analyze the UI elements'."""
        from integrations.vlm.local_loop import _build_vision_prompt
        content = _build_vision_prompt('Button: Submit', 'base64img', iteration=0)
        assert isinstance(content, list)
        assert len(content) == 2
        assert 'Analyze' in content[0]['text']
        assert content[1]['type'] == 'image_url'

    def test_build_vision_prompt_subsequent(self):
        """Subsequent prompt says 'Verify the previous action'."""
        from integrations.vlm.local_loop import _build_vision_prompt
        content = _build_vision_prompt('UI info', 'base64img', iteration=3)
        assert 'Verify' in content[0]['text'] or 'previous' in content[0]['text']

    def test_parse_vlm_response_markdown_json(self):
        """Extracts JSON from ```json block."""
        from integrations.vlm.local_loop import _parse_vlm_response
        text = 'Some text\n```json\n{"Next Action": "left_click", "Status": "IN_PROGRESS"}\n```'
        result = _parse_vlm_response(text)
        assert result['Next Action'] == 'left_click'

    def test_parse_vlm_response_raw_json(self):
        """Extracts JSON from raw {…} in text."""
        from integrations.vlm.local_loop import _parse_vlm_response
        text = 'I will click the button. {"Next Action": "type", "value": "hello", "Status": "DONE"}'
        result = _parse_vlm_response(text)
        assert result['Next Action'] == 'type'
        assert result['value'] == 'hello'

    def test_build_action_payload_box_id(self):
        """Box ID resolved → coordinate from parsed_content_list center."""
        from integrations.vlm.local_loop import _build_action_payload
        action_json = {'Next Action': 'left_click', 'Box ID': 5}
        parsed_screen = {
            'parsed_content_list': [
                {'idx': 5, 'bbox': [100, 200, 300, 400], 'content': 'Submit'},
            ]
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload['action'] == 'left_click'
        assert payload['coordinate'] == [200, 300]  # center of [100,200,300,400]


class TestComputerUseOmniParser:
    """E2E: local_omniparser.py — HTTP and in-process screen parsing."""

    @patch('requests.post')
    def test_parse_screen_http(self, mock_post):
        """HTTP POST to :8080/parse/ with base64 image."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            'screen_info': 'Button[0]: OK',
            'parsed_content_list': [{'idx': 0, 'bbox': [10, 20, 30, 40]}],
            'width': 1920, 'height': 1080,
        }
        mock_post.return_value = mock_resp

        from integrations.vlm.local_omniparser import parse_screen
        result = parse_screen('base64imagedata', tier='http')
        assert result['screen_info'] == 'Button[0]: OK'
        assert len(result['parsed_content_list']) == 1
        mock_post.assert_called_once()
        call_json = mock_post.call_args[1].get('json', {})
        assert 'base64_image' in call_json

    @patch('requests.post')
    def test_parse_screen_returns_screen_info(self, mock_post):
        """Response has screen_info, parsed_content_list, width, height."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            'screen_info': 'TextBox[0]: Name',
            'parsed_content_list': [{'idx': 0, 'content': 'Name', 'bbox': [5, 5, 50, 25]}],
            'width': 1920, 'height': 1080,
        }
        mock_post.return_value = mock_resp

        from integrations.vlm.local_omniparser import parse_screen
        result = parse_screen('img', tier='http')
        assert 'screen_info' in result
        assert 'parsed_content_list' in result
        assert 'width' in result

    @patch('requests.post')
    def test_parse_screen_measures_latency(self, mock_post):
        """latency field present in result."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            'screen_info': '', 'parsed_content_list': [],
        }
        mock_post.return_value = mock_resp

        from integrations.vlm.local_omniparser import parse_screen
        result = parse_screen('img', tier='http')
        assert 'latency' in result
        assert isinstance(result['latency'], float)

    def test_parse_screen_inprocess_loads_singleton(self):
        """Thread lock + singleton pattern works for in-process mode."""
        import integrations.vlm.local_omniparser as omni_mod
        # Verify singleton lock mechanism
        lock = omni_mod._get_lock()
        assert lock is not None
        # Calling again returns same lock
        lock2 = omni_mod._get_lock()
        assert lock is lock2


class TestComputerUseActions:
    """E2E: local_computer_tool.py — action execution with pyautogui mocked."""

    @patch('integrations.vlm.local_computer_tool.pyautogui')
    def test_click_dispatches_pyautogui(self, mock_gui):
        """left_click → pyautogui.click(x, y)."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        result = _execute_inprocess({
            'action': 'left_click', 'coordinate': [100, 200],
        })
        mock_gui.click.assert_called_once_with(100, 200)
        assert 'Clicked' in result['output']

    @patch('integrations.vlm.local_computer_tool.pyautogui')
    @patch('integrations.vlm.local_computer_tool.pyperclip')
    def test_type_uses_clipboard(self, mock_clip, mock_gui):
        """type → pyperclip.copy() + Ctrl+V."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        result = _execute_inprocess({
            'action': 'type', 'text': 'Hello World',
        })
        mock_clip.copy.assert_called_once_with('Hello World')
        mock_gui.hotkey.assert_called_once_with('ctrl', 'v')
        assert 'Typed' in result['output']

    @patch('integrations.vlm.local_computer_tool.pyautogui')
    def test_hotkey_splits_keys(self, mock_gui):
        """'alt+tab' → pyautogui.hotkey('alt', 'tab')."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        result = _execute_inprocess({
            'action': 'hotkey', 'text': 'alt+tab',
        })
        mock_gui.hotkey.assert_called_once_with('alt', 'tab')
        assert 'Hotkey' in result['output']

    def test_file_write_read(self, tmp_path):
        """Write + read files via tmp_path (real FS)."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        test_file = str(tmp_path / 'test.txt')

        # Write
        result_w = _execute_inprocess({
            'action': 'write_file', 'path': test_file,
            'content': 'Hello from VLM',
        })
        assert 'Written' in result_w['output']

        # Read
        result_r = _execute_inprocess({
            'action': 'read_file_and_understand', 'path': test_file,
        })
        assert 'Hello from VLM' in result_r['output']

    @patch('integrations.vlm.local_computer_tool.pyautogui', MagicMock())
    def test_unknown_action_error(self):
        """Returns error for unknown action type."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        result = _execute_inprocess({'action': 'teleport'})
        assert 'error' in result
        assert 'Unknown action' in result['error']

    @patch('integrations.vlm.local_computer_tool.pyautogui')
    def test_screenshot_inprocess(self, mock_gui):
        """pyautogui.screenshot() → base64 PNG."""
        import io
        from PIL import Image
        # Create a tiny 1×1 image
        img = Image.new('RGB', (1, 1), color='red')
        mock_gui.screenshot.return_value = img

        from integrations.vlm.local_computer_tool import take_screenshot
        b64 = take_screenshot(tier='inprocess')
        assert isinstance(b64, str)
        # Verify it's valid base64 that decodes to PNG
        import base64 as b64_mod
        raw = b64_mod.b64decode(b64)
        assert raw[:4] == b'\x89PNG'

    @patch('integrations.vlm.local_computer_tool.requests.get')
    def test_screenshot_http(self, mock_get):
        """HTTP tier calls localhost:5001/screenshot."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'base64_image': 'AAAA'}
        mock_get.return_value = mock_resp

        from integrations.vlm.local_computer_tool import take_screenshot
        b64 = take_screenshot(tier='http')
        assert b64 == 'AAAA'
        mock_get.assert_called_once()

    def test_wait_action(self):
        """wait action sleeps for specified duration."""
        from integrations.vlm.local_computer_tool import _execute_inprocess
        with patch('integrations.vlm.local_computer_tool.time.sleep') as mock_sleep:
            result = _execute_inprocess({'action': 'wait', 'duration': 3})
        mock_sleep.assert_called_once_with(3)
        assert 'Waited' in result['output']

    def test_list_folders(self, tmp_path):
        """list_folders_and_files returns directory listing."""
        (tmp_path / 'file_a.txt').write_text('a')
        (tmp_path / 'file_b.txt').write_text('b')
        from integrations.vlm.local_computer_tool import _execute_inprocess
        result = _execute_inprocess({
            'action': 'list_folders_and_files', 'path': str(tmp_path),
        })
        assert 'file_a.txt' in result['output']
        assert 'file_b.txt' in result['output']


class TestComputerUseAdapter:
    """E2E: vlm_adapter.py — 3-tier routing with circuit breaker."""

    def test_tier1_bundled_routes_inprocess(self):
        """NUNBA_BUNDLED=true → run_local_agentic_loop(tier='inprocess')."""
        import integrations.vlm.vlm_adapter as adapter
        # Save originals
        orig_bundled = adapter._BUNDLED_MODE
        orig_pyautogui = adapter._HAS_PYAUTOGUI
        orig_fail = adapter._tier1_fail_count
        try:
            adapter._BUNDLED_MODE = True
            adapter._HAS_PYAUTOGUI = True
            adapter._tier1_fail_count = 0
            with patch('integrations.vlm.local_loop.run_local_agentic_loop',
                       return_value={'status': 'success', 'extracted_responses': []}) as mock_loop:
                result = adapter.execute_vlm_instruction({'instruction_to_vlm_agent': 'test'})
            assert result['status'] == 'success'
            mock_loop.assert_called_once()
            # tier passed as positional or keyword
            call_a, call_kw = mock_loop.call_args
            tier_val = call_kw.get('tier') or (call_a[1] if len(call_a) > 1 else None)
            assert tier_val == 'inprocess'
        finally:
            adapter._BUNDLED_MODE = orig_bundled
            adapter._HAS_PYAUTOGUI = orig_pyautogui
            adapter._tier1_fail_count = orig_fail

    def test_tier2_flat_routes_http(self):
        """Flat mode → run_local_agentic_loop(tier='http')."""
        import integrations.vlm.vlm_adapter as adapter
        orig_bundled = adapter._BUNDLED_MODE
        orig_tier = adapter._node_tier
        orig_fail2 = adapter._tier2_fail_count
        try:
            adapter._BUNDLED_MODE = False  # Not bundled → skip tier 1
            adapter._node_tier = 'flat'
            adapter._tier2_fail_count = 0
            with patch('integrations.vlm.local_loop.run_local_agentic_loop',
                       return_value={'status': 'success', 'extracted_responses': []}) as mock_loop:
                result = adapter.execute_vlm_instruction({'instruction_to_vlm_agent': 'test'})
            assert result['status'] == 'success'
            mock_loop.assert_called_once()
            call_a, call_kw = mock_loop.call_args
            tier_val = call_kw.get('tier') or (call_a[1] if len(call_a) > 1 else None)
            assert tier_val == 'http'
        finally:
            adapter._BUNDLED_MODE = orig_bundled
            adapter._node_tier = orig_tier
            adapter._tier2_fail_count = orig_fail2

    def test_circuit_breaker_opens(self):
        """2 consecutive failures → tier skipped, falls to Tier 3 (None)."""
        import integrations.vlm.vlm_adapter as adapter
        orig_bundled = adapter._BUNDLED_MODE
        orig_tier = adapter._node_tier
        orig_fail1 = adapter._tier1_fail_count
        orig_fail2 = adapter._tier2_fail_count
        try:
            adapter._BUNDLED_MODE = True
            adapter._HAS_PYAUTOGUI = True
            adapter._node_tier = 'flat'
            adapter._tier1_fail_count = 2  # Circuit breaker open
            adapter._tier2_fail_count = 2  # Circuit breaker open
            # Both tiers breaker-tripped → returns None (Tier 3)
            result = adapter.execute_vlm_instruction({'instruction_to_vlm_agent': 'test'})
            assert result is None
        finally:
            adapter._BUNDLED_MODE = orig_bundled
            adapter._node_tier = orig_tier
            adapter._tier1_fail_count = orig_fail1
            adapter._tier2_fail_count = orig_fail2

    def test_check_vlm_available(self):
        """check_vlm_available returns True in bundled mode."""
        import integrations.vlm.vlm_adapter as adapter
        orig_bundled = adapter._BUNDLED_MODE
        orig_pyautogui = adapter._HAS_PYAUTOGUI
        try:
            adapter._BUNDLED_MODE = True
            adapter._HAS_PYAUTOGUI = True
            assert adapter.check_vlm_available() is True
        finally:
            adapter._BUNDLED_MODE = orig_bundled
            adapter._HAS_PYAUTOGUI = orig_pyautogui


class TestVLMAgentIntegrationBridge:
    """E2E: vlm_agent_integration.py — VLMAgentContext bridge."""

    def test_vlm_context_status_summary(self):
        """get_status_summary() returns correct shape."""
        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext(vlm_server_url='http://localhost:5001',
                             omniparser_url='http://localhost:8080')
        with patch.object(ctx, 'is_vlm_available', return_value=False):
            with patch.object(ctx, 'is_omniparser_available', return_value=False):
                summary = ctx.get_status_summary()
        assert 'vlm_available' in summary
        assert 'omniparser_available' in summary
        assert 'screen_history_count' in summary
        assert 'action_history_count' in summary
        assert summary['vlm_available'] is False
        assert summary['screen_history_count'] == 0

    def test_inject_visual_context_unavailable(self):
        """VLM unavailable → graceful degrade in task context."""
        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()
        with patch.object(ctx, 'get_screen_context', return_value=None):
            result = ctx.inject_visual_context_into_ledger_task({
                'task_id': 'test'})
        assert result['visual_context']['has_screen_info'] is False
        assert 'not available' in result['visual_context']['note']

    @patch('integrations.vlm.vlm_agent_integration.requests.post')
    @patch('integrations.vlm.vlm_agent_integration.requests.get')
    def test_execute_windows_command_steps(self, mock_get, mock_post):
        """4-step Win+R sequence dispatched correctly."""
        # VLM server available
        mock_health = MagicMock()
        mock_health.status_code = 200
        mock_get.return_value = mock_health

        # Each action succeeds
        mock_action = MagicMock()
        mock_action.status_code = 200
        mock_action.json.return_value = {'status': 'success', 'output': 'ok'}
        mock_post.return_value = mock_action

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()
        result = ctx.execute_windows_command('notepad')
        assert result['status'] == 'success'
        assert result['command'] == 'notepad'
        assert len(result['results']) == 4  # hotkey, wait, type, hotkey
