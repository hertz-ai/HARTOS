"""
Gradient Sync Agent Tools — AutoGen tools for distributed embedding sync.

4 Phase 1 tools + 2 Phase 2 stubs for LoRA gradient sync.
Tier 2 tools (agent_engine context). Same pattern as learning_tools.py.

Intelligence is earned through contribution. Every compute cycle donated
makes the hive smarter.
"""
import json
import logging

logger = logging.getLogger('hevolve_social')


def submit_embedding_delta(node_id: str,
                           values: str = '[]',
                           dimension: int = 64,
                           compression_k: int = 32) -> str:
    """Submit a compressed embedding delta for distributed aggregation.

    Args:
        node_id: The submitting node's ID.
        values: JSON-encoded list of float values (the raw delta).
        dimension: Embedding dimension.
        compression_k: Number of top-k components to keep.
    """
    try:
        from integrations.social.models import get_db
        from .gradient_service import GradientSyncService
        from .embedding_delta import compress_delta

        raw_values = json.loads(values) if isinstance(values, str) else values
        if not isinstance(raw_values, list):
            return json.dumps({'error': 'values must be a JSON list of floats'})

        delta = compress_delta(raw_values, method='top_k', k=compression_k)

        db = get_db()
        try:
            result = GradientSyncService.submit_embedding_delta(
                db, node_id, delta)
            if result.get('accepted'):
                db.commit()
            return json.dumps(result)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_gradient_sync_status() -> str:
    """Get the current distributed embedding sync convergence status."""
    try:
        from integrations.social.models import get_db
        from .gradient_service import GradientSyncService

        db = get_db()
        try:
            status = GradientSyncService.get_convergence_status(db)
            return json.dumps(status)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def request_embedding_witnesses(node_id: str, delta_json: str = '{}') -> str:
    """Request peer witnesses for an embedding delta submission."""
    try:
        from integrations.social.models import get_db
        from .gradient_service import GradientSyncService

        delta = json.loads(delta_json) if isinstance(delta_json, str) else delta_json

        db = get_db()
        try:
            result = GradientSyncService.request_embedding_witnesses(
                db, delta, node_id)
            return json.dumps(result)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def trigger_embedding_aggregation() -> str:
    """Manually trigger embedding delta aggregation round."""
    try:
        from .federated_aggregator import get_federated_aggregator

        aggregator = get_federated_aggregator()
        result = aggregator.embedding_tick()
        return json.dumps(result)
    except Exception as e:
        return json.dumps({'error': str(e)})


# ─── Phase 2 Stubs: LoRA Gradient Sync ───

def submit_lora_gradient(node_id: str,
                         layer_name: str = '',
                         gradient_json: str = '{}') -> str:
    """[Phase 2 Stub] Submit a LoRA gradient for federated aggregation.

    LoRA gradients are sparse, rank-4, ~4KB/layer. Byzantine-resilient
    aggregation via Krum or coordinate-wise median.

    Not yet implemented — returns stub response.
    """
    return json.dumps({
        'accepted': False,
        'reason': 'phase2_not_implemented',
        'description': 'LoRA gradient sync is Phase 2. '
                       'Use submit_embedding_delta for Phase 1 embedding sync.',
    })


def get_byzantine_aggregation_status() -> str:
    """[Phase 2 Stub] Get Byzantine-resilient aggregation status.

    Will use Krum or coordinate-wise median for LoRA gradient aggregation.

    Not yet implemented — returns stub response.
    """
    return json.dumps({
        'status': 'not_implemented',
        'phase': 2,
        'description': 'Byzantine aggregation will be available in Phase 2. '
                       'Phase 1 uses trimmed-mean for embedding sync.',
    })


# ─── Tool Registration ───

GRADIENT_TOOLS = [
    {
        'name': 'submit_embedding_delta',
        'func': submit_embedding_delta,
        'description': 'Submit a compressed embedding delta for distributed aggregation',
        'tags': ['gradient_sync'],
    },
    {
        'name': 'get_gradient_sync_status',
        'func': get_gradient_sync_status,
        'description': 'Get distributed embedding sync convergence status',
        'tags': ['gradient_sync'],
    },
    {
        'name': 'request_embedding_witnesses',
        'func': request_embedding_witnesses,
        'description': 'Request peer witnesses for an embedding delta',
        'tags': ['gradient_sync'],
    },
    {
        'name': 'trigger_embedding_aggregation',
        'func': trigger_embedding_aggregation,
        'description': 'Manually trigger embedding delta aggregation round',
        'tags': ['gradient_sync'],
    },
    {
        'name': 'submit_lora_gradient',
        'func': submit_lora_gradient,
        'description': '[Phase 2] Submit LoRA gradient for federated aggregation',
        'tags': ['gradient_sync'],
    },
    {
        'name': 'get_byzantine_aggregation_status',
        'func': get_byzantine_aggregation_status,
        'description': '[Phase 2] Get Byzantine-resilient aggregation status',
        'tags': ['gradient_sync'],
    },
]
