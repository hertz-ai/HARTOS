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
#
# We go through security.native_hive_loader.try_import_hevolveai_names so
# that an armored bundle (HevolveArmor) is transparently unlocked before
# the import — the scattered bare `import hevolveai.X` probes used to miss
# the armored path when a key was needed.
import logging as _logging
_logger = _logging.getLogger(__name__)

_visual_encoding_result = None
try:
    from security.native_hive_loader import try_import_hevolveai_names
    _visual_encoding_result = try_import_hevolveai_names(
        'hevolveai.embodied_ai.utils.visual_encoding',
        ('compute_frame_difference', 'decode_jpeg'),
    )
except ImportError:
    # security package itself unavailable — treat as missing HevolveAI
    _visual_encoding_result = None

if _visual_encoding_result is not None:
    compute_frame_difference, decode_jpeg = _visual_encoding_result
else:
    # Fallback if HevolveAI not installed (e.g. tests without full deps,
    # or armored bundle present but key unavailable)
    _logger.warning(
        "HevolveAI not installed/armored — visual_encoding using numpy fallback")
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

_pixel_difference = compute_frame_difference


def compute_frame_difference(frame1, frame2) -> float:
    """How different two frames are, 0.0 (same) to 1.0.  Two frames of
    different sizes (another camera, a phone turned on its side) are a change
    of scene, 1.0: compared pixel by pixel they raised, and the description
    loop's tick for that person died on every frame until a frame of the old
    size came back, screen captions included (measured live 2026-11-10 on
    build 7f8504b7: a 640x480 page camera frame, then 480x360 frames, 84
    'operands could not be broadcast' tracebacks and not one caption)."""
    if getattr(frame1, 'shape', None) != getattr(frame2, 'shape', None):
        return 1.0
    return _pixel_difference(frame1, frame2)


#: The most pixels a frame may claim and still be decoded by shrink_frame:
#: 8192x6144, the largest frame a 50 megapixel phone camera gives (about
#: 150 MB once decoded).  A claim past it is answered as no frame: the
#: bytes are not decoded.
MAX_FRAME_PIXELS = 8192 * 6144


