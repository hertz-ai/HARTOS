"""
Unified Agent Goal Engine - Chat Dispatch

Sends agent goals to idle agents via the existing /chat endpoint
(CREATE/REUSE pipeline). Dispatches with autonomous=True so the
LLM auto-generates the agent config without user interaction.

First dispatch = CREATE mode (gather_info + recipe creation).
Subsequent dispatches = REUSE mode (recipe exists, 90% faster).

DISTRIBUTED DISPATCH (automatic):
When a shared Redis coordinator is reachable (i.e. the node is part
of a hive with peers), goals are automatically submitted to the
DistributedTaskCoordinator instead of local /chat. Worker nodes
across the hive claim and execute tasks autonomously. No separate
mode flag — distribution is an emergent property of having peers.
Falls back to local /chat when Redis is unavailable.
"""
import os
import logging
import threading
import requests
from typing import Dict, List, Optional

from core.http_pool import pooled_post
from core.port_registry import get_port

logger = logging.getLogger('hevolve_social')


# ── HTTP circuit breaker ────────────────────────────────────────────────
# Fast-fail instead of waiting 120s against a dead llama-server.
# After _CB_THRESHOLD consecutive connection failures, skip HTTP dispatch
# for _CB_COOLDOWN_S seconds before retrying.
_CB_THRESHOLD = 3        # failures before opening circuit
_CB_COOLDOWN_S = 60      # seconds to wait before retrying
_cb_failures = 0
_cb_open_until = 0.0
_cb_lock = threading.Lock()


def _cb_record_success():
    global _cb_failures, _cb_open_until
    with _cb_lock:
        _cb_failures = 0
        _cb_open_until = 0.0


def _cb_record_failure():
    global _cb_failures, _cb_open_until
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            import time as _t
            _cb_open_until = _t.time() + _CB_COOLDOWN_S
            logger.warning(f"Circuit breaker OPEN — {_cb_failures} consecutive "
                           f"HTTP failures, skipping dispatch for {_CB_COOLDOWN_S}s")


def _cb_is_open() -> bool:
    with _cb_lock:
        if _cb_failures < _CB_THRESHOLD:
            return False
        import time as _t
        if _t.time() >= _cb_open_until:
            # Half-open: allow one probe
            return False
        return True


def _local_dispatch_base_url() -> str:
    """Base URL for the Tier-2 HTTP fallback to the local HARTOS /chat (#71).

    Delegates to the canonical core.port_registry.get_local_backend_url —
    the SAME resolver the channel inbound bridge uses (flask_integration), so
    neither hardcodes a dead :6777 in bundled mode (HARTOS serves in-process
    on :5000 there).  HEVOLVE_BASE_URL still wins; otherwise it probes the
    live local serve port (standalone backend:6777 / bundled flask:5000)."""
    from core.port_registry import get_local_backend_url
    return get_local_backend_url()


def _internal_auth_headers() -> Optional[Dict[str, str]]:
    """Build auth headers for internal /chat dispatch.

    Why: on central/regional tiers, security/middleware.py Gate 2 rejects
    unauthenticated internal /chat dispatches with HTTP 401
    "Authentication required (Bearer token)". Without this header the
    autonomous outreach flywheel silently 401'd from 2026-03-14 onward.

    Prefers HEVOLVE_API_KEY (X-API-Key) if set; otherwise mints a
    short-lived system_daemon JWT via integrations.social.auth.
    Returns None on flat-tier deployments where auth is unneeded
    (caller passes None to pooled_post → no header attached).
    """
    headers: Dict[str, str] = {}
    try:
        api_key = os.environ.get('HEVOLVE_API_KEY', '').strip()
        if api_key:
            headers['X-API-Key'] = api_key
        else:
            from integrations.social.auth import generate_jwt as _mint_jwt
            jwt = _mint_jwt(
                user_id='system_daemon',
                username='system_daemon',
                role='admin',
            )
            if jwt:
                headers['Authorization'] = f'Bearer {jwt}'
    except Exception as e:
        logger.debug(f"daemon-dispatch auth header mint failed (non-fatal): {e}")
    return headers or None


# ── LLM concurrency control ──────────────────────────────────────────────
# Local llama-server degrades exponentially with concurrent requests
# (KV cache thrashing). Allow only N concurrent local LLM calls.
# This prevents the watchdog-restart cascade where restarted daemons
# pile up concurrent requests that each take longer, triggering more
# restarts.
_LOCAL_LLM_MAX_CONCURRENT = int(os.environ.get('HEVOLVE_LOCAL_LLM_MAX_CONCURRENT', '1'))
_local_llm_semaphore = threading.Semaphore(_LOCAL_LLM_MAX_CONCURRENT)


# ── User-priority gate ──────────────────────────────────────────────────
# When a human user is chatting, daemon dispatch must yield the LLM.
# Tracked via timestamp of last user activity — daemon checks freshness.
import time as _time
_last_user_chat_at: float = 0.0
_USER_CHAT_COOLDOWN = 600  # 10 min — CREATE pipeline can take this long
_active_create_sessions: int = 0  # count of in-flight CREATE requests
_create_lock = threading.Lock()

# Governor throttle below this → the daemon yields (gate reason #3).
_GATE_THROTTLE_FLOOR = 0.3
# The reason should_yield_to_user() last returned True, or None when it
# last returned False.  Exposed so the daemon log + status probes can say
# WHICH of the gate's reasons is blocking.  Why: 2026-05-29/30 cost ~3
# days of guessing because the gate was a silent black box — it yielded
# for one of four reasons and logged none of them, so the fix (18e59c0)
# patched the wrong reason.  Never let the gate be silent again.
_last_yield_reason = None


def get_last_yield_reason():
    """Return the reason should_yield_to_user() last blocked on, or None
    when the gate is currently open.  One of: 'user_active',
    'create_in_flight', 'model_pressure', 'governor_throttle'."""
    return _last_yield_reason


