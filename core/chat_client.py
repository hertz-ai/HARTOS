"""The ONE way anything inside HARTOS calls ``POST /chat``.

WHY THIS EXISTS
───────────────
There was no shared /chat client. Every caller hand-built its own body, and
measured 2026-08-09, **9 of 11 never mentioned ``request_id`` at all**:

    hartos_bootstrap · hart_cli · distributed_agent/worker_loop ·
    deploy/linux/dbus/hart_dbus_service · robotics/intelligence_api ·
    openclaw/shell_openclaw_apis · openclaw/hart_skill_server ·
    channels/registry · agent_engine/liquid_ui_service

``hart_intelligence_entry.py`` does no defaulting (``request_id = data.get(...)
or ... or None``) and ``dispatch.is_genuine_user_request`` is explicit: *"EMPTY /
None request_id is NOT a user — it is background"*. So every one of those callers
was classified as autonomous daemon work — including the ones where the requester
is unambiguously a person: someone asking the OS over D-Bus, a human at the node's
terminal, ``/api/assistant/chat``, the desktop's own intent bar, and **every
Discord / Telegram / WhatsApp / Matrix user**.

Three consequences, and the third is the worst:

  1. the inbound foreground gate never fires, so running daemons never yield;
  2. ``llm_outbound_logger`` admits the turn as ``kind='daemon'`` and the llama
     scheduler NEVER preempts for a daemon — the human queues BEHIND the flywheel;
  3. daemon calls run on the CLOSABLE background client, so a later foreground
     preempt can ABORT the human's own request mid-flight.

Measured symptom: ``POST /chat`` took 49.2s then 76.5s while the flywheel held both
llama slots, against the desktop shell's 30s budget — so the A2UI
INTENT → DECOMPOSE → COMPOSE loop could never paint.

THE POLICY THIS IMPLEMENTS (steward, 2026-08-09)
────────────────────────────────────────────────
    "all active user related conversation based agents, chat session backs an
     agent is priority, all seeded goals and daemon agents are by default daemon"

So *what backs the turn* decides, and both sides are **declared**:

  PRIORITY (user)  a conversation-backed turn — the desktop intent bar, the CLI,
                   D-Bus, a chat channel, the assistant route. Carries a real id.
  DAEMON           seeded goals, the agent daemon, worker loops. Carries an
                   EXPLICIT ``daemon_<id>`` tag.

Emptiness stops being a classification and becomes what it actually is: a caller
that forgot. Today the system cannot tell "I am a daemon" from "someone omitted a
field"; after this, it can.

IDEMPOTENT — PASSTHROUGH, NEVER RE-MINT (steward, 2026-08-09)
─────────────────────────────────────────────────────────────
    "it shd be idempotent if any previous layer already handled what it enforces
     and act like passthrough for those, No redundant parallel generations."

``request_id`` is a CORRELATION and DEDUP key, not a flag:

  * every HARTOS caller stamps it into the OpenAI ``user`` field, so each
    llama-server task line correlates 1:1 with the frozen_debug RequestID column
    (CLAUDE.md, logs table). Re-minting mid-stack breaks that 1:1.
  * ``hart_intelligence_entry.py:2350`` records a bug ALREADY FIXED ONCE — two
    audio playbacks per turn, because "the SPA's request_id-keyed dedup didn't
    catch them when envelope shapes diverged across transports". A second minting
    layer reintroduces exactly that class.
  * ``chatbot_routes.py:77`` keys the TTS SSE payload on it.

So a body that already carries an id passes through UNTOUCHED. A layer that
regenerates an id another layer assigned is the redundant parallel generation the
policy rules out.

NUNBA IS OUT OF SCOPE — DO NOT CHANGE IT
────────────────────────────────────────
``Nunba-HART-Companion/routes/chatbot_routes.py:2529`` already does the right
thing::

    request_id = data.get('request_id', str(int(time.time())))

It normalizes at its adapter and forwards. That is the pattern copied here, not a
thing to change: anything arriving via Nunba reaches HARTOS with an id ALREADY
SET, so this module sees it populated and passes through. Nunba's request must be
byte-identical before and after.
"""
import time
from typing import Any, Dict, Optional

#: The prefix ``dispatch.is_genuine_user_request`` keys "background" on
#: (``dispatch.py:211``: non-empty AND not starting with this = a user).
#: Imported from here so a second spelling can never appear.
DAEMON_PREFIX = 'daemon_'


def mint_request_id() -> str:
    """A fresh id for a turn that has none.

    Deliberately the SAME rule Nunba's adapter uses
    (``chatbot_routes.py:2529``: ``str(int(time.time()))``) so the two layers can
    never disagree on shape. Do not "improve" this to a uuid without changing
    Nunba in the same commit — divergent shapes are how correlation keys rot.
    """
    return str(int(time.time()))


def daemon_request_id(goal_id: Any) -> str:
    """The explicit background tag: ``daemon_<goal_id>``.

    Matches ``dispatch.py:816``'s ``_daemon_request_id``. Background work should
    say so with this, rather than relying on an empty field to be read as
    background — that inference is what makes a forgotten field indistinguishable
    from a declared daemon.
    """
    return '%s%s' % (DAEMON_PREFIX, goal_id)


def normalize_chat_body(body: Optional[Dict[str, Any]] = None,
                        daemon_id: Any = None) -> Dict[str, Any]:
    """Return a /chat body guaranteed to carry a ``request_id``.

    PASSTHROUGH: if ``body`` already has a non-empty ``request_id``, it is
    returned unchanged — no re-minting, whatever ``daemon_id`` says. A previous
    layer (Nunba's adapter, a frontend, an upstream hop) already owns that id and
    downstream correlation depends on it surviving.

    Otherwise: ``daemon_<daemon_id>`` when ``daemon_id`` is given, else a freshly
    minted user id.

    Never mutates the caller's dict — callers reuse bodies, and a normalizer that
    edits its input turns one forgotten field into a shared-state bug.
    """
    out = dict(body or {})
    existing = out.get('request_id')
    if existing not in (None, ''):
        return out                      # passthrough — a previous layer handled it
    out['request_id'] = (daemon_request_id(daemon_id) if daemon_id is not None
                         else mint_request_id())
    return out


def is_user_turn(body: Optional[Dict[str, Any]]) -> bool:
    """Would HARTOS classify this body as a genuine user turn?

    Mirrors ``dispatch.is_genuine_user_request`` so a caller can assert its own
    intent in a test without standing up the Flask app. Kept as a thin predicate
    over the SAME rule, never a second rule.
    """
    rid = (body or {}).get('request_id')
    return bool(rid) and not str(rid).startswith(DAEMON_PREFIX)


def post_chat(url: str, body: Optional[Dict[str, Any]] = None,
              daemon_id: Any = None, timeout: float = 300.0, **kwargs):
    """POST a normalized body to ``/chat`` through the pooled/scheduled path.

    Routes via ``core.http_pool.pooled_post`` so llama slot admission and the
    foreground-preempt scheduler stay on the ONE existing path — this adds a
    normalizer, never a second HTTP path.

    ``timeout`` defaults high on purpose. A desktop intent that decomposes on the
    CPU-only potato floor legitimately takes minutes; the shell's old hardcoded
    30s was shorter than the measured p50 and could only ever time out. Callers
    that genuinely need to bound the wait should say so explicitly.
    """
    from core.http_pool import pooled_post
    return pooled_post(url, json=normalize_chat_body(body, daemon_id=daemon_id),
                       timeout=timeout, **kwargs)
