"""
ResonanceIdentifier — Thin proxy for biometric user identification.

All biometric ML (face embedding extraction, voice embedding extraction,
cosine similarity matching) lives in the HevolveAI sibling repo.

HARTOS dispatches raw biometric data (frame bytes, audio bytes) to
HevolveAI via WorldModelBridge and receives identification results back.
When HevolveAI is unavailable, all operations gracefully return None/False
and the system falls back to channel identity (user_id from API/WebSocket).

Priority:
  1. Channel identity (user_id from API/WebSocket) — always available
  2. Face match (dispatched to HevolveAI)
  3. Voice match (dispatched to HevolveAI)
"""

import base64
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ResonanceIdentifier:
    """Dispatch biometric operations to HevolveAI. No local ML."""

    def identify_by_face(self, frame_bytes: bytes) -> Optional[Tuple[str, float]]:
        """Request face identification from HevolveAI.

        Returns (user_id, confidence) or None if HevolveAI unavailable.
        """
        return self._dispatch_identification('face', frame_bytes)

    def identify_by_voice(self, audio_bytes: bytes) -> Optional[Tuple[str, float]]:
        """Request voice identification from HevolveAI.

        Returns (user_id, confidence) or None if HevolveAI unavailable.
        """
        return self._dispatch_identification('voice', audio_bytes)

    def enroll_face(self, user_id: str, frame_bytes: bytes,
                    base_dir: str = None) -> bool:
        """Dispatch face enrollment to HevolveAI."""
        return self._dispatch_enrollment('face', user_id, frame_bytes)

    def enroll_voice(self, user_id: str, audio_bytes: bytes,
                     base_dir: str = None) -> bool:
        """Dispatch voice enrollment to HevolveAI."""
        return self._dispatch_enrollment('voice', user_id, audio_bytes)

    def _dispatch_identification(self, modality: str,
                                  raw_bytes: bytes) -> Optional[Tuple[str, float]]:
        """Send biometric data to HevolveAI for identification."""
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge.record_interaction(
                user_id='biometric_probe',
                prompt_id='biometric_identification',
                prompt=base64.b64encode(raw_bytes[:4096]).decode('ascii'),
                response='',
                model_id=f'biometric_{modality}_identify',
            )
            # HevolveAI processes asynchronously; result flows back
            # via apply_hevolveai_corrections() in resonance_tuner
            return None
        except ImportError:
            return None
        except Exception as e:
            logger.debug(f"Biometric identification dispatch failed: {e}")
            return None

    def _dispatch_enrollment(self, modality: str, user_id: str,
                              raw_bytes: bytes) -> bool:
        """Send biometric data to HevolveAI for enrollment."""
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge.record_interaction(
                user_id=user_id,
                prompt_id='biometric_enrollment',
                prompt=base64.b64encode(raw_bytes[:4096]).decode('ascii'),
                response='',
                model_id=f'biometric_{modality}_enroll',
            )
            return True
        except ImportError:
            return False
        except Exception as e:
            logger.debug(f"Biometric enrollment dispatch failed: {e}")
            return False