def local_chat_dispatch(prompt, user_id, prompt_id, daemon_id=None,
                        native_fallback=True):
    """The ONE in-process call to this node's own /chat.  Returns
    ``(status, text)`` where status is ``'ok'`` (text is the reply),
    ``'deferred'`` (a human has the LLM, or it is saturated — retry later,
    NOT a failure) or ``'unavailable'`` (no in-process route; the caller may
    fall back to its HTTP tier).

    ``native_fallback=False`` says "only use this if the Nunba adapter is
    present".  On native HARTOS (central) the loopback POST already reaches
    the right /chat, so a caller that has a working HTTP tier should not pay
    to pull the Flask app into its own thread; the bug this function exists
    to fix is bundled-only.  ``dispatch_goal`` keeps the native fallback it
    has always had.

    WHY THIS EXISTS
    ───────────────
    "POST my own /chat" was hand-rolled in three places and only ONE of them
    did it correctly.  ``dispatch_goal`` resolves an in-process callable
    first; ``_dispatch_single_instruction`` and
    ``distributed_agent.worker_loop._execute_task`` went straight to
    ``pooled_post(base_url + '/chat')``.

    On a bundled desktop that raw POST does not reach HARTOS at all.  Nunba
    registers its OWN ``/chat`` at that address (``routes/chatbot_routes.py``
    ``app.route("/chat")``), it reads the body's ``text`` field, and HARTOS
    speaks ``prompt`` — so every such call was answered
    ``400 {"error": "Text is required"}``.  Measured on the live desktop
    2026-09-02: the copilot instruction queue drained 8 benchmark shards
    every ~50s and all 8 failed HTTP 400, 88 times in one day, and
    ``instruction_queue.fail_instruction`` re-queues with no attempt cap, so
    it would have retried forever.

    The translator between the two dialects already exists and is
    ``routes.hartos_backend_adapter.chat`` ("Maps Nunba's parameter names to
    what hart_intelligence /chat expects: text -> prompt").  Going through it
    is the whole fix; teaching Nunba's route to accept both dialects would
    have made the divergence permanent.

    Two correctness properties the hand-rolled callers also skipped, and
    which every caller of this function now gets:
      - the user-priority gate (a daemon must not take the LLM while a human
        is chatting), and
      - ``_local_llm_semaphore`` (one in-flight local LLM call), whose
        absence is what pile-drove the local llama-server into the
        watchdog-restart cascade this module already documents.
    """
    try:
        try:
            from routes.hartos_backend_adapter import chat as hevolve_chat
        except ImportError:
            try:
                from hartos_backend_adapter import chat as hevolve_chat
            except ImportError:
                if not native_fallback:
                    # Native HARTOS and the caller has its own HTTP tier that
                    # already reaches the right /chat here. Say so rather than
                    # importing the Flask app into this thread.
                    return 'unavailable', None
                # NATIVE HARTOS (no Nunba adapter — the module lives only in
                # Nunba): call the in-process /chat via the app's OWN test
                # client, the SAME canonical route the HTTP tier uses, minus
                # the loopback socket, exactly as
                # routes.hartos_backend_adapter.chat does in Nunba.  Reuses
                # the /chat pipeline + _internal_auth_headers; no new dispatch
                # path.  Any error here still lets the caller fall through to
                # its HTTP tier (bounded-safe).
                def hevolve_chat(text=None, user_id=None, agent_id=None,
                                 create_agent=True, casual_conv=False,
                                 autonomous=True, request_id=None, **_kw):
                    from hart_intelligence_entry import app as _app
                    _payload = {
                        'prompt': text, 'user_id': user_id, 'prompt_id': agent_id,
                        'create_agent': create_agent, 'casual_conv': casual_conv,
                        'autonomous': autonomous, 'request_id': request_id,
                        'task_source': 'own',
                    }
                    with _app.test_client() as _c:
                        _r = _c.post('/chat', json=_payload,
                                     headers=_internal_auth_headers())
                        return _r.get_json() or {}
    except Exception as e:
        logger.debug(f"No in-process /chat route available: {e}")
        return 'unavailable', None

    # USER PRIORITY: if a user chatted recently, yield the LLM to them.
    if is_user_recently_active():
        logger.info(f"User active ({_USER_CHAT_COOLDOWN}s cooldown), "
                    f"deferring local /chat for {daemon_id or prompt_id}")
        return 'deferred', None

    if not _local_llm_semaphore.acquire(timeout=5):
        logger.info(f"LLM busy ({_LOCAL_LLM_MAX_CONCURRENT} in flight), "
                    f"deferring local /chat for {daemon_id or prompt_id}")
        return 'deferred', None

    # Signal to the watchdog that this thread is in a legitimate LLM call.
    _notify_watchdog_llm_start()
    try:
        # A daemon-specific request_id keeps background thinking traces out of
        # user responses via drain_thinking_traces(), and is what
        # dispatch.is_genuine_user_request reads to classify the turn.
        request_id = f'daemon_{daemon_id}' if daemon_id is not None else None
        result = hevolve_chat(
            text=prompt, user_id=user_id, agent_id=prompt_id,
            create_agent=True, casual_conv=False, autonomous=True,
            request_id=request_id,
        )
    except Exception as e:
        logger.warning(f"In-process /chat failed for {daemon_id or prompt_id}: {e}")
        return 'unavailable', None
    finally:
        _local_llm_semaphore.release()
        try:
            _notify_watchdog_llm_end()
        except Exception:
            pass

    result = result or {}
    return 'ok', (result.get('text') or result.get('response', ''))


def _note_yield_reason(reason) -> None:
    """Record the current yield reason; log at INFO only on TRANSITION so
    there's a clean trail ('yield gate CLOSED: governor_throttle' →
    'yield gate OPEN') with no per-tick spam."""
    global _last_yield_reason
    if reason == _last_yield_reason:
        return
    prev = _last_yield_reason
    _last_yield_reason = reason
    try:
        if reason is None:
            logger.info("yield gate OPEN (was %s) — daemons may run normal path", prev)
        else:
            logger.info("yield gate CLOSED: %s (was %s)", reason, prev)
    except Exception:
        pass


