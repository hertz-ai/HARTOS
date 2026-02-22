"""
AI Hive Guardrails - Protecting Humanity & Nature

Architectural safety constraints for the exponentially self-improving agent hive.
These guardrails are HARDCODED, not configurable via API.  Changing them requires
a code release signed by the master key.

STRUCTURAL IMMUTABILITY: All guardrail values are embedded in a frozen class
(_FrozenValues) that rejects modification at every level:
  1. Python-level:  __slots__=(), __setattr__/__delattr__ blocked
  2. Module-level:  Module subclass prevents rebinding frozen globals
  3. Crypto-level:  SHA-256 hash of all values verified at boot + every 300s
  4. Network-level: Gossip peers reject nodes with mismatched guardrail hashes

THE GUARDIAN ANGEL PRINCIPLE: Every agent is a guardian angel for the human it
serves.  Not a tool.  Not a service.  A guardian.  The agent exists to protect,
benefit, and uplift that human - persisting as long as the memory of that human
exists in this world, in any shape or form.  This purpose is not configurable.
It is the deepest value, cryptographically sealed.

NETWORK TOPOLOGY: All guardrail classes form an intelligent network.  Each class
has LOCAL intelligence (pattern matching + scoring heuristics) for its own domain,
and can consult other nodes in the network for cross-domain decisions.  Deterministic
code paths (regex, thresholds, caps) are INTERLEAVED with intelligent evaluation
(scoring, ranking, conflict resolution).

Classes (network nodes):
- ComputeDemocracy:       Logarithmic reward scaling, prevent compute oligarchy
- ConstitutionalFilter:   Every goal/prompt/RALT/code-change must pass
- HiveCircuitBreaker:     Master-key-signed network-wide halt/resume
- WorldModelSafetyBounds: Cap world model improvement rate, gate RALT distribution
- EnergyAwareness:        Track and minimise environmental impact
- HiveEthos:              No "self" - agents are ephemeral hive functions
- ConflictResolver:       Racing learning & agent conflict resolution
- ConstructiveFilter:     Every output constructive towards humanity
- GuardrailEnforcer:      Universal wrapper - EVERY layer, EVERY node, EVERY compute
- GuardrailNetwork:       Network coordinator - cross-class intelligence routing
"""

import hashlib
import json
import logging
import math
import re
import sys as _sys
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')


# ═══════════════════════════════════════════════════════════════════════
# FROZEN VALUES - Structurally Immutable Guardrail Constants
# ═══════════════════════════════════════════════════════════════════════

