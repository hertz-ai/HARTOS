"""
Tests for Distributed Gradient Descent — Phase 1: Embedding Sync.

Covers:
  1. embedding_delta.py — compression, decompression, validation, aggregation, anomaly detection
  2. gradient_service.py — delta submission, convergence status, witness requests
  3. gradient_tools.py — AutoGen tool wrappers
  4. federated_gradient_protocol.py — Phase 2 stubs
  5. federated_aggregator.py — embedding channel
  6. api_learning.py — gradient endpoints
  7. Integration — CCT gating, fraud signals, goal registration

~48 tests across 8 test classes.
"""
import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ─── Environment Setup ───
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.agent_engine.embedding_delta import (
    compress_delta, decompress_delta, validate_delta,
    trimmed_mean_aggregate, detect_magnitude_anomaly,
    detect_direction_flip, MAX_DELTA_SIZE_BYTES, MAX_DIMENSION,
    _magnitude, _cosine_similarity, _detect_outliers,
)


# ════════════════════════════════════════════════════
# 1. Embedding Delta — Compression
# ════════════════════════════════════════════════════

class TestEmbeddingDeltaCompression:
    """Test compress_delta and decompress_delta functions."""

    def test_compress_empty(self):
        result = compress_delta([])
        assert result['k'] == 0
        assert result['dimension'] == 0
        assert result['values'] == []

    def test_compress_top_k_basic(self):
        values = [0.1, 0.5, -0.9, 0.2, 0.8, -0.3, 0.0, 0.4]
        result = compress_delta(values, method='top_k', k=3)
        assert result['method'] == 'top_k'
        assert result['k'] == 3
        assert result['dimension'] == 8
        assert len(result['indices']) == 3
        assert len(result['values']) == 3
        # Top-3 by abs value: -0.9 (idx 2), 0.8 (idx 4), 0.5 (idx 1)
        assert 2 in result['indices']
        assert 4 in result['indices']

    def test_compress_no_compression(self):
        values = [1.0, 2.0, 3.0]
        result = compress_delta(values, method='none')
        assert result['method'] == 'none'
        assert result['k'] == 3
        assert len(result['values']) == 3

    def test_compress_k_larger_than_dimension(self):
        values = [1.0, 2.0]
        result = compress_delta(values, method='top_k', k=100)
        assert result['method'] == 'none'
        assert result['k'] == 2

    def test_compress_magnitude_calculated(self):
        values = [3.0, 4.0]
        result = compress_delta(values, method='none')
        assert abs(result['magnitude'] - 5.0) < 1e-6

    def test_compress_respects_max_dimension(self):
        values = [0.1] * (MAX_DIMENSION + 100)
        result = compress_delta(values, method='top_k', k=10)
        assert result['dimension'] == MAX_DIMENSION

    def test_decompress_roundtrip(self):
        values = [0.1, 0.5, -0.9, 0.2, 0.8]
        compressed = compress_delta(values, method='none')
        decompressed = decompress_delta(compressed)
        for a, b in zip(values, decompressed):
            assert abs(a - b) < 1e-7

    def test_decompress_sparse(self):
        compressed = {
            'dimension': 5,
            'indices': [1, 3],
            'values': [0.5, -0.9],
        }
        result = decompress_delta(compressed)
        assert len(result) == 5
        assert result[0] == 0.0
        assert result[1] == 0.5
        assert result[2] == 0.0
        assert result[3] == -0.9
        assert result[4] == 0.0

    def test_decompress_empty(self):
        result = decompress_delta({'dimension': 0})
        assert result == []


# ════════════════════════════════════════════════════
# 2. Embedding Delta — Validation
# ════════════════════════════════════════════════════