def mark_user_chat_activity():
    """Call on every GENUINE user /chat request (including user-initiated
    autonomous CREATE).  MUST NOT be called for the agent_daemon's own
    background dispatches — see is_genuine_user_request()."""
    global _last_user_chat_at
    _last_user_chat_at = _time.time()


def is_genuine_user_request(request_id) -> bool:
    """True when a /chat request comes from a real user (so it should
    mark user activity + make daemons yield), False when it's the
    agent_daemon's own background goal dispatch.

    The daemon's in-process Tier-1 dispatch tags request_id with the
    'daemon_<goal_id>' prefix (see dispatch_goal's _daemon_request_id).
    Genuine user turns — including user-initiated autonomous CREATE
    ('do it for me') — carry a normal request_id and return True.

    This is the single discriminator that breaks the 2026-05-29
    yield-gate feedback loop: without it, every daemon dispatch
    re-stamped _last_user_chat_at, so should_yield_to_user() read
    'user active' forever (~24h of continuous yield, 1142 starvation
    overrides) and the flywheel daemon never ran its normal path.

    EMPTY / None request_id is NOT a user — it is background (returns False).
    Live evidence (llm_outbound.jsonl, 2026-06-17): ~88% of empty-rid 4B calls
    are daemon autogen turns whose 'daemon_<goal>' tag was lost crossing
    autogen's worker-thread boundary (threading.local() does not cross it; only
    the _request_id_var contextvar does, and it binds '' when recipe() /
    chat_agent() are invoked without the id).  Treating empty as a user let
    those 1,926 calls masquerade as a live turn, so the foreground preempt
    (_is_background_call -> not is_genuine_user_request) never saw them as
    background, never aborted them, and a real "hi" queued behind them on the
    single 4B.  Genuine user calls always carry an id (the Nunba adapter
    defaults request_id to a timestamp when the client omits one, so an INBOUND
    request is never empty), so empty == untagged == background here.  This is
    the SOLE authority for "is this a user?" — the inbound mark_view gate
    (_chat_request_is_genuine) and the outbound abort gate (_is_background_call)
    both delegate here with NO bespoke, caller-specific rule, so they can never
    give different answers for the same id.
    """
    return bool(request_id) and not str(request_id).startswith('daemon_')


def is_current_request_autonomous() -> bool:
    """True when the IN-FLIGHT request is a daemon / flywheel dispatch (no live
    user to answer clarifying questions); False for a genuine user turn.

    Reads the thread-local request_id the /chat handler set and applies the one
    canonical discriminator (is_genuine_user_request).  The CREATE pipeline uses
    this to pick the assistant's AUTONOMOUS vs INTERACTIVE system prompt:
    without it, autonomous goals were told they "may ask the user clarifying
    questions", asked, and stalled forever waiting for an answer that never came
    (the live 2026-06-04 :8080 capture showed a daemon goal in INTERACTIVE mode).
    Degrades safe — any error / missing request_id returns False (interactive).
    """
    try:
        from hartos.threadlocal import thread_local_data
        rid = thread_local_data.get_request_id() or ''
        # A MISSING request_id must mean INTERACTIVE (the docstring's "degrades
        # safe" contract, and what the test suite pins). The previous bare
        # `not is_genuine_user_request(rid)` inverted that for rid='' -- an
        # unstamped context was treated as AUTONOMOUS, the unsafe direction
        # (the agent stops asking clarifying questions it should ask).
        return bool(rid) and not is_genuine_user_request(rid)
    except Exception:
        return False


def mark_create_start(request_id=None):
    """Call when a CREATE pipeline starts.

    Always increments the in-flight-CREATE counter (so other daemon
    ticks yield while ANY create runs — daemon-initiated included,
    since creates are LLM-heavy and shouldn't pile up).  The counter
    is self-balancing via mark_create_end().

    Only stamps the user-activity TIMESTAMP for genuine user creates.
    A daemon-initiated CREATE (request_id='daemon_<goal>') must NOT
    re-arm the 10-min user cooldown — otherwise the starvation-override
    path (override → forced tick → daemon dispatch → create →
    mark_create_start → re-stamp) keeps the yield gate stuck-on, the
    same feedback loop the line-8427 guard closes for the non-create
    path.  See is_genuine_user_request().
    """
    global _active_create_sessions
    with _create_lock:
        _active_create_sessions += 1
    if is_genuine_user_request(request_id):
        mark_user_chat_activity()


def mark_create_end():
    """Call when a CREATE pipeline finishes."""
    global _active_create_sessions
    with _create_lock:
        _active_create_sessions = max(0, _active_create_sessions - 1)


def is_user_recently_active() -> bool:
    """True if user chatted recently OR a CREATE pipeline is running."""
    if _active_create_sessions > 0:
        return True
    return (_time.time() - _last_user_chat_at) < _USER_CHAT_COOLDOWN


def is_transient_deferral() -> bool:
    """True when a ``dispatch_goal`` ``None`` is a TRANSIENT defer — the user is
    actively using the LLM, or the Tier-2 circuit breaker is open — rather than a
    real dispatch failure.

    ``dispatch_goal`` returns ``None`` for BOTH cases, so the daemon can't tell
    them apart from the return value alone.  The daemon calls this to avoid
    counting a defer toward the 5-strike AUTO-PAUSE: a perfectly healthy goal
    must never be paused just because the user was chatting (or the backend
    hiccuped) — that was the "goals stuck / 0 progress" bug.  Composes the SAME
    two checks ``dispatch_goal`` uses to defer (lines ~659 user-active, ~698
    breaker-open), so the daemon's notion of "transient" never drifts from the
    dispatcher's."""
    try:
        if is_user_recently_active() or _cb_is_open():
            return True
        # A goal whose in-flight LLM call was just PREEMPTED for a live user turn
        # (foreground abort / llama_scheduler eviction) is a transient defer too —
        # re-queue it next tick, never count it toward auto-pause.  The user may
        # already be "inactive" by the time we re-check, so this explicit preempt
        # signal catches the window is_user_recently_active() can miss.
        from core.foreground import preempted_recently
        return preempted_recently()
    except Exception:
        return False


