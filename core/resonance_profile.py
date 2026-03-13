"""
UserResonanceProfile — Per-user continuous personality tuning parameters.

Each user_id gets a learned profile with continuous floats (0.0-1.0)
instead of binary switches. Stored as JSON at:
  agent_data/resonance/{user_id}_resonance.json

Reuses agent_data/ storage pattern from helper_ledger.py (DRY).
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Default tuning parameters — neutral midpoint for all dimensions
DEFAULT_TUNING = {
    'formality_score': 0.5,       # 0.0=very casual, 1.0=very formal
    'verbosity_score': 0.5,       # 0.0=terse, 1.0=very detailed
    'warmth_score': 0.6,          # 0.0=professional distance, 1.0=very warm
    'pace_score': 0.5,            # 0.0=slow/thorough, 1.0=fast/action-oriented
    'technical_depth': 0.5,       # 0.0=simple, 1.0=highly technical
    'encouragement_level': 0.6,   # 0.0=matter-of-fact, 1.0=very encouraging
    'humor_receptivity': 0.3,     # 0.0=serious, 1.0=playful
    'autonomy_preference': 0.5,   # 0.0=ask before acting, 1.0=act autonomously
}

# Ordered dimension keys — index-stable for matrix operations
TUNING_DIM_KEYS = list(DEFAULT_TUNING.keys())
TUNING_DIM_COUNT = len(TUNING_DIM_KEYS)  # 8

RESONANCE_STORAGE_DIR = os.environ.get(
    'RESONANCE_STORAGE_DIR',
    os.path.join('agent_data', 'resonance')
)


@dataclass
class UserResonanceProfile:
    """Per-user continuous tuning profile — the 'frequency' for this user."""

    user_id: str = ""

    # Multi-dimensional tuning (continuous floats 0.0-1.0)
    tuning: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TUNING))

    # Biometric signatures (optional, stored as list of floats)
    face_embedding: Optional[List[float]] = None
    voice_embedding: Optional[List[float]] = None
    face_enrollment_count: int = 0
    voice_enrollment_count: int = 0

    # Interaction patterns learned
    avg_message_length: float = 0.0
    avg_response_time_ms: float = 0.0
    vocabulary_complexity: float = 0.5  # 0=simple, 1=complex
    topic_preferences: Dict[str, float] = field(default_factory=dict)

    # Oscillation tracking (HARTOS detects, HevolveAI corrects)
    tuning_history: List[List[float]] = field(default_factory=list)  # last 20 snapshots
    gradient_active: bool = False  # True when oscillation detected, flags HevolveAI

    # Per-user EMA alpha (None = use global default from RESONANCE_EMA_ALPHA)
    ema_alpha: Optional[float] = None

    # Metadata
    total_interactions: int = 0
    resonance_confidence: float = 0.0  # 0.0=untuned, 1.0=highly tuned
    last_interaction_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserResonanceProfile':
        """Deserialize, merging any missing tuning keys from defaults."""
        if 'tuning' in data:
            for key, default in DEFAULT_TUNING.items():
                data['tuning'].setdefault(key, default)
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def get_tuning(self, key: str) -> float:
        return self.tuning.get(key, DEFAULT_TUNING.get(key, 0.5))

    def set_tuning(self, key: str, value: float) -> None:
        self.tuning[key] = max(0.0, min(1.0, value))
        self.updated_at = time.time()


# ═══════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════

def save_resonance_profile(profile: UserResonanceProfile,
                           base_dir: str = None) -> None:
    """Save profile to agent_data/resonance/{user_id}_resonance.json.

    Encrypted at rest when HEVOLVE_DATA_KEY is configured.
    Falls back to plaintext JSON when encryption key is not set.
    """
    base_dir = base_dir or RESONANCE_STORAGE_DIR
    os.makedirs(base_dir, exist_ok=True)
    path = os.path.join(base_dir, f"{profile.user_id}_resonance.json")
    try:
        from security.crypto import encrypt_json_file
        encrypt_json_file(path, profile.to_dict())
    except ImportError:
        # Fallback: no crypto module available
        with open(path, 'w') as f:
            json.dump(profile.to_dict(), f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save resonance profile: {e}")


def load_resonance_profile(user_id: str,
                           base_dir: str = None) -> Optional[UserResonanceProfile]:
    """Load profile from disk. Auto-detects encrypted vs plaintext.

    Returns None if not found.
    """
    base_dir = base_dir or RESONANCE_STORAGE_DIR
    path = os.path.join(base_dir, f"{user_id}_resonance.json")
    if not os.path.exists(path):
        return None
    try:
        from security.crypto import decrypt_json_file
        data = decrypt_json_file(path)
        if data is None:
            return None
        return UserResonanceProfile.from_dict(data)
    except ImportError:
        # Fallback: no crypto module
        with open(path, 'r') as f:
            data = json.load(f)
        return UserResonanceProfile.from_dict(data)
    except Exception as e:
        logger.warning(f"Failed to load resonance profile: {e}")
        return None


def get_or_create_profile(user_id: str,
                          base_dir: str = None) -> UserResonanceProfile:
    """Load existing profile or create a fresh one."""
    profile = load_resonance_profile(user_id, base_dir)
    if profile is None:
        profile = UserResonanceProfile(user_id=user_id)
    return profile