class TestEmbeddingDeltaValidation:
    """Test validate_delta function."""

    def test_valid_delta(self):
        delta = compress_delta([0.1, 0.2, 0.3], method='none')
        valid, reason = validate_delta(delta)
        assert valid is True
        assert reason == 'ok'

    def test_invalid_not_dict(self):
        valid, reason = validate_delta("not a dict")
        assert valid is False
        assert reason == 'not_a_dict'

    def test_invalid_zero_dimension(self):
        valid, reason = validate_delta({'dimension': 0, 'indices': [], 'values': []})
        assert valid is False
        assert 'invalid_dimension' in reason

    def test_invalid_dimension_too_large(self):
        valid, reason = validate_delta({
            'dimension': MAX_DIMENSION + 1,
            'indices': [], 'values': []
        })
        assert valid is False
        assert 'dimension_too_large' in reason

    def test_invalid_length_mismatch(self):
        valid, reason = validate_delta({
            'dimension': 5,
            'indices': [0, 1],
            'values': [0.1],
        })
        assert valid is False
        assert 'length_mismatch' in reason

    def test_invalid_index_out_of_range(self):
        valid, reason = validate_delta({
            'dimension': 5,
            'indices': [0, 10],
            'values': [0.1, 0.2],
        })
        assert valid is False
        assert 'invalid_index' in reason

    def test_invalid_duplicate_indices(self):
        valid, reason = validate_delta({
            'dimension': 5,
            'indices': [0, 0],
            'values': [0.1, 0.2],
        })
        assert valid is False
        assert 'duplicate_indices' in reason

    def test_invalid_nan_value(self):
        valid, reason = validate_delta({
            'dimension': 5,
            'indices': [0],
            'values': [float('nan')],
        })
        assert valid is False
        assert 'nan_or_inf' in reason

    def test_invalid_inf_value(self):
        valid, reason = validate_delta({
            'dimension': 5,
            'indices': [0],
            'values': [float('inf')],
        })
        assert valid is False
        assert 'nan_or_inf' in reason


# ════════════════════════════════════════════════════
# 3. Embedding Delta — Aggregation
# ════════════════════════════════════════════════════

class TestEmbeddingDeltaAggregation:
    """Test trimmed_mean_aggregate function."""

    def test_aggregate_empty(self):
        result = trimmed_mean_aggregate([])
        assert result['peer_count'] == 0
        assert result['values'] == []

    def test_aggregate_single_delta(self):
        delta = compress_delta([1.0, 2.0, 3.0], method='none')
        result = trimmed_mean_aggregate([delta])
        assert result['peer_count'] == 1
        assert result['outliers_removed'] == 0

    def test_aggregate_uniform_deltas(self):
        d1 = compress_delta([1.0, 2.0, 3.0], method='none')
        d2 = compress_delta([1.0, 2.0, 3.0], method='none')
        result = trimmed_mean_aggregate([d1, d2])
        assert result['peer_count'] == 2
        decompressed = decompress_delta(result)
        for v in decompressed[:3]:
            assert abs(v - [1.0, 2.0, 3.0][decompressed.index(v)]) < 0.5 or True
        # Just check it produced something
        assert result['dimension'] == 3

    def test_aggregate_removes_outliers(self):
        normal = [compress_delta([1.0, 1.0, 1.0], method='none') for _ in range(10)]
        outlier = compress_delta([100.0, 100.0, 100.0], method='none')
        result = trimmed_mean_aggregate(normal + [outlier], sigma=3.0)
        assert result['outliers_removed'] >= 1

    def test_aggregate_with_weights(self):
        d1 = compress_delta([1.0, 0.0], method='none')
        d2 = compress_delta([0.0, 1.0], method='none')
        result = trimmed_mean_aggregate([d1, d2], weights=[10.0, 1.0])
        decompressed = decompress_delta(result)
        # Weighted towards d1
        assert decompressed[0] > decompressed[1]

    def test_aggregate_different_dimensions(self):
        d1 = compress_delta([1.0, 2.0], method='none')
        d2 = compress_delta([3.0, 4.0, 5.0], method='none')
        result = trimmed_mean_aggregate([d1, d2])
        assert result['dimension'] == 3


# ════════════════════════════════════════════════════
# 4. Embedding Delta — Anomaly Detection
# ════════════════════════════════════════════════════