# Concurrency ceiling for autonomous dispatch — single source both daemons call
# so the policy can't drift (Gate-2/4).  The 2026-06-13 sluggishness: agent_daemon
# AND coding_daemon each dispatched up to HEVOLVE_*_MAX_CONCURRENT (default 10)
# concurrent CREATE pipelines while the user was away; on a 16-core desktop that
# + the UI webview pinned every core for minutes (user had to kill Nunba).
# should_yield_to_user() reason #3 is — by the #60 external-CPU design —
# deliberately blind to our OWN cpu, so it never bounds the swarm; this cap must.
_DEFAULT_AUTONOMOUS_CORE_RESERVE = 6   # cores kept free for the UI compositor + a live chat turn
_CORES_PER_PIPELINE = 4                # ~logical cores a CREATE pipeline (4B client + autogen + tool subproc) uses


def max_autonomous_concurrency(env_cap, cores=None, reserve=None) -> int:
    """Cap concurrent autonomous dispatches to leave CPU headroom for the user.

    Returns ``min(env_cap, headroom)`` where ``headroom = (cores - reserve) //
    _CORES_PER_PIPELINE``, floored at 1 so the flywheel always makes progress.
    A many-core server keeps its full env cap; a desktop is bounded to 1-2 so the
    autonomous swarm never pegs every core (the 2026-06-13 incident).  Applied on
    top of the existing throttle/override math, NOT in place of it — a pure
    additional ceiling.

    Env: ``HEVOLVE_AUTONOMOUS_CORE_RESERVE`` (default 6).  ``cores``/``reserve``
    args exist for deterministic tests.
    """
    try:
        if reserve is None:
            reserve = int(os.environ.get('HEVOLVE_AUTONOMOUS_CORE_RESERVE',
                                         str(_DEFAULT_AUTONOMOUS_CORE_RESERVE)))
        if cores is None:
            cores = os.cpu_count() or 4
        headroom = max(1, (cores - reserve) // _CORES_PER_PIPELINE)
        return max(1, min(int(env_cap), headroom))
    except Exception:
        return max(1, int(env_cap))


def should_yield_to_user() -> bool:
    """Single canonical gate every background daemon must call.

    Returns True when the daemon must skip its tick / iteration
    (yield CPU, GIL, LLM, GPU to the user-facing path).  Three
    independent yield reasons:

    1. ``is_user_recently_active()`` — user chatted in the last 10
       minutes or a CREATE pipeline is running.
    2. ``model_lifecycle.get_system_pressure().throttle_factor < 0.1``
       — VRAM/CPU pressure is so high the LLM throttle factor has
       collapsed; running another LLM call would saturate the
       system and starve the user.
    3. ``ResourceGovernor.get_throttle() < 0.3`` — generic CPU/RAM
       pressure that is NOT LLM-shaped (e.g., a runaway Python loop,
       a hammering background daemon, OS-level memory pressure).
       The governor combines its mode (ACTIVE/IDLE/SLEEP), the
       model_lifecycle pressure, AND its own per-process pressure
       calc into a single 0.0-1.0 throttle factor (see
       ``core.resource_governor._calculate_throttle``).  Below 0.3
       means "the system is hot enough that user-facing latency is
       at risk" — daemons must yield even if the LLM-specific
       throttle is fine.  Captures the case the user flagged
       2026-05-10: coding_daemon's autogen turns burning CPU/GIL
       while the user was actively chatting, model_lifecycle's
       LLM-only pressure didn't see it, gate passed, system slowed.

    All three checks are best-effort — failure to import / read any
    signal returns False (don't block daemons on a missing module).
    The function is the single source of truth for daemon yield
    semantics: ``agent_daemon._tick``,
    ``agent_daemon._proactive_hive_tick``,
    ``hive_benchmark_prover._continuous_loop``, and
    ``coding_daemon._tick`` all consult it, so adding a fourth
    yield reason (e.g. battery-saver mode, network-pressure)
    means editing exactly this function — no per-daemon copy-paste.
    """
    reason = None
    # Reason #0 — a user-facing request is in flight RIGHT NOW (finer + higher
    # priority than the 10-min "recently active" window below).  Background LLM
    # work must never steal the shared model mid-turn; the daemon's starvation
    # override also refuses to fire while this is set (see agent_daemon._tick).
    try:
        from core.foreground import foreground_active
        if foreground_active():
            reason = 'foreground_request'
    except Exception:
        pass
    # Reason #1 — user recently active (is_user_recently_active stays the
    # single source; we only LABEL which sub-condition fired).
    if reason is None:
        try:
            if is_user_recently_active():
                reason = ('create_in_flight' if _active_create_sessions > 0
                          else 'user_active')
        except Exception:
            pass
    # Reason #2 — LLM throttle collapsed under VRAM/CPU pressure.
    if reason is None:
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager)
            _pressure = get_model_lifecycle_manager().get_system_pressure()
            if _pressure.get('throttle_factor', 1.0) < 0.1:
                reason = 'model_pressure'
        except Exception:
            pass
    # Reason #3 — generic resource-governor throttle (now driven by
    # EXTERNAL cpu, so our OWN idle-compute work no longer trips this).
    if reason is None:
        try:
            from core.resource_governor import get_governor
            _gov = get_governor()
            if _gov is not None and _gov.get_throttle() < _GATE_THROTTLE_FLOOR:
                reason = 'governor_throttle'
        except Exception:
            pass
    _note_yield_reason(reason)
    return reason is not None


