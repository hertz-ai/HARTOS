"""
Vision Sidecar — packages MiniCPM + frame handling for desktop apps.

Replaces external Redis with in-process FrameStore.
Manages MiniCPM as a subprocess sidecar with auto-download.
Provides continuous scene descriptions for the embodied AI agent.
"""
from .frame_store import FrameStore
from .vision_service import VisionService

__all__ = ['FrameStore', 'VisionService']