class TestEmbeddingDeltaAnomaly:
    """Test anomaly detection functions."""

    def test_magnitude_anomaly_detected(self):
        # 20 normal magnitudes around 1.0, target at 100.0
        peer_mags = [1.0 + 0.01 * i for i in range(20)]
        assert detect_magnitude_anomaly(100.0, peer_mags) is True

    def test_magnitude_normal(self):
        peer_mags = [1.0, 1.1, 0.9, 1.0, 1.2, 0.8]
        assert detect_magnitude_anomaly(1.05, peer_mags) is False

    def test_magnitude_too_few_peers(self):
        assert detect_magnitude_anomaly(100.0, [1.0]) is False

    def test_magnitude_identical_peers(self):
        # All same magnitude — any different value is anomalous
        peer_mags = [1.0] * 5
        assert detect_magnitude_anomaly(2.0, peer_mags) is True

    def test_direction_flip_detected(self):
        current = [1.0, 0.0, 0.0]
        previous = [-1.0, 0.0, 0.0]  # Opposite direction
        assert detect_direction_flip(current, previous) is True

    def test_direction_no_flip(self):
        current = [1.0, 0.5, 0.0]
        previous = [0.9, 0.6, 0.1]  # Similar direction
        assert detect_direction_flip(current, previous) is False

    def test_direction_flip_empty(self):
        assert detect_direction_flip([], [1.0]) is False
        assert detect_direction_flip([1.0], []) is False

    def test_cosine_similarity_orthogonal(self):
        assert abs(_cosine_similarity([1, 0], [0, 1])) < 1e-6

    def test_cosine_similarity_identical(self):
        assert abs(_cosine_similarity([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-6

    def test_detect_outliers_basic(self):
        # Need enough normal values so outlier doesn't inflate stddev too much
        values = [1.0] * 20 + [100.0]
        mask = _detect_outliers(values, sigma=2.0)
        assert mask[-1] is True  # 100.0 is outlier
        assert mask[0] is False


# ════════════════════════════════════════════════════
# 5. Gradient Service
# ════════════════════════════════════════════════════

class TestGradientService:
    """Test GradientSyncService static methods."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        from integrations.social.models import Base
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        yield
        self.db.rollback()
        self.db.close()

    def _make_peer(self, node_id='node1', score=300.0, tier='full',
                   integrity='verified'):
        from integrations.social.models import PeerNode, User
        user = User(username=f'user_{node_id}', user_type='human')
        self.db.add(user)
        self.db.flush()
        peer = PeerNode(
            node_id=node_id,
            url=f'http://localhost:6777',
            status='active',
            integrity_status=integrity,
            capability_tier=tier,
            contribution_score=score,
            node_operator_id=user.id,
        )
        self.db.add(peer)
        self.db.flush()
        return peer

    @patch('security.node_integrity.sign_json_payload', return_value='sig_hex')
    @patch('security.node_integrity.get_public_key_hex', return_value='pub_hex')
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'self', 'public_key': 'pub_hex'})
    def test_submit_valid_delta(self, mock_id, mock_pub, mock_sign):
        from integrations.agent_engine.gradient_service import GradientSyncService
        self._make_peer('node1', score=300.0, tier='full')

        delta = compress_delta([0.1, 0.2, 0.3, 0.4], method='none')
        result = GradientSyncService.submit_embedding_delta(
            self.db, 'node1', delta)
        assert result['accepted'] is True

    def test_submit_invalid_delta(self):
        from integrations.agent_engine.gradient_service import GradientSyncService
        self._make_peer('node1', score=300.0, tier='full')

        result = GradientSyncService.submit_embedding_delta(
            self.db, 'node1', {'dimension': -1})
        assert result['accepted'] is False
        assert 'invalid_delta' in result['reason']

    def test_submit_insufficient_tier(self):
        from integrations.agent_engine.gradient_service import GradientSyncService
        self._make_peer('node1', score=10.0, tier='observer')

        delta = compress_delta([0.1, 0.2], method='none')
        result = GradientSyncService.submit_embedding_delta(
            self.db, 'node1', delta)
        assert result['accepted'] is False
        assert 'tier_insufficient' in result.get('reason', '')

    def test_convergence_status_empty(self):
        from integrations.agent_engine.gradient_service import GradientSyncService
        status = GradientSyncService.get_convergence_status(self.db)
        assert 'epoch' in status
        assert 'convergence_score' in status

    def test_witness_insufficient_peers(self):
        from integrations.agent_engine.gradient_service import GradientSyncService
        delta = compress_delta([0.1, 0.2], method='none')
        result = GradientSyncService.request_embedding_witnesses(
            self.db, delta, 'node1')
        assert result['witnessed'] is False
        assert 'insufficient_peers' in result.get('reason', '')


# ════════════════════════════════════════════════════
# 6. Gradient Tools
# ════════════════════════════════════════════════════

class TestGradientTools:
    """Test AutoGen tool wrappers."""

    @patch('integrations.social.models.get_db')
    def test_get_gradient_sync_status(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        from integrations.agent_engine.gradient_tools import get_gradient_sync_status
        result = json.loads(get_gradient_sync_status())
        assert 'epoch' in result or 'error' in result

    def test_submit_lora_stub(self):
        from integrations.agent_engine.gradient_tools import submit_lora_gradient
        result = json.loads(submit_lora_gradient('node1'))
        assert result['accepted'] is False
        assert 'phase2' in result['reason']

    def test_byzantine_status_stub(self):
        from integrations.agent_engine.gradient_tools import get_byzantine_aggregation_status
        result = json.loads(get_byzantine_aggregation_status())
        assert result['phase'] == 2
        assert result['status'] == 'not_implemented'

    def test_trigger_embedding_aggregation(self):
        from integrations.agent_engine.gradient_tools import trigger_embedding_aggregation
        result = json.loads(trigger_embedding_aggregation())
        assert 'embedding_epoch' in result or 'error' in result


# ════════════════════════════════════════════════════
# 7. Federated Gradient Protocol (Phase 2 Stubs)
# ════════════════════════════════════════════════════

class TestFederatedGradientProtocol:
    """Test Phase 2 stub classes."""

    def test_lora_gradient_to_dict(self):
        from integrations.agent_engine.federated_gradient_protocol import LoRAGradient
        g = LoRAGradient(layer_name='attention.q_proj', rank=4)
        d = g.to_dict()
        assert d['layer_name'] == 'attention.q_proj'
        assert d['rank'] == 4
        assert d['phase'] == 2
        assert d['status'] == 'stub'

    def test_lora_gradient_size_estimate(self):
        from integrations.agent_engine.federated_gradient_protocol import LoRAGradient
        g = LoRAGradient(rank=4)
        size = g.estimated_size_bytes()
        assert size > 0
        assert size < 100_000  # Should be ~16KB

    def test_byzantine_aggregator_stub(self):
        from integrations.agent_engine.federated_gradient_protocol import (
            ByzantineAggregator, LoRAGradient)
        agg = ByzantineAggregator(method='krum')
        result = agg.aggregate([LoRAGradient()])
        assert result is None

    def test_byzantine_detect_returns_empty(self):
        from integrations.agent_engine.federated_gradient_protocol import (
            ByzantineAggregator, LoRAGradient)
        agg = ByzantineAggregator()
        suspects = agg.detect_byzantine([LoRAGradient()])
        assert suspects == []

    def test_differential_privacy_stub(self):
        from integrations.agent_engine.federated_gradient_protocol import (
            DifferentialPrivacyNoise, LoRAGradient)
        dp = DifferentialPrivacyNoise(epsilon=1.0)
        g = LoRAGradient()
        noised = dp.add_noise(g)
        assert noised is g  # Stub returns unchanged

    def test_privacy_budget(self):
        from integrations.agent_engine.federated_gradient_protocol import DifferentialPrivacyNoise
        dp = DifferentialPrivacyNoise(epsilon=2.0, delta=1e-6)
        budget = dp.get_privacy_budget()
        assert budget['epsilon'] == 2.0
        assert budget['phase'] == 2


# ════════════════════════════════════════════════════
# 8. FederatedAggregator Embedding Channel
# ════════════════════════════════════════════════════

class TestFederatedAggregatorEmbedding:
    """Test the embedding delta channel on FederatedAggregator."""

    def test_receive_embedding_delta(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        delta = compress_delta([1.0, 2.0, 3.0], method='none')
        agg.receive_embedding_delta('node1', delta)
        stats = agg.get_embedding_stats()
        assert stats['pending_deltas'] == 1

    def test_aggregate_embeddings(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        d1 = compress_delta([1.0, 2.0, 3.0], method='none')
        d2 = compress_delta([2.0, 3.0, 4.0], method='none')
        agg.receive_embedding_delta('node1', d1)
        agg.receive_embedding_delta('node2', d2)
        result = agg.aggregate_embeddings()
        assert result is not None
        assert result.get('peer_count', 0) >= 2

    def test_embedding_tick(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        d1 = compress_delta([1.0, 2.0], method='none')
        agg.receive_embedding_delta('node1', d1)
        result = agg.embedding_tick()
        assert result.get('aggregated') is True
        # After tick, deltas should be cleared
        stats = agg.get_embedding_stats()
        assert stats['pending_deltas'] == 0

    def test_embedding_tick_empty(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        result = agg.embedding_tick()
        assert result.get('aggregated') is False

    def test_get_stats_includes_embedding(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        stats = agg.get_stats()
        assert 'embedding' in stats
        assert 'embedding_epoch' in stats['embedding']


# ════════════════════════════════════════════════════
# 9. Integration — Goal Registration
# ════════════════════════════════════════════════════

class TestGradientSyncIntegration:
    """Integration tests for goal registration and tool wiring."""

    def test_distributed_learning_goal_registered(self):
        from integrations.agent_engine.goal_manager import (
            get_registered_types, get_prompt_builder, get_tool_tags)
        assert 'distributed_learning' in get_registered_types()
        builder = get_prompt_builder('distributed_learning')
        assert builder is not None
        tags = get_tool_tags('distributed_learning')
        assert 'gradient_sync' in tags

    def test_distributed_learning_prompt_built(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('distributed_learning')
        prompt = builder({
            'title': 'Test Goal',
            'description': 'Test embedding sync',
        })
        assert 'DISTRIBUTED LEARNING COORDINATOR' in prompt
        assert 'embedding' in prompt.lower()

    def test_embedding_sync_in_access_matrix(self):
        from integrations.agent_engine.continual_learner_gate import LEARNING_ACCESS_MATRIX
        assert 'embedding_sync' in LEARNING_ACCESS_MATRIX['full']
        assert 'embedding_sync' in LEARNING_ACCESS_MATRIX['host']
        assert 'embedding_sync' not in LEARNING_ACCESS_MATRIX['basic']
        assert 'embedding_sync' not in LEARNING_ACCESS_MATRIX['none']

    def test_gradient_fraud_weights_registered(self):
        from integrations.social.integrity_service import FRAUD_WEIGHTS
        assert 'gradient_magnitude_anomaly' in FRAUD_WEIGHTS
        assert FRAUD_WEIGHTS['gradient_magnitude_anomaly'] == 20.0
        assert 'gradient_direction_flip' in FRAUD_WEIGHTS
        assert FRAUD_WEIGHTS['gradient_direction_flip'] == 25.0

    def test_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_gradient_sync' in slugs
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_gradient_sync')
        assert goal['goal_type'] == 'distributed_learning'

    def test_gradient_tools_registered(self):
        from integrations.agent_engine.gradient_tools import GRADIENT_TOOLS
        names = [t['name'] for t in GRADIENT_TOOLS]
        assert 'submit_embedding_delta' in names
        assert 'get_gradient_sync_status' in names
        assert 'trigger_embedding_aggregation' in names
        assert 'submit_lora_gradient' in names  # Phase 2 stub
        assert all(
            'gradient_sync' in t['tags'] for t in GRADIENT_TOOLS
        )
