"""HARTOS framework bootstrap — single entry point for consuming apps.

Consuming apps (Nunba desktop, HART OS, cloud Hevolve, embedded edge,
future React Native bridge) call ``bootstrap(app, config=None)`` ONCE
after their Flask app is created.  That single call owns the entire
HARTOS init lifecycle:

  1. Flask setup-lock bypass — lets deferred blueprint registration
     succeed despite Flask serving concurrent requests.
  2. ``init_social(app)`` — HARTOS social subsystem (gamification, mcp,
     sharing, games, discovery, channel admin, dashboard, tracker, fleet
     update, regional host, sync, audit, content gen, learning, theme,
     thought experiments, coding agent blueprints).
  3. Core ``social_bp`` (auth, users, posts, feed, comments).
  4. ``distributed_agent_bp``.
  5. ``blueprint_registry.register_all_blueprints`` (marketplace,
     benchmarks, robotics, hive, compute optimizer, ...).
  6. DB init + migrations.
  7. Consumer hook — any consumer-app-specific blueprints (Nunba's
     ``kids_media_routes`` / ``upload_routes`` / ``db_routes``) get
     registered here, INSIDE the same setup-lock-bypass window so they
     don't hit Flask's "you can't register after first request" guard.
  8. Channel adapters (config-driven + env-var-driven + always-on web).
  9. Agent engine (daemon, goal seeding, world model, baselines).

Idempotent: a second call from the same process is a no-op.
Non-blocking: spawns one daemon thread named ``hartos-bootstrap`` and
returns immediately.

Consumer apps MUST NOT import any of:

  - ``integrations.social.init_social``
  - ``integrations.social.api.social_bp``
  - ``integrations.distributed_agent.distributed_agent_bp``
  - ``integrations.blueprint_registry.register_all_blueprints``
  - ``integrations.social.models.init_db``
  - ``integrations.social.migrations.run_migrations``
  - ``integrations.channels.flask_integration.init_channels``
  - ``integrations.agent_engine.init_agent_engine``

Use ``hartos_bootstrap.bootstrap(app, config)`` instead.  CI guards in
consumer repos enforce this — see Nunba's
``tests/test_hartos_facade_only.py`` for the AST-walking pattern.

Why a facade: prior to this module the consumer (Nunba ``main.py``)
hand-coded the init sequence, knew about the Flask setup-lock dance,
and called every HARTOS sub-init directly.  Every time HARTOS added a
new init step, Nunba had to be edited to match — and four 240s+
boot-deadlock incidents (2026-04-19, 2026-04-26, 2026-04-28×2) all
traced back to that leaky abstraction.  With this facade, HARTOS owns
its own bootstrap order, locking, retry, and degradation-on-failure.
"""

from __future__ import annotations

import logging
import os
import threading
import types
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Module-level idempotency state (single bootstrap per process)
# ─────────────────────────────────────────────────────────────────────────
_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAP_THREAD: Optional[threading.Thread] = None
_BOOTSTRAP_DONE = False


def is_bootstrap_complete() -> bool:
    """True once ``bootstrap()``'s internal thread has finished.

    Useful for health-check endpoints that want to surface init state.
    """
    return _BOOTSTRAP_DONE


def get_bootstrap_thread() -> Optional[threading.Thread]:
    """Return the bootstrap thread (or None if bootstrap was never called)."""
    return _BOOTSTRAP_THREAD


