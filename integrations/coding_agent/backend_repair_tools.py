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
  That serves outside callers only.
* In-process agents get them from the ServiceToolRegistry:
  ``BackendRepairTool`` (bottom of this module), which ``create_recipe``
  and ``reuse_recipe`` register beside the other native tools, tagged
  ``backend_repair``.  A goal or a turn whose words name one of these
  tools resolves that tag (``marketing_tools.detect_goal_tags``), and the
  build-time Tier-1 filter or the per-turn ``attach_for_turn`` attaches
  them.  Keyword discovery (``request_tools``) never offers them.
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


#: JSON-schema types for the repair functions' parameter annotations.
_JSON_TYPES = {str: 'string', bool: 'boolean', int: 'integer', float: 'number'}


def _params_schema(func) -> dict:
    """The registry's flat params_schema for ``func``, read from its own
    signature so the two cannot drift."""
    import inspect
    return {name: {'type': _JSON_TYPES.get(p.annotation, 'string'),
                   'description': name.replace('_', ' ')}
            for name, p in inspect.signature(func).parameters.items()}


def _refusal(name, sig, missing, unknown) -> str:
    """What a call the function cannot take is told: what was wrong and how
    to call it (owner rule: every tool refusal tells the model what to do).
    Before this the model read the bare TypeError text ("missing 1 required
    positional argument") and nothing about the call that works."""
    params = list(sig.parameters.items())
    wrong = []
    if missing:
        wrong.append(f"{', '.join(missing)} "
                     f"{'is' if len(missing) == 1 else 'are'} missing")
    if unknown:
        wrong.append(f"{', '.join(unknown)} "
                     f"{'is not a parameter' if len(unknown) == 1 else 'are not parameters'}"
                     f" of {name}")
    call = (f"{name}("
            + ', '.join(f'{n}=...' for n, p in params if p.default is p.empty)
            + ')')
    takes = (', '.join(f"{n} ({_JSON_TYPES.get(p.annotation, 'string')}, "
                       f"{'required' if p.default is p.empty else 'optional'})"
                       for n, p in params) or 'no parameters')
    return (f"{name} was not run: {'; '.join(wrong)}. Call it again as "
            f"{call}. It takes {takes}.")


def _native_handler(entry):
    """The registry's native handler for one BACKEND_REPAIR_TOOLS entry: the
    model's JSON arguments go to the entry's own function, and an argument
    the model left out (None) keeps the function's default.  A call the
    function cannot take (a required argument missing, a name it does not
    take) is refused with how to call it, and the function does not run."""
    import inspect

    def handler(payload: str) -> str:
        params = {k: v for k, v in json.loads(payload or '{}').items()
                  if v is not None}
        sig = inspect.signature(entry['func'])
        unknown = sorted(set(params) - set(sig.parameters))
        missing = [n for n, p in sig.parameters.items()
                   if p.default is p.empty and n not in params]
        if missing or unknown:
            return json.dumps({'success': False, 'message': _refusal(
                entry['name'], sig, missing, unknown)})
        return entry['func'](**params)
    return handler


class BackendRepairTool:
    """Service-tool registration shim for BACKEND_REPAIR_TOOLS (mirrors
    ExperimentVoteTool / GhPrTool / SeoAuditTool).

    The door every turn opens on both pipelines is
    core.agent_tool_menu.attach_for_turn: it resolves the turn's own words
    and attaches the registry tools whose tags meet the capability tags
    goal_manager.get_tool_tags gives them.  A self-heal turn names
    repair_backend_venv, which detect_goal_tags tags 'backend_repair', and
    goal_manager._CAPABILITY_TAGS maps that to the 'backend_repair' tag these
    entries carry.  Measured live 13 Nov 2026 (Nunba build 75): the turn
    resolved 'backend_repair' and attached 0 tools, because these were not
    in the registry.

    One registry tool per function, its endpoint named after it, so the
    model sees the names the prompts use.  Tagged 'backend_repair' only, not
    the entries' documentary tags: 'tts' is unlocked by every 'media' turn
    and 'coding' by every coding turn, and neither should carry an
    installer.  The tool asks the owner before it installs or wipes.

    Offered by that tag and by name only ('tag_only' on the endpoint):
    request_tools' keyword discovery never attaches them.  Review of
    ea8e9b3a4: one request_tools('text to speech') call attached
    repair_backend_venv ('text' is inside 'context.backend' in its
    description), so did 'install a package', 'use a model' and 'context',
    and 'set up the next engine' attached next_tts_venv_to_provision, on
    turns that never resolved 'backend_repair'.  This is the only path the
    agents get them by: 8e944550b's Tier-2 loader, a second copy with a
    second schema, is gone.
    """

    TAGS = ('backend_repair',)

    @classmethod
    def create_tool_infos(cls):
        from integrations.service_tools.registry import ServiceToolInfo
        return [
            ServiceToolInfo(
                name=entry['name'],
                description=entry['description'],
                base_url='native://in-process',
                endpoints={entry['name']: {
                    'path': f"/{entry['name']}",
                    'method': 'POST',
                    'description': entry['description'],
                    'params_schema': _params_schema(entry['func']),
                    'native_handler': _native_handler(entry),
                    'tag_only': True,
                }},
                health_endpoint=None,  # native: no HTTP service to ping
                tags=list(cls.TAGS),
            )
            for entry in BACKEND_REPAIR_TOOLS
        ]

    @classmethod
    def register(cls) -> bool:
        """Register on the global service_tool_registry.  Idempotent."""
        try:
            from integrations.service_tools import registry as _reg
            return all([_reg.service_tool_registry.register_tool(info)
                        for info in cls.create_tool_infos()])
        except Exception as exc:
            logger.warning('backend repair tools registration skipped: %s', exc)
            return False
