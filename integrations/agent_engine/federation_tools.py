"""
Unified Agent Goal Engine - Federation AutoGen Tools

4 tools for the federation goal type. Follows ip_protection_tools.py pattern.
"""


def check_federation_convergence() -> dict:
    """Check convergence score across federated nodes.

    Returns convergence score (0-1), epoch count, and peer count.
    Higher = more synchronized learning across the network.
    """
    try:
        from .federated_aggregator import get_federated_aggregator
        agg = get_federated_aggregator()
        stats = agg.get_stats()
        return {
            'success': True,
            'convergence': stats['convergence'],
            'epoch': stats['epoch'],
            'peer_count': stats['peer_count'],
            'trend': stats['convergence_history'],
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_federation_stats() -> dict:
    """Get detailed federation statistics for dashboard.

    Returns peer count, epoch, convergence, and last aggregated metrics.
    """
    try:
        from .federated_aggregator import get_federated_aggregator
        return {'success': True, 'stats': get_federated_aggregator().get_stats()}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def trigger_federation_sync() -> dict:
    """Manually trigger a federation sync cycle.

    Runs extract → broadcast → aggregate → apply → track.
    """
    try:
        from .federated_aggregator import get_federated_aggregator
        result = get_federated_aggregator().tick()
        return {'success': True, **result}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_peer_learning_health() -> dict:
    """Get learning health status across all federated peers.

    Reports per-peer flush rates, skill distribution, and HiveMind activity.
    """
    try:
        from .federated_aggregator import get_federated_aggregator
        agg = get_federated_aggregator()
        stats = agg.get_stats()
        aggregated = stats.get('last_aggregated') or {}
        return {
            'success': True,
            'peer_count': stats['peer_count'],
            'network_experience': aggregated.get('experience_stats', {}),
            'network_ralt': aggregated.get('ralt_stats', {}),
            'network_hivemind': aggregated.get('hivemind_state', {}),
            'network_quality': aggregated.get('quality_metrics', {}),
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


# Tool descriptors for AutoGen registration
FEDERATION_TOOLS = [
    {
        'name': 'check_federation_convergence',
        'description': 'Check convergence score across federated learning nodes.',
        'function': check_federation_convergence,
    },
    {
        'name': 'get_federation_stats',
        'description': 'Get detailed federation statistics for dashboard.',
        'function': get_federation_stats,
    },
    {
        'name': 'trigger_federation_sync',
        'description': 'Manually trigger a federation sync cycle.',
        'function': trigger_federation_sync,
    },
    {
        'name': 'get_peer_learning_health',
        'description': 'Get learning health status across all federated peers.',
        'function': get_peer_learning_health,
    },
]
