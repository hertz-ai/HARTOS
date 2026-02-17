"""
VisionService — manages the MiniCPM sidecar, WebSocket frame receiver,
and periodic description loop with intelligent adaptive sampling.

Architecture:
    VisionService.start()
        +-> MiniCPM subprocess (port 9891)       [sidecar]
        +-> WebSocket server (port 5460)          [receives camera/screen frames]
        +-> Description loop (adaptive interval)  [sends frame to MiniCPM only when scene changes]
        +-> FrameStore                            [in-process, replaces Redis]
        +-> Visual trigger evaluation             [fires callbacks on description match]
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np
import requests

from .frame_store import FrameStore, compute_frame_difference, decode_jpeg
from .minicpm_installer import MiniCPMInstaller

logger = logging.getLogger('hevolve_vision')


class VisionService:
    """Orchestrates the vision pipeline: sidecar + frames + descriptions.

    Intelligent sampling: only calls MiniCPM when the scene actually changes.
    Adaptive intervals: describes more often when active, backs off when static.
    Visual triggers: delegates to TriggerManager (VISUAL_MATCH / SCREEN_MATCH).
    """

    def __init__(
        self,
        minicpm_port: int = 9891,
        ws_port: int = 5460,
        description_interval: float = 4.0,
        max_description_interval: float = 30.0,
        min_scene_change: float = 0.01,
        frame_store: Optional[FrameStore] = None,
        minicpm_model_dir: Optional[str] = None,
        config_path: Optional[str] = None,
        callback_url: Optional[str] = None,
        trigger_manager=None,
    ):
        self._minicpm_port = int(os.environ.get('HEVOLVE_MINICPM_PORT', minicpm_port))
        self._ws_port = int(os.environ.get('VISION_WS_PORT', ws_port))
        self._description_interval = description_interval
        self._max_interval = max_description_interval
        self._min_scene_change = min_scene_change
        self.store = frame_store or FrameStore()
        self._installer = MiniCPMInstaller(
            model_dir=minicpm_model_dir or MiniCPMInstaller().model_dir
        )
        self._config = self._load_config(config_path)
        self._callback_url = callback_url or self._config.get('database_url')
        self._trigger_manager = trigger_manager  # Optional TriggerManager

        self._minicpm_process: Optional[subprocess.Popen] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._desc_thread: Optional[threading.Thread] = None
        self._running = False

        # Circuit breaker for MiniCPM health
        self._consecutive_failures = 0
        self._max_failures = 5
        self._circuit_open = False

        # Intelligent sampling state (per-user)
        self._last_described_frame: Dict[str, np.ndarray] = {}  # user_id → numpy
        self._user_intervals: Dict[str, float] = {}  # user_id → current interval
        self._last_describe_time: Dict[str, float] = {}  # user_id → timestamp
        self._frames_skipped: int = 0
        self._frames_described: int = 0

    # ─── Public API ───

    def start(self):
        """Start the vision pipeline (non-blocking)."""
        if self._running:
            logger.warning("VisionService already running")
            return

        self._running = True

        if not self._installer.is_installed():
            if self._installer.detect_gpu():
                logger.info("MiniCPM not installed — downloading...")
                self._installer.install()
            else:
                logger.warning("No GPU — vision sidecar will not start")
                self._running = False
                return

        self._start_minicpm()

        # Register atexit handler to prevent orphan subprocess on crash
        import atexit
        atexit.register(self._cleanup_subprocess)

        self._ws_thread = threading.Thread(
            target=self._run_ws_server, daemon=True, name='vision-ws',
        )
        self._ws_thread.start()

        self._desc_thread = threading.Thread(
            target=self._description_loop, daemon=True, name='vision-desc',
        )
        self._desc_thread.start()

        logger.info("VisionService started (adaptive sampling enabled)")

    def _cleanup_subprocess(self):
        """atexit handler — ensures MiniCPM subprocess is killed on exit."""
        if self._minicpm_process and self._minicpm_process.poll() is None:
            try:
                self._minicpm_process.terminate()
                self._minicpm_process.wait(timeout=3)
            except Exception:
                try:
                    self._minicpm_process.kill()
                except Exception:
                    pass

    def stop(self):
        """Stop all vision components."""
        self._running = False
        if self._minicpm_process:
            self._minicpm_process.terminate()
            try:
                self._minicpm_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._minicpm_process.kill()
            self._minicpm_process = None
            logger.info("MiniCPM sidecar stopped")
        logger.info(
            f"VisionService stopped (described={self._frames_described}, "
            f"skipped={self._frames_skipped})"
        )

    def get_frame(self, user_id: str) -> Optional[bytes]:
        """Get latest camera frame for a user."""
        return self.store.get_frame(user_id)

    def get_description(self, user_id: str) -> Optional[str]:
        """Get latest camera scene description for a user."""
        return self.store.get_description(user_id)

    def get_screen_description(self, user_id: str) -> Optional[str]:
        """Get latest screen description for a user."""
        return self.store.get_screen_description(user_id)

    def describe_screen_frame(self, user_id: str, frame_bytes: bytes) -> Optional[str]:
        """Describe a screen capture frame via MiniCPM and store the result.

        Uses intelligent sampling: skips if screen hasn't changed.
        """
        if self._circuit_open or not self._running:
            return None

        # Intelligent sampling: check if screen changed
        if not self._should_describe(user_id, frame_bytes, channel='screen'):
            self._frames_skipped += 1
            return None

        self.store.put_screen_frame(user_id, frame_bytes)

        desc = self._describe_frame(
            user_id, frame_bytes,
            prompt='describe what is on the computer screen in 20 words'
        )
        if desc:
            self.store.put_screen_description(user_id, desc)
            self._post_description_to_db(
                user_id, desc, label='Screen Context',
                zeroshot_label='Screen Reasoning'
            )
            self._record_to_world_model(user_id, desc, 'screen')
            self._evaluate_visual_triggers(user_id, desc, 'screen')
            self._frames_described += 1
        return desc

    def register_visual_trigger(
        self,
        channel: str,
        callback: Callable[[Dict], None],
        conditions: Optional[List] = None,
        keywords: Optional[List[str]] = None,
        pattern: Optional[str] = None,
        cooldown_seconds: int = 0,
        name: Optional[str] = None,
    ):
        """Register a trigger that fires when a description matches conditions.

        Delegates to TriggerManager (VISUAL_MATCH / SCREEN_MATCH types).

        Args:
            channel: 'camera' or 'screen'
            callback: fn({'user_id': str, 'description': str, 'channel': str})
            conditions: list of TriggerCondition objects (optional)
            keywords: list of keywords to match in description (optional)
            pattern: regex pattern to match description (optional)
            cooldown_seconds: minimum seconds between fires
            name: optional trigger name
        """
        if self._trigger_manager is None:
            from integrations.channels.automation.triggers import TriggerManager
            self._trigger_manager = TriggerManager()

        from integrations.channels.automation.triggers import TriggerType
        trigger_type = TriggerType.VISUAL_MATCH if channel == 'camera' else TriggerType.SCREEN_MATCH
        self._trigger_manager.register(
            trigger_type=trigger_type,
            callback=callback,
            name=name,
            conditions=conditions,
            keywords=keywords,
            pattern=pattern,
            cooldown_seconds=cooldown_seconds,
        )

    def get_status(self) -> Dict:
        """Return service status for health dashboards."""
        minicpm_alive = self._check_minicpm_health()
        return {
            'running': self._running,
            'minicpm_alive': minicpm_alive,
            'minicpm_port': self._minicpm_port,
            'ws_port': self._ws_port,
            'circuit_open': self._circuit_open,
            'consecutive_failures': self._consecutive_failures,
            'frames_described': self._frames_described,
            'frames_skipped': self._frames_skipped,
            'visual_triggers': self._trigger_manager.get_stats()['total_triggers'] if self._trigger_manager else 0,
            'installer': self._installer.get_status(),
            'store': self.store.stats(),
        }

    # ─── Intelligent Sampling ───

    def _should_describe(
        self, user_id: str, frame_bytes: bytes, channel: str = 'camera'
    ) -> bool:
        """Decide whether this frame needs a new MiniCPM description.

        Returns True if scene changed significantly since last description.
        Also manages per-user adaptive intervals.
        """
        key = f"{user_id}:{channel}"
        current = decode_jpeg(frame_bytes)
        if current is None:
            return False

        last = self._last_described_frame.get(key)
        if last is None:
            # First frame for this user/channel — always describe
            self._last_described_frame[key] = current
            self._user_intervals[key] = self._description_interval
            return True

        # Check if enough time has passed (per-user adaptive interval)
        now = time.time()
        last_time = self._last_describe_time.get(key, 0)
        interval = self._user_intervals.get(key, self._description_interval)
        if now - last_time < interval:
            return False

        # Compute frame difference
        diff = compute_frame_difference(last, current)

        if diff > self._min_scene_change:
            # Scene changed — describe and reset interval to fast
            self._last_described_frame[key] = current
            self._user_intervals[key] = self._description_interval
            self._last_describe_time[key] = now
            return True
        else:
            # Static — back off (×1.5, capped)
            self._user_intervals[key] = min(interval * 1.5, self._max_interval)
            self._last_describe_time[key] = now
            return False

    # ─── Visual Triggers ───

    def _evaluate_visual_triggers(
        self, user_id: str, description: str, channel: str
    ):
        """Evaluate registered triggers against a new description.

        Delegates to TriggerManager — zero extra compute, piggybacks on
        descriptions already produced by MiniCPM.
        """
        if self._trigger_manager is None:
            return

        from integrations.channels.automation.triggers import TriggerType
        trigger_type = TriggerType.VISUAL_MATCH if channel == 'camera' else TriggerType.SCREEN_MATCH
        event_data = {
            'user_id': user_id,
            'description': description,
            'channel': channel,
            'timestamp': time.time(),
        }
        try:
            self._trigger_manager.evaluate(trigger_type, event_data)
        except Exception as e:
            logger.debug(f"Visual trigger evaluation error: {e}")

    # ─── Sidecar Management ───

    def _start_minicpm(self):
        """Launch the MiniCPM server as a subprocess."""
        model_dir = self._installer.get_model_dir()
        if not model_dir:
            logger.error("MiniCPM not installed — cannot start sidecar")
            return

        cmd = [
            sys.executable, '-m', 'integrations.vision.minicpm_server',
            '--model_dir', model_dir,
            '--port', str(self._minicpm_port),
        ]
        logger.info(f"Starting MiniCPM sidecar: {' '.join(cmd)}")

        self._minicpm_process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._wait_for_minicpm(timeout=120)

    def _wait_for_minicpm(self, timeout: float = 120):
        """Poll MiniCPM health endpoint until ready."""
        start = time.time()
        while time.time() - start < timeout and self._running:
            if self._check_minicpm_health():
                logger.info("MiniCPM sidecar is healthy")
                return True
            time.sleep(2)
        logger.error(f"MiniCPM sidecar not healthy after {timeout}s")
        return False

    def _check_minicpm_health(self) -> bool:
        """Check if MiniCPM sidecar is responding."""
        try:
            r = requests.get(
                f'http://localhost:{self._minicpm_port}/status', timeout=3,
            )
            if r.status_code == 200:
                self._consecutive_failures = 0
                self._circuit_open = False
                return True
        except requests.RequestException:
            pass
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._circuit_open = True
        return False

    # ─── WebSocket Frame Receiver ───

    def _run_ws_server(self):
        """Run async WebSocket server in a new event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_serve())
        except Exception as e:
            logger.error(f"WebSocket server error: {e}")
        finally:
            loop.close()

    async def _ws_serve(self):
        """Start WebSocket server and handle connections."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed — frame receiver disabled")
            return

        server = await websockets.serve(self._ws_handler, '0.0.0.0', self._ws_port)
        # Read actual bound port (important when ws_port=0 for dynamic allocation)
        if server.sockets:
            actual_port = server.sockets[0].getsockname()[1]
            self._ws_port = actual_port
        logger.info(f"WebSocket frame receiver on port {self._ws_port}")
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            server.close()
            await server.wait_closed()

    async def _ws_handler(self, websocket, path=None):
        """Handle a single WebSocket connection (one per user).

        Protocol:
            1. Client sends user_id (digit string)
            2. Client sends "video_start" (camera, default) or "screen_start"
            3. Client sends binary JPEG frames
            4. Client sends "video_stop" to end
        """
        import cv2

        user_id = None
        channel = 'camera'  # default channel
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    frame = cv2.imdecode(
                        np.frombuffer(message, np.uint8), cv2.IMREAD_COLOR,
                    )
                    if frame is not None and user_id:
                        frame = cv2.fastNlMeansDenoisingColored(
                            frame, None, 10, 10, 7, 21
                        )
                        _, encoded = cv2.imencode('.jpg', frame)
                        jpeg_bytes = encoded.tobytes()
                        if channel == 'screen':
                            self.store.put_screen_frame(user_id, jpeg_bytes)
                        else:
                            self.store.put_frame(user_id, jpeg_bytes)
                elif isinstance(message, str):
                    if message.isdigit():
                        user_id = message
                        logger.info(f"Frame session started for user {user_id}")
                    elif message == 'screen_start':
                        channel = 'screen'
                        logger.info(f"User {user_id} switched to screen channel")
                    elif message == 'video_start':
                        channel = 'camera'
                    elif message == 'video_stop':
                        break
        except Exception as e:
            logger.debug(f"WebSocket session ended: {e}")
        finally:
            if user_id:
                logger.info(f"Frame session ended for user {user_id} ({channel})")

    # ─── Description Loop ───

    def _description_loop(self):
        """Periodically describe frames via MiniCPM with intelligent sampling.

        Only calls MiniCPM when frame difference exceeds threshold.
        Backs off interval for static scenes (4s → 8s → ... → 30s cap).
        Evaluates visual triggers after each new description.
        Processes both camera and screen channels.
        """
        while self._running:
            if self._circuit_open:
                time.sleep(self._description_interval * 2)
                self._check_minicpm_health()
                continue

            try:
                users = self.store.active_users()
                for user_id in users:
                    if not self._running:
                        return

                    # Camera channel
                    frame_bytes = self.store.get_frame(user_id)
                    if frame_bytes:
                        if self._should_describe(user_id, frame_bytes, 'camera'):
                            desc = self._describe_frame(user_id, frame_bytes)
                            if desc:
                                self.store.put_description(user_id, desc)
                                self._post_description_to_db(user_id, desc)
                                self._record_to_world_model(user_id, desc, 'camera')
                                self._evaluate_visual_triggers(
                                    user_id, desc, 'camera'
                                )
                                self._frames_described += 1
                        else:
                            self._frames_skipped += 1

                    # Screen channel
                    screen_bytes = self.store.get_screen_frame(user_id)
                    if screen_bytes:
                        if self._should_describe(user_id, screen_bytes, 'screen'):
                            desc = self._describe_frame(
                                user_id, screen_bytes,
                                prompt='describe what is on the computer screen in 20 words',
                            )
                            if desc:
                                self.store.put_screen_description(user_id, desc)
                                self._post_description_to_db(
                                    user_id, desc,
                                    label='Screen Context',
                                    zeroshot_label='Screen Reasoning',
                                )
                                self._record_to_world_model(user_id, desc, 'screen')
                                self._evaluate_visual_triggers(
                                    user_id, desc, 'screen'
                                )
                                self._frames_described += 1
                        else:
                            self._frames_skipped += 1
            except Exception as e:
                logger.debug(f"Description loop error: {e}")

            # Sleep the minimum user interval (so we check the fastest user on time)
            min_interval = self._description_interval
            if self._user_intervals:
                min_interval = min(
                    min_interval,
                    min(self._user_intervals.values())
                )
            time.sleep(min_interval)

    def _describe_frame(
        self, user_id: str, frame_bytes: bytes,
        prompt: str = 'describe what the user is doing in 20 words',
    ) -> Optional[str]:
        """Send frame to MiniCPM /describe endpoint and return text."""
        if self._circuit_open:
            return None
        try:
            r = requests.post(
                f'http://localhost:{self._minicpm_port}/describe',
                data=frame_bytes,
                params={'prompt': prompt},
                headers={'Content-Type': 'application/octet-stream'},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json().get('result')
        except requests.RequestException:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                self._circuit_open = True
        return None

    def _post_description_to_db(
        self, user_id: str, description: str,
        label: str = 'Visual Context',
        zeroshot_label: str = 'Video Reasoning',
    ):
        """POST description to main server DB (/create_action pattern)."""
        if not self._callback_url:
            return
        try:
            requests.post(
                f'{self._callback_url}/create_action',
                json={
                    'user_id': user_id,
                    'conv_id': '0',
                    'action': description[:100],
                    'zeroshot_label': zeroshot_label,
                    'gpt3_label': label,
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    # ─── World Model Integration ───

    def _record_to_world_model(self, user_id: str, description: str,
                                channel: str = 'camera'):
        """Feed scene descriptions to world model for continuous learning."""
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            get_world_model_bridge().record_interaction(
                user_id=user_id, prompt_id=f'vision_{channel}',
                prompt=f'[{channel}] describe what you see',
                response=description, model_id='minicpm-v2')
        except Exception:
            pass

    # ─── Config ───

    def _load_config(self, config_path: Optional[str]) -> Dict:
        """Load embodied AI config if available."""
        if config_path and os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    return json.load(f)
            except Exception:
                pass

        default = os.path.join(
            os.path.expanduser('~'), '.hevolve', 'embodied_ai_config.json'
        )
        if os.path.isfile(default):
            try:
                with open(default) as f:
                    return json.load(f)
            except Exception:
                pass

        return {}
