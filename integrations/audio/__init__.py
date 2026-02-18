"""
Audio Sidecar - speaker diarization as a managed subprocess.

Replaces standalone speaker_diarization service.
Manages WhisperX + pyannote as a subprocess sidecar with auto-start.
"""
from .diarization_service import DiarizationService

__all__ = ['DiarizationService']
