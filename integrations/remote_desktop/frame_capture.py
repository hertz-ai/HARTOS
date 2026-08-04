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

from core.circuit_breaker import KeyedCircuitBreaker

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


# Per-backend circuit breaking now uses the canonical
# core.circuit_breaker.KeyedCircuitBreaker (was a local _CaptureCircuitBreaker
# duplicate) — one breaker impl, with the canonical cooldown + half-open recovery.


# ── Frame Capture ───────────────────────────────────────────────

class FrameCapture:
    """Cross-platform screen capture with tiered fallback."""

    def __init__(self, config: Optional[FrameConfig] = None):
        self.config = config or FrameConfig()
        self._circuit = KeyedCircuitBreaker(threshold=5, name='frame_capture')
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

    def record_to_video(self, duration_s: float = 10.0, fps: int = 10,
                        output_path: Optional[str] = None,
                        max_frames: int = 1200) -> dict:
        """Record the screen for ``duration_s`` at ``fps`` into a SHAREABLE video.

        This is the missing "frames → shareable artifact" step for marketing demo
        videos.  It reuses ``capture_frame()`` (the cross-platform dxcam/mss/
        pyautogui tiers — so it works on Windows where Nunba runs, unlike the
        Linux-only ``ffmpeg x11grab`` route in shell_os_apis) and assembles via
        imageio (the same encoder vision/ltx2_server.py uses).  Prefers mp4
        (H.264); falls back to GIF when the imageio-ffmpeg plugin is unavailable.

        Captures at a FIXED cadence (no unchanged-frame skipping — a demo needs a
        steady timeline), bounded by ``max_frames`` so a runaway duration can't
        exhaust memory.

        Returns: {ok, path, format, frames, fps, duration_s} or {ok: False, error}.
        """
        try:
            import imageio.v2 as imageio
        except ImportError:
            try:
                import imageio  # type: ignore
            except ImportError:
                return {'ok': False, 'error': 'imageio not available — cannot assemble video'}

        if output_path is None:
            try:
                from core.platform_paths import get_data_dir
                base = get_data_dir()
            except Exception:
                import tempfile
                base = tempfile.gettempdir()
            import os
            demo_dir = os.path.join(base, 'demos')
            os.makedirs(demo_dir, exist_ok=True)
            output_path = os.path.join(demo_dir, f'demo_{int(time.time())}.mp4')

        fps = max(1, int(fps))
        interval = 1.0 / fps
        n_target = min(int(max_frames), max(1, int(duration_s * fps)))

        frames = []
        self._running = True
        t0 = time.monotonic()
        try:
            for _ in range(n_target):
                if not self._running:
                    break
                start = time.monotonic()
                jpeg = self.capture_frame()
                if jpeg:
                    try:
                        frames.append(imageio.imread(io.BytesIO(jpeg)))
                    except Exception as e:
                        logger.debug(f"demo frame decode failed: {e}")
                elapsed = time.monotonic() - start
                time.sleep(max(0, interval - elapsed))
        finally:
            wall_s = time.monotonic() - t0
            self._running = False
            self._cleanup()

        if not frames:
            return {'ok': False, 'error': (
                'no frames captured — no screen-capture backend. Install one: '
                'pip install mss (cross-platform), or dxcam on Windows.')}

        # Encode at the rate actually ACHIEVED, not the rate requested.
        #
        # Capture rarely keeps up: 8 fps asked for on a modest box measured 4.5.
        # Writing the requested fps into the container makes playback faster than
        # the events really were, which for a demo of a local model silently
        # overstates how quick it is. Nobody who suspects one sped-up clip
        # believes the rest of the reel, so the honest timeline is the useful one.
        actual_fps = (len(frames) / wall_s) if wall_s > 0 else float(fps)
        actual_fps = max(1.0, round(actual_fps, 2))
        if actual_fps < fps * 0.8:
            logger.info(f"capture achieved {actual_fps:g} fps of {fps} requested; "
                        f"encoding at {actual_fps:g} so playback is real time")

        # Prefer mp4 (H.264); fall back to GIF if the ffmpeg plugin is missing.
        try:
            imageio.mimwrite(output_path, frames, fps=actual_fps, codec='libx264',
                             macro_block_size=None)
            fmt = 'mp4'
        except Exception as e:
            logger.info(f"mp4 encode unavailable ({e}); falling back to GIF")
            import os
            output_path = os.path.splitext(output_path)[0] + '.gif'
            imageio.mimwrite(output_path, frames, duration=1.0 / actual_fps)
            fmt = 'gif'

        return {
            'ok': True,
            'path': output_path,
            'format': fmt,
            'frames': len(frames),
            'fps': actual_fps,
            'requested_fps': fps,
            'duration_s': round(len(frames) / actual_fps, 2),
        }

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
