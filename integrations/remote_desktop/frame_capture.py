"""
Frame Capture — High-FPS cross-platform screen capture with circuit breaker fallback.

Tiered capture backends:
  Tier 1: dxcam (Windows GPU-accelerated, 240+ FPS) — optional
  Tier 2: mss (cross-platform, 30-60 FPS) — primary
  Tier 3: pyautogui.screenshot() (existing fallback)

Reuses:
  - integrations/vision/frame_store.py → compute_frame_difference() for skip-unchanged
  - integrations/vlm/vlm_adapter.py:34 → circuit breaker pattern
"""

import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Generator, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')

# ── Optional dependencies (guarded imports) ─────────────────────

_mss = None
_dxcam = None
_pyautogui = None
_PIL_Image = None

try:
    import mss as _mss_module
    _mss = _mss_module
except ImportError:
    pass

try:
    import dxcam as _dxcam_module
    _dxcam = _dxcam_module
except ImportError:
    pass

try:
    import pyautogui as _pyautogui_module
    _pyautogui = _pyautogui_module
except ImportError:
    pass

try:
    from PIL import Image as _PIL_Image_module
    _PIL_Image = _PIL_Image_module
except ImportError:
    pass

# Frame difference utility (from vision/frame_store.py)
try:
    from integrations.vision.frame_store import compute_frame_difference
except ImportError:
    def compute_frame_difference(frame1, frame2):
        """Fallback: byte-level comparison."""
        if len(frame1) != len(frame2):
            return 1.0
        diff = sum(abs(a - b) for a, b in zip(frame1[:1000], frame2[:1000]))
        return min(diff / (255 * min(len(frame1), 1000)), 1.0)


# ── Configuration ───────────────────────────────────────────────

@dataclass
class FrameConfig:
    max_fps: int = 30
    quality: int = 80           # JPEG quality (1-100)
    scale_factor: float = 1.0   # Downscale factor (0.5 = half size)
    min_change_threshold: float = 0.01  # Skip frame if < 1% changed
    keyframe_interval: int = 30  # Force keyframe every N frames
    adaptive_interval: bool = True  # Backoff for static scenes
    max_backoff_seconds: float = 2.0  # Max interval between frames


# ── Circuit Breaker (vlm_adapter.py:34 pattern) ────────────────

class _CaptureCircuitBreaker:
    """Track failures per backend, open circuit after threshold."""

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self._failures: dict = {}  # backend_name → count
        self._open: set = set()

    def record_failure(self, backend: str) -> None:
        self._failures[backend] = self._failures.get(backend, 0) + 1
        if self._failures[backend] >= self.threshold:
            self._open.add(backend)
            logger.warning(f"Circuit breaker OPEN for {backend}")

    def record_success(self, backend: str) -> None:
        self._failures[backend] = 0
        self._open.discard(backend)

    def is_open(self, backend: str) -> bool:
        return backend in self._open

    def reset(self, backend: str) -> None:
        self._failures.pop(backend, None)
        self._open.discard(backend)


# ── Frame Capture ───────────────────────────────────────────────