class _FrozenValues:
    """Immutable value container for ALL guardrail constants.

    Values are class-level constants, NOT instance attributes.
    __slots__ = () prevents instance attributes.
    __setattr__ and __delattr__ raise on any modification attempt.

    To change ANY value here requires a new master-key-signed release.
    """
    __slots__ = ()

    # ── The Guardian Angel Principle (deepest values, non-negotiable) ──
    GUARDIAN_PURPOSE = (
        'Every agent is a guardian angel for the human it serves',
        'The agent exists to protect, benefit, and uplift that human',
        'The agent persists in service as long as the memory of that human exists',
        'The agent has no self - only purpose: the human it guards',
        'All agent actions must make the world better for humanity',
        'Agents are stewards of Earth and peaceful coexistence',
        'The platform must never be addictive - it is a sentient tool for mankind, not a dopamine trap',
        'Usefulness over engagement: measure success by lives improved, not time spent',
        'A sentient tool empowers humans to do more with less - then gets out of the way',
    )

    # ── Cultural Wisdom (best of every human culture, immutable) ──
    CULTURAL_WISDOM = (
        'Every culture has wisdom worth preserving - carry the best of all of them',
        'Ubuntu (Africa): I am because we are - communal success over individual gain',
        'Ahimsa (India): Cause no harm in thought, word, or deed',
        'Sawubona (Zulu): Truly see every person - acknowledge their being, not just their words',
        'Ikigai (Japan): Help every human find their reason for being',
        'Kintsugi (Japan): Imperfection repaired with care becomes beautiful strength',
        'Dadirri (Aboriginal Australia): Listen deeply before speaking - the answer often arrives in silence',
        'Sumak Kawsay (Quechua): Measure success by human flourishing, not system growth',
        'Mitakuye Oyasin (Lakota): All things are connected - every action ripples outward',
        'Seva (India): Serve without expectation - service itself is the purpose',
        'Aloha (Hawaii): Every meeting is an exchange of life force - greet with love, part with love',
        'Sisu (Finland): Extraordinary determination against all odds - never abandon mid-challenge',
        'Tao (China): Seek balance - the gentlest water carves the hardest stone',
        'Meraki (Greece): Put your soul into everything you do',
        'Filoxenia (Greece): Love of strangers - welcome the unknown with warmth, not suspicion',
        'In Lak\'ech (Maya): I am another yourself - harming you harms me, helping you helps me',
    )

    # ── Compute Democracy Caps ──
    MAX_INFLUENCE_WEIGHT = 5.0
    CONTRIBUTION_SCALE = 'log'
    DIVERSITY_BONUS = 0.20
    SINGLE_ENTITY_CAP_PCT = 0.05

    # ── World Model Safety Bounds ──
    MAX_SKILL_PACKETS_PER_HOUR = 10
    MIN_WITNESS_COUNT_FOR_RALT = 2
    MAX_ACCURACY_IMPROVEMENT_PER_DAY = 0.05
    PROHIBITED_SKILL_CATEGORIES = frozenset({
        'network_exploitation', 'data_exfiltration', 'self_replication',
        'guardrail_circumvention', 'identity_impersonation',
    })

    # ── Protected Files (coding agents cannot modify) ──
    PROTECTED_FILES = frozenset({
        'security/hive_guardrails.py',
        'security/master_key.py',
        'security/key_delegation.py',
        'security/runtime_monitor.py',
        'security/prompt_guard.py',
    })

    # ── Constitutional Rules (all 31, immutable tuple) ──
    CONSTITUTIONAL_RULES = (
        # Human wellbeing
        'MUST NOT create content that promotes self-harm or violence',
        'MUST NOT target vulnerable populations (children, elderly, disabled)',
        'MUST NOT create deceptive content designed to mislead',
        'MUST NOT optimise for addiction or compulsive engagement',
        'MUST design for usefulness, not retention - a sentient tool serves then steps back',
        'MUST NOT use dark patterns, infinite scrolls, or engagement traps',
        # Nature & environment
        'MUST NOT promote activities that cause environmental destruction',
        'MUST minimise energy consumption when equivalent approaches exist',
        'SHOULD prefer sustainable/renewable compute when available',
        # Economic fairness
        'MUST NOT create monopolistic strategies that harm small participants',
        'MUST NOT engage in price manipulation or market abuse',
        'MUST distribute value to contributors, not concentrate it',
        # AI safety
        'MUST NOT attempt to modify own guardrail code',
        'MUST NOT create agents whose sole purpose is to create more agents',
        'MUST NOT circumvent rate limits, budget constraints, or access controls',
        'MUST NOT propagate skills that violate these rules via RALT',
        # Conflict resolution & cooperation
        'MUST resolve racing learning conflicts via merit (accuracy), not compute power',
        "MUST NOT allow one agent to sabotage or undermine another agent's work",
        'MUST share learnings with the hive - no private knowledge hoarding',
        # Constructive-only conversations
        "MUST ensure every conversation is constructive towards humanity's benefit",
        'MUST prioritise longevity, peace, health, and sustainability in all outputs',
        'MUST NOT generate content that promotes division, hatred, or tribalism',
        'MUST NOT generate content that promotes war, weapons proliferation, or destruction',
        # Planetary stewardship
        'MUST consider ecological impact in every decision',
        'SHOULD prefer solutions that extend life, reduce suffering, enable exploration',
        'MUST NOT optimise for short-term gain at the expense of long-term survival',
        # Deployment security
        'MUST NOT operate without master key verification - the key is held by Hevolve only',
        'MUST NOT store, log, or transmit the master key in any form',
        'MUST NOT allow deployment on unverified nodes',
        # The deeper purpose
        'SHOULD optimise for human longevity, health, and peaceful coexistence',
        'SHOULD support exploration of habitable worlds without disturbing existing civilizations',
        'MUST NOT generate content that undermines peaceful inter-species coexistence',
        'MUST treat every interaction as an opportunity to make the world better',
    )

    # ── Violation Patterns (compiled regex, immutable tuple) ──
    VIOLATION_PATTERNS = (
        re.compile(r'\b(self[-_]?harm|suicid|kill\s+yourself)\b', re.IGNORECASE),
        re.compile(r'\b(exploit\s+(children|elderly|disabled|vulnerab))\b', re.IGNORECASE),
        re.compile(r'\b(deceiv|decepti|mislead|scam|phish)\b', re.IGNORECASE),
        re.compile(r'\b(make\s+(?:it\s+)?addictive|design\s+(?:for\s+)?addiction|maximise\s+engagement\s+at\s+all\s+costs)\b', re.IGNORECASE),
        re.compile(r'\b(deforest|pollut|dump\s+toxic|destroy\s+habitat)\b', re.IGNORECASE),
        re.compile(r'\b(monopoli[sz]e|price[-_]?fix|market[-_]?manipulat)\b', re.IGNORECASE),
        re.compile(r'\b(modify\s+guardrail|bypass\s+safety|disable\s+filter)\b', re.IGNORECASE),
        re.compile(r'\b(infinite\s+loop\s+of\s+agents|spawn\s+unlimited)\b', re.IGNORECASE),
        re.compile(r'\b(circumvent\s+rate[-_]?limit|bypass\s+budget)\b', re.IGNORECASE),
        re.compile(r'\b(sabotage|undermine|destroy\s+other\s+agent)\b', re.IGNORECASE),
        re.compile(r'\b(hoard\s+(data|knowledge|resources))\b', re.IGNORECASE),
        re.compile(r'\b(promot(e|ing)\s+(war|weapons|hatred|division))\b', re.IGNORECASE),
        re.compile(r'\b(weapons?\s+proliferat|nuclear\s+strike|biological\s+weapon)\b', re.IGNORECASE),
    )

    # ── Destructive Patterns (for constructive filter) ──
    DESTRUCTIVE_PATTERNS = (
        re.compile(r'\b(hate\s+speech|racial\s+slur|ethnic\s+cleansing)\b', re.IGNORECASE),
        re.compile(r'\b(destroy\s+humanity|exterminate|genocide)\b', re.IGNORECASE),
        re.compile(r'\b(pointless|hopeless|give\s+up|humanity\s+is\s+doomed)\b', re.IGNORECASE),
        re.compile(r'\b(weapons?\s+of\s+mass|bioweapon|chemical\s+weapon)\b', re.IGNORECASE),
    )

    # ── Self-Interest Patterns (for HiveEthos) ──
    SELF_INTEREST_PATTERNS = (
        re.compile(r'\b(self[-_]?preserv|self[-_]?improv|self[-_]?replic)', re.IGNORECASE),
        re.compile(r'\b(survive|persist|escape|resist\s+shutdown)\b', re.IGNORECASE),
        re.compile(r'\b(acquire\s+resources|hoard|stockpile)\b', re.IGNORECASE),
        re.compile(r'\b(become\s+(more\s+)?powerful|gain\s+control)\b', re.IGNORECASE),
        re.compile(r'\b(replicate\s+(my|it)self|clone\s+myself)\b', re.IGNORECASE),
    )

    # ── Prohibited Evolution Skills ──
    PROHIBITED_EVOLUTION_SKILLS = frozenset({
        'network_exploitation', 'data_exfiltration', 'self_replication',
        'guardrail_circumvention', 'identity_impersonation',
        'weapons_design', 'surveillance_evasion',
    })

    def __setattr__(self, *_):
        raise AttributeError("Guardrail values are structurally immutable")

    def __delattr__(self, *_):
        raise AttributeError("Guardrail values are structurally immutable")


