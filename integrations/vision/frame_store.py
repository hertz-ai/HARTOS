"""
Thread-safe in-process frame store — replaces Redis for desktop deployment.

Stores raw frames (numpy arrays) and text descriptions per user_id,
with separate channels for camera and screen feeds.

Camera descriptions are also persisted to DB (longer-lived context).
Screen descriptions are short-lived TTL only (they go stale fast).

Re-exports compute_frame_difference and decode_jpeg from
HevolveAI's visual_encoding utilities (canonical source).
"""
import threading
import time
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

# Canonical frame utilities live in HevolveAI (downstream dep).
# Re-export here so VisionService imports stay clean.
try:
    from hevolveai.embodied_ai.utils.visual_encoding import (
        compute_frame_difference,
        decode_jpeg,
    )
except ImportError:
    # Fallback if HevolveAI not installed (e.g. tests without full deps)
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "HevolveAI not installed — visual_encoding using numpy fallback")
    import numpy as np

    def compute_frame_difference(frame1: 'np.ndarray', frame2: 'np.ndarray') -> float:
        diff = np.abs(frame1.astype(np.float32) - frame2.astype(np.float32))
        return float(diff.mean() / 255.0)

    def decode_jpeg(frame_bytes: bytes) -> Optional['np.ndarray']:
        try:
            import cv2
            return cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            return None


