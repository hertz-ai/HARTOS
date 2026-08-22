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

    def __init__(self, manifest: Optional[dict] = None, check_interval: int = None,
                 code_root: str = None):
        self._manifest = manifest or {}
        self._expected_hash = self._manifest.get('code_hash', '')
        self._check_interval = check_interval or int(
            os.environ.get('HEVOLVE_TAMPER_CHECK_INTERVAL', '300'))
        self._code_root = code_root
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._tampered = False
        self._boot_manifest_snapshot = None
        # How many cycles between UNCONDITIONAL full verifies.  Between
        # them, each cycle is a stat-only sweep (metadata reads, no file
        # contents), so the steady-state cost of the monitor on a running
        # system is milliseconds per cycle.  The slow full walk exists to
        # catch an attacker who back-dates mtimes to fool the stat sweep.
        self._full_every = max(1, int(
            os.environ.get('HEVOLVE_TAMPER_FULL_EVERY', '12')))
        self._cycles = 0
        self._baseline_mode = not self._expected_hash
        if not self._baseline_mode:
            # Purge __pycache__ before snapshot - blocks bytecode injection.
            # Manifest mode (central containers) only: on a bundled desktop
            # this would force recompilation of everything imported after
            # init_social, a boot-time cost the baseline mode refuses to add.
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
        # Boot-baseline mode: no signed manifest to compare against — the
        # manifest is CENTRAL-ONLY by policy, so every bundled desktop lands
        # here (and until 2026-08-22 was simply never monitored).  The
        # expected hash is DERIVED from the boot snapshot just taken —
        # zero additional IO — and means "the code as it was at boot":
        # the monitor then answers "did the code change since boot", which
        # is exactly what a periodic tamper check is for.  What it cannot
        # see is a modification made while the app was closed — that is a
        # provenance question, and self-reported hashes cannot answer it on
        # central either (see release_hash_registry.has_trust_basis); it
        # belongs to the challenge/attestation endpoints.
        if self._baseline_mode and self._boot_manifest_snapshot:
            try:
                from security.node_integrity import manifest_to_code_hash
                self._expected_hash = manifest_to_code_hash(
                    self._boot_manifest_snapshot)
            except Exception as e:
                logger.warning(f"Runtime monitor boot baseline failed: {e}")
                self._expected_hash = ''
        # Stat baseline for the cheap per-cycle sweep: {rel: (mtime_ns, size)}.
        # One os.stat per file, no reads.
        self._stat_baseline = self._stat_sweep()

    def _stat_sweep(self) -> dict:
        """{rel_path: (mtime_ns, size)} for every tracked .py — metadata only."""
        result = {}
        try:
            from pathlib import Path
            from security.node_integrity import _collect_py_files, _CODE_ROOT
            root = Path(self._code_root or _CODE_ROOT)
            for rel, path in _collect_py_files(root, root):
                try:
                    st = path.stat()
                    result[rel] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    result[rel] = (0, -1)
        except Exception as e:
            logger.debug(f"Runtime monitor stat sweep failed: {e}")
        return result

    def start(self) -> None:
        """Start the background monitoring thread (daemon=True)."""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Runtime integrity monitor started (interval={self._check_interval}s, "
            f"mode={'boot-baseline' if self._baseline_mode else 'signed-manifest'})")

    def stop(self) -> None:
        """Stop the monitor."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking checks."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('runtime_monitor')
        except Exception:
            pass

    def _check_loop(self) -> None:
        """Background loop: periodic code hash + guardrail hash verification."""
        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            self._wd_heartbeat()
            try:
                # Tiered check, so a running system pays ~nothing.
                #   every cycle : stat-only sweep (metadata, no file reads)
                #   full verify : only when the sweep sees a change, or every
                #                 _full_every cycles to catch mtime back-dating
                # The full verify hashes ACTUAL BYTES (force_walk) — the
                # default path short-circuits to HEVOLVE_CODE_HASH_PRECOMPUTED,
                # on bundles a constant, which made the old comparison unable
                # to move no matter what was edited on disk.
                self._cycles += 1
                current_stats = self._stat_sweep()
                stats_changed = (current_stats != self._stat_baseline)
                due_full = (self._cycles % self._full_every == 0)
                if stats_changed or due_full:
                    from security.node_integrity import compute_code_hash
                    current_hash = compute_code_hash(
                        self._code_root, force_walk=True)
                    if stats_changed and self._expected_hash and \
                            current_hash == self._expected_hash:
                        # Metadata moved but the bytes did not (a touch, an
                        # AV scan restoring mtimes...).  Adopt the new stats
                        # so a benign touch doesn't force a full walk every
                        # cycle.
                        self._stat_baseline = current_stats
                    if self._expected_hash and current_hash != self._expected_hash:
                        logger.critical(
                            f"TAMPERING DETECTED: code hash changed from "
                            f"{self._expected_hash[:16]}... to {current_hash[:16]}...")
                        self._tampered = True
                        self._on_tamper_detected()
                        return  # Stop checking after tamper
            except Exception as e:
                logger.warning(f"Runtime integrity check error: {e}")

            self._wd_heartbeat()

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

            self._wd_heartbeat()

            # Origin attestation check — detect branding removal
            try:
                from security.origin_attestation import verify_origin
                origin = verify_origin(self._code_root)
                if not origin['genuine']:
                    logger.critical(
                        f"ORIGIN ATTESTATION FAILED: {origin['details']}")
            except Exception:
                pass

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
            current_hash = compute_code_hash(self._code_root, force_walk=True)
            if self._expected_hash and current_hash != self._expected_hash:
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
