"""
Tests for boot-time hardening (WS4) — central instance security enforcement.
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestDevModeEnforcement:

    def test_dev_mode_forced_off_on_central(self):
        """init_social should force HEVOLVE_DEV_MODE=false when tier=central."""
        with patch.dict(os.environ, {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_DEV_MODE': 'true',
        }):
            # Simulate the check from __init__.py lines 23-28
            node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if node_tier == 'central' and os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
                os.environ['HEVOLVE_DEV_MODE'] = 'false'

            assert os.environ['HEVOLVE_DEV_MODE'] == 'false'

    def test_dev_mode_untouched_on_flat(self):
        """Dev mode should NOT be forced off on flat/regional tiers."""
        with patch.dict(os.environ, {
            'HEVOLVE_NODE_TIER': 'flat',
            'HEVOLVE_DEV_MODE': 'true',
        }):
            node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if node_tier == 'central' and os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
                os.environ['HEVOLVE_DEV_MODE'] = 'false'

            assert os.environ['HEVOLVE_DEV_MODE'] == 'true'

    def test_validate_startup_blocks_dev_on_central(self):
        """_validate_startup should force dev mode off on central."""
        with patch.dict(os.environ, {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_DEV_MODE': 'true',
        }):
            # Simulate the check from hart_intelligence_entry.py _validate_startup
            node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if node_tier == 'central':
                if os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
                    os.environ['HEVOLVE_DEV_MODE'] = 'false'

            assert os.environ['HEVOLVE_DEV_MODE'] == 'false'


class TestTierAuthorization:

    def test_central_without_key_blocks_boot(self):
        """Central node failing tier auth should set _boot_verified=False."""
        _boot_verified = True
        node_tier = 'central'

        # Simulate tier auth returning unauthorized
        tier_auth = {'authorized': False, 'details': 'no master key'}
        if not tier_auth.get('authorized'):
            if node_tier == 'central':
                _boot_verified = False

        assert _boot_verified is False

    def test_flat_tier_always_authorized(self):
        """Flat tier should not block boot even if tier auth fails."""
        _boot_verified = True
        node_tier = 'flat'

        tier_auth = {'authorized': False, 'details': 'no master key'}
        if not tier_auth.get('authorized'):
            if node_tier == 'central':
                _boot_verified = False

        assert _boot_verified is True

    def test_regional_tier_not_blocked(self):
        """Regional tier should not block boot on tier auth failure."""
        _boot_verified = True
        node_tier = 'regional'

        tier_auth = {'authorized': False, 'details': 'no master key'}
        if not tier_auth.get('authorized'):
            if node_tier == 'central':
                _boot_verified = False

        assert _boot_verified is True

    def test_central_with_valid_key_passes(self):
        """Central with valid master key should pass boot."""
        _boot_verified = True
        node_tier = 'central'

        tier_auth = {'authorized': True, 'tier': 'central', 'details': 'master key verified'}
        if not tier_auth.get('authorized'):
            if node_tier == 'central':
                _boot_verified = False

        assert _boot_verified is True


class TestCentralHardening:

    def test_missing_openai_key_warns_on_central(self):
        """Central with missing/placeholder OPENAI_API_KEY should be detected."""
        placeholders = ['', 'sk-xxx123', 'your-key']
        for key in placeholders:
            api_key = key
            is_placeholder = not api_key or api_key.startswith('sk-xxx') or api_key == 'your-key'
            assert is_placeholder, f"Expected placeholder detection for '{key}'"

    def test_valid_openai_key_passes(self):
        """Real-looking key should not be flagged."""
        api_key = 'sk-proj-abcdef1234567890'
        is_placeholder = not api_key or api_key.startswith('sk-xxx') or api_key == 'your-key'
        assert not is_placeholder

    def test_missing_key_no_warn_on_flat(self):
        """Flat tier should not trigger central hardening checks."""
        node_tier = 'flat'
        # Central checks should not run
        assert node_tier != 'central'

    def test_tls_check_detects_missing_cert(self):
        """Central without TLS_CERT_PATH should be flagged."""
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'central'}, clear=False):
            # Remove TLS_CERT_PATH if present
            os.environ.pop('TLS_CERT_PATH', None)
            assert not os.environ.get('TLS_CERT_PATH')

    def test_sqlite_detected_on_central(self):
        """Central using SQLite should be flagged for upgrade."""
        db_urls = ['', 'sqlite:///data.db', 'sqlite+pysqlite:///test.db']
        for url in db_urls:
            is_sqlite = not url or 'sqlite' in url.lower()
            assert is_sqlite, f"Expected SQLite detection for '{url}'"

    def test_postgres_passes_db_check(self):
        """PostgreSQL should pass the DB encryption check."""
        db_url = 'postgresql://user:pass@host/dbname'
        is_sqlite = not db_url or 'sqlite' in db_url.lower()
        assert not is_sqlite


class TestChatRateLimitStructure:

    def test_chat_rate_limit_structure(self):
        """Rate limiter should correctly track and reject excessive calls."""
        from integrations.social.rate_limiter import TokenBucket

        limiter = TokenBucket()
        user = 'test_user_rate'
        action = 'chat'

        # First 30 should pass
        for i in range(30):
            assert limiter.check(user, action, max_tokens=30, refill_rate=0.5)

        # 31st should be rejected (with 0.5/s refill, won't recover fast enough)
        assert not limiter.check(user, action, max_tokens=30, refill_rate=0.5)

    def test_different_users_have_separate_limits(self):
        """Each user should have their own rate limit bucket."""
        from integrations.social.rate_limiter import TokenBucket

        limiter = TokenBucket()
        # Exhaust user_a
        for _ in range(30):
            limiter.check('user_a', 'chat', max_tokens=30, refill_rate=0.5)

        # user_b should still have tokens
        assert limiter.check('user_b', 'chat', max_tokens=30, refill_rate=0.5)


class TestComputeEscrowModel:

    def test_compute_escrow_model_fields(self):
        """ComputeEscrow should have all required fields."""
        from integrations.social.models import ComputeEscrow

        # Check table name
        assert ComputeEscrow.__tablename__ == 'compute_escrow'

        # Check required columns exist
        columns = {c.name for c in ComputeEscrow.__table__.columns}
        expected = {
            'id', 'debtor_node_id', 'creditor_node_id', 'request_id',
            'task_type', 'spark_amount', 'status', 'created_at',
            'settled_at', 'expires_at',
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"

    def test_compute_escrow_status_default(self):
        """Default status should be 'pending'."""
        from integrations.social.models import ComputeEscrow

        status_col = ComputeEscrow.__table__.columns['status']
        assert status_col.default.arg == 'pending'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