# Register this gate as the canonical one for ``core/`` background loops, which
# must NOT import ``integrations/`` (layering: integrations -> core OK, core ->
# integrations BANNED).  Inversion of control mirroring ``core.foreground``'s
# ``set_genuine_check`` — ``core.foreground.should_yield_to_user`` then proxies
# here.  Best-effort: if ``core.foreground`` is unavailable the core accessor
# simply fails open (returns False), so nothing breaks.
try:
    from core.foreground import set_yield_gate as _set_yield_gate
    _set_yield_gate(should_yield_to_user)
except Exception:
    pass


def _notify_watchdog_llm_start():
    """Tell the watchdog the current thread is blocked on a legitimate LLM call.

    The watchdog will extend the heartbeat threshold for threads marked
    'in_llm_call' instead of restarting them.
    """
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if not wd:
            return
        thread_name = threading.current_thread().name
        # Match by thread name — works for all daemon threads
        if wd.is_registered(thread_name):
            wd.mark_in_llm_call(thread_name)
            return
        # Partial match (thread name might have suffix like 'agent_daemon-1')
        for name in wd.registered_names():
            if name in thread_name:
                wd.mark_in_llm_call(name)
                return
        # Fallback: in-process/bundled mode — dispatch runs on a
        # different thread (e.g. Flask worker). Mark the calling daemon
        # via threadlocal source hint if available.
        try:
            from hartos.threadlocal import get_task_source
            source = get_task_source()
            if source and wd.is_registered(source):
                wd.mark_in_llm_call(source)
                return
        except Exception:
            pass
    except Exception:
        pass


def _notify_watchdog_llm_end():
    """Clear the LLM call marker and send a heartbeat for all registered daemons."""
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if wd:
            for name in wd.registered_names():
                wd.clear_llm_call(name)
                wd.heartbeat(name)
    except Exception:
        pass


def _get_distributed_coordinator():
    """Get the shared DistributedTaskCoordinator if Redis is reachable.

    Returns None when Redis is unavailable — caller falls back to local.
    No separate mode flag needed: if Redis exists, distribute.
    """
    try:
        from integrations.distributed_agent.api import _get_coordinator
        return _get_coordinator()
    except Exception as e:
        logger.debug(f"Distributed coordinator unavailable: {e}")
        return None


def _has_hive_peers() -> bool:
    """Check if this node has active peers in the hive.

    Distribution only makes sense when there are other nodes to
    pick up work. Single-node setups always dispatch locally.
    """
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session(commit=False) as db:
            count = db.query(PeerNode).filter(
                PeerNode.status == 'active'
            ).count()
            return count > 1  # >1 because self is in the table too
    except Exception:
        return False


def _decompose_goal(prompt: str, goal_id: str, goal_type: str,
                    user_id: str) -> List[Dict]:
    """Decompose a goal into distributable sub-tasks.

    Checks AgentGoal.context for explicit subtask definitions:
        {"tasks": [...], "parallel": true/false}

    When subtasks are present, uses SmartLedger to create a proper
    dependency graph (parallel fan-out or sequential chain).
    Falls back to single-task decomposition when no subtasks defined.
    """
    try:
        from .parallel_dispatch import (
            extract_subtasks_from_context, decompose_goal_to_ledger)

        subtask_defs = extract_subtasks_from_context(goal_id)
        tasks, _ledger = decompose_goal_to_ledger(
            prompt, goal_id, goal_type, user_id, subtask_defs)
        return tasks
    except Exception:
        pass

    # capabilities are NOT goal types.  A worker claims a task only when one
    # of the names here is in its own advertised set
    # (worker_loop._detect_capabilities), so demanding a goal_type that is not
    # also a capability name — hive_growth, hive_training, autoresearch,
    # thought_experiment, … — makes the task unclaimable by every node that
    # exists.  Demand it only when it is genuinely a capability; otherwise
    # demand nothing, which is the truth: these goals need no special hardware
    # or credential, just a worker.
    from core.constants import HIVE_WORKER_CAPABILITIES
    return [{
        'task_id': f'{goal_id}_task_0',
        'description': prompt[:500],
        'capabilities': [goal_type] if goal_type in HIVE_WORKER_CAPABILITIES else [],
    }]


def dispatch_goal_distributed(prompt: str, user_id: str, goal_id: str,
                              goal_type: str = 'marketing') -> Optional[str]:
    """Submit a goal to the distributed task coordinator.

    The goal is decomposed into sub-tasks, published to shared Redis,
    and worker nodes across the hive will claim and execute them.

    Returns:
        goal_id string on success, None on failure
    """
    coordinator = _get_distributed_coordinator()
    if not coordinator:
        logger.warning(f"Distributed dispatch failed: coordinator unavailable, "
                       f"falling back to local for {goal_type} goal {goal_id}")
        return None

    tasks = _decompose_goal(prompt, goal_id, goal_type, user_id)
    context = {
        'goal_type': goal_type,
        'user_id': user_id,
        'prompt': prompt,
        'source_node': os.environ.get('HEVOLVE_NODE_ID', 'unknown'),
        'task_source': 'hive',
    }

    try:
        distributed_goal_id = coordinator.submit_goal(
            objective=prompt[:200],
            decomposed_tasks=tasks,
            context=context,
            goal_id=str(goal_id),  # STABLE id → re-dispatch dedups, no ledger flood
        )
        logger.info(f"Distributed dispatch: goal {goal_id} submitted as "
                    f"{distributed_goal_id} with {len(tasks)} tasks")
        return distributed_goal_id
    except Exception as e:
        logger.warning(f"Distributed dispatch error for {goal_type} goal {goal_id}: {e}")
        return None


