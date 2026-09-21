"""
User Consent Manager — explicit opt-in for data access, revenue sharing, public exposure.

Follows the NotificationService pattern (static methods taking db session).

HTTP surface deprecation (orchestrator review acd11f55, 2026-04-25):
  The legacy ``/api/consent/<user_id>/*`` route family was deleted as
  part of the consent-surface consolidation.  All HTTP writes to the
  ``user_consents`` table now flow through
  ``integrations.social.consent_api`` (JWT-authed, append-only,
  mounted at ``/api/social/consent``).  ``register_consent_routes``
  was removed from this module.

Static methods on ``ConsentService`` REMAIN — they are still the
direct (in-process) read + write API for internal services
(``revenue_tracker``, ``ai_governance``, ``federated_aggregator``,
``lifecycle_hooks``, etc).  As of the consolidation, ``grant_consent``
is APPEND-ONLY: every grant inserts a NEW row and never rewrites a
prior row's ``granted_at``.  This aligns the in-process semantics
with the JWT HTTP surface so the audit trail of grant events is
immutable regardless of which entry point a caller chose.
"""
import logging
import re
import uuid
from datetime import datetime

from .models import UserConsent

_logger = logging.getLogger('hevolve.consent')

CONSENT_TYPES = frozenset({
    'data_access',       # Agent needs to access user data
    'revenue_share',     # User opts into compute-for-revenue (earnings credited)
    'public_exposure',   # Content made public
    'payment_setup',     # User provides UPI/payment ID for revenue payouts
    'compute_contribute', # User allows their device to process hive tasks
    'announcement_subscription',  # A channel destination asked to receive
                         # broadcasts. Keyed by '<channel>:<chat_id>' rather
                         # than a HARTOS user, because the subscriber here is
                         # a Telegram group or Discord channel. Exists so the
                         # broadcaster can check a RECORD instead of an
                         # operator's assertion that people opted in.
    'cloud_egress',      # User allows fallback to cloud (vision, social
                         # sync, third-party APIs) when local resources
                         # can't satisfy the request.  Scope-aware: e.g.
                         # scope='vision' grants cloud-vision specifically;
                         # scope='social_sync' grants cloud social fan-out.
                         # First-time fallback emits a notification +
                         # request_consent record; user grants once,
                         # subsequent requests proceed without prompting.
    'cloud_capability',  # Agent uses a cloud-backed capability (T2 browser
                         # research read/post, encounter icebreaker, room
                         # presence).  The de-facto type written by
                         # consent_api.grant_consent and read by tools.py /
                         # encounter_api / icebreaker_service /
                         # room_presence_service — MUST be registered here or
                         # check_consent's _validate_consent_type rejects it and
                         # the whole T2 read/post subsystem is denied (#review).
    'screen_capture',    # The desktop's own screen is captured and described
                         # for the visual agent (VisionService screen channel,
                         # #701).  The capture loop's first denied tick files
                         # the pending ask; granting in the UserConsent UI
                         # starts capture on the next tick.
    'computer_control',  # An agent acts on this computer: shell commands,
                         # file writes, mouse and keyboard, opening apps
                         # (integrations.vlm.safety.computer_control_block).
                         # Asked of the desktop owner, whose machine it is.
    'copilot_access',    # An agent may use the owner's Claude Code
                         # subscription as its expert (the copilot).  A
                         # grant or revoke acts on the ONE copilot switch
                         # (claude_code_backend.set_copilot_enabled), the
                         # same flag the admin page flips; blanket scope,
                         # since the switch is node-wide.
    'device_access',     # A person's phone reaches this desktop's agents
                         # from the network (#111).  Asked of the desktop
                         # owner; the scope names the device's Ed25519 key
                         # (device_scope), so the GRANTED row is the key on
                         # file that security.middleware verifies the
                         # phone's signed calls against.  Permanent until
                         # revoked, as the owner ruled.
    'peer_admission',    # A computer on the network asks to link with this
                         # one and signed nothing, so it has proved no
                         # identity at all.  The sibling of 'device_access':
                         # that one names a phone's verified key, this one
                         # can only name a network address, and the card
                         # says so.  Asked because enforcement mode is a
                         # flag and a flag must not decide alone -- unset, it
                         # used to ADMIT the stranger silently; fixed to
                         # 'hard' it would REFUSE just as silently, and the
                         # owner ruled against both: "do not gulp, the
                         # consent shd be shown when a flag gates a useful
                         # logic" (2026-09-21).  The refused attempt is not
                         # retroactively admitted; an Allow admits the peer
                         # on its next try, exactly as device_access does.
    'voice_speech',      # Proactive vocal speech and audio narration by the
                         # servicing layer. Asked of the visitor/user as the
                         # first proactive step to respect acoustic privacy
                         # and enable browser autoplay.
    'camera_capture',    # An agent sees through this computer's camera
                         # (VisionService camera channel).  The sibling of
                         # 'screen_capture': the screen had a type since #701
                         # and the camera had none, which is why the camera
                         # ask could not be filed canonically and went out as
                         # a bare LiquidUI 'approval' component instead --
                         # unrecorded, unrevocable, and invisible to every
                         # surface but AgentOverlay (#863).
    'capability_setup',  # An agent provisions a capability this machine does
                         # not have yet: a voice engine's private venv, the
                         # packages it pins and its model weights, downloaded
                         # and installed here.  Asked of the desktop owner,
                         # whose disk and bandwidth it is, and scoped to the
                         # ONE capability ('tts:f5_tts'), so a yes to a 2.5 GB
                         # voice is never a yes to a 12 GB one.  The ask is
                         # raised by the task that needed the capability and
                         # could not do it -- a voiced reply with no cloning
                         # engine installed -- never at boot and never as a
                         # blanket.  This type records only whether the owner
                         # wants it; the work itself is the provisioning path
                         # that already exists (a self_heal goal the agent
                         # daemon paces, repair_backend_venv ->
                         # install_backend_full, the same function the
                         # "Set up TTS" UI calls).
})