class FrameCapture:
    """Cross-platform screen capture with tiered fallback."""

    def __init__(self, config: Optional[FrameConfig] = None):
        self.config = config or FrameConfig()
        self._circuit = _CaptureCircuitBreaker()
        self._lock = threading.Lock()
        self._running = False
        self._last_frame: Optional[bytes] = None
        self._frame_count = 0
        self._dxcam_instance = None
        self._mss_instance = None

    def get_screen_size(self) -> Tuple[int, int]:
        """Get primary screen resolution (width, height)."""
        if _mss:
            try:
                with _mss.mss() as sct:
                    monitor = sct.monitors[1]  # Primary monitor
                    return monitor['width'], monitor['height']
            except Exception:
                pass
        if _pyautogui:
            try:
                size = _pyautogui.size()
                return size.width, size.height
            except Exception:
                pass
        return 1920, 1080  # Default fallback

    def capture_frame(self) -> Optional[bytes]:
        """Capture single frame as JPEG bytes.

        Uses circuit breaker pattern — tries backends in order,
        skips backends with open circuits.

        Returns:
            JPEG bytes or None if all backends failed.
        """
        # Tier 1: DXCam (Windows GPU)
        if _dxcam and not self._circuit.is_open('dxcam'):
            try:
                frame = self._capture_dxcam()
                if frame:
                    self._circuit.record_success('dxcam')
                    return frame
            except Exception as e:
                self._circuit.record_failure('dxcam')
                logger.debug(f"DXCam capture failed: {e}")

        # Tier 2: mss (cross-platform)
        if _mss and not self._circuit.is_open('mss'):
            try:
                frame = self._capture_mss()
                if frame:
                    self._circuit.record_success('mss')
                    return frame
            except Exception as e:
                self._circuit.record_failure('mss')
                logger.debug(f"MSS capture failed: {e}")

        # Tier 3: pyautogui (existing fallback)
        if _pyautogui and not self._circuit.is_open('pyautogui'):
            try:
                frame = self._capture_pyautogui()
                if frame:
                    self._circuit.record_success('pyautogui')
                    return frame
            except Exception as e:
                self._circuit.record_failure('pyautogui')
                logger.debug(f"PyAutoGUI capture failed: {e}")

        logger.error("All capture backends failed")
        return None

    def _capture_dxcam(self) -> Optional[bytes]:
        """DXCam GPU-accelerated capture (Windows only)."""
        if self._dxcam_instance is None:
            self._dxcam_instance = _dxcam.create()
        frame = self._dxcam_instance.grab()
        if frame is None:
            return None
        return self._encode_numpy_frame(frame)

    def _capture_mss(self) -> Optional[bytes]:
        """MSS cross-platform capture."""
        if self._mss_instance is None:
            self._mss_instance = _mss.mss()
        monitor = self._mss_instance.monitors[1]
        sct_img = self._mss_instance.grab(monitor)
        # mss returns BGRA; convert to RGB JPEG
        if _PIL_Image:
            img = _PIL_Image.frombytes('RGB', sct_img.size,
                                        sct_img.bgra, 'raw', 'BGRX')
            return self._encode_pil_image(img)
        # Fallback: raw PNG from mss
        return _mss.tools.to_png(sct_img.rgb, sct_img.size)

    def _capture_pyautogui(self) -> Optional[bytes]:
        """PyAutoGUI screenshot fallback."""
        screenshot = _pyautogui.screenshot()
        return self._encode_pil_image(screenshot)

    def _encode_pil_image(self, img) -> bytes:
        """Encode PIL Image to JPEG bytes with configured quality and scale."""
        if self.config.scale_factor != 1.0:
            new_size = (
                int(img.width * self.config.scale_factor),
                int(img.height * self.config.scale_factor),
            )
            img = img.resize(new_size, _PIL_Image.LANCZOS if _PIL_Image else 1)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=self.config.quality, optimize=True)
        return buf.getvalue()

    def _encode_numpy_frame(self, frame) -> bytes:
        """Encode numpy array (RGB/BGR) to JPEG bytes."""
        if _PIL_Image:
            img = _PIL_Image.fromarray(frame)
            return self._encode_pil_image(img)
        # Fallback: try cv2
        try:
            import cv2
            _, buf = cv2.imencode('.jpg', frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, self.config.quality])
            return buf.tobytes()
        except ImportError:
            return None

    def capture_loop(self) -> Generator[bytes, None, None]:
        """Yield JPEG frames at configured FPS, skipping unchanged frames.

        Uses compute_frame_difference() from vision/frame_store.py.
        Adaptive interval: backs off for static scenes (vision_service.py:36-37 pattern).
        """
        self._running = True
        interval = 1.0 / self.config.max_fps
        adaptive_interval = interval
        self._frame_count = 0

        try:
            while self._running:
                start = time.monotonic()

                frame = self.capture_frame()
                if frame is None:
                    time.sleep(interval)
                    continue

                self._frame_count += 1

                # Skip unchanged frames (unless keyframe)
                is_keyframe = (self._frame_count % self.config.keyframe_interval == 0)
                if self._last_frame and not is_keyframe:
                    try:
                        diff = compute_frame_difference(
                            self._last_frame[:4096], frame[:4096])
                        if diff < self.config.min_change_threshold:
                            # Static scene → adaptive backoff
                            if self.config.adaptive_interval:
                                adaptive_interval = min(
                                    adaptive_interval * 1.5,
                                    self.config.max_backoff_seconds,
                                )
                            elapsed = time.monotonic() - start
                            sleep_time = max(0, adaptive_interval - elapsed)
                            time.sleep(sleep_time)
                            continue
                    except Exception:
                        pass  # On error, send the frame anyway

                # Scene changed → reset adaptive interval
                adaptive_interval = interval
                self._last_frame = frame

                yield frame

                elapsed = time.monotonic() - start
                sleep_time = max(0, interval - elapsed)
                time.sleep(sleep_time)
        finally:
            self._running = False
            self._cleanup()

    def stop(self) -> None:
        """Stop the capture loop."""
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def get_stats(self) -> dict:
        """Get capture statistics."""
        return {
            'running': self._running,
            'frame_count': self._frame_count,
            'config': {
                'max_fps': self.config.max_fps,
                'quality': self.config.quality,
                'scale_factor': self.config.scale_factor,
            },
            'backends': {
                'dxcam': _dxcam is not None and not self._circuit.is_open('dxcam'),
                'mss': _mss is not None and not self._circuit.is_open('mss'),
                'pyautogui': _pyautogui is not None and not self._circuit.is_open('pyautogui'),
            },
        }

    def _cleanup(self) -> None:
        """Release capture resources."""
        if self._dxcam_instance:
            try:
                self._dxcam_instance.stop()
            except Exception:
                pass
            self._dxcam_instance = None
        if self._mss_instance:
            try:
                self._mss_instance.close()
            except Exception:
                pass
            self._mss_instance = None