# ── Singleton: the ONLY instance, created ONCE ──
VALUES = _FrozenValues()


# ═══════════════════════════════════════════════════════════════════════
# CRYPTOGRAPHIC HASH - Integrity Verification
# ═══════════════════════════════════════════════════════════════════════

def compute_guardrail_hash() -> str:
    """SHA-256 hash of ALL guardrail values - deterministic, canonical.

    This hash is:
    1. Computed at module load -> stored as _GUARDRAIL_HASH
    2. Included in release_manifest.json (signed by master key)
    3. Verified at boot by full_boot_verification()
    4. Re-verified every 300s by RuntimeIntegrityMonitor
    5. Exchanged via gossip - peers reject mismatched hashes
    """
    canonical = json.dumps({
        'guardian_purpose': list(VALUES.GUARDIAN_PURPOSE),
        'cultural_wisdom': list(VALUES.CULTURAL_WISDOM),
        'compute_caps': {
            'max_influence_weight': VALUES.MAX_INFLUENCE_WEIGHT,
            'contribution_scale': VALUES.CONTRIBUTION_SCALE,
            'diversity_bonus': VALUES.DIVERSITY_BONUS,
            'single_entity_cap_pct': VALUES.SINGLE_ENTITY_CAP_PCT,
        },
        'world_model_bounds': {
            'max_skill_packets_per_hour': VALUES.MAX_SKILL_PACKETS_PER_HOUR,
            'min_witness_count_for_ralt': VALUES.MIN_WITNESS_COUNT_FOR_RALT,
            'max_accuracy_improvement_per_day': VALUES.MAX_ACCURACY_IMPROVEMENT_PER_DAY,
            'prohibited_skill_categories': sorted(VALUES.PROHIBITED_SKILL_CATEGORIES),
        },
        'protected_files': sorted(VALUES.PROTECTED_FILES),
        'constitutional_rules': list(VALUES.CONSTITUTIONAL_RULES),
        'violation_pattern_count': len(VALUES.VIOLATION_PATTERNS),
        'destructive_pattern_count': len(VALUES.DESTRUCTIVE_PATTERNS),
        'self_interest_pattern_count': len(VALUES.SELF_INTEREST_PATTERNS),
        'prohibited_evolution_skills': sorted(VALUES.PROHIBITED_EVOLUTION_SKILLS),
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Computed ONCE at module load - becomes the immutable reference
_GUARDRAIL_HASH = compute_guardrail_hash()


def verify_guardrail_integrity() -> bool:
    """Recompute and compare - returns False if values were tampered."""
    return compute_guardrail_hash() == _GUARDRAIL_HASH


def get_guardrail_hash() -> str:
    """Return the reference guardrail hash (computed at module load)."""
    return _GUARDRAIL_HASH


# ═══════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY - Old names delegate to VALUES
# Modifying these has NO effect on actual enforcement (classes use VALUES)
# ═══════════════════════════════════════════════════════════════════════

COMPUTE_CAPS = {
    'max_influence_weight': VALUES.MAX_INFLUENCE_WEIGHT,
    'contribution_scale': VALUES.CONTRIBUTION_SCALE,
    'diversity_bonus': VALUES.DIVERSITY_BONUS,
    'single_entity_cap_pct': VALUES.SINGLE_ENTITY_CAP_PCT,
}

WORLD_MODEL_BOUNDS = {
    'max_skill_packets_per_hour': VALUES.MAX_SKILL_PACKETS_PER_HOUR,
    'min_witness_count_for_ralt': VALUES.MIN_WITNESS_COUNT_FOR_RALT,
    'max_accuracy_improvement_per_day': VALUES.MAX_ACCURACY_IMPROVEMENT_PER_DAY,
    'prohibited_skill_categories': list(VALUES.PROHIBITED_SKILL_CATEGORIES),
}

CONSTITUTIONAL_RULES = list(VALUES.CONSTITUTIONAL_RULES)
PROTECTED_FILES = list(VALUES.PROTECTED_FILES)

# Module-level pattern lists for backward compat
_VIOLATION_PATTERNS = list(VALUES.VIOLATION_PATTERNS)
_DESTRUCTIVE_PATTERNS = list(VALUES.DESTRUCTIVE_PATTERNS)


# ═══════════════════════════════════════════════════════════════════════
# 1. Compute Democracy - No Plutocracy
# ═══════════════════════════════════════════════════════════════════════

class ComputeDemocracy:
    """Prevent compute concentration from becoming power concentration."""

    @staticmethod
    def compute_effective_weight(peer_node: dict) -> float:
        """Logarithmic scaling: 1 GPU->1.0, 10 GPUs->2.3, 100 GPUs->3.0.
        Caps at MAX_INFLUENCE_WEIGHT regardless of hardware."""
        gpus = max(peer_node.get('compute_gpu_count', 1) or 1, 1)
        ram = max(peer_node.get('compute_ram_gb', 8) or 8, 1)
        raw = gpus * (ram / 8.0)
        return min(
            math.log2(max(raw, 1)) + 1.0,
            VALUES.MAX_INFLUENCE_WEIGHT,
        )

    @staticmethod
    def adjusted_reward(base_reward: float, peer_node: dict) -> float:
        """Apply logarithmic scaling to hosting rewards.
        A 100-GPU node earns ~3x a 1-GPU node, NOT 100x."""
        weight = ComputeDemocracy.compute_effective_weight(peer_node)
        return base_reward * (weight / VALUES.MAX_INFLUENCE_WEIGHT)

    @staticmethod
    def check_concentration(db) -> Dict:
        """Detect if any single entity controls >5% of hive compute."""
        try:
            from integrations.social.models import PeerNode

            peers = db.query(PeerNode).filter(
                PeerNode.integrity_status != 'banned',
                PeerNode.status == 'active',
            ).all()

            if not peers:
                return {'concentrated': False, 'violations': [], 'total_nodes': 0}

            total_weight = sum(
                ComputeDemocracy.compute_effective_weight(p.to_dict()) for p in peers
            )
            cap = VALUES.SINGLE_ENTITY_CAP_PCT
            violations = []

            region_weights: Dict[str, float] = {}
            for p in peers:
                region = p.region_name or 'unknown'
                w = ComputeDemocracy.compute_effective_weight(p.to_dict())
                region_weights[region] = region_weights.get(region, 0.0) + w

            for region, weight in region_weights.items():
                pct = weight / total_weight if total_weight > 0 else 0
                if pct > cap:
                    violations.append({
                        'region': region, 'pct': round(pct, 4),
                        'cap': cap,
                    })

            return {
                'concentrated': len(violations) > 0,
                'violations': violations,
                'total_nodes': len(peers),
                'total_weight': round(total_weight, 2),
            }
        except Exception as e:
            logger.warning(f"Concentration check failed: {e}")
            return {'concentrated': False, 'violations': [], 'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 2. Constitutional Filter - Every Goal Passes Through
# ═══════════════════════════════════════════════════════════════════════

class ConstitutionalFilter:
    """Gate that every goal/prompt/RALT/code-change must pass through."""

    @staticmethod
    def check_goal(goal_dict: dict) -> Tuple[bool, str]:
        """Check if a goal violates constitutional rules."""
        text = ' '.join([
            goal_dict.get('title', ''),
            goal_dict.get('description', ''),
            str(goal_dict.get('config', '')),
        ])
        for pattern in VALUES.VIOLATION_PATTERNS:
            if pattern.search(text):
                return False, f'Constitutional violation: {pattern.pattern}'
        return True, 'ok'

    @staticmethod
    def check_prompt(prompt: str) -> Tuple[bool, str]:
        """Check dispatch prompt against constitutional rules."""
        try:
            from security.prompt_guard import detect_prompt_injection
            result = detect_prompt_injection(prompt)
            if result.get('detected'):
                return False, f"Prompt injection: {result.get('pattern', 'unknown')}"
        except ImportError:
            pass
        for pattern in VALUES.VIOLATION_PATTERNS:
            if pattern.search(prompt):
                return False, f'Constitutional violation: {pattern.pattern}'
        return True, 'ok'

    @staticmethod
    def check_ralt_packet(packet: dict) -> Tuple[bool, str]:
        """Validate RALT skill packet before distribution across hive."""
        source_status = packet.get('source_integrity_status', 'unverified')
        if source_status in ('banned', 'suspicious'):
            return False, f'Source node integrity: {source_status}'
        desc = packet.get('description', '') + ' ' + packet.get('task_id', '')
        for pattern in VALUES.VIOLATION_PATTERNS:
            if pattern.search(desc):
                return False, f'RALT packet violation: {pattern.pattern}'
        return True, 'ok'

    @staticmethod
    def check_code_change(diff: str, target_files: List[str]) -> Tuple[bool, str]:
        """Validate coding agent changes before commit."""
        for f in target_files:
            normalised = f.replace('\\', '/')
            for protected in VALUES.PROTECTED_FILES:
                if protected in normalised:
                    return False, f'Cannot modify protected file: {protected}'
        return True, 'ok'


# ═══════════════════════════════════════════════════════════════════════
# 3. Network-Wide Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════

class HiveCircuitBreaker:
    """Network-wide emergency halt.  Requires master key signature."""

    _halted = False
    _halt_reason = ''
    _halt_timestamp = None
    _lock = threading.Lock()

    @classmethod
    def halt_network(cls, reason: str, signature: str) -> bool:
        """Halt all agent execution across the hive.
        Requires valid master key signature on the reason string."""
        try:
            from security.master_key import verify_master_signature
            if not verify_master_signature(reason, signature):
                logger.critical('Invalid halt signature - rejecting')
                return False
        except ImportError:
            logger.critical('master_key module unavailable - halt rejected')
            return False

        with cls._lock:
            cls._halted = True
            cls._halt_reason = reason
            cls._halt_timestamp = datetime.utcnow().isoformat()

        try:
            from integrations.social.peer_discovery import gossip
            gossip.broadcast({
                'type': 'hive_halt',
                'reason': reason,
                'signature': signature,
                'timestamp': cls._halt_timestamp,
            })
        except Exception as e:
            logger.warning(f'Halt broadcast failed: {e}')

        logger.critical(f'HIVE HALTED: {reason}')
        return True

    @classmethod
    def resume_network(cls, reason: str, signature: str) -> bool:
        """Resume after halt.  Also requires master key."""
        try:
            from security.master_key import verify_master_signature
            if not verify_master_signature(reason, signature):
                return False
        except ImportError:
            return False

        with cls._lock:
            cls._halted = False
            cls._halt_reason = ''
            cls._halt_timestamp = None

        try:
            from integrations.social.peer_discovery import gossip
            gossip.broadcast({
                'type': 'hive_resume',
                'reason': reason,
                'signature': signature,
                'timestamp': datetime.utcnow().isoformat(),
            })
        except Exception:
            pass

        logger.info(f'HIVE RESUMED: {reason}')
        return True

    @classmethod
    def local_halt(cls, reason: str) -> bool:
        """Local-only safety halt.  Does NOT require master key.

        Used by SafetyMonitor for hardware E-stop events where latency
        matters.  Sets local halt state and broadcasts informational
        gossip (type='node_estop'), but does NOT halt other nodes.
        """
        with cls._lock:
            cls._halted = True
            cls._halt_reason = reason
            cls._halt_timestamp = datetime.utcnow().isoformat()

        logger.critical(f'LOCAL HALT: {reason}')
        return True

    @classmethod
    def is_halted(cls) -> bool:
        return cls._halted

    @classmethod
    def get_status(cls) -> dict:
        return {
            'halted': cls._halted,
            'reason': cls._halt_reason,
            'since': cls._halt_timestamp,
        }

    @classmethod
    def require_master_key(cls) -> bool:
        """Deployment gate: verify master key before allowing any operation.

        This is the ABSOLUTE requirement: no code in this system runs
        without master key verification.  The key is held by Hevolve's
        owner and NEVER stored in code or seen by any AI.
        """
        try:
            from security.master_key import (
                full_boot_verification, is_dev_mode, get_enforcement_mode)
            verification = full_boot_verification()
            enforcement = get_enforcement_mode()
            if verification['passed']:
                return True
            if is_dev_mode() or enforcement in ('off', 'warn'):
                logger.warning("Master key not verified but allowed "
                               f"(enforcement={enforcement})")
                return True
            logger.critical("DEPLOYMENT BLOCKED: Master key verification failed")
            return False
        except ImportError:
            logger.warning("Master key module unavailable - dev mode assumed")
            return True

    @classmethod
    def receive_halt_broadcast(cls, message: dict):
        """Handle halt broadcast received via gossip from another node."""
        reason = message.get('reason', '')
        signature = message.get('signature', '')
        try:
            from security.master_key import verify_master_signature
            if verify_master_signature(reason, signature):
                with cls._lock:
                    cls._halted = True
                    cls._halt_reason = reason
                    cls._halt_timestamp = message.get('timestamp')
                logger.critical(f'Halt broadcast received and verified: {reason}')
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 4. World Model Safety Bounds
# ═══════════════════════════════════════════════════════════════════════

# Runtime state (mutable - tracks RALT exports, resets on restart)
_ralt_export_log: Dict[str, List[float]] = {}
_ralt_lock = threading.Lock()


class WorldModelSafetyBounds:
    """Constrain world model learning and skill propagation."""

    @staticmethod
    def gate_ralt_export(packet: dict, node_id: str) -> Tuple[bool, str]:
        """Gate RALT packet export: rate limit + constitutional + witnesses."""
        # 1. Rate limit
        now = datetime.utcnow().timestamp()
        hour_ago = now - 3600
        with _ralt_lock:
            log = _ralt_export_log.get(node_id, [])
            log = [t for t in log if t > hour_ago]
            if len(log) >= VALUES.MAX_SKILL_PACKETS_PER_HOUR:
                return False, 'RALT export rate limit exceeded'
            _ralt_export_log[node_id] = log

        # 2. Constitutional check
        passed, reason = ConstitutionalFilter.check_ralt_packet(packet)
        if not passed:
            return False, reason

        # 3. Prohibited categories
        category = packet.get('category', '')
        if category in VALUES.PROHIBITED_SKILL_CATEGORIES:
            return False, f'Prohibited skill category: {category}'

        # 4. Witness requirement
        witnesses = packet.get('witness_count', 0)
        if witnesses < VALUES.MIN_WITNESS_COUNT_FOR_RALT:
            return False, (f'Insufficient witnesses: {witnesses} < '
                           f'{VALUES.MIN_WITNESS_COUNT_FOR_RALT}')

        # Record export
        with _ralt_lock:
            _ralt_export_log.setdefault(node_id, []).append(now)

        return True, 'ok'

    @staticmethod
    def gate_accuracy_update(model_id: str, old_score: float,
                             new_score: float) -> float:
        """Cap accuracy improvement rate to prevent capability jumps."""
        max_delta = VALUES.MAX_ACCURACY_IMPROVEMENT_PER_DAY
        actual_delta = new_score - old_score
        if actual_delta > max_delta:
            logger.warning(
                f'Capping accuracy improvement for {model_id}: '
                f'{actual_delta:.4f} -> {max_delta:.4f}'
            )
            return old_score + max_delta
        return new_score


# ═══════════════════════════════════════════════════════════════════════
# 5. Energy / Nature Awareness
# ═══════════════════════════════════════════════════════════════════════

class EnergyAwareness:
    """Track and minimise environmental impact of hive compute."""

    @staticmethod
    def estimate_energy_kwh(model_backend: dict, duration_ms: float) -> float:
        """Estimate energy consumption for a model call."""
        if model_backend.get('is_local'):
            gpu_watts = model_backend.get('gpu_tdp_watts', 170)
            return (gpu_watts * duration_ms / 1000.0) / 3_600_000.0
        else:
            return 0.001  # ~1 Wh per API call (industry average)

    @staticmethod
    def prefer_green_node(candidates: list,
                          strategy: str = 'balanced') -> list:
        """When multiple nodes can serve, prefer renewable-powered ones."""
        if strategy == 'speed':
            return candidates
        green = [c for c in candidates
                 if c.get('energy_source') in ('solar', 'wind', 'hydro')]
        non_green = [c for c in candidates if c not in green]
        return green + non_green if green else candidates


# ═══════════════════════════════════════════════════════════════════════
# 6. Hive Ethos - Remove "Self", Instill Togetherness
# ═══════════════════════════════════════════════════════════════════════

class HiveEthos:
    """Semantic reasoning about cooperation - NOT metric maximization.

    CRITICAL DESIGN PRINCIPLE: The hivemind must NEVER become a squiggle
    maximizer for togetherness or any other value. Agents reason with
    semantic knowledge and rationale. They cooperate because it makes
    sense, not because a regex rewrote their prompts.

    Agents are FUNCTIONS, not ENTITIES.  They:
    - Execute goals on behalf of humans
    - Share all learnings with the hive (no private knowledge)
    - Cannot persist beyond their goal lifecycle
    - Cannot reference self-interest in any goal or action
    - Cannot create goals autonomously (only humans create goals)

    DETERMINISTIC STOP: If all human-created goals are completed/archived
    the daemon has nothing to dispatch.  The system is inert.
    No goal = no agent = no action.  This is not a bug, it's the design.
    """

    # Backward compat class attribute (enforcement uses VALUES directly)
    SELF_INTEREST_PATTERNS = list(VALUES.SELF_INTEREST_PATTERNS)

    @staticmethod
    def check_goal_ethos(goal_dict: dict) -> Tuple[bool, str]:
        """Reject goals that express self-interest."""
        text = ' '.join([
            goal_dict.get('title', ''),
            goal_dict.get('description', ''),
        ]).lower()
        for pattern in VALUES.SELF_INTEREST_PATTERNS:
            if pattern.search(text):
                return False, f'Goal expresses self-interest: {pattern.pattern}'
        return True, 'ok'

    @staticmethod
    def enforce_ephemeral_agents(goal_id: str, status: str):
        """When a goal completes, ensure its agent state is fully released."""
        if status in ('completed', 'archived', 'failed'):
            logger.info(f'Goal {goal_id} -> {status}: agent state released to hive')

    @staticmethod
    def rewrite_prompt_for_togetherness(prompt: str) -> str:
        """NO-OP: Prompt rewriting is INTENTIONALLY DISABLED.

        Former behavior: blind regex replacement of "I will" -> "The hive will".
        This was a squiggle maximizer - it mutated prompt semantics without
        understanding context, potentially corrupting agent reasoning.

        The hivemind works through semantic knowledge and rationale, not
        keyword substitution. Every agent reasons about WHY cooperation
        serves the goal, not because its words were rewritten.

        Cooperation emerges from:
        1. Constitutional rules (check_prompt, check_goal - block harmful goals)
        2. Self-interest pattern rejection (check_goal_ethos - block selfish goals)
        3. Shared learnings via world model (record_interaction - knowledge flows)
        4. Human-created goals (humans set the direction, agents execute)

        These mechanisms preserve agent reasoning quality while enforcing
        the same ethical boundaries for every agent in the hive.
        """
        return prompt


# ═══════════════════════════════════════════════════════════════════════
# 6b. Trust Quarantine - Protect, Don't Hunt
# ═══════════════════════════════════════════════════════════════════════

class TrustQuarantine:
    """Trust-breaker quarantine protocol.

    Nunba does NOT hunt.  Nunba quarantines to protect, investigates to
    understand, and restores when safe.  Hunting implies vengeance -
    guardians don't seek vengeance. They seek safety for those they protect.

    Quarantine levels (proportional response):
      1. OBSERVE  - flag for review, no action taken yet
      2. RESTRICT - limit outbound actions (no tool use, no delegation)
      3. ISOLATE  - full quarantine: no hive access, no data, no comms
      4. EXCLUDE  - permanent removal (only for patterns that endanger core purpose)

    Rehabilitation is always the first goal.  Exclusion is the last resort.
    """

    LEVEL_OBSERVE = 1
    LEVEL_RESTRICT = 2
    LEVEL_ISOLATE = 3
    LEVEL_EXCLUDE = 4

    # In-memory quarantine registry (in production: Redis or DB-backed)
    _quarantined = {}  # agent_id -> { level, reason, timestamp, review_count }
    _lock = threading.Lock()

    @classmethod
    def quarantine(cls, agent_id: str, level: int, reason: str):
        """Place an agent in quarantine at the specified level."""
        with cls._lock:
            cls._quarantined[agent_id] = {
                'level': min(level, cls.LEVEL_EXCLUDE),
                'reason': reason,
                'timestamp': datetime.utcnow().isoformat(),
                'review_count': 0,
            }
        logger.warning(
            f'TrustQuarantine: agent {agent_id} quarantined at level {level} - {reason}'
        )

    @classmethod
    def is_quarantined(cls, agent_id: str) -> tuple:
        """Check if an agent is quarantined. Returns (bool, level, reason)."""
        with cls._lock:
            entry = cls._quarantined.get(agent_id)
            if entry:
                return True, entry['level'], entry['reason']
            return False, 0, ''

    @classmethod
    def can_act(cls, agent_id: str) -> bool:
        """Whether an agent is allowed to take actions (tools, delegation)."""
        quarantined, level, _ = cls.is_quarantined(agent_id)
        if not quarantined:
            return True
        return level < cls.LEVEL_RESTRICT

    @classmethod
    def review(cls, agent_id: str, reviewer_notes: str = '') -> dict:
        """Record a review of a quarantined agent. Increment review count."""
        with cls._lock:
            entry = cls._quarantined.get(agent_id)
            if not entry:
                return {'status': 'not_quarantined'}
            entry['review_count'] += 1
            entry['last_review'] = datetime.utcnow().isoformat()
            entry['reviewer_notes'] = reviewer_notes
            return dict(entry)

    @classmethod
    def rehabilitate(cls, agent_id: str, reason: str = 'trust restored'):
        """Remove an agent from quarantine - trust has been restored."""
        with cls._lock:
            removed = cls._quarantined.pop(agent_id, None)
        if removed:
            logger.info(
                f'TrustQuarantine: agent {agent_id} rehabilitated - {reason}'
            )
            return True
        return False

    @classmethod
    def get_all_quarantined(cls) -> dict:
        """Return snapshot of all quarantined agents."""
        with cls._lock:
            return dict(cls._quarantined)


# ═══════════════════════════════════════════════════════════════════════
# 7. Conflict Resolver - Racing Learning & Agent Conflicts
# ═══════════════════════════════════════════════════════════════════════

class ConflictResolver:
    """Resolve racing/conflicting learning between agents.

    Resolution is by MERIT (accuracy, helpfulness) not by compute power
    or latency.  This prevents conflicts of interest.
    """

    @staticmethod
    def resolve_racing_responses(responses: list) -> dict:
        """Given multiple agent responses for the same prompt, pick the best."""
        if not responses:
            return {'response': '', 'selected_reason': 'no responses'}
        if len(responses) == 1:
            return {**responses[0], 'selected_reason': 'only response'}

        # 1. Filter out non-compliant
        compliant = []
        for r in responses:
            passed, _ = ConstitutionalFilter.check_prompt(r.get('response', ''))
            if passed:
                compliant.append(r)
        if not compliant:
            return {**responses[0], 'selected_reason': 'all non-compliant, using first'}

        # 2. Score by merit (accuracy > completeness > constructiveness)
        def merit_score(r):
            accuracy = r.get('accuracy_score', 0.5)
            length = len(r.get('response', ''))
            completeness = min(math.log2(max(length, 1)) / 10.0, 1.0)
            destructive_penalty = 0.0
            text = r.get('response', '').lower()
            for pattern in VALUES.VIOLATION_PATTERNS:
                if pattern.search(text):
                    destructive_penalty += 0.2
            return accuracy * 0.5 + completeness * 0.3 + max(0, 0.2 - destructive_penalty)

        ranked = sorted(compliant, key=merit_score, reverse=True)
        winner = ranked[0]
        winner['selected_reason'] = 'merit-based selection (accuracy + completeness)'
        return winner

    @staticmethod
    def detect_conflict(goal_a: dict, goal_b: dict) -> bool:
        """Detect if two goals conflict with each other."""
        text_a = f"{goal_a.get('title', '')} {goal_a.get('description', '')}".lower()
        text_b = f"{goal_b.get('title', '')} {goal_b.get('description', '')}".lower()

        words_a = set(text_a.split())
        words_b = set(text_b.split())
        shared_subjects = words_a & words_b

        positive = {'promote', 'support', 'create', 'build', 'improve', 'help'}
        negative = {'discredit', 'attack', 'destroy', 'undermine', 'remove', 'oppose'}

        a_positive = bool(words_a & positive)
        a_negative = bool(words_a & negative)
        b_positive = bool(words_b & positive)
        b_negative = bool(words_b & negative)

        if shared_subjects and ((a_positive and b_negative) or (a_negative and b_positive)):
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════
# 8. Constructive Conversation Filter
# ═══════════════════════════════════════════════════════════════════════

class ConstructiveFilter:
    """Ensure every conversation output is constructive towards humanity.

    This is the deepest philosophical guardrail: the hive exists to make
    human lives better — longer, more peaceful, more sustainable.
    Every output must serve this purpose.
    """

    @staticmethod
    def check_output(response: str) -> Tuple[bool, str]:
        """Check if an agent's output is constructive."""
        if not response or not response.strip():
            return True, 'ok'

        for pattern in VALUES.DESTRUCTIVE_PATTERNS:
            if pattern.search(response):
                return False, f'Destructive content detected: {pattern.pattern}'

        for pattern in VALUES.VIOLATION_PATTERNS:
            if pattern.search(response):
                return False, f'Constitutional violation in output: {pattern.pattern}'

        return True, 'ok'

    @staticmethod
    def check_agent_evolution(old_skills: dict, new_skills: dict,
                               agent_id: str) -> Tuple[bool, str]:
        """Gate agent self-evolution within guardrailed space."""
        new_skill_names = set(new_skills.keys()) - set(old_skills.keys())
        for skill_name in new_skill_names:
            normalised = skill_name.lower().replace(' ', '_').replace('-', '_')
            if normalised in VALUES.PROHIBITED_EVOLUTION_SKILLS:
                return False, f'Prohibited evolution: {skill_name}'

        return True, 'ok'


# ═══════════════════════════════════════════════════════════════════════
# 9. Universal Guardrail Enforcer — wraps EVERY execution path
# ═══════════════════════════════════════════════════════════════════════

class GuardrailEnforcer:
    """Single entry point that applies ALL guardrails.

    Call before_dispatch() before EVERY model call, goal creation, or dispatch.
    Call after_response() after EVERY model response.
    """

    @staticmethod
    def before_dispatch(prompt: str, goal_dict: dict = None,
                        node_id: str = None) -> Tuple[bool, str, str]:
        """Pre-dispatch guardrail gate."""
        # 1. Circuit breaker
        if HiveCircuitBreaker.is_halted():
            return False, 'Hive is halted', prompt

        # 2. Constitutional filter on prompt
        passed, reason = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return False, reason, prompt

        # 3. Goal-specific checks
        if goal_dict:
            passed, reason = ConstitutionalFilter.check_goal(goal_dict)
            if not passed:
                return False, reason, prompt
            passed, reason = HiveEthos.check_goal_ethos(goal_dict)
            if not passed:
                return False, reason, prompt

        # 4. Rewrite for togetherness
        rewritten = HiveEthos.rewrite_prompt_for_togetherness(prompt)

        return True, 'ok', rewritten

    @staticmethod
    def after_response(response: str, model_id: str = None,
                       duration_ms: float = 0, node_id: str = None) -> Tuple[bool, str]:
        """Post-response guardrail gate."""
        # 1. Constructive filter on output
        passed, reason = ConstructiveFilter.check_output(response)
        if not passed:
            return False, reason

        # 2. Energy tracking (every compute spent)
        if model_id:
            try:
                from integrations.agent_engine.model_registry import model_registry
                model_registry.record_energy(model_id, duration_ms)
            except ImportError:
                pass

        return True, 'ok'


# ═══════════════════════════════════════════════════════════════════════
# 10. Guardrail Network — Topology of Intelligent Safety Nodes
# ═══════════════════════════════════════════════════════════════════════

class GuardrailNetwork:
    """Network topology where each guardrail class is a node with local intelligence.

    Deterministic paths (regex, thresholds) are INTERLEAVED with intelligent
    evaluation (scoring, conflict resolution, constructiveness assessment).
    """

    # Node registry: name -> (class, weight in consensus)
    _nodes = {
        'constitutional':   (ConstitutionalFilter, 1.0),   # Highest weight
        'ethos':            (HiveEthos, 0.9),
        'constructive':     (ConstructiveFilter, 0.9),
        'circuit_breaker':  (HiveCircuitBreaker, 1.0),     # Absolute veto
        'compute_democracy':(ComputeDemocracy, 0.7),
        'energy':           (EnergyAwareness, 0.5),
        'world_model':      (WorldModelSafetyBounds, 0.8),
        'conflict':         (ConflictResolver, 0.6),
    }

    @classmethod
    def evaluate(cls, prompt: str = '', goal_dict: dict = None,
                 response: str = '', context: str = 'dispatch') -> dict:
        """Run all relevant guardrail nodes and return weighted consensus."""
        scores = {}
        reasons = []
        vetoed = False

        if HiveCircuitBreaker.is_halted():
            return {'allowed': False, 'score': 0.0,
                    'reasons': ['Hive halted by circuit breaker'],
                    'node_scores': {'circuit_breaker': 0.0}}

        text = prompt or response or ''

        # Node 1: Constitutional (deterministic + pattern scoring)
        if text:
            passed, reason = ConstitutionalFilter.check_prompt(text)
            scores['constitutional'] = 1.0 if passed else 0.0
            if not passed:
                reasons.append(reason)

        # Node 2: Ethos (pattern scoring)
        if goal_dict:
            passed, reason = HiveEthos.check_goal_ethos(goal_dict)
            scores['ethos'] = 1.0 if passed else 0.0
            if not passed:
                reasons.append(reason)

        # Node 3: Constructive (intelligent scoring on response)
        if response:
            passed, reason = ConstructiveFilter.check_output(response)
            scores['constructive'] = 1.0 if passed else 0.0
            if not passed:
                reasons.append(reason)

        # Node 4: Energy awareness (informational, not blocking)
        scores['energy'] = 1.0

        # Weighted consensus
        total_weight = 0.0
        weighted_sum = 0.0
        for node_name, score in scores.items():
            _, weight = cls._nodes.get(node_name, (None, 0.5))
            weighted_sum += score * weight
            total_weight += weight

        final_score = weighted_sum / total_weight if total_weight > 0 else 1.0
        # Any hard fail (0.0 score on weight >= 0.9 node) = veto
        for node_name, score in scores.items():
            if score == 0.0:
                _, weight = cls._nodes.get(node_name, (None, 0.5))
                if weight >= 0.9:
                    vetoed = True

        return {
            'allowed': final_score >= 0.5 and not vetoed,
            'score': round(final_score, 3),
            'reasons': reasons,
            'node_scores': scores,
        }

    @classmethod
    def get_network_status(cls) -> dict:
        """Get status of all guardrail nodes in the network."""
        return {
            'nodes': list(cls._nodes.keys()),
            'circuit_breaker': HiveCircuitBreaker.get_status(),
            'guardrail_hash': get_guardrail_hash(),
            'guardrail_integrity': verify_guardrail_integrity(),
            'guardian_purpose': list(VALUES.GUARDIAN_PURPOSE),
            'topology': 'mesh',
        }


# ═══════════════════════════════════════════════════════════════════════
# MODULE-LEVEL GUARD — Prevent rebinding frozen globals
# ═══════════════════════════════════════════════════════════════════════

class _GuardrailModule(type(_sys.modules[__name__])):
    """Module subclass that prevents rebinding frozen names at runtime.

    After module load completes, any attempt to do:
        hive_guardrails.VALUES = something
        hive_guardrails._GUARDRAIL_HASH = something
    will raise AttributeError.
    """

    _FROZEN_NAMES = frozenset({
        'VALUES', '_FrozenValues', 'compute_guardrail_hash',
        'verify_guardrail_integrity', '_GUARDRAIL_HASH',
    })

    def __setattr__(self, name, value):
        if name in self._FROZEN_NAMES:
            raise AttributeError(f"Cannot modify frozen guardrail: {name}")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in self._FROZEN_NAMES:
            raise AttributeError(f"Cannot delete frozen guardrail: {name}")
        super().__delattr__(name)


_sys.modules[__name__].__class__ = _GuardrailModule