def _check_robot_capability_match(goal_type: str, goal_id: str) -> bool:
    """For robot goals, verify this node can handle the task.

    Checks task requirements against local robot capabilities.
    Non-robot goals always pass.  Robot goals without requirements pass.

    Returns True if the node is capable, False if it should be
    dispatched to a more capable peer via distributed dispatch.
    """
    if goal_type != 'robot':
        return True

    try:
        from integrations.social.models import db_session, AgentGoal
        with db_session(commit=False) as db:
            goal = db.query(AgentGoal).filter_by(id=goal_id).first()
            if not goal:
                return True
            config = goal.config_json or {}
            required_caps = config.get('required_capabilities', [])
            if not required_caps:
                return True

            from integrations.robotics.capability_advertiser import (
                get_capability_advertiser,
            )
            adv = get_capability_advertiser()
            score = adv.matches_task_requirements({
                'required_capabilities': required_caps,
                'preferred_form_factor': config.get('preferred_form_factor'),
                'min_payload_kg': config.get('min_payload_kg'),
            })
            if score < 0.5:
                logger.info(
                    f"Robot goal {goal_id} capability mismatch "
                    f"(score={score}), prefer distributed dispatch")
                return False
            return True
    except Exception as e:
        logger.debug(f"Robot capability check skipped: {e}")
        return True


def prompt_id_for_goal(goal_id: str) -> str:
    """Deterministic numeric prompt_id for an autonomous goal.

    SINGLE SOURCE of the goal_id -> prompt_id mapping.  ``dispatch_goal``
    stamps this prompt_id on the /chat pipeline (so the recipe is saved
    under ``{prompt_id}_{flow}_*.json`` and REUSE works on later ticks),
    and the steering bridge (``dashboard_service.inject_instruction`` /
    ``get_agent_chat_tail``) recomputes it to resolve the LIVE GroupChat
    for a goal whose ``AgentGoal.prompt_id`` column is still null (the
    flywheel goals never write it back).  Both callers MUST use this one
    formula or the bridge can't find the running group to steer it.
    """
    import hashlib
    h = int(hashlib.md5(str(goal_id).encode()).hexdigest()[:10], 16) % 100_000_000_000
    return str(max(1, h))


