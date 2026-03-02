"""
Service Lifecycle Manager — Manages external remote desktop engines as HARTOS services.

Follows the NodeWatchdog register pattern (heartbeat-based process monitoring)
and CodingToolBackend detection pattern (detect/install/start lifecycle).

Each engine (RustDesk, Sunshine, Moonlight) is managed as a background service:
  detect → install prompt → start → health monitor → auto-restart

The ServiceManager is the lifecycle layer; the Orchestrator (orchestrator.py)
is the brain that decides *which* engine to use and coordinates sessions.
"""

import logging
import platform
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')


class EngineState(Enum):
    UNKNOWN = 'unknown'
    NOT_INSTALLED = 'not_installed'
    INSTALLED = 'installed'       # Detected but not running
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPED = 'stopped'
    ERROR = 'error'


@dataclass
class EngineInfo:
    """Runtime state for a managed engine."""
    name: str
    state: EngineState = EngineState.UNKNOWN
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_health_check: Optional[float] = None
    healthy: bool = False
    restart_count: int = 0
    error: Optional[str] = None


class EngineService:
    """Lifecycle wrapper for a single external remote desktop engine.

    Delegates detection and control to the appropriate bridge module.
    """

    def __init__(self, engine_name: str):
        self.engine_name = engine_name
        self.info = EngineInfo(name=engine_name)
        self._bridge = None

    def _get_bridge(self):
        """Lazy-load the bridge for this engine."""
        if self._bridge is not None:
            return self._bridge

        try:
            if self.engine_name == 'rustdesk':
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                self._bridge = get_rustdesk_bridge()
            elif self.engine_name == 'sunshine':
                from integrations.remote_desktop.sunshine_bridge import get_sunshine_bridge
                self._bridge = get_sunshine_bridge()
            elif self.engine_name == 'moonlight':
                from integrations.remote_desktop.sunshine_bridge import get_moonlight_bridge
                self._bridge = get_moonlight_bridge()
        except Exception as e:
            logger.debug(f"Bridge load failed for {self.engine_name}: {e}")
        return self._bridge

    def detect(self) -> bool:
        """Check if the engine binary is installed on this system."""
        bridge = self._get_bridge()
        if bridge and bridge.available:
            if self.info.state == EngineState.UNKNOWN:
                self.info.state = EngineState.INSTALLED
            return True
        self.info.state = EngineState.NOT_INSTALLED
        return False

    def install_command(self) -> str:
        """Get platform-specific install command."""
        bridge = self._get_bridge()
        if bridge and hasattr(bridge, 'get_install_command'):
            return bridge.get_install_command()
        return f"Visit https://hartosai.com/docs/remote-desktop/{self.engine_name}"

    def start(self) -> bool:
        """Start the engine service/daemon."""
        bridge = self._get_bridge()
        if not bridge or not bridge.available:
            self.info.state = EngineState.NOT_INSTALLED
            self.info.error = 'Not installed'
            return False

        self.info.state = EngineState.STARTING
        try:
            if hasattr(bridge, 'start_service'):
                ok = bridge.start_service()
            else:
                # Moonlight doesn't have a persistent service
                self.info.state = EngineState.INSTALLED
                return True

            if ok:
                self.info.state = EngineState.RUNNING
                self.info.started_at = time.time()
                self.info.healthy = True
                self.info.error = None
                logger.info(f"Engine {self.engine_name} started")
                return True
            else:
                self.info.state = EngineState.ERROR
                self.info.error = 'start_service returned False'
                return False
        except Exception as e:
            self.info.state = EngineState.ERROR
            self.info.error = str(e)
            logger.warning(f"Failed to start {self.engine_name}: {e}")
            return False

    def stop(self) -> bool:
        """Stop the engine service."""
        bridge = self._get_bridge()
        if not bridge:
            return False

        try:
            if hasattr(bridge, 'stop_service'):
                bridge.stop_service()
            self.info.state = EngineState.STOPPED
            self.info.pid = None
            self.info.healthy = False
            logger.info(f"Engine {self.engine_name} stopped")
            return True
        except Exception as e:
            self.info.error = str(e)
            return False

    def is_running(self) -> bool:
        """Check if engine process is alive and healthy."""
        bridge = self._get_bridge()
        if not bridge:
            return False

        try:
            if hasattr(bridge, 'is_service_running'):
                running = bridge.is_service_running()
            elif hasattr(bridge, 'is_running'):
                running = bridge.is_running()
            else:
                # Moonlight is on-demand, not a service
                running = bridge.available

            self.info.last_health_check = time.time()
            self.info.healthy = running
            if running and self.info.state != EngineState.RUNNING:
                self.info.state = EngineState.RUNNING
            elif not running and self.info.state == EngineState.RUNNING:
                self.info.state = EngineState.STOPPED
            return running
        except Exception:
            self.info.healthy = False
            return False

    def restart(self) -> bool:
        """Stop then start the engine."""
        self.stop()
        time.sleep(1)
        ok = self.start()
        if ok:
            self.info.restart_count += 1
        return ok

    def get_status(self) -> dict:
        """Get engine status dict."""
        uptime = None
        if self.info.started_at and self.info.state == EngineState.RUNNING:
            uptime = time.time() - self.info.started_at

        return {
            'engine': self.engine_name,
            'state': self.info.state.value,
            'installed': self.detect(),
            'running': self.info.state == EngineState.RUNNING,
            'healthy': self.info.healthy,
            'pid': self.info.pid,
            'uptime_seconds': uptime,
            'restart_count': self.info.restart_count,
            'error': self.info.error,
            'install_command': self.install_command() if not self.detect() else None,
        }


