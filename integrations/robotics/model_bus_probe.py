"""
Robot Model-Bus capability probe — the embodied twin of hart-compat-smoketest.

WHY:
  hart-compat-smoketest MEASURES (does not claim) that each foreign-OS runtime
  can actually execute a command, writing an honest per-runtime verdict to
  /run/hart/compat-status. The embodied vision needs the SAME discipline for a
  robot: before a robot trusts this node for its brain, PROVE it can actually
  reach the core intelligences over the Model Bus — LLM (language), vision
  (VLM), and the embodied VLA / world-model path — instead of assuming they are
  up. "A robot's give-me-intelligence is the SAME Model Bus call as a desktop
  app's" (hartos_universal_ai_native_os_vision). This probe is that proof.

HONEST SCOPE — a REAL reachability probe, not a claim and not a full policy run:
  * model_bus  — GET {bus}/health actually answered.
  * llm        — a real tiny {bus}/v1/chat round-trip returned a non-error
                 answer (`ok`); the bus answered but no LLM backend is loaded
                 (`no-model`); the bus is unreachable (`down`).
  * vision     — a vision backend is REGISTERED + reachable via {bus}/v1/models
                 (`ready`); none registered (`no-model`); bus down (`down`).
                 Like android=ready, we do NOT force a full VLM inference (it
                 needs an image file + a warm GPU model) in a boot smoke-test.
  * vla        — the embodied VLA / world-model surface is REACHABLE: the model
                 catalog carries the Qwen-RobotSuite embodied entries AND the
                 WorldModelBridge reports a live learning path (`ready`); the
                 metadata is present but no live world-model backend
                 (`no-backend`); neither (`no-model`).
  * intelligence_api — the on-node 7-intelligence fusion API imports + answers a
                 cheap local stat call (`ok`) so a robot's /think endpoint exists.
  * robots     — count of robots currently registered on this node (context).

NEVER-BLOCK / FAIL-SAFE (the hart smoke-test contract):
  * Every network call is bounded by an explicit timeout and wrapped so a
    hang/error records the honest fail-state and the probe CONTINUES — one dead
    capability never aborts the others.
  * probe() NEVER raises and main() ALWAYS exits 0 — this is a measurement, not
    a gate. The nix oneshot runs it IN PARALLEL with the desktop (never before
    greetd), so it can never delay first paint.
"""
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger('hevolve_robotics')

# The honest per-capability status file. One `key=value` line per capability,
# in /run (tmpfs) so it is re-derived every boot — a reachability verdict must
# never outlive the backend state it measured (mirrors /run/hart/compat-status).
DEFAULT_STATUS_PATH = '/run/hart/robot-capability-status'

# Bounded per-probe timeouts (seconds). A real LLM round-trip can be slow on a
# cold model, but this is a post-boot oneshot in parallel with the desktop, so a
# modest cap is honest without wedging the unit (the nix TimeoutStartSec is the
# outer belt). Overridable via env for constrained substrates.
_HEALTH_TIMEOUT = float(os.environ.get('HART_ROBOT_PROBE_HEALTH_TIMEOUT', '3'))
_LLM_TIMEOUT = float(os.environ.get('HART_ROBOT_PROBE_LLM_TIMEOUT', '20'))
_MODELS_TIMEOUT = float(os.environ.get('HART_ROBOT_PROBE_MODELS_TIMEOUT', '5'))


def _default_base_url() -> str:
    """Resolve the Model Bus HTTP base URL (the transport a robot/APK/Wine app
    reaches — the same one on every substrate)."""
    port = os.environ.get('HART_MODEL_BUS_PORT') or os.environ.get(
        'MODEL_BUS_HTTP_PORT')
    if not port:
        try:
            from core.port_registry import get_port
            port = str(get_port('model_bus', 6790))
        except Exception:
            port = '6790'
    return f'http://localhost:{port}'


def _resolve_http():
    """Return (get, post) callables — the pooled, timeout-safe HTTP clients.

    Lazily imported so the module stays importable (and unit-testable) without
    the full HTTP stack; callers may inject their own for hermetic tests.
    """
    from core.http_pool import pooled_get, pooled_post
    return pooled_get, pooled_post


def _probe_model_bus_health(base_url: str, http_get: Callable) -> str:
    """`ok` iff GET {base}/health answered < 500, else `down`."""
    try:
        resp = http_get(f'{base_url}/health', timeout=_HEALTH_TIMEOUT)
        return 'ok' if getattr(resp, 'status_code', 500) < 500 else 'down'
    except Exception as e:
        logger.debug("Model Bus health probe failed: %s", e)
        return 'down'


def _probe_llm(base_url: str, http_post: Callable) -> str:
    """Real tiny round-trip through the bus LLM path.

    `ok` = a non-error answer came back; `no-model` = the bus answered but no
    LLM backend served it; `down` = the bus was unreachable.
    """
    try:
        resp = http_post(
            f'{base_url}/v1/chat',
            json={'prompt': 'ping', 'max_tokens': 1},
            timeout=_LLM_TIMEOUT,
        )
    except Exception as e:
        logger.debug("LLM probe transport failed: %s", e)
        return 'down'
    if getattr(resp, 'status_code', 500) >= 500:
        return 'down'
    try:
        data = resp.json()
    except Exception:
        return 'no-model'
    if not isinstance(data, dict):
        return 'no-model'
    # A served answer has a non-empty 'response' and no 'error'. The bus returns
    # {'error': 'No LLM backend available'} etc. when nothing can serve.
    if data.get('error'):
        return 'no-model'
    return 'ok' if data.get('response') else 'no-model'