def bootstrap(
    app,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[threading.Thread]:
    """Initialise HARTOS on the given Flask app.

    Idempotent.  Non-blocking.  Returns the bootstrap thread (already
    started, daemon=True) on the first call, or ``None`` on subsequent
    calls (since the work has already been kicked off).

    Args:
        app: Flask application instance.
        config: Optional consumer-tuning mapping.  Recognised keys:

            ``device_id`` (str)
                Unique device identifier for channel adapters.

            ``default_user_id`` (int, default ``10077``)
                user_id passed to channel adapters.

            ``default_prompt_id`` (int, default ``8888``)
                prompt_id passed to channel adapters.

            ``backend_chat_url`` (str, default ``http://localhost:5000/chat``)
                Full URL used by channel adapters as the agent endpoint.

            ``register_consumer_routes`` (callable taking ``app``)
                Called inside the setup-lock-bypass window, AFTER
                ``social_bp`` + ``distributed_agent_bp`` but BEFORE
                ``blueprint_registry.register_all_blueprints`` and DB
                init — matches legacy ``_deferred_social_init``
                ordering exactly.  Use this to register consumer-
                specific Flask blueprints / routes (e.g. Nunba's
                kids_media / kids_recommendation / upload_routes /
                db_routes).

            ``on_bootstrap_complete`` (callable, no args)
                Called inside the bootstrap thread AFTER
                ``init_agent_engine`` and BEFORE the setup-lock bypass
                is lifted.  Used to kick off consumer-side post-init
                work that must serialise after HARTOS imports settle —
                e.g. Nunba's
                ``routes.hartos_backend_adapter.start_hartos_init_background``
                Tier-1 chat-dispatcher thread, which imports
                ``hart_intelligence`` and would otherwise race on the
                package import lock.

            ``mode`` (str, default ``flat``)
                One of ``flat`` / ``regional`` / ``central`` /
                ``embedded``.  Reserved for future per-mode tuning;
                currently informational only.

    Returns:
        The bootstrap thread on first call; ``None`` on subsequent
        idempotent re-calls.
    """
    global _BOOTSTRAP_THREAD

    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP_THREAD is not None:
            return None
        cfg = dict(config or {})
        t = threading.Thread(
            target=_run_bootstrap,
            args=(app, cfg),
            daemon=True,
            name='hartos-bootstrap',
        )
        _BOOTSTRAP_THREAD = t

    t.start()
    return t


# ─────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────


def _run_bootstrap(app, cfg: dict) -> None:
    """Bootstrap sequence body — runs in the ``hartos-bootstrap`` thread.

    Step order is preserved bit-for-bit from the legacy
    ``Nunba/main.py::_deferred_social_init`` (2026-04-19 → 2026-04-28)
    so this refactor is a pure relocation, not a behaviour change.
    Specifically:

      1. setup-lock bypass ON
      2. init_social
      3. social_bp + distributed_agent_bp
      4. consumer routes (Nunba: kids_media / kids_recommendation /
         upload_routes / db_routes)
      5. blueprint_registry.register_all_blueprints
      6. init_db + run_migrations
      7. channel adapters (config + env-var + web)
      8. init_agent_engine
      9. on_bootstrap_complete consumer callback (e.g. Tier-1 chat
         adapter kickoff — runs WHILE setup-lock is still bypassed
         AND after agent_engine, matching legacy sequencing exactly)
     10. setup-lock bypass OFF

    Any sub-init failure logs with context but never aborts the chain
    — the legacy code wrapped each step in its own try/except for the
    same reason (degraded boot is preferable to no boot).
    """
    global _BOOTSTRAP_DONE
    try:
        bypass_active = _enable_setup_lock_bypass(app)
        try:
            _init_social_subsystem(app)
            _register_core_blueprints(app)
            _run_consumer_hook(app, cfg)
            _register_hive_blueprints(app)
            _init_database(cfg)
            _init_channel_adapters(app, cfg)
            _init_agent_engine_subsystem(app)
            _run_on_bootstrap_complete(cfg)
        finally:
            if bypass_active:
                _disable_setup_lock_bypass(app)

        _BOOTSTRAP_DONE = True
        logger.info("HARTOS bootstrap complete")
    except Exception as e:
        logger.error(f"HARTOS bootstrap failed: {e}", exc_info=True)
        # Still mark done so health checks don't hang forever on failure;
        # individual sub-init failures are logged with their own context.
        _BOOTSTRAP_DONE = True


# ─── Step 1: Flask setup-lock bypass ────────────────────────────────────


_setup_lock_bypass = threading.local()


def _enable_setup_lock_bypass(flask_app) -> bool:
    """Override Flask 3.x ``_check_setup_finished`` to honour a bypass flag.

    Background: Flask 3.x's ``full_dispatch_request`` re-sets
    ``_got_first_request = True`` on every request.  A one-shot reset
    is insufficient because the React SPA polls ``/backend/health``,
    ``/api/guest-id``, etc. every few seconds — between our reset and
    the first deferred ``register_blueprint`` call, a poll request
    re-engages the lock.

    Fix: install a per-app ``_check_setup_finished`` that respects a
    thread-local bypass flag.  Only the bootstrap thread sets the flag;
    request-dispatch threads don't, so production-time mutation guards
    remain intact for any post-bootstrap stray ``register_*`` calls.

    Returns ``True`` on success.  Failure is non-fatal — the lock simply
    stays in place and any deferred registration that hits it logs the
    exception via the per-call ``try/except`` in each ``_register_*``
    helper below.
    """
    try:
        setattr(flask_app, '_got_first_request', False)

        def _patched_check(self, f_name):
            if getattr(_setup_lock_bypass, 'active', False):
                return
            if self._got_first_request:
                raise AssertionError(
                    f"The setup method '{f_name}' can no longer be called"
                    " on the application. It has already handled its first"
                    " request, any changes will not be applied consistently."
                )

        flask_app._check_setup_finished = types.MethodType(
            _patched_check, flask_app,
        )
        _setup_lock_bypass.active = True
        return True
    except Exception as e:
        logger.debug(f"Flask setup-lock bypass install skipped: {e}")
        return False


def _disable_setup_lock_bypass(flask_app) -> None:
    """Clear the thread-local bypass flag.  Flask's setup-lock semantics
    return to normal for any post-bootstrap stray ``register_*`` calls,
    matching Flask's intended invariant."""
    _setup_lock_bypass.active = False


# ─── Idempotent blueprint registration helpers ─────────────────────────


def _has_bp(app, name: str) -> bool:
    try:
        return name in getattr(app, 'blueprints', {})
    except Exception:
        return False


def _safe_register_bp(app, bp, *, name_hint: str = '') -> bool:
    """Idempotent ``app.register_blueprint``.  Returns True if newly
    registered, False if already present or if registration raised."""
    try:
        bp_name = getattr(bp, 'name', None) or name_hint
        if bp_name and _has_bp(app, bp_name):
            return False
        app.register_blueprint(bp)
        return True
    except Exception as e:
        logger.debug(
            f"blueprint {name_hint or getattr(bp, 'name', '?')} "
            f"register failed: {e}",
        )
        return False


# ─── Step 2: HARTOS social subsystem ────────────────────────────────────


def _init_social_subsystem(app) -> None:
    """Run ``init_social(app)`` — registers gamification/mcp/sharing/games/
    discovery/admin/channel_user/dashboard/tracker/fleet_update/
    regional_host/sync/audit/content_gen/learning/theme/thought_experiments/
    coding_agent blueprints.

    Failure here is non-fatal: ``social_bp`` and the rest still register
    in subsequent steps, and the app degrades gracefully.
    """
    try:
        from integrations.social import init_social
        init_social(app)
        logger.info("HARTOS init_social() completed")
    except Exception as e:
        logger.warning(
            "HARTOS init_social() failed (non-fatal; "
            f"social_bp will still register): {e}"
        )


# ─── Step 3 & 4: Core blueprints ────────────────────────────────────────


def _register_core_blueprints(app) -> None:
    """Register ``social_bp`` (auth/users/posts/feed/comments) and
    ``distributed_agent_bp`` (distributed coding agent endpoints).

    Each is independent — failure of one does not block the others.
    """
    try:
        from integrations.social.api import social_bp
        if _safe_register_bp(app, social_bp, name_hint='social'):
            logger.info(
                "Social core blueprint (social_bp) registered — "
                "/api/social/posts, /auth/*, /users, /feed live"
            )
    except Exception as e:
        logger.warning(f"Social core blueprint registration failed: {e}")

    try:
        from integrations.distributed_agent import distributed_agent_bp
        _safe_register_bp(app, distributed_agent_bp,
                          name_hint='distributed_agent')
    except Exception:
        # Distributed agent is optional — many consumers don't enable it.
        pass


# ─── Step 5: Hive blueprint registry ────────────────────────────────────


def _register_hive_blueprints(app) -> None:
    """Run the canonical hive-blueprint registry — registers marketplace,
    benchmarks, robotics, hive, compute_optimizer, etc."""
    try:
        from integrations.blueprint_registry import register_all_blueprints
        result = register_all_blueprints(app)
        logger.info(
            f"HARTOS blueprints: {len(result['registered'])} registered, "
            f"{len(result['skipped'])} skipped: {result['registered']}"
        )
    except Exception as e:
        logger.warning(f"HARTOS blueprint registry failed: {e}")


# ─── Step 6: DB + migrations ────────────────────────────────────────────


def _init_database(cfg: dict) -> None:
    """Run ``init_db()`` then ``run_migrations()``.

    Both are heavy I/O — safe to defer here since chat hot-path doesn't
    touch the social DB until the user opens the social tab.
    """
    try:
        from integrations.social.models import init_db
        init_db()
        db_path = cfg.get('nunba_db_path') or '<engine default>'
        logger.info(f"hart-backend DB initialised: {db_path}")
    except Exception as e:
        logger.warning(f"hart-backend init_db failed: {e}")

    try:
        from integrations.social.migrations import run_migrations
        run_migrations()
    except Exception as e:
        logger.warning(f"hart-backend migrations: {e}")


# ─── Step 7: Consumer hook ──────────────────────────────────────────────


def _run_consumer_hook(app, cfg: dict) -> None:
    """Call the consumer-supplied route registration callback (if any).

    Consumer-app-specific blueprints (Nunba's ``kids_media_routes`` /
    ``upload_routes`` / ``db_routes``) belong here — INSIDE the
    setup-lock-bypass window so Flask doesn't reject them, but separated
    from HARTOS framework concerns so future consumers (HART OS, cloud
    Hevolve, embedded) don't have to inherit Nunba-specific routes.
    """
    hook = cfg.get('register_consumer_routes')
    if not callable(hook):
        return
    try:
        hook(app)
    except Exception as e:
        logger.warning(f"Consumer route registration hook failed: {e}")


# ─── Step 8: Channel adapters ───────────────────────────────────────────


def _init_channel_adapters(app, cfg: dict) -> None:
    """Bring up the channel adapter subsystem and auto-activate channels
    from the saved admin config + present env-vars + always-on web.

    Heavy SDK modules (telegram, discord, whatsapp, slack, signal — each
    ~30-80 MB) are only loaded if credentials are actually present.  The
    in-process ``web`` adapter is always registered.
    """
    try:
        from core.port_registry import get_port
        from integrations.channels.flask_integration import init_channels

        backend_url = cfg.get('backend_chat_url')
        if not backend_url:
            backend_url = f'http://localhost:{get_port("backend")}/chat'

        channels = init_channels(app, {
            'agent_api_url': backend_url,
            'default_user_id': cfg.get('default_user_id', 10077),
            'default_prompt_id': cfg.get('default_prompt_id', 8888),
            'device_id': cfg.get('device_id'),
        })

        # Auto-activate channels saved in admin config
        activated_from_cfg = 0
        try:
            from integrations.channels.admin.api import get_api
            for ch_type, ch_cfg in get_api()._channels.items():
                if ch_cfg.get('enabled', True):
                    tok = ch_cfg.get('token') or ch_cfg.get('api_key')
                    if channels.register_channel(ch_type, token=tok):
                        activated_from_cfg += 1
        except Exception:
            pass

        # Env-var-driven channels (only if credentials present — heavy
        # SDK modules are NOT imported speculatively).
        env_creds = {
            'telegram': os.environ.get('TELEGRAM_BOT_TOKEN'),
            'discord':  os.environ.get('DISCORD_BOT_TOKEN'),
            'whatsapp': os.environ.get('WHATSAPP_ACCESS_TOKEN'),
            'slack':    os.environ.get('SLACK_BOT_TOKEN'),
            'signal':   os.environ.get('SIGNAL_SERVICE_URL'),
        }
        registered_from_env = 0
        for ch_type, tok in env_creds.items():
            if tok and ch_type not in (channels.registry._adapters or {}):
                if channels.register_channel(ch_type, token=tok):
                    registered_from_env += 1

        # `web` adapter is in-process, cheap, always register
        if 'web' not in (channels.registry._adapters or {}):
            channels.register_channel('web')

        channels.start()
        logger.info(
            f"Channel adapters initialised "
            f"({activated_from_cfg} from config, "
            f"{registered_from_env} from env, web)"
        )
    except Exception as e:
        logger.debug(f"Channel adapters skipped: {e}")


# ─── Step 9: Agent engine ───────────────────────────────────────────────


def _init_agent_engine_subsystem(app) -> None:
    """Initialise the HARTOS agent engine — daemon thread, goal seeding,
    world model, baselines.  Idempotent inside ``init_agent_engine``
    itself (added 2026-04-28) so re-entry is harmless.
    """
    try:
        from integrations.agent_engine import init_agent_engine
        init_agent_engine(app)
        logger.info("Agent engine initialised")
    except Exception as e:
        logger.warning(f"Agent engine init failed: {e}")


# ─── Step 9: on_bootstrap_complete consumer callback ─────────────────


def _run_on_bootstrap_complete(cfg: dict) -> None:
    """Run the consumer-supplied ``on_bootstrap_complete`` callback (if any).

    Runs INSIDE the bootstrap thread (so it is sequenced after
    ``init_agent_engine`` and before the setup-lock bypass is lifted —
    matches legacy ``_deferred_social_init`` ordering bit-for-bit).
    Used by Nunba to kick off ``start_hartos_init_background()`` (the
    Tier-1 chat-dispatcher import thread) AFTER HARTOS bootstrap's own
    imports have settled, so the two threads do not race on the
    ``hart_intelligence`` per-package import lock.
    """
    cb = cfg.get('on_bootstrap_complete')
    if not callable(cb):
        return
    try:
        cb()
    except Exception as e:
        logger.warning(f"on_bootstrap_complete callback failed: {e}")