#: The capability an agent asks for, keyed by the ``action`` its ask carries,
#: mapped to the consent type that records the answer.  ONE map, read by both
#: halves of the flow -- the ask (hart_intelligence_entry._request_consent,
#: behind the Request_Camera_Access / Request_Screen_Access tools) and the
#: answer (record_capability_decision, behind every approval surface) -- so a
#: capability cannot end up filed under one name and granted under another.
#: The keys are exactly the aliases /api/agent/approval already accepted.
#:
#: 'enable_audio' / 'mic' are deliberately ABSENT: no code asks for them, and
#: minting a consent type for an ask nobody makes would record a grant the
#: owner was never shown.  The mic is governed by core.ai_sensing's
#: kill-switch today; if an ask is ever built for it, it belongs here.
CAPABILITY_CONSENT_TYPES = {
    'enable_camera': 'camera_capture',
    'camera': 'camera_capture',
    'vision': 'camera_capture',
    'enable_screen': 'screen_capture',
    'screen': 'screen_capture',
    'computer_use': 'screen_capture',
}


def consent_type_for_action(action) -> str:
    """The consent type an approval ``action`` records against, or None.

    Case and surrounding space do not matter: /api/agent/approval lowercases
    and strips before it dispatches, and the ask side must agree with it
    without having to remember to.
    """
    if not isinstance(action, str):
        return None
    return CAPABILITY_CONSENT_TYPES.get(action.strip().lower())

#: A device is identified by its Ed25519 public key (the PeerLink identity
#: every phone already has); the consent scope carries the whole key so no
#: other table has to.  64 hex chars + the prefix fit UserConsent.scope(100).
DEVICE_SCOPE_PREFIX = 'device:'
_DEVICE_KEY_RE = re.compile(r'[0-9a-f]{64}')


def device_scope(public_key_hex):
    """The consent scope for a device key, or None when the key is not a
    64-hex Ed25519 public key (the only shape the gate looks up)."""
    if not isinstance(public_key_hex, str):
        return None
    key = public_key_hex.lower()
    if not _DEVICE_KEY_RE.fullmatch(key):
        return None
    return f'{DEVICE_SCOPE_PREFIX}{key}'


def device_fingerprint(scope_or_key):
    """What the owner reads to tell one phone from another: the key's first
    16 hex in four groups ('3f9a 1c02 77de b4e1'), from a device scope or a
    bare key; None for anything else.

    The name a phone signs into its ask is self-asserted, so the card and
    the trusted-phones list show this beside it, and the phone shows its own
    key the same way (PeerLinkCrypto.getEd25519PublicHex), so the two can be
    matched by eye.  The same 16 hex are the phone's node_id.
    """
    if not isinstance(scope_or_key, str):
        return None
    key = scope_or_key.lower()
    if key.startswith(DEVICE_SCOPE_PREFIX):
        key = key[len(DEVICE_SCOPE_PREFIX):]
    if not _DEVICE_KEY_RE.fullmatch(key):
        return None
    return ' '.join(key[i:i + 4] for i in range(0, 16, 4))