def _probe_vision(base_url: str, http_get: Callable) -> str:
    """`ready` iff a vision backend is registered + reachable via /v1/models."""
    try:
        resp = http_get(f'{base_url}/v1/models', timeout=_MODELS_TIMEOUT)
    except Exception as e:
        logger.debug("Vision probe transport failed: %s", e)
        return 'down'
    if getattr(resp, 'status_code', 500) >= 500:
        return 'down'
    try:
        models = (resp.json() or {}).get('models', [])
    except Exception:
        return 'no-model'
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and str(m.get('type', '')).lower() == 'vision':
            return 'ready'
    return 'no-model'


def _probe_vla() -> str:
    """Embodied VLA / world-model reachability, in-process (no network needed).

    `ready`      = the Qwen-RobotSuite embodied entries are in the model catalog
                   AND the WorldModelBridge reports a live learning path.
    `no-backend` = the embodied metadata is present but no live world model.
    `no-model`   = the embodied catalog itself is unavailable.
    """
    has_embodied_entry = False
    try:
        from integrations.service_tools.model_catalog import (
            get_catalog, ModelType,
        )
        # get_catalog() populates from subsystems on first access, which runs
        # _populate_embodied_models() (the Qwen-RobotSuite VLA entries).
        catalog = get_catalog()
        has_embodied_entry = bool(catalog.list_by_type(ModelType.EMBODIED))
    except Exception as e:
        logger.debug("Embodied catalog probe skipped: %s", e)

    if not has_embodied_entry:
        return 'no-model'

    # Metadata present — is a live world-model path reachable?
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        health = get_world_model_bridge().check_health()
        if isinstance(health, dict) and health.get('healthy'):
            return 'ready'
    except Exception as e:
        logger.debug("World-model health probe skipped: %s", e)
    return 'no-backend'


def _probe_intelligence_api() -> tuple:
    """The on-node robot /think fusion API. Returns (status, robot_count).

    `ok` = it imports + answers a cheap local stat call.
    """
    try:
        from integrations.robotics.intelligence_api import get_robot_api
        stats = get_robot_api().get_hive_stats()
        count = int(stats.get('total_robots', 0)) if isinstance(stats, dict) else 0
        return 'ok', count
    except Exception as e:
        logger.debug("Intelligence API probe failed: %s", e)
        return 'unavailable', 0


def probe(
    status_path: Optional[str] = DEFAULT_STATUS_PATH,
    base_url: Optional[str] = None,
    http_get: Optional[Callable] = None,
    http_post: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Measure every core intelligence a robot reaches over the Model Bus.

    Writes one honest `key=value` line per capability to ``status_path`` (also
    echoed to the journal) and returns the same verdicts as a dict. NEVER
    raises — a failing probe records its fail-state; the file/return always
    reflect what was actually observed.

    Args:
        status_path: where to write the verdicts (None = don't write, just
            return — used by tests). Defaults to /run/hart/robot-capability-status.
        base_url: Model Bus HTTP base (default: resolved from the port registry).
        http_get / http_post: injectable HTTP clients (default: pooled clients),
            so the probe is hermetically unit-testable without a live bus.
    """
    base_url = base_url or _default_base_url()
    if http_get is None or http_post is None:
        try:
            _g, _p = _resolve_http()
        except Exception as e:
            logger.debug("HTTP pool unavailable for robot probe: %s", e)
            _g = _p = None
        http_get = http_get or _g
        http_post = http_post or _p

    results: Dict[str, Any] = {}

    if http_get is not None:
        results['model_bus'] = _probe_model_bus_health(base_url, http_get)
    else:
        results['model_bus'] = 'down'

    if http_post is not None and results['model_bus'] != 'down':
        results['llm'] = _probe_llm(base_url, http_post)
    else:
        results['llm'] = 'down'

    if http_get is not None and results['model_bus'] != 'down':
        results['vision'] = _probe_vision(base_url, http_get)
    else:
        results['vision'] = 'down'

    # VLA + the fusion API are in-process — independent of the bus HTTP being up.
    results['vla'] = _probe_vla()
    intel_status, robot_count = _probe_intelligence_api()
    results['intelligence_api'] = intel_status
    results['robots'] = str(robot_count)

    if status_path:
        _write_status(status_path, results)
    return results


def _write_status(status_path: str, results: Dict[str, Any]) -> None:
    """Write the verdicts as `key=value` lines (fresh each boot) + journal echo.

    Best-effort: a write failure is logged but never raised — the return value
    still carries the measurement.
    """
    try:
        parent = os.path.dirname(status_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        lines = []
        for key, value in results.items():
            lines.append(f'{key}={value}')
            logger.info('[hart-robot-probe] %s = %s', key, value)
        with open(status_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except OSError as e:
        logger.warning("Robot capability status write failed (%s): %s",
                       status_path, e)


def main() -> int:
    """Oneshot entry for the nix unit. Always returns 0 (a measurement, not a
    gate) — mirrors hart-compat-smoketest's `exit 0`."""
    logging.basicConfig(level=logging.INFO)
    try:
        results = probe()
        logger.info("Robot Model-Bus capability probe complete: %s",
                    json.dumps(results))
    except Exception as e:  # belt-and-braces: probe() shouldn't raise, but never fail boot
        logger.warning("Robot capability probe errored (non-fatal): %s", e)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