def shrink_frame(frame_bytes: bytes, max_side: int = 640,
                 quality: int = 70) -> Optional[bytes]:
    """``frame_bytes`` re-encoded as a JPEG at most ``max_side`` px on its
    long side, the size the page's own camera sends and the caption model
    reads; None when the bytes are no image.  A frame already that small
    keeps its size (it is never enlarged).

    For a producer whose frames are larger than a frame entry point keeps:
    a phone sends its camera's own JPEGs, and Nunba's /api/vision/frame
    answered every one 413 until it shrank them (live 13 Nov 2026).  The
    step lived in that route; it is the store's, so any entry point that
    takes frames from a camera it does not size can use the same one.

    The size the bytes claim is read from their header before anything is
    decoded, and a claim past ``MAX_FRAME_PIXELS`` is answered None like a
    body that is no image: decoding costs memory by the claim, not by the
    body, and a 656 KB body claiming 8000x8000 took 0.5 s and 355 MB to turn
    into one 640 px frame.  Bytes OpenCV refuses outright (none at all, or a
    claim past its own limits) are None too; they raised, and the route
    answered 500 where it says 413."""
    import io
    import cv2
    import numpy as np
    from PIL import Image
    try:
        with Image.open(io.BytesIO(frame_bytes)) as claimed:
            width, height = claimed.size
    except Exception:
        return None
    if width * height > MAX_FRAME_PIXELS:
        return None
    try:
        img = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = min(1.0, float(max_side) / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    except cv2.error:
        return None
    return buf.tobytes() if ok else None


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
        frame_ttl: float = 10.0,
    ):
        self._lock = threading.RLock()
        # Camera channel (default, backward compatible)
        # user_id → deque of (monotonic time taken, frame_bytes, who sent it)
        self._frames: Dict[str, deque] = {}
        self._descriptions: Dict[str, tuple] = {}  # user_id → (timestamp, text)
        # Screen channel
        self._screen_frames: Dict[str, deque] = {}
        self._screen_descriptions: Dict[str, tuple] = {}
        # Description history (bounded ring buffer for last N descriptions per channel)
        self._camera_desc_history: Dict[str, deque] = {}  # user_id → deque of (ts, text)
        self._screen_desc_history: Dict[str, deque] = {}
        self._max_desc_history = 20  # Keep last 20 descriptions per channel

        # user_id -> monotonic time of that person's last phone camera frame
        # (a 'device:' source: a phone the owner allowed), see
        # phone_camera_live.
        self._phone_frame_at: Dict[str, float] = {}

        self._max_frames = max_frames
        # A camera frame older than this is not "the latest frame": nothing
        # takes a frame out when the camera stops, so the last one was read
        # as live for as long as the process ran (measured 2026-11-10: the
        # description loop logged a camera frame 46 s after the session
        # ended).  A streaming camera sends several frames a second.
        self._frame_ttl = frame_ttl
        self._description_ttl = description_ttl
        self._screen_description_ttl = screen_description_ttl

    # ─── The sense gate ───

    def _open(self, sensor: str, frames: Dict[str, deque]) -> bool:
        """True when ``sensor`` may be sensed now (core.ai_sensing.allowed:
        the eye button's cut, or the owner's No to the feed).  When it may
        not, every frame held for it is dropped too, so nothing taken before
        the No is described after it.  Every producer (the WS receiver, the
        screen-capture loop, Nunba's frame routes, the ROS bridge) and every
        reader goes through this store, whichever VisionService owns it.
        Caller holds self._lock."""
        from core.ai_sensing import allowed
        if allowed(sensor):
            return True
        frames.clear()
        return False

    # ─── Camera Channel (default, backward compatible) ───

    def put_frame(self, user_id: str, frame_bytes: bytes,
                  source: Optional[str] = None):
        """Store a raw camera frame for a user (FIFO bounded); dropped while
        the camera may not be sensed.  ``source`` says who sent it (the
        producer and its connection); see frame_source."""
        with self._lock:
            if not self._open('camera', self._frames):
                return
            if user_id not in self._frames:
                self._frames[user_id] = deque(maxlen=self._max_frames)
            # Each frame keeps who sent it: kept per person instead, a caption
            # of A's frame was posted as B's when B's frame landed while A's
            # was being described (review of a7d79664d).  Monotonic time, so
            # a clock set back does not keep a stopped camera live.
            now = time.monotonic()
            self._frames[user_id].append(
                (now, frame_bytes, source or self.UNSTATED))
            if str(source or '').startswith('device:'):
                self._phone_frame_at[str(user_id)] = now

    #: The source of a frame whose producer did not say who it is.
    UNSTATED = 'unstated'

    #: How long after a phone's last camera frame that phone is still the
    #: person's camera.  The phone sends the desktop one frame in ~2 s
    #: (Android DesktopCameraFrames.EVERY_MS), so a few missed frames do not
    #: hand the camera back.
    PHONE_CAMERA_WINDOW_S = 10.0

    def phone_camera_live(self, user_id) -> bool:
        """True while ``user_id``'s phone streams its camera here: one camera
        per person, so the page on this desktop frees the desktop camera for
        other apps (a Zoom call) and its frames do not take turns with the
        phone's in that person's captions (owner 2026-11-13)."""
        with self._lock:
            seen = self._phone_frame_at.get(str(user_id))
            return (seen is not None
                    and time.monotonic() - seen <= self.PHONE_CAMERA_WINDOW_S)

    def latest(self, user_id: str, channel: str = 'camera') -> Optional[Tuple[bytes, str]]:
        """``user_id``'s latest frame on ``channel`` ('camera' or 'screen') and
        who sent it, read together, so a caption names the sender of the
        frame it describes; None when there is none, when the sense is off,
        or for a camera frame older than the frame age limit."""
        with self._lock:
            sensor = 'screen' if channel == 'screen' else 'camera'
            held = self._screen_frames if sensor == 'screen' else self._frames
            if not self._open(sensor, held):
                return None
            buf = held.get(user_id)
            if not buf:
                return None
            taken, frame_bytes, sent_by = buf[-1]
            if (sensor == 'camera' and self._frame_ttl
                    and time.monotonic() - taken > self._frame_ttl):
                return None
            return frame_bytes, sent_by

    def frame_source(self, user_id: str, channel: str = 'camera') -> Optional[str]:
        """Who sent ``user_id``'s latest frame on ``channel``, as the producer
        stated it; UNSTATED when it did not; None when there is no frame (see
        latest).  A frame carried only (time, bytes): a caption could not be
        traced to a camera, a screen grab or a connection, and the receiver
        takes the account id on trust (owner 2026-11-10: "my images do not
        have source provenance?")."""
        got = self.latest(user_id, channel)
        return got[1] if got else None

    def get_frame(self, user_id: str) -> Optional[bytes]:
        """Get the latest camera frame for a user, or None (see latest)."""
        got = self.latest(user_id, 'camera')
        return got[0] if got else None

    def camera_stopped(self, user_id: str) -> bool:
        """True when ``user_id``'s camera sent frames here and the latest is
        older than the frame age limit: the camera stopped, as opposed to
        there being no camera.  A camera question is then answered "the
        camera stopped", never from something else (review of a7d79664d:
        hart_intelligence_entry.get_frame answered it from a screenshot)."""
        with self._lock:
            buf = self._frames.get(user_id)
            if not buf:
                return False
            return bool(self._frame_ttl) and (
                time.monotonic() - buf[-1][0] > self._frame_ttl)

    def get_frame_count(self, user_id: str) -> int:
        """Number of buffered camera frames for a user."""
        with self._lock:
            if not self._open('camera', self._frames):
                return 0
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

    def put_screen_frame(self, user_id: str, frame_bytes: bytes,
                         source: Optional[str] = None):
        """Store a raw screen capture frame for a user (FIFO bounded);
        dropped while the screen may not be sensed.  ``source`` as put_frame."""
        with self._lock:
            if not self._open('screen', self._screen_frames):
                return
            if user_id not in self._screen_frames:
                self._screen_frames[user_id] = deque(maxlen=self._max_frames)
            self._screen_frames[user_id].append(
                (time.monotonic(), frame_bytes, source or self.UNSTATED))

    def get_screen_frame(self, user_id: str) -> Optional[bytes]:
        """Get the latest screen frame for a user, or None (see latest)."""
        got = self.latest(user_id, 'screen')
        return got[0] if got else None

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
