"""Backend Repair Tools — agent-callable wrappers over Nunba's
per-backend TTS venv install/wipe layer.

Why this exists
---------------
``error_advice.handle_exception(category='tts.probe',
context={'backend': 'indic_parler'})`` creates ``self_heal`` AgentGoals
that the coding daemon picks up.  The autoresearch tool family in
``autoevolve_code_tools.py`` only does source edits — useful when the
fix is "patch a Python file", useless when the fix is "the venv at
``~/Documents/Nunba/data/venvs/indic_parler/`` is missing the
``parler_tts`` package".  Source-edit alone leaves the live broken
venv broken until the next rebuild.

This module gives the agent a tool that actually repairs the venv:
``repair_backend_venv(backend_name, wipe_first=False)`` calls Nunba's
canonical ``install_backend_full()``, the same function the user-side
"Set up TTS" UI uses.

Wired in
--------
* ``mcp_http_bridge.py`` registers ``BACKEND_REPAIR_TOOLS`` for autogen
  via the same module-list registration loop that exposes
  ``AUTOEVOLVE_CODE_TOOLS`` / ``AUTO_EVOLVE_TOOLS`` /
  ``THOUGHT_EXPERIMENT_TOOLS`` — see
  ``mcp_http_bridge._load_tools`` around the registration tuple list.
* ``goal_manager._build_self_heal_prompt`` mentions
  ``repair_backend_venv`` when the goal's ``category`` is one of the
  TTS / subprocess install-failure shapes AND ``context.backend`` is
  set on the goal config.

Bundled-mode-only contract
--------------------------
In source-mode HARTOS (a checkout without the Nunba freeze), Nunba's
``tts.*`` modules are not on ``sys.path``.  The tool returns a
graceful ``{success: False, message: 'Backend repair tools require
the Nunba bundled environment ...'}`` JSON so the agent can mark the
goal failed instead of crashing the autogen group.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('hevolve.backend_repair')


def _get_known_backends() -> set:
    """Return engine_ids known to HARTOS's ``ENGINE_REGISTRY``.

    Single source of truth — never hand-maintain a second list.  When
    the registry can't be loaded (HARTOS-side circular import during
    boot, missing module), returns an empty set so the caller fails
    closed with a clear message rather than silently accepting any
    string.
    """
    try:
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
        return {spec.engine_id for spec in ENGINE_REGISTRY.values()}
    except Exception as e:
        logger.warning(
            f"backend_repair_tools: ENGINE_REGISTRY unavailable for "
            f"validation ({type(e).__name__}: {e})"
        )
        return set()


def _resolve_log_path(backend_name: str) -> Optional[str]:
    """Return the path Nunba writes pip output to for this backend.

    Mirrors ``tts.backend_venv._venv_log_path`` so the agent can grep
    a specific file when ``install_backend_full`` returns False.
    Best-effort — returns None if the path cannot be derived (the
    repair still runs; only the diagnostic hint is missing).
    """
    from core.platform_paths import get_log_dir
    log_dir = Path(get_log_dir())
    try:
        return str(log_dir / f'venv_{backend_name}.log')
    except Exception:
        return None


def repair_backend_venv(backend_name: str, wipe_first: bool = False) -> str:
    """Reinstall a TTS backend's per-engine venv.

    Use this tool when a ``self_heal`` goal carries
    ``category='tts.probe'``, ``category='tts.install'``,
    ``category='tts.install.self_heal_exhausted'``, or
    ``category='subprocess.tool_load'`` AND ``context.backend`` names
    a broken backend.

    The work is delegated to Nunba's existing
    ``tts.package_installer.install_backend_full`` — the same function
    the user-side "Set up TTS" UI calls.  That function:

      1. Creates the per-engine venv if missing (idempotent).
      2. Routes the canonical pip_install_plan from
         ``ENGINE_REGISTRY[backend].pip_install_plan`` into either the
         backend's venv (when ``install_target='venv'``) or the main
         interpreter (when ``install_target='main'``).
      3. Applies post-install patches.
      4. Downloads model weights via huggingface_hub.

    Args:
        backend_name: One of the engine_ids in
            ``integrations.channels.media.tts_router.ENGINE_REGISTRY``.
            Common names: ``'indic_parler'``, ``'kokoro'``,
            ``'melotts'``, ``'f5_tts'``, ``'chatterbox_turbo'``,
            ``'chatterbox'``, ``'omnivoice'``, ``'piper'``,
            ``'pocket_tts'``, ``'neutts_air'``.
        wipe_first: When True, delete the existing venv directory
            before reinstall.  Use after corruption, partial installs,
            or when changing transitive-dep cages.  Default False
            (idempotent reinstall — pip skips already-satisfied specs).

    Consent: nothing is installed or wiped without the owner's yes for
    ``tts:<backend_name>`` (``capability_setup.ask_owner_for_setup``).
    Without it the tool returns ``success=False`` with ``consent`` set to
    ``'asked'`` / ``'declined'`` / ``'unavailable'`` and touches nothing.

    Returns:
        JSON string with:
            ``success`` (bool): pip + model_weights both succeeded.
            ``backend`` (str): the validated backend name.
            ``message`` (str): human-readable status.
            ``log_path`` (str | None): path to the venv install log
                (``~/Documents/Nunba/logs/venv_<backend>.log``).
            ``wiped`` (bool): whether ``wipe_venv`` was actually called.
    """
    log_path = _resolve_log_path(backend_name)

    # Validate against ENGINE_REGISTRY — prevents path-traversal /
    # arbitrary-name injection BEFORE any filesystem touch.
    known = _get_known_backends()
    if not known:
        return json.dumps({
            'success': False,
            'backend': backend_name,
            'message': (
                'Cannot validate backend: HARTOS ENGINE_REGISTRY '
                'unavailable in this process. Likely a non-bundled '
                'HARTOS server without tts_router on sys.path.'
            ),
            'log_path': log_path,
            'wiped': False,
        })
    if backend_name not in known:
        return json.dumps({
            'success': False,
            'backend': backend_name,
            'message': (
                f"Unknown backend {backend_name!r}. "
                f"Known backends: {sorted(known)}"
            ),
            'log_path': log_path,
            'wiped': False,
        })

    # The owner's consent, checked HERE so it holds whoever calls the tool
    # (a self_heal goal, the bootstrap provisioner, any agent): installing
    # or reinstalling an engine spends the owner's disk and bandwidth.  The
    # same ask capability_setup makes (scope tts:<engine>); a missing answer
    # files the card, and nothing is touched until the owner says yes.
    try:
        from integrations.agent_engine.capability_setup import (
            ask_owner_for_setup,
        )
        consent = ask_owner_for_setup(
            f'tts:{backend_name}',
            reason=(
                f"Set up the {backend_name} voice? It downloads and installs "
                f"its packages and model on this computer."
            ),
        )
    except Exception as e:  # noqa: BLE001 -- no answer means no install
        logger.warning(f"repair_backend_venv: consent for {backend_name!r} "
                       f"could not be checked ({type(e).__name__}: {e})")
        consent = 'unavailable'
    if consent != 'granted':
        # No owner at all (a server) is not "the owner has not answered":
        # say what the node's operator does instead of waiting for nobody.
        from integrations.agent_engine.capability_setup import (
            NO_OWNER_REMEDY, setup_owner,
        )
        no_owner = consent == 'unavailable' and setup_owner() is None
        if no_owner:
            message = (f"Not installed: tts:{backend_name}. "
                       f"{NO_OWNER_REMEDY} Do not retry until then.")
        else:
            message = (f"Not installed: the owner has not allowed setting "
                       f"up tts:{backend_name} (consent {consent}). Nothing "
                       f"was changed; do not retry until the owner answers.")
        return json.dumps({
            'success': False,
            'backend': backend_name,
            'consent': consent,
            'no_owner': no_owner,
            'message': message,
            'log_path': log_path,
            'wiped': False,
        })

    # Lazy-import Nunba's venv layer.  In source-mode HARTOS the
    # imports fail — return a structured "not in bundled" message.
    try:
        from tts.backend_venv import wipe_venv as _wipe_venv  # type: ignore
        from tts.package_installer import install_backend_full  # type: ignore
    except ImportError as e:
        return json.dumps({
            'success': False,
            'backend': backend_name,
            'message': (
                f'Backend repair requires the Nunba bundled environment '
                f'(tts.backend_venv + tts.package_installer must be '
                f'importable). ImportError: {e}. Source-mode HARTOS '
                f'deploys cannot reinstall TTS venvs — the agent should '
                f'mark this self_heal goal failed and the operator '
                f'should run the repair from a Nunba install.'
            ),
            'log_path': log_path,
            'wiped': False,
        })

    wiped = False
    if wipe_first:
        try:
            _wipe_venv(backend_name)
            wiped = True
            logger.info(f"repair_backend_venv: wiped {backend_name!r}")
        except Exception as e:
            return json.dumps({
                'success': False,
                'backend': backend_name,
                'message': (
                    f'wipe_venv({backend_name!r}) failed: '
                    f'{type(e).__name__}: {e}'
                ),
                'log_path': log_path,
                'wiped': False,
            })

    try:
        ok, msg = install_backend_full(backend_name)
    except Exception as e:
        return json.dumps({
            'success': False,
            'backend': backend_name,
            'message': (
                f'install_backend_full({backend_name!r}) raised: '
                f'{type(e).__name__}: {e}'
            ),
            'log_path': log_path,
            'wiped': wiped,
        })

    return json.dumps({
        'success': bool(ok),
        'backend': backend_name,
        'message': str(msg),
        'log_path': log_path,
        'wiped': wiped,
    })


def next_tts_venv_to_provision() -> str:
    """Which TTS engine the venv provisioner should set up next.

    The first engine, in ENGINE_REGISTRY order, whose spec installs into its
    own venv (install_target 'venv'), whose venv is not healthy
    (tts.backend_venv.is_venv_healthy), and that the owner has not declined
    (capability_setup.setup_declined, a read that never asks).  Declined
    engines are skipped and listed: the bootstrap goal used to pick "the
    first unhealthy engine" in its prose, so an engine the owner said no to
    was picked on every tick and the others were never asked.

    Returns JSON: ``engine`` (str | None), ``declined`` (engines skipped
    because the owner said no), ``done`` (True when there is nothing left
    to offer), and ``error`` when the choice cannot be made here (no
    ENGINE_REGISTRY, or Nunba's venv layer not importable).
    """
    known = _get_known_backends()
    if not known:
        return json.dumps({'engine': None, 'declined': [], 'done': False,
                           'error': 'ENGINE_REGISTRY unavailable in this '
                                    'process'})
    try:
        from tts.backend_venv import is_venv_healthy  # type: ignore
    except ImportError as e:
        return json.dumps({
            'engine': None, 'declined': [], 'done': False,
            'error': (f'Choosing an engine requires the Nunba bundled '
                      f'environment (tts.backend_venv importable). '
                      f'ImportError: {e}'),
        })
    from integrations.agent_engine.capability_setup import setup_declined
    from integrations.channels.media.tts_router import ENGINE_REGISTRY
    declined = []
    for engine_id, spec in ENGINE_REGISTRY.items():
        if getattr(spec, 'install_target', 'main') != 'venv':
            continue
        try:
            if is_venv_healthy(engine_id):
                continue
        except Exception as e:
            logger.warning(f"next_tts_venv_to_provision: health of "
                           f"{engine_id!r} unreadable ({e}); offering it")
        if setup_declined(f'tts:{engine_id}'):
            declined.append(engine_id)
            continue
        return json.dumps({'engine': engine_id, 'declined': declined,
                           'done': False})
    return json.dumps({'engine': None, 'declined': declined, 'done': True})


# Tool registration list — same shape as AUTOEVOLVE_CODE_TOOLS /
# AUTO_EVOLVE_TOOLS / THOUGHT_EXPERIMENT_TOOLS.  Consumed by
# mcp_http_bridge._load_tools via the registration tuple list around
# line 998.  The 'tags' field is documentary — the bridge's dedup is
# by name, and the goal_manager.tool_tags filter is per goal_type.
BACKEND_REPAIR_TOOLS = [
    {
        'name': 'repair_backend_venv',
        'func': repair_backend_venv,
        'description': (
            'Reinstall a broken TTS backend venv (pip install plan + '
            'model weights). Use when a self_heal goal carries '
            'category in {tts.probe, tts.install, '
            'tts.install.self_heal_exhausted, subprocess.tool_load} '
            'AND context.backend names the failed backend. Optional '
            'wipe_first=True for full clean reinstall (use after '
            'corruption / version-conflict situations).'
        ),
        'tags': ['coding', 'self_heal', 'tts'],
    },
    {
        'name': 'next_tts_venv_to_provision',
        'func': next_tts_venv_to_provision,
        'description': (
            'Which TTS engine to set up next: the first venv engine that is '
            'not healthy and that the owner has not declined (declined ones '
            'are skipped and listed). Returns JSON {engine, declined, done}; '
            'call repair_backend_venv(backend_name=engine) with it.'
        ),
        'tags': ['provision', 'self_heal', 'tts'],
    },
]