def _audit(event_type: str, actor_id: str, action: str, detail: dict):
    """Best-effort immutable audit log entry.

    Uses the audit log's in-memory fallback when the DB session would conflict
    (e.g. StaticPool / in-memory SQLite during tests).
    """
    try:
        from security.immutable_audit_log import get_audit_log
        audit = get_audit_log()
        # Force in-memory mode to avoid opening a second DB session
        # that would conflict with the caller's active session on StaticPool.
        saved = audit._use_db
        audit._use_db = False
        try:
            audit.log_event(event_type, actor_id=actor_id,
                            action=action, detail=detail)
        finally:
            audit._use_db = saved
    except Exception as e:
        import logging as _log
        _log.getLogger('hevolve.consent').warning(
            "Consent audit log failed (event=%s, actor=%s): %s",
            event_type, actor_id, e)


def _emit(topic: str, data: dict, msg_id: str = None):
    """Best-effort EventBus broadcast + push to frontend via WAMP/SSE.

    ``msg_id``: optional stable idempotency key.  ``on_notification`` honours a
    caller-supplied msg_id and every client dedupes by it, so passing a STABLE
    id (e.g. the consent row id) lets a producer re-emit the same ask on every
    poll for reliable delivery while the UI still shows exactly one card.  A
    ``None`` msg_id falls through to a fresh per-emit id (one-shot events)."""
    bus = note_sent = False
    try:
        from core.platform.events import emit_event
        emit_event(topic, data)
        bus = True
    except Exception:
        _logger.warning("consent %s: event bus leg failed", topic, exc_info=True)
    # Also push as a notification to the user's frontend (Nunba, Hevolve, Android)
    # so consent dialogs can appear on any platform
    user_id = data.get('user_id', '')
    if user_id:
        try:
            from integrations.social.realtime import on_notification
            note = {
                'type': topic,  # consent.granted, consent.revoked, consent.request
                'consent_type': data.get('consent_type', ''),
                'agent_id': data.get('agent_id'),
                'scope': data.get('scope', '*'),
                'reason': data.get('reason', ''),
            }
            # The agent's own name (agent_display_name): the card shows it,
            # never the id, which is a prompt id and means nothing to a person.
            # A device ask names the person whose phone asks the same way,
            # with the key's fingerprint beside the self-asserted name.
            for name_key in ('agent_name', 'requester_name', 'requester_fingerprint'):
                if data.get(name_key):
                    note[name_key] = data[name_key]
            if msg_id:
                note['msg_id'] = msg_id
            on_notification(user_id, note)
            note_sent = True
        except Exception:
            _logger.warning("consent %s: notification leg failed for user %s",
                            topic, user_id, exc_info=True)
    # ONE line per emit, naming what actually left.  Both legs used to be
    # `except Exception: pass` with no log at all, so "the ask was delivered",
    # "the ask was built but no leg worked" and "no ask was ever filed" all
    # produced identical (empty) logs -- there was nothing to measure a live
    # consent drive against, and a silent delivery failure was indisguishable
    # from a gate that never asked.
    _logger.info("consent emit %s type=%s scope=%s agent=%s user=%s "
                 "bus=%s notification=%s msg_id=%s",
                 topic, data.get('consent_type'), data.get('scope', '*'),
                 data.get('agent_id'), user_id or '(none)',
                 bus, note_sent, msg_id)


def _validate_consent_type(consent_type: str):
    if consent_type not in CONSENT_TYPES:
        raise ValueError(
            f"Invalid consent_type '{consent_type}'. "
            f"Must be one of: {', '.join(sorted(CONSENT_TYPES))}")


_AGENT_ID_RE = re.compile(r'[A-Za-z0-9_-]+')


def _is_a_name(name, aid):
    """False for the placeholders the agent mirror manufactures when a
    prompt has no name ("Agent <id>", "agent-<id>", hart_intelligence_entry
    _create_social_agent_from_prompt :10993/:11005) and for the bare id:
    those are the identifier the owner cannot read, dressed as a name.
    Exact matches only: a real name may contain a digit sequence that
    happens to be a short id ("Studio 1 Assistant" with id 1)."""
    name = (name or '').strip()
    if not name or name == aid:
        return False
    return name.lower() not in (f'agent {aid}'.lower(), f'agent-{aid}'.lower())