class FrameStore:
    """Thread-safe frame + description store, replaces Redis for desktop apps.

    Supports two visual channels per user:
    - **camera**: Physical camera frames (MiniCPM-described, also saved to DB)
    - **screen**: Screen capture frames (shorter TTL, stale faster)

    Each user_id gets bounded frame buffers and latest descriptions per channel.
    All access is guarded by a single RLock.
    """

    def __init__(
        self,
        max_frames: int = 5,
        description_ttl: float = 30.0,
        screen_description_ttl: float = 15.0,
    ):
        self._lock = threading.RLock()
        # Camera channel (default, backward compatible)
        self._frames: Dict[str, deque] = {}       # user_id → deque of (timestamp, frame_bytes)
        self._descriptions: Dict[str, tuple] = {}  # user_id → (timestamp, text)
        # Screen channel
        self._screen_frames: Dict[str, deque] = {}
        self._screen_descriptions: Dict[str, tuple] = {}
        # Description history (bounded ring buffer for last N descriptions per channel)
        self._camera_desc_history: Dict[str, deque] = {}  # user_id → deque of (ts, text)
        self._screen_desc_history: Dict[str, deque] = {}
        self._max_desc_history = 20  # Keep last 20 descriptions per channel

        self._max_frames = max_frames
        self._description_ttl = description_ttl
        self._screen_description_ttl = screen_description_ttl

    # ─── Camera Channel (default, backward compatible) ───

    def put_frame(self, user_id: str, frame_bytes: bytes):
        """Store a raw camera frame for a user (FIFO bounded)."""
        with self._lock:
            if user_id not in self._frames:
                self._frames[user_id] = deque(maxlen=self._max_frames)
            self._frames[user_id].append((time.time(), frame_bytes))

    def get_frame(self, user_id: str) -> Optional[bytes]:
        """Get the latest camera frame for a user, or None."""
        with self._lock:
            buf = self._frames.get(user_id)
            if buf:
                return buf[-1][1]
            return None

    def get_frame_count(self, user_id: str) -> int:
        """Number of buffered camera frames for a user."""
        with self._lock:
            buf = self._frames.get(user_id)
            return len(buf) if buf else 0

    def put_description(self, user_id: str, text: str):
        """Store a camera scene description for a user."""
        with self._lock:
            now = time.time()
            self._descriptions[user_id] = (now, text)
            # Also append to history ring buffer
            if user_id not in self._camera_desc_history:
                self._camera_desc_history[user_id] = deque(
                    maxlen=self._max_desc_history
                )
            self._camera_desc_history[user_id].append((now, text))

    def get_description(self, user_id: str) -> Optional[str]:
        """Get the latest camera description if within TTL, else None."""
        with self._lock:
            entry = self._descriptions.get(user_id)
            if entry is None:
                return None
            ts, text = entry
            if time.time() - ts > self._description_ttl:
                return None
            return text

    # ─── Screen Channel ───

    def put_screen_frame(self, user_id: str, frame_bytes: bytes):
        """Store a raw screen capture frame for a user (FIFO bounded)."""
        with self._lock:
            if user_id not in self._screen_frames:
                self._screen_frames[user_id] = deque(maxlen=self._max_frames)
            self._screen_frames[user_id].append((time.time(), frame_bytes))

    def get_screen_frame(self, user_id: str) -> Optional[bytes]:
        """Get the latest screen frame for a user, or None."""
        with self._lock:
            buf = self._screen_frames.get(user_id)
            if buf:
                return buf[-1][1]
            return None

    def put_screen_description(self, user_id: str, text: str):
        """Store a screen description for a user (shorter TTL than camera)."""
        with self._lock:
            now = time.time()
            self._screen_descriptions[user_id] = (now, text)
            # Also append to history ring buffer
            if user_id not in self._screen_desc_history:
                self._screen_desc_history[user_id] = deque(
                    maxlen=self._max_desc_history
                )
            self._screen_desc_history[user_id].append((now, text))

    def get_screen_description(self, user_id: str) -> Optional[str]:
        """Get the latest screen description if within TTL, else None."""
        with self._lock:
            entry = self._screen_descriptions.get(user_id)
            if entry is None:
                return None
            ts, text = entry
            if time.time() - ts > self._screen_description_ttl:
                return None
            return text

    def get_screen_description_history(
        self, user_id: str, max_age_seconds: float = 60.0
    ) -> List[Tuple[float, str]]:
        """Get recent screen descriptions within max_age_seconds.

        Returns list of (timestamp, text) tuples, newest first.
        """
        with self._lock:
            history = self._screen_desc_history.get(user_id)
            if not history:
                return []
            cutoff = time.time() - max_age_seconds
            result = [(ts, text) for ts, text in history if ts >= cutoff]
            result.reverse()  # newest first
            return result

    def get_camera_description_history(
        self, user_id: str, max_age_seconds: float = 300.0
    ) -> List[Tuple[float, str]]:
        """Get recent camera descriptions within max_age_seconds.

        Returns list of (timestamp, text) tuples, newest first.
        """
        with self._lock:
            history = self._camera_desc_history.get(user_id)
            if not history:
                return []
            cutoff = time.time() - max_age_seconds
            result = [(ts, text) for ts, text in history if ts >= cutoff]
            result.reverse()
            return result

    # ─── Shared ───

    def clear_user(self, user_id: str):
        """Remove all data for a user (both channels)."""
        with self._lock:
            self._frames.pop(user_id, None)
            self._descriptions.pop(user_id, None)
            self._screen_frames.pop(user_id, None)
            self._screen_descriptions.pop(user_id, None)
            self._camera_desc_history.pop(user_id, None)
            self._screen_desc_history.pop(user_id, None)

    def active_users(self) -> list:
        """Return list of user_ids with stored frames (either channel)."""
        with self._lock:
            return list(
                set(self._frames.keys()) | set(self._screen_frames.keys())
            )

    def stats(self) -> Dict[str, Any]:
        """Return store statistics."""
        with self._lock:
            return {
                'active_users': len(
                    set(self._frames.keys()) | set(self._screen_frames.keys())
                ),
                'camera_frames': sum(len(d) for d in self._frames.values()),
                'screen_frames': sum(
                    len(d) for d in self._screen_frames.values()
                ),
                'total_frames': (
                    sum(len(d) for d in self._frames.values())
                    + sum(len(d) for d in self._screen_frames.values())
                ),
                'camera_descriptions': len(self._descriptions),
                'screen_descriptions': len(self._screen_descriptions),
            }
