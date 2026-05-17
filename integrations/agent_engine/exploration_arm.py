"""Non-deterministic exploration arm for the auto-evolve loop.

Closes the search-space axis of the recursive self-improvement loop.
The baseline pipeline (``autoevolve_code_tools``) generates candidates
via an LLM hypothesis — deterministic given the prompt.  That exploits
what the LLM already knows but never reaches candidates the LLM hasn't
considered.

This module adds an ε-greedy exploration arm: with probability ε a
candidate is sampled stochastically from a pool weighted by
AgentAttribution usage priors; with probability 1-ε the caller runs
the standard exploit (LLM hypothesis) path.

Feature flag: ``HEVOLVE_RSI_EXPLORE=1`` (off by default).  When off,
``select_strategy()`` always returns ``'exploit'`` and the arm is
inert.  The promoted candidate still passes RSI-1 (Constitutional) +
RSI-2 (AgentBaselineService delta) gates inside
``autoevolve_code_tools.commit_improvement`` — exploration is
ADDITIVE; safety is non-negotiable.

Tunables:
    HEVOLVE_RSI_EPSILON   (float in [0,1], default 0.1)  exploration rate.
"""
import logging
import os
import random
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve.rsi_explore')


_DEFAULT_EPSILON = 0.1


def _flag_enabled() -> bool:
    return os.environ.get('HEVOLVE_RSI_EXPLORE', '').lower() in (
        '1', 'true', 'yes', 'on'
    )


def _epsilon() -> float:
    raw = os.environ.get('HEVOLVE_RSI_EPSILON')
    if raw is None:
        return _DEFAULT_EPSILON
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EPSILON
    # Clamp — no point in an epsilon outside [0,1].
    return max(0.0, min(1.0, v))


def select_strategy(rng: Optional[random.Random] = None) -> str:
    """Return ``'explore'`` or ``'exploit'``.

    ``'exploit'`` always when the feature flag is off — callers should
    treat any value other than ``'explore'`` as "use the deterministic
    LLM hypothesis path".
    """
    if not _flag_enabled():
        return 'exploit'
    r = rng if rng is not None else random
    return 'explore' if r.random() < _epsilon() else 'exploit'


def weighted_sample(candidates: List[str],
                     weights: Optional[List[float]] = None,
                     rng: Optional[random.Random] = None) -> Optional[str]:
    """Pick one candidate weighted by ``weights``.

    Falls back to a uniform pick when weights are missing, mismatched
    in length, or sum to zero.  Returns ``None`` iff ``candidates`` is
    empty.  Pure function — no I/O, no global state.
    """
    if not candidates:
        return None
    r = rng if rng is not None else random
    if not weights or len(weights) != len(candidates):
        return r.choice(candidates)
    non_neg = [max(0.0, float(w)) for w in weights]
    if sum(non_neg) <= 0:
        return r.choice(candidates)
    return r.choices(candidates, weights=non_neg, k=1)[0]


def usage_priors_from_attribution(tool_name: str
                                   ) -> Tuple[List[str], List[float]]:
    """Pull recent-use priors from AgentAttribution for ``tool_name``.

    Returns ``(candidate_keys, weights)``.  Fails open to ``([], [])``
    when AgentAttribution is unavailable or returns an unexpected
    shape — the caller then falls back to its own pool or to the
    exploit path.
    """
    try:
        from integrations.agent_engine.agent_attribution import (
            AgentAttributionOrchestrator,
        )
    except Exception as e:
        logger.debug('exploration_arm: AgentAttribution unavailable (%s)', e)
        return [], []
    try:
        orch = AgentAttributionOrchestrator()
        snapshot_fn = getattr(orch, 'get_usage_snapshot', None)
        stats = snapshot_fn() if callable(snapshot_fn) else {}
    except Exception as e:
        logger.debug('exploration_arm: usage snapshot failed (%s)', e)
        return [], []
    if not isinstance(stats, dict):
        return [], []
    related = stats.get(tool_name) or {}
    if not isinstance(related, dict):
        return [], []
    keys: List[str] = []
    weights: List[float] = []
    for k, v in related.items():
        if not k:
            continue
        keys.append(str(k))
        try:
            weights.append(max(0.0, float(v)))
        except (TypeError, ValueError):
            weights.append(0.0)
    return keys, weights


def pick_exploration_candidate(
    tool_name: str,
    fallback_candidates: Optional[List[str]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Sample one mutation key using usage priors.

    Preference order:
        1. AgentAttribution stats for ``tool_name`` (≥ 1 entry).
        2. caller-supplied ``fallback_candidates`` (uniform).
        3. ``None`` — caller MUST fall back to the exploit path.
    """
    keys, weights = usage_priors_from_attribution(tool_name)
    if keys:
        return weighted_sample(keys, weights, rng=rng)
    if fallback_candidates:
        return weighted_sample(fallback_candidates, rng=rng)
    return None


def describe_state() -> dict:
    """Return a small dict for dashboards / diagnostics."""
    return {
        'enabled': _flag_enabled(),
        'epsilon': _epsilon(),
    }