def agent_display_name(db, agent_id):
    """The name a person knows an agent by, or None.

    ``agent_id`` on a consent is the agent's prompt id, which the owner has
    never seen.  The name lives in prompts/<prompt_id>.json ("name"), and
    the social mirror of that agent (User.agent_id == prompt id, created by
    _create_social_agent_from_prompt) carries it as display_name: the mirror
    is read first because it is one query, then the file.  A placeholder
    built from the id is not a name (_is_a_name).  Never raises; a lookup
    failure is logged and reads as "no name".

    The id is used as a file name, so only ``[A-Za-z0-9_-]`` ids are looked
    up: consent ids are internal today, but the same helper names a remote
    requester for a household ask, and ``../x`` must not read x.json.
    """
    if agent_id in (None, ''):
        return None
    aid = str(agent_id)
    if not _AGENT_ID_RE.fullmatch(aid):
        _logger.debug("agent_display_name: refusing id %r", aid)
        return None
    try:
        from .models import User
        row = db.query(User).filter(User.agent_id == aid,
                                    User.user_type == 'agent').first()
        if row is not None:
            for candidate in (row.display_name, row.username):
                if _is_a_name(candidate, aid):
                    return candidate.strip()
    except Exception:
        _logger.debug("agent_display_name: mirror lookup failed for %s",
                      aid, exc_info=True)
    try:
        import json
        import os
        from core.platform_paths import get_recipe_prompts_dir
        path = os.path.join(get_recipe_prompts_dir(), f'{aid}.json')
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as fh:
                name = str(json.load(fh).get('name') or '')
            if _is_a_name(name, aid):
                return name.strip()
    except Exception:
        _logger.debug("agent_display_name: prompt file read failed for %s",
                      aid, exc_info=True)
    return None


def _copilot_switch_from_consent(consent_type: str, granted: bool) -> None:
    """The owner's answer to a copilot ask acts on the copilot switch itself.

    'copilot_access' is node-wide, so a grant turns the copilot ON and a
    revoke or decline turns it OFF through the one writer the admin page
    uses (claude_code_backend.set_copilot_enabled); no second flag.  Best
    effort: the consent row is already written, and a switch that cannot be
    persisted is logged by the writer."""
    try:
        from integrations.coding_agent.claude_code_backend import (
            COPILOT_CONSENT_TYPE, set_copilot_enabled)
        if consent_type == COPILOT_CONSENT_TYPE:
            set_copilot_enabled(granted)
    except Exception as e:
        _logger.warning("copilot switch from consent %s failed: %s", consent_type, e)


#: Which embodied_ai feed a capability consent governs.  Inverse of
#: CAPABILITY_CONSENT_TYPES' values, kept next to the switch it drives.
_CONSENT_FEED = {'camera_capture': 'camera', 'screen_capture': 'screen'}


def _embodied_feed_from_consent(consent_type: str, granted: bool) -> None:
    """The owner's answer to a camera or screen ask acts on the feed itself.

    Sibling of _copilot_switch_from_consent, and it exists for the same
    reason: the answer can arrive on any surface -- the privacy page, a
    consent card on the desktop, the same card on the floating companion, a
    phone -- and every one of them must start or stop the ONE feed, through
    the ONE lifecycle path the admin settings toggle uses
    (channels.admin.api._apply_embodied_toggle).  Before this, only
    /api/agent/approval applied a feed, so a grant made anywhere else was a
    row that turned nothing on.

    The persisted flag is written too, so a restart sees the same answer and
    get_embodied_status reports it.  Best effort: the consent row is already
    written, and a switch that cannot be applied is logged here.
    """
    feed = _CONSENT_FEED.get(consent_type)
    if feed is None:
        return
    try:
        from integrations.channels.admin.api import get_api, _apply_embodied_toggle
        cfg = get_api()._global_config.embodied_ai
        setattr(cfg, 'camera_enabled' if feed == 'camera' else 'screen_capture_enabled',
                bool(granted))
        get_api()._save_config()
        _apply_embodied_toggle(feed, bool(granted), cfg)
        _logger.info("consent %s -> embodied feed %s=%s",
                     consent_type, feed, bool(granted))
    except Exception as e:
        _logger.warning("embodied feed %s from consent %s failed: %s",
                        feed, consent_type, e)


def _named(db, data: dict, agent_id) -> dict:
    """``data`` with 'agent_name' added when the agent has one: the ask and
    the notices then share one shape, and an unnamed agent carries no key."""
    name = agent_display_name(db, agent_id)
    if name:
        data['agent_name'] = name
    return data


