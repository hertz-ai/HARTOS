"""
Functional tests for the revenue system with REAL SQLite database.

These tests use an in-memory SQLite database with actual inserts, queries,
and settlements -- no mocks. They exercise query_revenue_streams(),
settle_metered_api_costs(), and get_dashboard() end-to-end.

Run:
    pytest tests/functional/test_revenue_functional.py -v --noconftest
"""
import os
import sys
import importlib
import uuid
from datetime import datetime, timedelta

# ── Force in-memory SQLite BEFORE any model import ──────────────────
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# Fixture: real in-memory SQLite database, shared across the module
# ---------------------------------------------------------------------------

# We need to build the engine/session independently of the production
# singleton so tests are fully isolated.  Import Base from models.py
# (which is what all model classes register against).

from integrations.social.models import Base

_engine = create_engine(
    'sqlite://',
    echo=False,
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)
_SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


@pytest.fixture(scope='function')
def db():
    """Yield a real SQLAlchemy session backed by an in-memory SQLite DB.

    Each test function gets a fresh set of tables (create_all / drop_all).
    """
    Base.metadata.create_all(_engine)
    session = _SessionFactory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(_engine)


# ---------------------------------------------------------------------------
# Model imports (after DB_PATH is set)
# ---------------------------------------------------------------------------
from integrations.social.models import (
    User, ResonanceWallet, ResonanceTransaction,
    MeteredAPIUsage, AdUnit, APIUsageLog, CommercialAPIKey,
    HostingReward, PaperPortfolio,
)
from integrations.agent_engine.revenue_aggregator import (
    query_revenue_streams,
    settle_metered_api_costs,
    RevenueAggregator,
    REVENUE_SPLIT_USERS,
    REVENUE_SPLIT_INFRA,
    REVENUE_SPLIT_CENTRAL,
    SPARK_PER_USD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(db, user_id=None, username=None) -> User:
    """Insert a minimal User row and return it."""
    uid = user_id or str(uuid.uuid4())
    uname = username or f'user_{uid[:8]}'
    user = User(id=uid, username=uname, user_type='human')
    db.add(user)
    db.flush()
    return user


def _make_wallet(db, user_id, spark=0) -> ResonanceWallet:
    """Insert a ResonanceWallet for the given user."""
    wallet = ResonanceWallet(user_id=user_id, spark=spark, spark_lifetime=spark)
    db.add(wallet)
    db.flush()
    return wallet


def _make_api_key(db, user_id) -> CommercialAPIKey:
    """Insert a CommercialAPIKey (required FK for APIUsageLog)."""
    key = CommercialAPIKey(
        user_id=user_id,
        key_hash=f'hash_{uuid.uuid4().hex[:16]}',
        key_prefix='hev_test',
    )
    db.add(key)
    db.flush()
    return key


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRevenueSplit:
    """Test 1: Insert API revenue + ad revenue rows. Call
    query_revenue_streams(). Verify 90/9/1 split math."""

    def test_revenue_split_90_9_1(self, db):
        # ── Arrange: create user + API usage logs ──
        user = _make_user(db)
        api_key = _make_api_key(db, user.id)

        # Insert 3 API usage logs totalling $150 in credits
        for cost in [50.0, 60.0, 40.0]:
            log = APIUsageLog(
                api_key_id=api_key.id,
                endpoint='/v1/chat',
                tokens_in=1000,
                tokens_out=500,
                cost_credits=cost,
                created_at=datetime.utcnow(),
            )
            db.add(log)

        # Insert 2 ad units with spent_spark totalling 300
        for spent in [200, 100]:
            ad = AdUnit(
                advertiser_id=user.id,
                title='Test Ad',
                click_url='https://example.com',
                spent_spark=spent,
                created_at=datetime.utcnow(),
            )
            db.add(ad)

        db.flush()

        # ── Act ──
        result = query_revenue_streams(db, period_days=30)

        # ── Assert ──
        expected_api = 150.0
        expected_ad = 300.0
        expected_gross = expected_api + expected_ad

        assert result['api_revenue'] == pytest.approx(expected_api)
        assert result['ad_revenue'] == pytest.approx(expected_ad)
        assert result['total_gross'] == pytest.approx(expected_gross)

        # 90/9/1 split
        assert result['user_pool_share'] == pytest.approx(expected_gross * 0.90)
        assert result['infra_pool_share'] == pytest.approx(expected_gross * 0.09)
        assert result['central_share'] == pytest.approx(expected_gross * 0.01)

        # platform_share = infra + central = 10%
        assert result['platform_share'] == pytest.approx(expected_gross * 0.10)

        # Sanity: shares sum to total
        total_shares = (
            result['user_pool_share']
            + result['infra_pool_share']
            + result['central_share']
        )
        assert total_shares == pytest.approx(expected_gross)


class TestSettleMeteredCosts:
    """Tests 2-4: settle_metered_api_costs() with real DB operations."""

    def test_settle_metered_costs_awards_spark(self, db):
        """Test 2: Hive task usage ($0.50) awards SPARK_PER_USD * 0.50 Spark."""
        operator = _make_user(db)
        wallet = _make_wallet(db, operator.id, spark=0)

        usage = MeteredAPIUsage(
            node_id='node_001',
            operator_id=operator.id,
            model_id='gpt-4',
            task_source='hive',
            actual_usd_cost=0.50,
            settlement_status='pending',
            created_at=datetime.utcnow(),
        )
        db.add(usage)
        db.flush()

        # ── Act ──
        result = settle_metered_api_costs(db, period_hours=24)

        # ── Assert ──
        assert result['settled_count'] == 1
        expected_spark = max(1, int(0.50 * SPARK_PER_USD))
        assert result['total_spark_awarded'] == expected_spark
        assert result['total_usd_settled'] == pytest.approx(0.50)

        # Verify wallet balance actually increased in DB
        db.refresh(wallet)
        assert wallet.spark == expected_spark
        assert wallet.spark_lifetime == expected_spark

        # Verify usage record is now settled
        db.refresh(usage)
        assert usage.settlement_status == 'settled'

    def test_settle_skips_own_tasks(self, db):
        """Test 3: task_source='own' must NOT be settled."""
        operator = _make_user(db)
        _make_wallet(db, operator.id, spark=0)

        usage = MeteredAPIUsage(
            node_id='node_002',
            operator_id=operator.id,
            model_id='gpt-4',
            task_source='own',
            actual_usd_cost=1.00,
            settlement_status='pending',
            created_at=datetime.utcnow(),
        )
        db.add(usage)
        db.flush()

        # ── Act ──
        result = settle_metered_api_costs(db, period_hours=24)

        # ── Assert ──
        assert result['settled_count'] == 0
        assert result['total_spark_awarded'] == 0

        # Record must still be pending (task_source filter excluded it)
        db.refresh(usage)
        assert usage.settlement_status == 'pending'

    def test_settle_written_off_no_operator(self, db):
        """Test 4: Usage with no operator_id is marked 'written_off'."""
        usage = MeteredAPIUsage(
            node_id='node_003',
            operator_id=None,
            model_id='claude-3',
            task_source='hive',
            actual_usd_cost=0.25,
            settlement_status='pending',
            created_at=datetime.utcnow(),
        )
        db.add(usage)
        db.flush()

        # ── Act ──
        result = settle_metered_api_costs(db, period_hours=24)

        # ── Assert ──
        assert result['settled_count'] == 0  # written_off is NOT counted as settled
        assert result['total_spark_awarded'] == 0

        # The function sets written_off on the object but only flushes when
        # settled_count > 0.  Flush the session (as a caller would on commit)
        # then verify the persisted state.
        db.flush()
        db.refresh(usage)
        assert usage.settlement_status == 'written_off'


class TestDashboard:
    """Test 5: get_dashboard() returns all expected keys."""

    def test_dashboard_returns_all_keys(self, db):
        # ── Act: call on empty DB ──
        dashboard = RevenueAggregator.get_dashboard(db)

        # ── Assert top-level keys ──
        assert 'revenue' in dashboard
        assert 'trading' in dashboard
        assert 'funding' in dashboard

        # ── Assert revenue sub-keys ──
        revenue = dashboard['revenue']
        for key in [
            'period_days', 'api_revenue', 'ad_revenue',
            'hosting_payouts', 'total_gross',
            'user_pool_share', 'infra_pool_share',
            'central_share', 'platform_share',
        ]:
            assert key in revenue, f"Missing revenue key: {key}"

        # ── Assert trading sub-keys ──
        trading = dashboard['trading']
        assert 'active_portfolios' in trading
        assert 'total_pnl' in trading

        # ── Assert funding sub-keys ──
        funding = dashboard['funding']
        assert 'threshold' in funding
        assert 'allocation_pct' in funding
        assert 'platform_excess' in funding

        # ── On empty DB all numeric values should be zero ──
        assert revenue['api_revenue'] == 0.0
        assert revenue['ad_revenue'] == 0.0
        assert revenue['total_gross'] == 0.0
        assert trading['active_portfolios'] == 0
        assert trading['total_pnl'] == 0.0


class TestSparkPerUsdEnvOverride:
    """Test 6: HEVOLVE_SPARK_PER_USD env override changes conversion rate."""

    def test_spark_per_usd_env_override(self, db):
        """Set HEVOLVE_SPARK_PER_USD=200, settle $1.00 usage, verify 200 Spark."""
        operator = _make_user(db)
        wallet = _make_wallet(db, operator.id, spark=0)

        usage = MeteredAPIUsage(
            node_id='node_env',
            operator_id=operator.id,
            model_id='gpt-4',
            task_source='hive',
            actual_usd_cost=1.00,
            settlement_status='pending',
            created_at=datetime.utcnow(),
        )
        db.add(usage)
        db.flush()

        # ── Override the module-level SPARK_PER_USD via env + reimport ──
        import integrations.agent_engine.revenue_aggregator as rev_mod
        original_spark_per_usd = rev_mod.SPARK_PER_USD
        try:
            os.environ['HEVOLVE_SPARK_PER_USD'] = '200'
            # Reload the module so it picks up the new env value
            importlib.reload(rev_mod)
            assert rev_mod.SPARK_PER_USD == 200

            # ── Act ──
            result = rev_mod.settle_metered_api_costs(db, period_hours=24)

            # ── Assert ──
            assert result['settled_count'] == 1
            expected_spark = max(1, int(1.00 * 200))
            assert result['total_spark_awarded'] == expected_spark

            db.refresh(wallet)
            assert wallet.spark == expected_spark
        finally:
            # ── Restore original value ──
            os.environ.pop('HEVOLVE_SPARK_PER_USD', None)
            importlib.reload(rev_mod)
            assert rev_mod.SPARK_PER_USD == original_spark_per_usd
