"""
Compute Config Resolution — Single code path for node compute policy.

Resolves effective compute policy with precedence: env vars > DB > defaults.
Provider identity (electricity_rate_kwh, cause_alignment) lives on PeerNode,
not here — this module only handles local policy settings.
"""
import logging
import os
import time

logger = logging.getLogger('hevolve_social')

# ─── Defaults ───

_DEFAULTS = {
    'compute_policy': 'local_preferred',      # local_only | local_preferred | any
    'hive_compute_policy': 'local_preferred',
    'max_hive_gpu_pct': 50,
    'allow_metered_for_hive': False,
    'metered_daily_limit_usd': 0.0,
    'offered_gpu_hours_per_day': 0.0,
    'accept_thought_experiments': True,
    'accept_frontier_training': False,
    'auto_settle': True,
    'min_settlement_spark': 10,
}

# ─── Cache ───

_policy_cache = {}    # {node_id: (timestamp, policy_dict)}
_CACHE_TTL = 30       # seconds


def get_compute_policy(node_id: str = None) -> dict:
    """Resolve effective compute policy. Precedence: env > DB > defaults.

    Returns dict with: compute_policy, hive_compute_policy, allow_metered_for_hive,
    metered_daily_limit_usd, max_hive_gpu_pct, offered_gpu_hours_per_day,
    accept_thought_experiments, accept_frontier_training, auto_settle, min_settlement_spark.

    NOTE: electricity_rate_kwh and cause_alignment come from PeerNode, not here.
    """
    cache_key = node_id or '__default__'
    now = time.time()

    # Check cache
    if cache_key in _policy_cache:
        cached_ts, cached_policy = _policy_cache[cache_key]
        if now - cached_ts < _CACHE_TTL:
            return cached_policy

    # Start from defaults
    policy = dict(_DEFAULTS)

    # Layer 2: DB (NodeComputeConfig row, if exists)
    if node_id:
        try:
            from integrations.social.models import db_session, NodeComputeConfig
            with db_session() as db:
                config = db.query(NodeComputeConfig).filter_by(
                    node_id=node_id).first()
                if config:
                    for key in _DEFAULTS:
                        val = getattr(config, key, None)
                        if val is not None:
                            policy[key] = val
        except Exception as e:
            logger.debug(f"compute_config: DB lookup skipped: {e}")

    # Layer 3: Env vars (highest precedence)
    env_map = {
        'HEVOLVE_COMPUTE_POLICY': ('compute_policy', str),
        'HEVOLVE_HIVE_COMPUTE_POLICY': ('hive_compute_policy', str),
        'HEVOLVE_MAX_HIVE_GPU_PCT': ('max_hive_gpu_pct', int),
        'HEVOLVE_ALLOW_METERED_HIVE': ('allow_metered_for_hive', _parse_bool),
        'HEVOLVE_METERED_DAILY_LIMIT': ('metered_daily_limit_usd', float),
    }
    for env_var, (key, converter) in env_map.items():
        val = os.environ.get(env_var)
        if val is not None:
            try:
                policy[key] = converter(val)
            except (ValueError, TypeError):
                pass

    # Cache and return
    _policy_cache[cache_key] = (now, policy)
    return policy


def invalidate_cache(node_id: str = None):
    """Invalidate cached policy for a node (or all if None)."""
    if node_id:
        _policy_cache.pop(node_id, None)
    else:
        _policy_cache.clear()


def _parse_bool(val: str) -> bool:
    return val.lower() in ('true', '1', 'yes')