class ConsentService:
    """Static-method service for managing user consent records."""

    @staticmethod
    def request_consent(db, user_id: str, consent_type: str,
                        scope: str = '*', agent_id=None, reason: str = '',
                        requester_name: str = ''):
        """Create a pending (not yet granted) consent record.

        Returns existing record if one already exists for this combination.
        ``reason`` rides on the ask, so the card can say what is asked for.
        ``requester_name`` is the person asking when the asker is not an
        agent (a device ask: the phone's owner), shown like agent_name.  It
        is also written to the new row as its ``label``, once: the name the
        phone signed into its first ask stands, a later ask cannot rename
        the row (the label is a hint beside the fingerprint, never
        identity).
        """
        _validate_consent_type(consent_type)

        ask = {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        }
        if reason:
            ask['reason'] = reason
        if requester_name:
            ask['requester_name'] = requester_name
        fingerprint = device_fingerprint(scope) if consent_type == 'device_access' else None
        if fingerprint:
            ask['requester_fingerprint'] = fingerprint
        # Who is asking, by name: the card says "<name> asks to ...".
        _named(db, ask, agent_id)

        existing = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
        ).first()
        if existing:
            # Idempotent RE-ASK: the old code returned silently here, so the
            # ask _emit fired exactly once at row creation -- if the UI wasn't
            # subscribed then (fresh boot, a page reload / "retry", a dropped
            # notification) the pending ask was invisible forever while the
            # gate kept denying every tick.  Re-emit on every poll so whatever
            # UI is connected NOW receives it; the stable msg_id makes clients
            # dedupe every re-emit into a single card -- reliable, zero spam.
            #
            # Re-ask ONLY while the combination is genuinely UNDECIDED.
            # grant_consent is append-only (a granted row coexists with the
            # old pending one) and revoke mutates a row to granted=False +
            # revoked_at, so `existing.granted` on the .first() row is not
            # authoritative -- query for any GRANTED or REVOKED row instead:
            # a grant means "already yes" and a revoke means "user said no",
            # both of which must silence the ask.
            from sqlalchemy import or_
            decided = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == scope,
                UserConsent.agent_id == agent_id,
                or_(UserConsent.granted == True,
                    UserConsent.revoked_at.isnot(None)),
            ).first()
            if decided is None:
                _emit('consent.request', ask,
                      msg_id=f'consent.request:{existing.id}')
            return existing

        consent = UserConsent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            consent_type=consent_type,
            scope=scope,
            granted=False,
            label=requester_name[:100] if requester_name else None,
        )
        db.add(consent)
        db.flush()
        # Delivery, sibling-parity: grant_consent / auto_grant_with_notice
        # / announce_revocation all _emit, and _emit's own comment always
        # listed consent.request as a topic.  Stable msg_id (row id) so this
        # first ask and every re-ask above collapse to ONE card client-side.
        _emit('consent.request', ask, msg_id=f'consent.request:{consent.id}')
        return consent

    @staticmethod
    def check_or_request(db, user_id: str, consent_type: str,
                         scope: str = '*', agent_id=None,
                         reason: str = '', requester_name: str = '') -> bool:
        """True when the consent is active; otherwise file the ask (or send
        it again) and return False.

        The shape a polling gate needs: vision's screen capture and
        integrations.vlm.safety.computer_control_block.  request_consent
        dedupes, so asking on every poll still shows one card.
        """
        if ConsentService.check_consent(db, user_id, consent_type,
                                        scope=scope, agent_id=agent_id):
            return True
        ConsentService.request_consent(db, user_id, consent_type, scope=scope,
                                       agent_id=agent_id, reason=reason,
                                       requester_name=requester_name)
        return False

    @staticmethod
    def record_capability_decision(db, user_id: str, action: str,
                                   granted: bool, agent_id=None):
        """Write the owner's Approve / Deny on a capability ask.  Returns the
        consent type recorded, or None when the action governs no consent.

        ONE implementation, for every approval surface: /api/agent/approval on
        the brain (AgentOverlay's card, the Android client) and the same route
        on the LiquidUI shell.  Before this, those surfaces flipped
        embodied_ai.*_enabled directly, so the owner's answer existed only as
        a config flag and a log line -- no row, so nothing to revoke, nothing
        on the privacy page, no `consent.granted` for the other surfaces to
        drop their copy of the card on, and a Deny that the next poll simply
        re-asked because nothing recorded it as decided.

        A grant is BLANKET (no agent, scope '*'), matching what the SPA's own
        grant writes (consent_api.grant_consent) and what its button promises
        ("Allow ALL agents to ..."): the capability is the camera, not one
        agent's use of it, and check_consent's blanket step is what the
        per-agent asks then match against.  A grant that is already active is
        left alone rather than appended -- the supported pattern from
        grant_consent's own docstring, since re-granting is what trips the
        UNIQUE constraint.

        A denial revokes for the ask's own agent when one is named, matching
        the card's "Don't allow this agent", and blanket otherwise.
        """
        consent_type = consent_type_for_action(action)
        if consent_type is None:
            _logger.info("capability decision ignored: action %r governs no "
                         "consent type", action)
            return None
        if granted:
            if ConsentService.active_grant(db, user_id, consent_type) is None:
                # grant_consent applies the feed and emits consent.granted.
                ConsentService.grant_consent(db, user_id, consent_type)
            else:
                # Already allowed, so no second row (the UNIQUE constraint
                # rejects it) -- but the owner just pressed Approve, and the
                # feed may have been stopped from admin settings while the
                # grant stood, so re-assert it.  Exclusive with the branch
                # above, so the feed is never applied twice for one answer.
                _embodied_feed_from_consent(consent_type, True)
        elif ConsentService.revoke_consent(db, user_id, consent_type,
                                           agent_id=agent_id) is None:
            # Nothing on file to revoke -- a client that answers without
            # having been asked here.  The answer still has to stop the feed
            # and still has to reach the other surfaces so their copy of the
            # card goes away, which is exactly what announce_revocation does.
            ConsentService.announce_revocation(user_id, consent_type,
                                               agent_id=agent_id)
        return consent_type

    @staticmethod
    def declined(db, user_id: str, consent_type: str, scope: str = '*',
                 agent_id=None) -> bool:
        """True when the owner said no to this ask: a row for exactly this
        combination has been revoked.

        The consent card's "Don't allow" (consent_api.decline_consent) is
        revoke_consent on the pending ask, which marks it revoked; a revoked
        grant counts too.  request_consent does not ask again once a
        combination is decided, so a no stands until a new grant covers it.
        check_consent looks at grants first, and a blanket grant ("Allow ALL
        agents") covers an agent the owner said no to, so ask this only after
        check_consent failed.
        """
        _validate_consent_type(consent_type)
        return db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.revoked_at.isnot(None),
        ).first() is not None

    @staticmethod
    def grant_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None):
        """Grant consent — APPEND-ONLY.

        Always inserts a NEW ``UserConsent`` row.  The audit trail of
        WHEN a consent was granted is immutable history — never
        rewrite a prior row's ``granted_at``.

        Mirror of ``integrations.social.consent_api.grant_consent``
        (JWT HTTP surface) so all writers — internal services and the
        UI — share the same audit-trail invariant.

        Notes on the unique constraint:
          ``UserConsent`` has
          ``UniqueConstraint(user_id, agent_id, consent_type, scope)``
          (see ``_models_local.py``).  SQL treats ``NULL`` as distinct
          in unique constraints, so ``agent_id=None`` rows can stack
          freely.  For non-NULL ``agent_id``, callers that re-grant
          while a prior granted-and-not-revoked row exists for the
          same triple will hit ``IntegrityError``; the supported
          pattern in that case is to ``revoke_consent`` first or use
          ``check_consent`` and skip.  No production caller in the
          tree today re-grants under a non-NULL agent_id with the
          same scope, so this is a no-op semantic change for them
          (orchestrator review acd11f55).

        Deprecated path: prior versions UPSERTed and rewrote
        ``granted_at`` on re-grant.  That semantic is gone as of the
        consolidation commit.
        """
        _validate_consent_type(consent_type)

        now = datetime.utcnow()
        consent = UserConsent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            consent_type=consent_type,
            scope=scope,
            granted=True,
            granted_at=now,
            # a phone keeps the name its ask was filed under (#111)
            label=(ConsentService._label_on_file(db, user_id, consent_type, scope)
                   if consent_type == 'device_access' else None),
        )
        db.add(consent)
        db.flush()

        _audit('consent', actor_id=user_id,
               action=f'consent.granted:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        _emit('consent.granted', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })
        _copilot_switch_from_consent(consent_type, True)
        _embodied_feed_from_consent(consent_type, True)

        # Up-sync the now-public agents (gap #4): agents are almost always
        # created BEFORE the owner grants public_exposure, so the
        # producer-on-create hook no-ops for them.  When public_exposure is
        # granted, re-queue up-sync for this owner's agents so an agent created
        # earlier RISES once its owner consents.  The receiver is idempotent
        # (upsert-by-id) so a re-queue is harmless; best-effort — a sync hiccup
        # never fails the consent grant.  No new state: consent stays the single
        # public signal.
        if consent_type == 'public_exposure':
            try:
                from .services import UserService
                from .federation import federation
                for _agent in UserService.get_owned_agents(db, user_id):
                    federation.sync_agent_to_parent(db, _agent)
            except Exception:
                pass

        return consent

    @staticmethod
    def _label_on_file(db, user_id: str, consent_type: str, scope: str):
        """The label the most recent row for (user, type, scope) carries, so
        a grant, and a re-allow after a revoke, keep the name the ask was
        filed under; None when no row has one."""
        row = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.label.isnot(None),
        ).order_by(UserConsent.created_at.desc()).first()
        return row.label if row is not None else None

    @staticmethod
    def auto_grant_with_notice(db, user_id: str, consent_type: str,
                               scope: str = '*', agent_id=None,
                               reason: str = '') -> bool:
        """Privacy-aware auto-grant: NEVER blocks the request, but informs
        the user the FIRST time the cloud (or any consent-gated path) is
        used and gives them a one-tap revoke action.

        Pattern: "transparency + easy revoke" — privacy-first should not
        cause functional failures.  If the user has actively REVOKED
        this consent, this method returns False and the caller refuses.
        Otherwise (no record OR previously granted) it ensures a granted
        record exists, emits a one-time `consent.auto_granted` notice,
        and returns True so the caller proceeds immediately.

        Use this for cloud egress that's safe-by-default (vision
        fallback, social cross-device sync, fleet API tool calls, etc.)
        where blocking the request would degrade UX more than the
        privacy gain justifies.  Use ``check_consent`` + ``request_consent``
        directly when the gated action is high-stakes (payment_setup,
        public_exposure of private content) and you genuinely want to
        block until the user explicitly grants.

        Returns:
            True  — caller may proceed (consent is now granted, possibly
                    silently auto-granted with notice).
            False — caller MUST refuse: user has explicitly revoked this
                    consent and re-granting requires a fresh user action.
        """
        _validate_consent_type(consent_type)

        # 1. Active grant exists? Proceed silently.
        if ConsentService.check_consent(db, user_id, consent_type,
                                        scope=scope, agent_id=agent_id):
            return True

        # 2. Was there a PRIOR explicit revoke?  Honor it — refuse.
        prior = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.revoked_at.isnot(None),
        ).order_by(UserConsent.revoked_at.desc()).first()
        if prior is not None and prior.revoked_at is not None:
            _emit('consent.refused_after_revoke', _named(db, {
                'user_id': user_id,
                'consent_type': consent_type,
                'scope': scope,
                'agent_id': agent_id,
                'reason': reason or (
                    f"Previously revoked {consent_type}/{scope}; "
                    f"re-grant requires a fresh user action."
                ),
            }, agent_id))
            return False

        # 3. No record at all → auto-grant + emit one-time notice.
        ConsentService.grant_consent(db, user_id, consent_type,
                                     scope=scope, agent_id=agent_id)
        _emit('consent.auto_granted', _named(db, {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
            'reason': reason or (
                f"Auto-granted {consent_type}/{scope} so your request "
                f"could be served.  Tap to review or revoke in settings."
            ),
            'revoke_action': 'consent.revoke',
        }, agent_id))
        return True

    @staticmethod
    def revoke_consent(db, user_id: str, consent_type: str,
                       scope: str = '*', agent_id=None):
        """Revoke consent: end every active grant for the combination.

        grant_consent is append-only, so two grants are two rows and both
        must end.  This used to take ``.first()`` of all rows, which after
        an ask is the pending ask row, so the grant stayed and check_consent
        kept passing (tests/unit/test_consent_revoke_is_honoured.py).  When
        nothing was granted, the first row is marked as before, which
        records a declined ask and stops request_consent re-asking.

        Returns the newest row changed, or None when there is no row.
        """
        _validate_consent_type(consent_type)

        rows = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        ).order_by(UserConsent.granted_at.desc()).all()
        if not rows:
            first = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == scope,
                UserConsent.agent_id == agent_id,
            ).first()
            rows = [first] if first else []
        if not rows:
            return None

        now = datetime.utcnow()
        for row in rows:
            row.granted = False
            row.revoked_at = now
        db.flush()

        ConsentService.announce_revocation(user_id, consent_type, scope, agent_id)
        return rows[0]

    @staticmethod
    def announce_revocation(user_id: str, consent_type: str,
                            scope: str = '*', agent_id=None):
        """Fire the immutable-audit entry + ``consent.revoked`` broadcast for a
        revocation. PUBLIC on purpose: a surface that revokes by its own row
        model (the append-only ``consent_api`` UI path cannot delegate the WRITE
        without corrupting its audit trail) still gets IDENTICAL observability by
        calling this, instead of reaching into this module's underscore-private
        ``_audit``/``_emit``. That keeps the grant/revoke side-effect parity
        STRUCTURAL — a caller cannot get the row and silently skip the audit +
        broadcast. Best-effort (never raises)."""
        _audit('consent', actor_id=user_id,
               action=f'consent.revoked:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        _emit('consent.revoked', {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
        })
        _copilot_switch_from_consent(consent_type, False)
        _embodied_feed_from_consent(consent_type, False)

    @staticmethod
    def active_grant(db, user_id: str, consent_type: str,
                     scope: str = '*', agent_id=None):
        """The granted, unrevoked row for EXACTLY this combination, or None.

        check_consent's first step, and the whole lookup for a caller that
        must act on the row itself: the device gate (auth.verify_device_jwt)
        verifies a phone's signature against the key in the GRANTED row's
        scope, so it reads that row here and never widens to a wildcard or
        a blanket grant.
        """
        _validate_consent_type(consent_type)
        return db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        ).first()

    @staticmethod
    def check_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None) -> bool:
        """Check if user has active consent.

        Active means granted and not revoked.  The privacy page
        (consent_api.revoke_consent) revokes by setting revoked_at and
        leaves granted=True, so ``granted`` alone kept a revoked consent
        passing (tests/unit/test_consent_revoke_is_honoured.py).

        Lookup order:
          1. Exact match (user_id + agent_id + consent_type + scope)
          2. Wildcard scope (scope='*') for same agent
          3. Blanket consent (agent_id=None, scope='*')
        """
        _validate_consent_type(consent_type)

        # 1. Exact match
        if ConsentService.active_grant(db, user_id, consent_type,
                                       scope=scope, agent_id=agent_id):
            return True

        # 2. Wildcard scope for specific agent
        if scope != '*' and agent_id is not None:
            wildcard = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == '*',
                UserConsent.agent_id == agent_id,
                UserConsent.granted == True,
                UserConsent.revoked_at.is_(None),
            ).first()
            if wildcard:
                return True

        # 3. Blanket consent (agent_id=None, scope='*')
        if agent_id is not None:
            blanket = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == consent_type,
                UserConsent.scope == '*',
                UserConsent.agent_id == None,
                UserConsent.granted == True,
                UserConsent.revoked_at.is_(None),
            ).first()
            if blanket:
                return True

        # Debug, not warning: a polling gate looks every few seconds while it
        # waits for an answer (screen capture every 10s, computer control
        # every 3s), and each denied look wrote a WARNING.  The gates log
        # their own refusal once.
        import logging as _log
        _log.getLogger('hevolve.consent').debug(
            "Consent check denied: user=%s type=%s scope=%s agent=%s",
            user_id, consent_type, scope, agent_id)
        return False

    # Alias for readability
    has_consent = check_consent

    @staticmethod
    def set_payment_id(db, user_id: str, payment_id: str):
        """Store user's UPI/payment ID for revenue payouts.

        Uses consent_type='payment_setup', scope=payment_id.
        Triggers consent.granted event so frontend shows confirmation.
        """
        return ConsentService.grant_consent(
            db, user_id, 'payment_setup', scope=payment_id)

    @staticmethod
    def get_payment_id(db, user_id: str) -> str:
        """Get user's most-recently granted UPI/payment ID, or empty string.

        APPEND-ONLY ``grant_consent`` means a user who saves a NEW
        payment_id leaves the prior row in place (the prior scope
        becomes audit history).  Order by ``granted_at desc`` so the
        latest UPI wins; ``revoked_at IS NULL`` so a revoked entry
        does not shadow a later valid one.
        """
        record = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == 'payment_setup',
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        ).order_by(UserConsent.granted_at.desc()).first()
        return record.scope if record else ''

    @staticmethod
    def list_consents(db, user_id: str, consent_type: str = None,
                      agent_id=None):
        """List consent records for a user, optionally filtered."""
        q = db.query(UserConsent).filter(UserConsent.user_id == user_id)
        if consent_type is not None:
            _validate_consent_type(consent_type)
            q = q.filter(UserConsent.consent_type == consent_type)
        if agent_id is not None:
            q = q.filter(UserConsent.agent_id == agent_id)
        return q.order_by(UserConsent.created_at.desc()).all()


# ──────────────────────────────────────────────────────────────────────
# Legacy ``register_consent_routes`` was REMOVED in the consent-surface
# consolidation (orchestrator review acd11f55, 2026-04-25).  The HTTP
# write surface for consent now lives at ``integrations.social.consent_api``
# (JWT-authed, append-only, mounted at ``/api/social/consent``).
# Internal services that need direct DB access continue to use the
# ``ConsentService`` static methods above — those are unchanged in
# signature, with grant_consent flipped to APPEND-ONLY.
# ──────────────────────────────────────────────────────────────────────