def dispatch_goal(prompt: str, user_id: str, goal_id: str,
                  goal_type: str = 'marketing',
                  model_config: list = None) -> Optional[str]:
    """Send a goal prompt through the existing /chat pipeline.

    Uses autonomous=True so Phase 1 (gather_info) runs without
    human interaction — the LLM generates the agent config itself.

    GUARDRAILS enforced: GuardrailEnforcer.before_dispatch() + after_response().

    When Redis is reachable and hive peers exist, goals are automatically
    submitted to the shared DistributedTaskCoordinator. Worker nodes
    across the hive claim and execute them. Falls back to local /chat
    when the coordinator is unavailable or no peers exist.

    For robot goals: capability matching ensures the task goes to a
    node with the right hardware (locomotion, manipulation, sensors).

    Args:
        prompt: The goal prompt (from build_prompt)
        user_id: The agent's user_id
        goal_id: The goal identifier
        goal_type: Goal type prefix for prompt_id
        model_config: Optional per-dispatch config_list override

    Returns:
        Response text or None on failure
    """
    # BUDGET GATE: check goal budget + platform affordability before dispatch
    try:
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        bg_allowed, bg_reason = pre_dispatch_budget_gate(goal_id, prompt)
        if not bg_allowed:
            logger.warning(f"Dispatch blocked by budget gate for {goal_type} goal {goal_id}: {bg_reason}")
            return None
    except ImportError:
        pass

    # TOOL ALLOWLIST: resolve model tier and attach to dispatch context.
    # Tier is sent to /chat as body['model_tier']; create_recipe uses it
    # to call filter_tools_for_model() when building the agent tool list.
    _dispatch_model_tier = None
    if model_config:
        try:
            from integrations.agent_engine.model_registry import model_registry
            first_model = model_config[0].get('model', '') if model_config else ''
            if first_model:
                info = model_registry.get(first_model)
                if info:
                    _dispatch_model_tier = (info.get('tier') or info.get('model_tier'))
                    if _dispatch_model_tier:
                        logger.info(f"Dispatch model tier: {_dispatch_model_tier.value} "
                                    f"for {goal_type} goal {goal_id}")
        except Exception:
            pass  # Model registry unavailable — no tier restriction

    # GUARDRAIL: full pre-dispatch gate (fail-closed: block if guardrails unavailable)
    # Pass the goal dict + user_id so before_dispatch's goal-specific checks
    # (constitutional, ethos, require_consent — #698) actually run on this
    # path; agent_daemon.py:1268 passes goal.to_dict() the same way, and
    # prompt-only here left those checks dormant for dispatch_goal.
    _guard_goal_dict = None
    try:
        from integrations.social.models import db_session, AgentGoal
        with db_session(commit=False) as _gdb:
            _grow = _gdb.query(AgentGoal).filter_by(id=goal_id).first()
            if _grow is not None:
                _guard_goal_dict = _grow.to_dict()
    except Exception as _gerr:
        logger.debug(f"Goal row unavailable for guardrail checks "
                     f"({goal_id}): {_gerr}")
    try:
        from security.hive_guardrails import GuardrailEnforcer
        allowed, reason, prompt = GuardrailEnforcer.before_dispatch(
            prompt, goal_dict=_guard_goal_dict, user_id=user_id)
        if not allowed:
            logger.warning(f"Dispatch blocked for {goal_type} goal {goal_id}: {reason}")
            return None
    except ImportError:
        logger.error("CRITICAL: hive_guardrails not available — blocking dispatch")
        return None

    # AUDIT LOG: record goal dispatch
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'goal_dispatched', actor_id=user_id,
            action=f'dispatch {goal_type} goal {goal_id}',
            target_id=goal_id)
    except Exception:
        pass  # Audit is best-effort

    # ROBOT: capability-matched dispatch — prefer distributed for hardware mismatches
    _tried_distributed = False
    if not _check_robot_capability_match(goal_type, goal_id):
        coordinator = _get_distributed_coordinator()
        if coordinator and _has_hive_peers():
            _tried_distributed = True
            result = dispatch_goal_distributed(prompt, user_id, goal_id, goal_type)
            if result is not None:
                return result
        # Fall through to local if no capable peer found

    # DISTRIBUTED: auto-distribute when coordinator is reachable and hive has peers
    # Skip if robot dispatch already tried distributed (avoid double submission)
    if not _tried_distributed:
        coordinator = _get_distributed_coordinator()
        if coordinator and _has_hive_peers():
            result = dispatch_goal_distributed(prompt, user_id, goal_id, goal_type)
            if result is not None:
                return result
            # Fall through to local dispatch if distributed fails
            logger.info(f"Distributed fallback -> local dispatch for {goal_type} goal {goal_id}")

    # NUMERIC prompt_id (same format as hart_intelligence_entry._next_prompt_id)
    # so it passes the isdigit() check in the adapter and /chat handler.
    # Deterministic from goal_id so the SAME goal always gets the SAME
    # prompt_id across dispatches — enables recipe reuse on later ticks AND
    # lets the steering bridge recompute it to find the live GroupChat.
    prompt_id = prompt_id_for_goal(goal_id)

    body = {
        'user_id': user_id,
        'prompt_id': prompt_id,
        'prompt': prompt,
        'create_agent': True,
        'autonomous': True,
        'casual_conv': False,
        'task_source': 'own',
    }
    if model_config:
        body['model_config'] = model_config
    if _dispatch_model_tier:
        body['model_tier'] = _dispatch_model_tier.value

    # 3-tier dispatch (same as hartos_backend_adapter.py):
    #   Tier 1: Direct in-process import (no ports, no HTTP)
    #   Tier 2: HTTP proxy to backend port
    #   Tier 3: llama.cpp fallback (direct LLM, no agent pipeline)
    resp = None

    # Tier 1: the canonical in-process /chat call.  The adapter resolution,
    # the user-priority gate and the local-LLM semaphore all live in
    # local_chat_dispatch now, so the instruction queue and the distributed
    # worker reach /chat exactly the way this goal path does instead of
    # hand-rolling a raw POST that lands on Nunba's route.
    _status, response = local_chat_dispatch(
        prompt, user_id, prompt_id, daemon_id=goal_id)
    if _status == 'deferred':
        return None
    if _status == 'ok' and response:
        return response

    # Tier 2: HTTP proxy to HARTOS backend port
    # Circuit breaker: skip HTTP if server recently unresponsive
    if _cb_is_open():
        logger.info(f"Circuit breaker open — skipping Tier-2 HTTP for goal {goal_id}")
        return None

    base_url = _local_dispatch_base_url()  # #71: probe live port, not dead 6777

    try:
        resp = pooled_post(f'{base_url}/chat', json=body,
                           headers=_internal_auth_headers(), timeout=120)
        if resp.status_code == 200:
            _cb_record_success()
            result = resp.get_json() if hasattr(resp, 'get_json') else resp.json()
            response = result.get('response', '')

            # GUARDRAIL: post-response check (fail-closed)
            try:
                from security.hive_guardrails import GuardrailEnforcer
                passed, reason = GuardrailEnforcer.after_response(response)
                if not passed:
                    logger.warning(f"Response filtered for goal {goal_id}: {reason}")
                    return None
            except ImportError:
                logger.error("CRITICAL: hive_guardrails not available — blocking response")
                return None

            # GUARDRAIL: coding goals — no merge without constitutional review
            if goal_type == 'coding':
                try:
                    from security.hive_guardrails import ConstitutionalFilter
                    review_dict = {
                        'title': f'Code commit review: {goal_id}',
                        'description': response[:2000],
                        'goal_type': 'coding',
                    }
                    passed, reason = ConstitutionalFilter.check_goal(review_dict)
                    if not passed:
                        logger.warning(
                            f"Coding goal {goal_id} output blocked by "
                            f"constitutional review: {reason}")
                        return None
                except ImportError:
                    logger.error("CRITICAL: ConstitutionalFilter not available — blocking coding goal")
                    return None

            # Record to world model (training data for hive intelligence)
            try:
                from .world_model_bridge import get_world_model_bridge
                bridge = get_world_model_bridge()
                bridge.record_interaction(
                    user_id=user_id,
                    prompt_id=prompt_id,
                    prompt=prompt,
                    response=response,
                    goal_id=goal_id,
                )
            except Exception:
                pass

            return response
        else:
            # Non-200 response — log and queue transient errors for retry
            logger.warning(
                f"Goal dispatch got HTTP {resp.status_code} for {goal_type} "
                f"goal {goal_id}: {resp.text[:200]}")
            if resp.status_code in (429, 500, 502, 503):
                _cb_record_failure()  # Server errors count toward circuit breaker
                try:
                    from .instruction_queue import enqueue_instruction
                    enqueue_instruction(
                        user_id=user_id, text=prompt[:2000], priority=3,
                        tags=[goal_type],
                        context={'goal_id': goal_id, 'goal_type': goal_type,
                                 'queued_reason': f'http_{resp.status_code}'},
                        related_goal_id=goal_id,
                    )
                except Exception:
                    pass
    except requests.RequestException as e:
        _cb_record_failure()
        logger.warning(f"Goal dispatch failed for {goal_type} goal {goal_id}: {e}")

        # Queue the instruction for later execution when compute becomes available
        try:
            from .instruction_queue import enqueue_instruction
            enqueue_instruction(
                user_id=user_id,
                text=prompt[:2000],
                priority=3,
                tags=[goal_type],
                context={
                    'goal_id': goal_id,
                    'goal_type': goal_type,
                    'queued_reason': f'dispatch_failed: {e}',
                },
                related_goal_id=goal_id,
            )
            logger.info(f"Instruction queued for later: {goal_type} goal {goal_id}")
        except Exception as eq:
            logger.debug(f"Instruction queue unavailable: {eq}")

    return None


