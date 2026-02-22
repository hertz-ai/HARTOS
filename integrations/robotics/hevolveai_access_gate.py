"""
Hevolve-Core Access Gate — Feature-level access control.

Gates specific Hevolve-Core features by:
  1. Certificate tier (central, regional, local)
  2. CCT capability (e.g. 'embodied_ai', 'sensor_fusion')
  3. Installation integrity (verified manifest)

Features that require gating:
  - 'in_process':     Direct Python calls (requires integrity verification)
  - 'sensor_fusion':  Native sensor fusion pipeline
  - 'navigation':     Path planning + SLAM
  - 'manipulation':   Arm control + IK
  - 'learning':       Continuous learning pipeline
  - 'hivemind':       Distributed intelligence

Reuses the existing PrivateRepoAccessService pattern.
"""
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger('hevolve_security')


def check_hevolveai_access(feature: str) -> Dict:
    """Check if this node can use a specific Hevolve-Core feature.

    Args:
        feature: Feature name (e.g. 'in_process', 'sensor_fusion')

    Returns:
        {
            'allowed': bool,
            'reason': str,
            'tier': str,
            'integrity_verified': bool,
        }
    """
    result: Dict = {
        'allowed': False,
        'reason': '',
        'tier': 'unknown',
        'integrity_verified': False,
    }

    # 1. Check certificate tier
    tier = _get_node_tier()
    result['tier'] = tier

    # 2. Check installation integrity
    integrity = _check_integrity()
    result['integrity_verified'] = integrity

    # 3. Feature-specific rules
    feature_rules = _FEATURE_RULES.get(feature)
    if feature_rules is None:
        result['allowed'] = True
        result['reason'] = 'no restrictions on this feature'
        return result

    # Tier check
    min_tier = feature_rules.get('min_tier', 'local')
    if not _tier_meets_minimum(tier, min_tier):
        result['reason'] = (
            f"feature '{feature}' requires tier '{min_tier}' or above, "
            f"current tier: '{tier}'"
        )
        return result

    # Integrity check (some features require verified install)
    if feature_rules.get('requires_integrity') and not integrity:
        result['reason'] = (
            f"feature '{feature}' requires verified Hevolve-Core installation"
        )
        return result

    # CCT capability check
    required_cct = feature_rules.get('required_cct')
    if required_cct and not _has_cct_capability(required_cct):
        result['reason'] = (
            f"feature '{feature}' requires CCT capability: {required_cct}"
        )
        return result

    result['allowed'] = True
    result['reason'] = 'access granted'
    return result


# ── Feature Rules ────────────────────────────────────────────────

_FEATURE_RULES: Dict[str, Dict] = {
    'in_process': {
        'min_tier': 'local',
        'requires_integrity': True,
        'required_cct': None,
    },
    'sensor_fusion': {
        'min_tier': 'local',
        'requires_integrity': False,
        'required_cct': 'embodied_ai',
    },
    'navigation': {
        'min_tier': 'local',
        'requires_integrity': False,
        'required_cct': 'embodied_ai',
    },
    'manipulation': {
        'min_tier': 'local',
        'requires_integrity': False,
        'required_cct': 'embodied_ai',
    },
    'learning': {
        'min_tier': 'local',
        'requires_integrity': False,
        'required_cct': 'learning',
    },
    'hivemind': {
        'min_tier': 'regional',
        'requires_integrity': True,
        'required_cct': 'hivemind',
    },
}

# Tier hierarchy (index = rank, higher = more privileged)
_TIER_RANK = ['observer', 'local', 'regional', 'central']


def _tier_meets_minimum(current: str, minimum: str) -> bool:
    """Check if current tier meets minimum requirement."""
    try:
        current_rank = _TIER_RANK.index(current)
    except ValueError:
        current_rank = 1  # Default to 'local' if unknown

    try:
        min_rank = _TIER_RANK.index(minimum)
    except ValueError:
        min_rank = 1

    return current_rank >= min_rank


def _get_node_tier() -> str:
    """Get this node's certificate tier."""
    try:
        from security.key_delegation import get_node_tier
        return get_node_tier()
    except Exception:
        return 'local'


def _check_integrity() -> bool:
    """Check if Hevolve-Core installation passes integrity verification."""
    try:
        from security.source_protection import SourceProtectionService
        result = SourceProtectionService.verify_hevolveai_integrity()
        return result.get('verified', False)
    except Exception:
        return False


def _has_cct_capability(capability: str) -> bool:
    """Check if this node has a valid CCT with the required capability.

    Fail-closed in production.  Dev-mode escape: HEVOLVE_DEV_MODE=true.
    """
    try:
        from integrations.agent_engine.continual_learner_gate import (
            ContinualLearnerGate,
        )
        gate = ContinualLearnerGate()
        status = gate.check_access()
        if not status.get('has_valid_cct'):
            return False
        capabilities = status.get('capabilities', [])
        return capability in capabilities
    except Exception:
        # Fail-closed in production; dev mode bypasses for test environments
        if os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
            return True
        return False
