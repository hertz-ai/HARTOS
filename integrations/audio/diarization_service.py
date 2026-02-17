"""
DiarizationService — manages the speaker diarization sidecar subprocess.

Mirrors VisionService's MiniCPM management pattern:
    DiarizationService.start()
        +-> diarization_server subprocess (WebSocket, port from env)
        +-> Readiness detection via stdout signal
        +-> atexit cleanup to prevent orphan processes
"""
import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger('hevolve_diarization')


class DiarizationService:
    """Manages the speaker diarization WebSocket sidecar.

    Starts diarization_server.py as a subprocess, waits for readiness,
    and provides lifecycle management (start/stop/health/status).
    """

    def __init__(self, port: int = 8004):
        self._port = int(os.environ.get('HEVOLVE_DIARIZATION_PORT', port))
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._ready = False

    # ─── Public API ───

    def start(self):
        """Start the diarization sidecar (non-blocking)."""
        if self._running:
            logger.warning("DiarizationService already running")
            return

        # Check if whisperx is available before starting subprocess
        if not self._is_whisperx_available():
            logger.info(
                "whisperx not installed — diarization sidecar disabled")
            return

        # Check HF token
        hf_token = os.environ.get('HEVOLVE_HF_TOKEN', '')
        if not hf_token:
            for cfg_path in [
                'config.json',
                os.path.join(
                    os.path.expanduser('~'), '.hevolve', 'config.json'),
            ]:
                if os.path.isfile(cfg_path):
                    try:
                        import json
                        with open(cfg_path) as f:
                            cfg = json.load(f)
                        hf_token = cfg.get('huggingface', '')
                        if hf_token:
                            break
                    except Exception:
                        pass
        if not hf_token:
            logger.warning(
                "No HuggingFace token — diarization sidecar disabled. "
                "Set HEVOLVE_HF_TOKEN env var.")
            return

        self._running = True
        self._start_subprocess()

        atexit.register(self._cleanup_subprocess)

        # Wait for readiness in background
        threading.Thread(
            target=self._wait_for_ready,
            daemon=True, name='diarization-wait',
        ).start()

        logger.info("DiarizationService starting...")

    def stop(self):
        """Stop the diarization sidecar."""
        self._running = False
        self._ready = False
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("Diarization sidecar stopped")

    def is_ready(self) -> bool:
        """Check if the sidecar is ready to accept connections."""
        return self._ready and self._running

    @property
    def port(self) -> int:
        """Actual bound port (may differ from requested if dynamic)."""
        return self._port

    @property
    def ws_url(self) -> str:
        """WebSocket URL for connecting to the sidecar."""
        return f'ws://localhost:{self._port}'

    def get_status(self) -> Dict:
        """Return service status for health dashboards."""
        alive = (
            self._process is not None
            and self._process.poll() is None
        )
        return {
            'running': self._running,
            'ready': self._ready,
            'alive': alive,
            'port': self._port,
        }

    # ─── Internal ───

    def _is_whisperx_available(self) -> bool:
        """Check if whisperx is importable."""
        try:
            import whisperx  # noqa: F401
            return True
        except ImportError:
            return False

    def _start_subprocess(self):
        """Launch the diarization server as a subprocess."""
        cmd = [
            sys.executable, '-m',
            'integrations.audio.diarization_server',
            '--port', str(self._port),
        ]
        logger.info(f"Starting diarization sidecar: {' '.join(cmd)}")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _wait_for_ready(self, timeout: float = 180):
        """Read stdout for DIARIZATION_READY signal.

        The server prints 'DIARIZATION_READY:<port>' after model load.
        Timeout is generous (180s) because model download can be slow.
        """
        if not self._process or not self._process.stdout:
            return

        start = time.time()
        try:
            for line in self._process.stdout:
                if time.time() - start > timeout:
                    logger.error(
                        f"Diarization sidecar not ready after {timeout}s")
                    break

                decoded = line.decode('utf-8', errors='replace').strip()
                if decoded.startswith('DIARIZATION_READY'):
                    # Parse actual port (for dynamic allocation)
                    parts = decoded.split(':')
                    if len(parts) >= 2:
                        try:
                            self._port = int(parts[1])
                        except ValueError:
                            pass
                    self._ready = True
                    # Set env var so AudioProcessor finds it
                    os.environ['HEVOLVE_DIARIZATION_URL'] = self.ws_url
                    logger.info(
                        f"Diarization sidecar ready on port {self._port}")
                    break

                if not self._running:
                    break
        except Exception as e:
            logger.debug(f"Stdout read error: {e}")

        if not self._ready:
            # Check if process crashed
            if self._process.poll() is not None:
                logger.error(
                    f"Diarization sidecar crashed "
                    f"(exit code {self._process.returncode})")
                self._running = False

    def _cleanup_subprocess(self):
        """atexit handler — kill orphan subprocess."""
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