class ServiceManager:
    """Manages lifecycle of all remote desktop engines.

    Singleton via get_service_manager(). Provides:
    - Engine detection and startup
    - Health monitoring via NodeWatchdog integration
    - Auto-restart on crash
    - Unified status across all engines
    """

    # Engines that run as persistent services
    SERVICE_ENGINES = ['rustdesk', 'sunshine']
    # Engines that are on-demand (no persistent service)
    ON_DEMAND_ENGINES = ['moonlight']
    ALL_ENGINES = SERVICE_ENGINES + ON_DEMAND_ENGINES

    def __init__(self):
        self._engines: Dict[str, EngineService] = {
            name: EngineService(name) for name in self.ALL_ENGINES
        }
        self._watchdog_registered = False
        self._lock = threading.Lock()

    def ensure_engine(self, engine_name: str) -> Tuple[bool, str]:
        """Ensure an engine is installed and running.

        Returns:
            (ready, message) — ready=True if engine is available for use.
        """
        if engine_name == 'native':
            return True, 'Native engine always available'

        service = self._engines.get(engine_name)
        if not service:
            return False, f'Unknown engine: {engine_name}'

        # Step 1: Detect
        if not service.detect():
            cmd = service.install_command()
            return False, f'{engine_name} not installed. Install with:\n{cmd}'

        # Step 2: Start if service-type engine and not running
        if engine_name in self.SERVICE_ENGINES:
            if not service.is_running():
                ok = service.start()
                if not ok:
                    return False, f'{engine_name} failed to start: {service.info.error}'

        return True, f'{engine_name} ready'

    def start_all_available(self) -> Dict[str, dict]:
        """Start all detected engines. Returns status map."""
        results = {}
        for name in self.SERVICE_ENGINES:
            service = self._engines[name]
            if service.detect():
                if not service.is_running():
                    service.start()
                results[name] = service.get_status()
            else:
                results[name] = service.get_status()

        # On-demand engines: just detect
        for name in self.ON_DEMAND_ENGINES:
            service = self._engines[name]
            service.detect()
            results[name] = service.get_status()

        results['native'] = {
            'engine': 'native',
            'state': 'running',
            'installed': True,
            'running': True,
            'healthy': True,
        }
        return results

    def stop_engine(self, engine_name: str) -> bool:
        """Stop a specific engine."""
        service = self._engines.get(engine_name)
        if service:
            return service.stop()
        return False

    def stop_all(self) -> None:
        """Stop all running engines."""
        for service in self._engines.values():
            if service.info.state == EngineState.RUNNING:
                service.stop()

    def get_engine_status(self, engine_name: str) -> dict:
        """Get status for a specific engine."""
        if engine_name == 'native':
            return {
                'engine': 'native',
                'state': 'running',
                'installed': True,
                'running': True,
                'healthy': True,
            }
        service = self._engines.get(engine_name)
        if service:
            return service.get_status()
        return {'engine': engine_name, 'error': 'Unknown engine'}

    def get_all_status(self) -> Dict[str, dict]:
        """Get status for all engines."""
        result = {}
        for name, service in self._engines.items():
            result[name] = service.get_status()
        result['native'] = {
            'engine': 'native',
            'state': 'running',
            'installed': True,
            'running': True,
            'healthy': True,
        }
        return result

    def register_with_watchdog(self) -> bool:
        """Register running engines with NodeWatchdog for auto-restart.

        Reuses security/node_watchdog.py:64 pattern:
        watchdog.register(name, expected_interval, restart_fn, stop_fn)
        """
        if self._watchdog_registered:
            return True

        try:
            from security.node_watchdog import get_watchdog
            watchdog = get_watchdog()
            if not watchdog:
                logger.debug("NodeWatchdog not running, skipping registration")
                return False

            for name in self.SERVICE_ENGINES:
                service = self._engines[name]
                if service.info.state == EngineState.RUNNING:
                    watchdog.register(
                        f'rd_{name}',
                        expected_interval=30,
                        restart_fn=service.restart,
                        stop_fn=service.stop,
                    )
                    logger.info(f"Registered {name} with NodeWatchdog")

            self._watchdog_registered = True
            return True
        except Exception as e:
            logger.debug(f"Watchdog registration failed: {e}")
            return False

    def heartbeat(self, engine_name: str) -> None:
        """Send heartbeat for an engine (called by health check loop).

        Reuses NodeWatchdog heartbeat pattern.
        """
        try:
            from security.node_watchdog import get_watchdog
            watchdog = get_watchdog()
            if watchdog:
                watchdog.heartbeat(f'rd_{engine_name}')
        except Exception:
            pass

    def health_check_all(self) -> Dict[str, bool]:
        """Run health checks on all engines and send heartbeats."""
        results = {}
        for name in self.SERVICE_ENGINES:
            service = self._engines[name]
            if service.info.state in (EngineState.RUNNING, EngineState.STARTING):
                running = service.is_running()
                results[name] = running
                if running:
                    self.heartbeat(name)
        results['native'] = True
        return results


# ── Singleton ────────────────────────────────────────────────

_service_manager: Optional[ServiceManager] = None


def get_service_manager() -> ServiceManager:
    """Get or create the singleton ServiceManager."""
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceManager()
    return _service_manager
