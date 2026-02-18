"""
Runtime Integrity Monitor: Background daemon that periodically re-checks code hash
against the boot-time signed manifest. Detects tampering and disconnects from network.
"""
import os
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger('hevolve_security')

_monitor: Optional['RuntimeIntegrityMonitor'] = None


class RuntimeIntegrityMonitor:
    """Background daemon that periodically re-checks code hash against manifest."""

    def __init__(self, manifest: dict, check_interval: int = None, code_root: str = None):
        self._manifest = manifest
        self._expected_hash = manifest.get('code_hash', '')
        self._check_interval = check_interval or int(
            os.environ.get('HEVOLVE_TAMPER_CHECK_INTERVAL', '300'))
        self._code_root = code_root
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._tampered = False
        self._boot_manifest_snapshot = None
        # Purge __pycache__ before snapshot - blocks bytecode injection
        try:
            from security.node_integrity import purge_pycache
            purge_pycache(code_root)
        except Exception:
            pass
        # Snapshot file manifest at boot for diff on tamper
        try:
            from security.node_integrity import compute_file_manifest
            self._boot_manifest_snapshot = compute_file_manifest(code_root)
        except Exception:
            pass

    def start(self) -> None:
        """Start the background monitoring thread (daemon=True)."""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info(f"Runtime integrity monitor started (interval={self._check_interval}s)")

    def stop(self) -> None:
        """Stop the monitor."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _check_loop(self) -> None:
        """Background loop: periodic code hash + guardrail hash verification.

        Skipped entirely when HEVOLVE_TAMPER_CHECK_SKIP=true (read-only FS,
        embedded devices where code can't change at runtime).
        """
        if os.environ.get('HEVOLVE_TAMPER_CHECK_SKIP', '').lower() == 'true':
            logger.info("Runtime monitor: tamper checks disabled (HEVOLVE_TAMPER_CHECK_SKIP)")
            return
        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('runtime_monitor')
            except Exception:
                pass
            try:
                from security.node_integrity import compute_code_hash
                current_hash = compute_code_hash(self._code_root)
                if current_hash != self._expected_hash:
                    logger.critical(
                        f"TAMPERING DETECTED: code hash changed from "
                        f"{self._expected_hash[:16]}... to {current_hash[:16]}...")
                    self._tampered = True
                    self._on_tamper_detected()
                    return  # Stop checking after tamper
            except Exception as e:
                logger.warning(f"Runtime integrity check error: {e}")

            # Guardrail values integrity check
            try:
                from security.hive_guardrails import verify_guardrail_integrity
                if not verify_guardrail_integrity():
                    logger.critical(
                        "GUARDRAIL TAMPERING DETECTED: frozen values hash changed")
                    self._tampered = True
                    self._on_tamper_detected()
                    return
            except Exception as e:
                logger.warning(f"Guardrail integrity check error: {e}")

    def _on_tamper_detected(self) -> None:
        """Respond to tampering: stop gossip, log changed files."""
        # Log which files changed
        try:
            from security.node_integrity import compute_file_manifest
            if self._boot_manifest_snapshot:
                current = compute_file_manifest(self._code_root)
                for path, boot_hash in self._boot_manifest_snapshot.items():
                    cur_hash = current.get(path)
                    if cur_hash != boot_hash:
                        logger.critical(f"TAMPERED FILE: {path}")
                for path in current:
                    if path not in self._boot_manifest_snapshot:
                        logger.critical(f"NEW FILE (post-boot): {path}")
        except Exception:
            pass

        # Stop gossip protocol
        try:
            from integrations.social.peer_discovery import gossip
            gossip.stop()
            logger.critical("Gossip protocol stopped due to code tampering")
        except Exception:
            pass

        self._running = False

    def _check_loop_once_for_test(self) -> None:
        """Run a single integrity check (for testing only)."""
        try:
            from security.node_integrity import compute_code_hash
            current_hash = compute_code_hash(self._code_root)
            if current_hash != self._expected_hash:
                self._tampered = True
        except Exception:
            pass

    @property
    def is_healthy(self) -> bool:
        """Returns False if tampering detected."""
        return not self._tampered


def start_monitor(manifest: dict, code_root: str = None) -> RuntimeIntegrityMonitor:
    """Start the runtime integrity monitor. Called from init_social()."""
    global _monitor
    _monitor = RuntimeIntegrityMonitor(manifest, code_root=code_root)
    _monitor.start()
    return _monitor


def get_monitor() -> Optional[RuntimeIntegrityMonitor]:
    """Get the current monitor instance."""
    return _monitor


def is_code_healthy() -> bool:
    """Quick check: True if no tampering detected. Safe to call even if monitor not started."""
    if _monitor is None:
        return True  # No monitor = no tamper info
    return _monitor.is_healthy