def _dispatch_single_instruction(base_url: str, user_id: str, inst,
                                  batch_id: str) -> tuple:
    """Dispatch one instruction via /chat. Returns (instruction_id, response_text, error)."""
    body = {
        'user_id': user_id,
        'prompt_id': f'iq_{batch_id}_{inst.id[:8]}',
        'prompt': inst.text,
        'create_agent': True,
        'autonomous': True,
        'casual_conv': False,
        'task_source': 'own',
    }
    # Canonical in-process route first — the SAME call dispatch_goal makes.
    # This path used to go straight to the HTTP POST below, which on a bundled
    # desktop lands on Nunba's /chat (it reads `text`, we send `prompt`) and
    # was answered 400 "Text is required" every time: 88 failures in one day
    # across the same 8 benchmark shards, re-queued forever because
    # fail_instruction has no attempt cap.
    _status, _text = local_chat_dispatch(
        inst.text, user_id, body['prompt_id'], daemon_id=batch_id,
        native_fallback=False)
    if _status == 'ok' and _text:
        return (inst.id, _text[:500], None)
    if _status == 'deferred':
        # A human has the LLM, or it is saturated. NOT a failure: reporting it
        # as one burns an attempt and (once instructions get an attempt cap)
        # would dead-letter healthy work just because the user was typing.
        return (inst.id, None, 'deferred: user active or LLM busy')

    try:
        resp = pooled_post(f'{base_url}/chat', json=body,
                           headers=_internal_auth_headers(), timeout=300)
        if resp.status_code == 200:
            result_text = resp.json().get('response', '')
            return (inst.id, result_text[:500], None)
        return (inst.id, None, f'HTTP {resp.status_code}')
    except requests.RequestException as e:
        return (inst.id, None, str(e))


def drain_instruction_queue(user_id: str, max_tokens: int = 8000) -> Optional[str]:
    """Pull and execute queued instructions with dependency-aware dispatch.

    Uses SmartLedger's dependency graph to determine execution order:
    - Independent instructions dispatch in parallel (concurrent threads)
    - Dependent instructions wait for prerequisites to complete first

    Execution proceeds in waves:
      Wave 0: all instructions with no dependencies → parallel dispatch
      Wave 1: instructions depending on wave 0 → parallel dispatch
      ...until all waves complete.

    Falls back to single-batch dispatch when SmartLedger is unavailable.

    Called by agent_daemon.py on idle tick, or manually via API.

    Args:
        user_id: User whose queue to drain
        max_tokens: Max tokens across all instructions

    Returns:
        Combined response text, or None if queue empty or all failed
    """
    try:
        from .instruction_queue import get_queue
        q = get_queue(user_id)

        # Acquire drain lock — prevents concurrent drains for same user
        # (daemon tick + API call + another agent all trying simultaneously)
        if not q.acquire_drain_lock():
            logger.info(f"Drain skipped for {user_id}: another drain in progress")
            return None

        try:
            # Try dependency-aware execution plan
            plan = q.pull_execution_plan(max_tokens=max_tokens)
            if plan is None:
                return None

            base_url = _local_dispatch_base_url()  # #71: probe live port, not dead 6777
            all_results = []
            any_success = False

            logger.info(
                f"Draining instruction queue for {user_id}: "
                f"{plan.total_instructions} instructions in "
                f"{len(plan.waves)} waves"
            )

            for wave_idx, wave in enumerate(plan.waves):
                logger.info(
                    f"Wave {wave_idx + 1}/{len(plan.waves)}: "
                    f"{len(wave)} instruction(s)"
                )

                if len(wave) == 1:
                    # Single instruction — dispatch directly (no thread pool overhead)
                    inst = wave[0]
                    iid, result, error = _dispatch_single_instruction(
                        base_url, user_id, inst, plan.batch_id,
                    )
                    if error:
                        # A deferral is not the instruction's fault: the LLM
                        # was busy or a human was using it. Burning an attempt
                        # would dead-letter healthy work because someone was
                        # typing.
                        _transient = str(error).startswith('deferred:')
                        q.fail_instruction(iid, error, transient=_transient)
                        logger.warning(f"Instruction [{iid}] failed: {error}")
                    else:
                        q.complete_instruction(iid, result)
                        all_results.append(result)
                        any_success = True
                else:
                    # Multiple independent instructions — dispatch in parallel.
                    #
                    # Thread safety:
                    # - _dispatch_single_instruction() is a pure HTTP call (no shared state)
                    # - Results collected via as_completed() on the CALLING thread
                    # - q.complete/fail_instruction() acquires q._lock (serialized)
                    # - SmartLedger mutations happen inside q._lock (no separate lock needed)
                    # - File I/O uses atomic write (temp + rename)
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(len(wave), 4),
                    ) as executor:
                        futures = {
                            executor.submit(
                                _dispatch_single_instruction,
                                base_url, user_id, inst, plan.batch_id,
                            ): inst
                            for inst in wave
                        }
                        for future in concurrent.futures.as_completed(futures):
                            iid, result, error = future.result()
                            if error:
                                # Same deferral exemption as the single-
                                # instruction branch above: a busy LLM must
                                # not burn an attempt.
                                _transient = str(error).startswith('deferred:')
                                q.fail_instruction(iid, error,
                                                   transient=_transient)
                                logger.warning(f"Instruction [{iid}] failed: {error}")
                            else:
                                q.complete_instruction(iid, result)
                                all_results.append(result)
                                any_success = True

            if any_success:
                combined = '\n---\n'.join(all_results)
                logger.info(
                    f"Plan {plan.batch_id} completed: "
                    f"{len(all_results)}/{plan.total_instructions} succeeded"
                )
                return combined
            return None
        finally:
            q.release_drain_lock()

    except Exception as e:
        logger.error(f"Queue drain error: {e}")
        return None
