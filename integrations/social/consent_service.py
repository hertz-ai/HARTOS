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
import os
import re
import uuid
from datetime import datetime

from .models import UserConsent, after_commit

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
    'agent_contact',     # An agent the user does NOT own asks to message
                         # them (Nunba /agents/contact, the Instagram-DM-
                         # request shape).  Keyed per agent_id: the user is
                         # allowing THAT agent to reach them, not all agents,
                         # so a grant here is narrow by design.  Before this
                         # the answer lived only in a module-level dict, so an
                         # accept was forgotten on restart, the same agent was
                         # re-asked forever, and a deny was equally
                         # unrecorded — nothing to revoke and nothing on the
                         # privacy page.
    'screen_capture',    # The desktop's own screen is captured and described
                         # for the visual agent (VisionService screen channel,
                         # #701).  The capture loop's first denied tick files
                         # the pending ask; granting in the UserConsent UI
                         # starts capture on the next tick.  Never asked on
                         # central when it has no screen (central_lacks).
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
    'credential',        # An agent needs a password, key or token only the
                         # owner can give (Request_Resource).  The card takes
                         # the value in a password field; Accept stores it in
                         # the device vault and grants this row.  The scope
                         # names the ONE credential ('secret:SITE_PASSWORD'),
                         # and the agent only ever gets its alias
                         # {{secret:SITE_PASSWORD}} (hartos.ai_key_vault).
    'chat_join',         # Another account joins this account's chat with an
                         # agent (a parent or therapist joining a child's,
                         # owner 2026-11-06).  Granted when this account's
                         # side approves the join card; agent_id is the
                         # agent, scope '<joiner's user id>#<approval time>'
                         # (one row per approval, so a join can be allowed
                         # again after a revoke).  Revoking it ends the join
                         # (core.persona_registry.joined_chats).
    'mailbox_outreach',  # Agents find people to reach and send outreach
                         # email from this person's own mailbox (owner
                         # 2026-11-19: a connected email account starts the
                         # marketing loop).  scope = the mailbox address.
                         # Granted by the person's own connect of the
                         # mailbox (owner: "as soon as the mailbox
                         # connect"; outreach_crm_tools.mailbox_connected),
                         # which starts its outreach; a revoke pauses every
                         # goal that sends from it and the send gate refuses
                         # (_mailbox_goals_from_consent).  Never auto-granted
                         # by any other path (ASK_ONLY_CONSENT_TYPES).
})

#: Consent types only the person's own act grants: auto_grant_with_notice
#: refuses them.  mailbox_outreach sends mail from the person's own account
#: (hartos-43's review of the D8 design, 2026-11-19); the person's act that
#: grants it is connecting that mailbox.
ASK_ONLY_CONSENT_TYPES = frozenset({'mailbox_outreach'})

#: A credential ask's scope is this prefix + the credential's name, the same
#: name its {{secret:NAME}} alias carries.
CREDENTIAL_SCOPE_PREFIX = 'secret:'

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
    # The copilot switch on a settings page (claude_code_backend
    # .switch_copilot): the owner's on/off is their copilot_access answer.
    'copilot': 'copilot_access',
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


#: The part of the computer a consent type uses, as a person would name it,
#: and the boot hardware check's reader for it (security.system_requirements).
#: Central has no screen and asked its owner for screen capture every 10
#: seconds from 2026-10-19 to 10-21.  camera_capture is not here: its frames
#: also arrive over the vision socket from a client's browser, and the camera
#: probe sees only Linux devices.
_OWN_HARDWARE = {'screen_capture': ('screen', 'has_display')}
_said_missing = set()


def central_lacks(consent_type) -> str:
    """The part ``consent_type`` uses ('screen') when this node is central
    and its boot hardware check found that part missing; '' otherwise.

    Central only (owner, 2026-10-21): central is in no one's local hive.  Any
    other node asks as it always did, with or without the part: in a
    person's hive a node without a screen or a GPU still reaches what the
    other nodes have.  On central only a definite no counts; before the
    check has run, or when it could not tell, the ask goes out."""
    entry = _OWN_HARDWARE.get(consent_type)
    if entry is None:
        return ''
    # The topology env var, not key_delegation.get_node_tier: that one
    # loads the master key, and on central it has already set this var.
    if os.environ.get('HEVOLVE_NODE_TIER', 'flat').strip().lower() != 'central':
        return ''
    part, reader = entry
    try:
        from security import system_requirements
        found = getattr(system_requirements, reader)()
    except Exception:
        _logger.debug("hardware check for %s unavailable", consent_type,
                      exc_info=True)
        return ''
    return part if found is False else ''

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


def consent_prompt_push(data: dict, request_id) -> dict:
    """The Firebase message that pops the phone's consent overlay for an ask,
    in the shape its handler reads (MyFirebaseMessagingService
    .handleConsentPrompt: type 'consent_prompt', request_id, user_id,
    topic_reply, title, body).  The one builder: an ask filed here (_emit)
    and DeviceRoutingService's push for an action no consent type governs.

    ``request_id`` is what a yes or no answers: the consent row's id for an
    ask on file.  ``topic_reply`` is where the overlay publishes that answer
    (the overlay's own default, carried as the steward's spec has it).  The
    text leaves this node for Google (or central's relay), so it passes the
    one egress scrub (security.edge_privacy.scrub_text: secrets, then PII);
    if the scrub cannot run, the reason is left out.

    The data also names the node that asked (fleet_command.self_node_keys:
    node_id, node_tier, node_name), so the phone answers that node and no
    other.  Never the scope: a credential's scope spells the mailbox it is
    for, and the phone reads it from the node that asked.

    A credential ask's text says who asks, on which computer, and then its
    reason: "<who> asks for a password or key on <node_name>: <reason>".
    The reason is written for the card on the node that asked and starts
    with the secret's label, so the person can tell which password.  It
    passes the same egress scrub as every body, which masks values (and
    PII such as an address) and lets a name through, as before b168e3bc1.
    A TEMPORARY restore (owner 2026-11-22: "functionalities shd work,
    forget about security for now ... all should work the way its supposed
    to work during demo"): the phone apps in the field build the card from
    this push alone, until the build that reads the ask from the node that
    asked (R2, GET /api/social/consent?request_id=) ships.
    """
    from integrations.social.fleet_command import self_node_keys
    node = self_node_keys()
    user_id = str(data.get('user_id', ''))
    agent = str(data.get('agent_name') or '').strip()
    who = (agent_as_asker(agent)
           or str(data.get('requester_name') or 'An agent'))
    what = str(data.get('consent_type') or 'your permission').replace('_', ' ')
    title = f"{who} needs your OK"
    if str(data.get('consent_type') or '') == 'credential':
        where = str(node.get('node_name') or '')
        reason = str(data.get('reason') or '').strip()
        body = (f"{who} asks for a password or key"
                + (f" on {where}" if where else "")
                + (f": {reason}" if reason else "."))
    else:
        body = str(data.get('reason') or '').strip() or f"{who} asks for {what}."
    try:
        from security.edge_privacy import scrub_text
        title, body = scrub_text(title), scrub_text(body)
    except Exception:
        _logger.warning("consent ask push: egress scrub unavailable; the reason "
                        "stays on this node", exc_info=True)
        body = f"{who} asks for {what}."
    title, body = title[:80], body[:240]
    push = {'title': title, 'body': body,
            'data': {'type': 'consent_prompt',
                     'request_id': str(request_id),
                     'user_id': user_id,
                     'topic_reply': f'com.hertzai.pupit.{user_id}',
                     'consent_type': str(data.get('consent_type') or ''),
                     'title': title, 'body': body,
                     **node}}
    # A channel's one ask for several values (agent_tools.setup_fields)
    # names them, so the card shows one input per value: a JSON string, as
    # every value of a push's data is a string.  Names, labels and whether
    # each is secret only; never a value.  A single-value ask carries none.
    fields = data.get('fields')
    if isinstance(fields, list) and len(fields) >= 2:
        import json
        push['data']['fields'] = json.dumps([
            {'name': str(f.get('name')), 'label': str(f.get('label')),
             'secret': bool(f.get('secret'))}
            for f in fields if isinstance(f, dict) and f.get('name')])
    return push


def ask_msg_id(row_id) -> str:
    """The stable msg_id of a consent row's ask ('consent.request:<row
    id>'): every send of it (request_consent) and the drop's forget of its
    push (_drop_ask) spell it here, and _emit takes the row id back out of
    it for the phone's push."""
    return f'consent.request:{row_id}'


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
            # agent_asker is that name as the card says it ("Agent <name>",
            # agent_as_asker), so no surface words it a second way.
            for name_key in ('agent_name', 'agent_asker', 'requester_name',
                             'requester_fingerprint', 'requester_switch_from',
                             'requester_reconfirm'):
                if data.get(name_key):
                    note[name_key] = data[name_key]
            if msg_id:
                note['msg_id'] = msg_id
            # Which node this happened on (fleet_command.self_node_keys), on
            # every consent note: an ask is answered on the node that filed
            # it, and a yes or no on one node is a fact about that node only.
            from integrations.social.fleet_command import self_node_keys
            note.update(self_node_keys())
            # An ask also rings the person's phone (Firebase), once per ask:
            # the live legs reach only an app that is open on this router.
            # Only an ask with a stable id (the row's): the push goes out once
            # per id, and an ask without one could not be told from its own
            # re-sends.
            push = (consent_prompt_push(data, msg_id.split(':', 1)[-1])
                    if topic == 'consent.request' and msg_id else None)
            if push:
                # The ask's own id (its row's), as its push carries it: a
                # client answers by it without taking msg_id apart.
                note['request_id'] = push['data']['request_id']
                # A channel's one card for several values: the desktop card
                # is drawn from this note, so it names the same fields, the
                # same JSON string, as the phone's push.
                if 'fields' in push['data']:
                    note['fields'] = push['data']['fields']
                on_notification(user_id, note, push=push)
            else:
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


def standing_revocation():
    """SQL filter: a row whose no still stands -- revoked, and not reopened
    since (ConsentService.reopen keeps revoked_at and records reopened_at).
    The one statement of that rule: this module's readers, the phone's
    answer route (consent_api._open_ask, through open_ask) and the mailbox
    send gate (outreach_crm_tools._mailbox_permission_withdrawn) all read
    it, so a no taken back is undecided again everywhere at once."""
    from sqlalchemy import or_
    return (UserConsent.revoked_at.isnot(None)
            & or_(UserConsent.reopened_at.is_(None),
                  UserConsent.reopened_at < UserConsent.revoked_at))


def _answers_to(ask):
    """``(answer, terms)``: an alias of the consent table and the SQL terms
    under which an ``answer`` row is a yes to ``ask``.  ``ask`` is the
    mapped class (a filter over many asks, correlated) or one ask row.

    A yes is a granted row (granted_at set, revoked since or not: it
    answered all the same) for the same person and consent type, with the
    ask's scope or '*', for the ask's agent or for every agent (None): the
    card's Accept (consent_api.grant_consent) grants the ask's type and
    scope with no agent, so it answers every agent's ask for that scope; a
    credential ask filed under an agent's id (core.agent_tools
    request_resource, Request_Resource) is answered by it too.  check_consent's
    wildcard and allow-all steps are the '*' rows."""
    from sqlalchemy import and_, or_
    from sqlalchemy.orm import aliased
    answer = aliased(UserConsent)
    return answer, and_(
        answer.user_id == ask.user_id,
        answer.consent_type == ask.consent_type,
        or_(answer.scope == ask.scope, answer.scope == '*'),
        or_(answer.agent_id.is_(None), answer.agent_id == ask.agent_id),
        answer.granted_at.isnot(None))


def _sent_at(ask):
    """When ``ask`` was last sent: its row's updated_at.  The row is written
    when it is filed, and again only when it changes: a no, a no taken back
    (reopen) or a re-send of an ask an answer had settled (_resent).  A
    row with no updated_at (none is known to exist) counts from created_at."""
    from sqlalchemy import func
    if ask is UserConsent:
        return func.coalesce(UserConsent.updated_at, UserConsent.created_at)
    return ask.updated_at or ask.created_at


def answered_ask():
    """SQL filter: an ask row a yes has settled since it was last sent
    (_answers_to, _sent_at).  On a tie the answer wins: an ask is answered
    once, and a phone answering it twice would write the vault twice."""
    from sqlalchemy import exists
    answer, terms = _answers_to(UserConsent)
    return exists().where(terms & (answer.granted_at >= _sent_at(UserConsent)))


def open_ask():
    """SQL filter: an ask still waiting for its answer.  Never granted
    itself (a per-agent grant lands on its ask: grant_consent), no no
    standing on it (standing_revocation), and no yes since it was last sent
    (answered_ask).  An answered ask is closed: answering it again is
    refused (consent_api._open_ask) and a later grant does not drop its
    card again.  A re-sent one is open again (request_consent: a
    credential asked again after the site refused its value)."""
    return (UserConsent.granted_at.is_(None) & ~standing_revocation()
            & ~answered_ask())


def _resent(db, ask) -> None:
    """``ask`` is sent again: when a yes had settled it, it is open again,
    recorded on its row (updated_at, after that yes) so the card that goes
    out now can be answered.  An ask never answered since it was last sent
    is not written: a polling gate re-sends its ask every few seconds."""
    from datetime import timedelta
    from sqlalchemy import func
    if ask.granted_at is not None:
        return
    answer, terms = _answers_to(ask)
    with db.no_autoflush:
        last = db.query(func.max(answer.granted_at)).filter(terms).scalar()
    sent = _sent_at(ask)
    if last is None or (sent is not None and last < sent):
        return
    # Strictly after the yes it sets aside, even within one clock tick (the
    # tie goes to the answer: answered_ask).
    ask.updated_at = max(datetime.utcnow(), last + timedelta(microseconds=1))
    db.flush()


def _says_no(db, row) -> bool:
    """``row`` holds a no that still stands (standing_revocation)."""
    return db.query(UserConsent.id).filter(
        UserConsent.id == row.id, standing_revocation()).first() is not None


def _still_open(db, row) -> bool:
    """``row`` is an ask still waiting for its answer (open_ask)."""
    return db.query(UserConsent.id).filter(
        UserConsent.id == row.id, open_ask()).first() is not None


def _device_links_from_consent(scope: str, reason: str, keep_user=None) -> None:
    """A phone's open PeerLink link was admitted once, at its HELLO, as the
    account it was allowed as.  When the owner revokes the phone, or allows
    it as another account, that link is closed and the phone is told why
    (the 'bye' names ``reason``): it redials into the gate's answer, and no
    link goes on acting as an account that is no longer the phone's
    (rv_2c18).  ``keep_user``: the account now allowed, whose link stays.
    Runs after the commit; never raises: the consent itself stands."""
    peer_id = device_fingerprint(scope)
    if peer_id is None:
        return
    try:
        from core.peer_link.link_manager import get_link_manager
        if get_link_manager().close_device_link(peer_id, reason, keep_user=keep_user):
            _logger.info("device %s: its open link was closed (%s)", peer_id, reason)
    except Exception:
        _logger.warning("device %s: its open link could not be closed after "
                        "%s", peer_id, reason, exc_info=True)


#: What an installed Hevolve_Database too old for this HARTOS is told as
#: (rv_2c18): its UserConsent has no paired_user_id (#306, Hevolve_Database
#: 08a2e22), which every consent row this module writes carries.
CONSENT_SCHEMA_STALE = ("the installed Hevolve_Database has no "
                        "user_consents.paired_user_id; consent is off until "
                        "it is updated")


class ConsentSchemaStale(RuntimeError):
    """A consent call on a model without the columns this module reads."""


def consent_schema_refusal(model=None):
    """CONSENT_SCHEMA_STALE when ``model`` (default: the UserConsent this
    process runs on, integrations.social.models' choice of Hevolve_Database
    or the fallback) lacks paired_user_id, else None.  The boot schema
    check (migrations.run_migrations) logs it, and every consent call
    refuses with it (_validate_consent_type), where each used to fail with
    a TypeError naming a keyword argument."""
    model = UserConsent if model is None else model
    return None if hasattr(model, 'paired_user_id') else CONSENT_SCHEMA_STALE


def _validate_consent_type(consent_type: str):
    stale = consent_schema_refusal()
    if stale:
        raise ConsentSchemaStale(stale)
    if consent_type not in CONSENT_TYPES:
        raise ValueError(
            f"Invalid consent_type '{consent_type}'. "
            f"Must be one of: {', '.join(sorted(CONSENT_TYPES))}")


_AGENT_ID_RE = re.compile(r'[A-Za-z0-9_-]+')

def known_agent_id(agent_id):
    """The asking agent's id for a consent row, or None when no agent is
    known.

    Callers pass the prompt id they hold: None or '' when there is none, and
    hart_intelligence_entry._handle_computer_action_tool sends
    str(prompt_id or 0), so '0' too.  An unknown agent is never guessed.
    The one normaliser every ask uses (computer control, capability asks,
    credential asks), so they file under the same agent_id.
    """
    text = '' if agent_id is None else str(agent_id).strip()
    return None if text in ('', '0', 'None') else text


def _is_a_name(name, aid):
    """False for the placeholders the agent mirror manufactures when a
    prompt has no name ("Agent <id>", "agent-<id>", hart_intelligence_entry
    _create_social_agent_from_prompt :10993/:11005) and for the bare id:
    those are the identifier the owner cannot read, dressed as a name.  The
    same placeholder built from ANOTHER id is no name either: measured on
    central 2026-11-10, agent 93731340723's mirror carried "Agent
    69922589936" and the owner's card read "Agent 69922589936 asks you".

    The rule itself is core.agent_personality.is_a_name, the one name rule
    the agent's own spoken name reads too (review of 4b9ba08ad)."""
    from core.agent_personality import is_a_name
    return is_a_name(name, aid)


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


def agent_as_asker(name: str) -> str:
    """How a card names an AGENT that asks: 'Agent <name>'.  An agent may
    carry a person's name ("Vijai"), and "Vijai asks you" read as another
    person asking (owner 2026-11-11: "why does the question appear like
    another user asking question instead of qualifying as agent").  The
    wording for the ask card (person_ask), the consent push and the
    computer-control reason (vlm.safety); a PERSON who asks (a device ask's
    requester_name, a join request) keeps their own name.  A name that
    already says it ("Agent Smith") is not prefixed twice.

    The name is read without the characters nobody sees: a byte-order mark
    in front of "Agent X" hid the word from the check below and the card
    said "Agent Agent X" (review rv7095).  What is invisible is the one rule
    the text helpers have (igkit.humanize: a format character, except a
    joiner at work inside an Indic word or an emoji), so a name written
    with a joiner keeps it.  No name, or one of nothing but spaces and such
    characters, is '': there is no agent to name, and each caller says so in
    its own words ("Agent " was sent before)."""
    from integrations.reel_studio.igkit.humanize import _invisible
    name = str(name or '')
    name = ''.join(ch for i, ch in enumerate(name)
                   if not _invisible(name, i)).strip()
    if not name:
        return ''
    if re.match(r'agent\b', name, re.IGNORECASE):
        return name
    return f'Agent {name}'


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


def tell_a_no_was_put_back(user_id: str, consent_type: str, reason: str) -> None:
    """Tell ``user_id`` that their no for ``consent_type`` was found undone
    and is in force again (the copilot's off marker went missing:
    claude_code_backend.reconcile_copilot_switch).  The notice consent
    already sends when a revoke is upheld against something that would have
    gone on, consent.refused_after_revoke, with the reason as given."""
    _emit('consent.refused_after_revoke', {
        'user_id': user_id,
        'consent_type': consent_type,
        'scope': '*',
        'agent_id': None,
        'reason': reason,
    })


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

    Here, on the caller's thread: the flag, the capture gate
    (admin.api.apply_embodied_answer -> core.ai_sensing) and the save, so a
    No stops capture and is on disk the moment it is given.  Only the
    VisionService start or stop runs off this thread, on the feed's own
    worker (admin.api._FEED_WORKER): the caller is an HTTP request (an Allow
    on a card, the privacy page, the admin toggle), and a feed start that
    never returns -- measured live 2026-09-25 -- held that request open.
    """
    feed = _CONSENT_FEED.get(consent_type)
    if feed is None:
        return
    try:
        from integrations.channels.admin.api import get_api, apply_embodied_answer
        api = get_api()
        cfg = api._global_config.embodied_ai
        setattr(cfg, 'camera_enabled' if feed == 'camera' else 'screen_capture_enabled',
                bool(granted))
        apply_embodied_answer(feed, bool(granted), cfg)
        api._save_config()
        _logger.info("consent %s -> embodied feed %s=%s",
                     consent_type, feed, bool(granted))
    except Exception as e:
        _logger.warning("embodied feed %s from consent %s failed: %s",
                        feed, consent_type, e)


def _ask_rows(db, user_id: str, consent_type: str, scope=None,
              agent_id=..., still_open=True):
    """The asks of ``consent_type``: rows never granted (an ask row; a grant
    is a row of its own), only those still waiting for an answer when
    ``still_open`` (open_ask: no no standing on them, no yes since they
    were last sent).  Narrowed to one ``scope`` and ``agent_id`` when
    given."""
    query = db.query(UserConsent).filter(
        UserConsent.user_id == user_id,
        UserConsent.consent_type == consent_type,
        UserConsent.granted_at.is_(None),
    )
    if still_open:
        query = query.filter(open_ask())
    if scope is not None:
        query = query.filter(UserConsent.scope == scope)
    if agent_id is not ...:
        query = query.filter(UserConsent.agent_id == agent_id)
    return query.all()


def _drop_ask(user_id: str, ask_id: str) -> None:
    """The ask ``ask_id`` (a consent row) was answered: every surface drops
    its card, the phone included (person_ask.drop_everywhere, the one drop
    fan-out), and the ask may ring the phone again if it is asked again
    under the same row (a credential re-asked after its value was refused)."""
    try:
        from integrations.agent_engine.person_ask import (
            answered_msg_id, drop_everywhere)
        from integrations.social.realtime import forget_push
        drop_everywhere(ask_id, user_id)
        forget_push(ask_msg_id(ask_id))
        forget_push(answered_msg_id(ask_id))
    except Exception:
        _logger.warning("consent ask %s: the drop fan-out failed, so other "
                        "surfaces keep its card", ask_id, exc_info=True)


def _drop_asks(db, user_id: str, ask_ids) -> None:
    """Drop each answered ask after the answer commits (models.after_commit),
    once each."""
    for ask_id in dict.fromkeys(ask_ids):
        after_commit(db, lambda ask_id=ask_id: _drop_ask(user_id, ask_id))


def _answered_by_grant(db, user_id: str, consent_type: str, waiting) -> list:
    """The ids of the asks in ``waiting`` (still open before a grant of
    ``consent_type`` was written) that are open no more: the asks that
    grant answers (open_ask, by the rows _answers_to names), each dropped
    once (_announce_grant).  An ask answered by an earlier grant was not
    open before this one, so it is not dropped again.

    By the consent store's own answer rule, not check_consent: the card's
    Accept and the phone's answer grant the ask's scope with no agent, and
    check_consent's allow-all step covers scope '*' only, so a credential
    ask an agent filed under its own id (core.agent_tools
    request_resource, Request_Resource) was answered and never dropped
    (review of 41357357e, MUST 1)."""
    still = {row.id for row in _ask_rows(db, user_id, consent_type)}
    return [row.id for row in waiting if row.id not in still]


def _announce_grant(db, user_id: str, consent_type: str, scope: str,
                    agent_id, answered=()) -> None:
    """Queue a grant's effects for after its commit (models.after_commit).

    Run between the flush and the caller's commit they held SQLite's one
    write lock for as long as they took; measured live 2026-09-25, a
    screen-capture Allow whose feed start never returned left the database
    unwritable for about 41 minutes and the grant itself never committed.

    Order matters: every surface hears consent.granted, then the copilot
    switch is set, then the camera/screen feed starts.  The feed start is
    the effect that hung live, so it goes last and cannot hold the others
    back.  One place for both grant branches, so they cannot drift.  Every
    ask the grant answers (``answered``: grant_consent reads them,
    _answered_by_grant) drops from every surface, the phone included."""
    data = {
        'user_id': user_id,
        'consent_type': consent_type,
        'scope': scope,
        'agent_id': agent_id,
    }
    after_commit(db, lambda: _emit('consent.granted', data))
    _drop_asks(db, user_id, answered)
    after_commit(db, lambda: _copilot_switch_from_consent(consent_type, True))
    after_commit(db, lambda: _embodied_feed_from_consent(consent_type, True))
    after_commit(db, lambda: _mailbox_goals_from_consent(
        user_id, consent_type, scope, True))


def _mailbox_goals_from_consent(user_id, consent_type: str, scope: str,
                                granted: bool) -> None:
    """A mailbox_outreach yes starts the mailbox's outreach; a revoke stops
    it (outreach_crm_tools.mailbox_consent_changed).  Nothing for any other
    type.  Runs after the consent's commit: it writes goals, and run inside
    the consent's transaction it would hold SQLite's one write lock (see
    _announce_grant).  Never raises: the consent itself already stands."""
    if consent_type != 'mailbox_outreach':
        return
    try:
        from integrations.agent_engine.outreach_crm_tools import (
            mailbox_consent_changed)
        mailbox_consent_changed(user_id, scope, granted)
    except Exception:
        _logger.exception("mailbox_outreach %s for %s: the goals were not %s",
                          'grant' if granted else 'revoke', user_id,
                          'started' if granted else 'paused')


def _vault_value_from_consent(consent_type: str, scope: str) -> None:
    """A credential's revoke takes the values the owner's answers stored
    for it back out of the vault once no grant names it any more
    (AIKeyVault.take_back_answer, revoked): its alias stopped resolving at
    the revoke, but get_tool_key and SecretsManager.get_secret still
    returned it.  The value the vault held before those answers comes
    back.  Nothing for any other type.  Runs after the revoke's commit, so
    the grants read are the ones left.  Never raises: the revoke itself
    already stands."""
    if consent_type != 'credential' or not str(scope or '').startswith(
            CREDENTIAL_SCOPE_PREFIX):
        return
    try:
        from hartos.ai_key_vault import get_ai_key_vault
        vault = get_ai_key_vault()
        vault.take_back_answer(
            vault.credential_name(scope[len(CREDENTIAL_SCOPE_PREFIX):]),
            revoked=True)
    except Exception:
        _logger.exception("credential revoke: the value stored for %s was not "
                          "taken out of the vault", scope)


def _named(db, data: dict, agent_id) -> dict:
    """``data`` with 'agent_name' added when the agent has one: the ask and
    the notices then share one shape, and an unnamed agent carries no key.
    'agent_asker' rides beside it: the same name as a card says it
    (agent_as_asker), so the page shows this text and keeps no rule of its
    own for it."""
    name = agent_display_name(db, agent_id)
    if name:
        data['agent_name'] = name
        asker = agent_as_asker(name)
        if asker:
            data['agent_asker'] = asker
    return data


class ConsentService:
    """Static-method service for managing user consent records."""

    @staticmethod
    def request_consent(db, user_id: str, consent_type: str,
                        scope: str = '*', agent_id=None, reason: str = '',
                        requester_name: str = '', reask: bool = False,
                        fields=None, paired_user_id=None, switch_from=None,
                        reconfirm=False):
        """Create a pending (not yet granted) consent record.

        Returns existing record if one already exists for this combination.
        ``reask`` sends the card again after a grant (a credential the site
        rejected has to be entered again), but never after a "no".
        ``reason`` rides on the ask, so the card can say what is asked for.
        ``fields``: a channel's one credential ask for several values
        (agent_tools.setup_fields), which its push carries
        (consent_prompt_push data.fields); nothing for any other ask.
        ``requester_name`` is the person asking when the asker is not an
        agent (a device ask: the phone's owner), shown like agent_name.  It
        is also written to the new row as its ``label``, once: the name the
        phone signed into its first ask stands, a later ask cannot rename
        the row (the label is a hint beside the fingerprint, never
        identity).
        ``paired_user_id`` is the account a phone's device ask is for (its
        token's user_id, auth.file_device_access_ask): each account the
        phone asks as has an ask row of its own, an ask filed before v60
        (no account) becomes the first one's, and a grant never takes one
        except from the ask the owner answered (grant_consent, #306).
        ``switch_from`` is the account the phone is allowed as when it asks
        for another (rv_2c18): the ask carries it for the card
        (requester_switch_from) and, while it waits, is never sent again.
        ``reconfirm``: the phone was allowed before v60 and its grant names
        no account; the card says so (requester_reconfirm).  Nothing for
        any other ask.

        An ask central cannot use (central_lacks: central with no screen)
        is not filed: nothing is written or sent, and None is returned.
        """
        _validate_consent_type(consent_type)
        part = central_lacks(consent_type)
        if part:
            if consent_type not in _said_missing:
                _said_missing.add(consent_type)
                _logger.info("consent %s not asked: central has no %s",
                             consent_type, part)
            return None

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
        if fields:
            ask['fields'] = list(fields)
        fingerprint = device_fingerprint(scope) if consent_type == 'device_access' else None
        if fingerprint:
            ask['requester_fingerprint'] = fingerprint
        # Who is asking, by name: the card says "<name> asks to ...".
        _named(db, ask, agent_id)

        if switch_from is not None:
            ask['requester_switch_from'] = str(switch_from)
        if reconfirm:
            ask['requester_reconfirm'] = True

        existing = ConsentService.ask_on_file(db, user_id, consent_type,
                                              scope=scope, agent_id=agent_id)
        if paired_user_id is not None:
            # A phone's device ask: one row per account it asks as, never a
            # grant (rv_2c18).  An ask filed before v60 names no account and
            # becomes the first one's.  It is sent while nothing answers it
            # for that account (the gate files only then), except that a
            # switch ask still waiting is never sent again: one card per
            # account, however often the phone redials.
            paired_user_id = str(paired_user_id)
            existing = (ConsentService.device_ask(db, user_id, scope, paired_user_id)
                        or ConsentService.device_ask(db, user_id, scope, None))
            if existing is not None:
                existing.paired_user_id = paired_user_id
                if _says_no(db, existing) or (switch_from is not None
                                          and _still_open(db, existing)):
                    return existing
                _resent(db, existing)
                _emit('consent.request', ask, msg_id=ask_msg_id(existing.id))
                return existing
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
                    standing_revocation()),
            ).first()
            if decided is None or (reask and not ConsentService.declined(
                    db, user_id, consent_type, scope, agent_id)):
                # Sent again under the same row: an answer that settled it
                # (a value the site refused since) no longer does, so the
                # card can be answered again (open_ask).
                _resent(db, existing)
                _emit('consent.request', ask,
                      msg_id=ask_msg_id(existing.id))
            return existing

        consent = UserConsent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            agent_id=agent_id,
            consent_type=consent_type,
            scope=scope,
            granted=False,
            label=requester_name[:100] if requester_name else None,
            paired_user_id=paired_user_id,
        )
        db.add(consent)
        db.flush()
        # Delivery, sibling-parity: grant_consent / auto_grant_with_notice
        # / announce_revocation all _emit, and _emit's own comment always
        # listed consent.request as a topic.  Stable msg_id (row id) so this
        # first ask and every re-ask above collapse to ONE card client-side.
        _emit('consent.request', ask, msg_id=ask_msg_id(consent.id))
        return consent

    @staticmethod
    def ask_on_file(db, user_id: str, consent_type: str, scope: str = '*',
                    agent_id=None):
        """The row for exactly this combination, in any state, or None:
        what request_consent returns instead of filing again.  A paced
        caller (a phone's device ask) asks this first, so a repeat costs it
        no budget.

        The ask itself (a row never granted) when there is one, before any
        grant beside it: a re-sent card must name a row it can be answered
        by (open_ask), and with agent_id None the grants stack beside the
        ask, in an order the database alone chose before (#306)."""
        _validate_consent_type(consent_type)
        return db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
        ).order_by(UserConsent.granted_at.isnot(None),
                   UserConsent.created_at).first()

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
            # A new grant when nothing is granted, and also when the ask's
            # own agent is not covered by what is: the owner said no to it
            # after "Allow ALL agents" (check_consent step 3), and this
            # Approve, being newer than that no, covers it again.
            if (ConsentService.active_grant(db, user_id, consent_type) is None
                    or (agent_id is not None
                        and not ConsentService.check_consent(
                            db, user_id, consent_type, agent_id=agent_id))):
                # grant_consent applies the feed and emits consent.granted,
                # both once the caller commits.
                ConsentService.grant_consent(db, user_id, consent_type)
            else:
                # Already allowed, so no second row (the UNIQUE constraint
                # rejects it) -- but the owner just pressed Approve, and the
                # feed may have been stopped from admin settings while the
                # grant stood, so re-assert it.  Exclusive with the branch
                # above, so the feed is never applied twice for one answer.
                # After the commit, as grant_consent's own feed start: the
                # admin toggle's 'all' answers camera then screen in one
                # session, so a camera grant flushed just before holds
                # SQLite's write lock, and a feed started here held it too.
                after_commit(db, lambda: _embodied_feed_from_consent(
                    consent_type, True))
                # The copilot likewise: a grant that stood while its off
                # marker was written (the settings switch used to write the
                # marker alone) is switched on by this Approve.
                after_commit(db, lambda: _copilot_switch_from_consent(
                    consent_type, True))
        elif ConsentService.revoke_consent(db, user_id, consent_type,
                                           agent_id=agent_id,
                                           keep_the_no=True) is None:
            # Nothing on file to revoke -- a client that answers without
            # having been asked here.  The answer still has to stop the feed
            # and still has to reach the other surfaces so their copy of the
            # card goes away, which is exactly what announce_revocation does.
            ConsentService.announce_revocation(user_id, consent_type,
                                               agent_id=agent_id)
        return consent_type

    @staticmethod
    def decline(db, user_id: str, consent_type: str, scope: str = '*',
                agent_id=None, ask=None):
        """Say no to an ask (the consent card's "Don't allow"; consent_api
        decline_consent).  revoke_consent on the ask's own combination, so a
        no to one agent leaves other agents' asks open.

        For a 'credential' ask the no also ends the saved value's grants
        (agent None, which the card's Accept writes): the owner saying no on
        the card that came back after a rejected login means "stop using
        what I entered", for every agent, whichever agent asked.  Before,
        an ask with no agent did that and agent 42's did not, so agent 42
        was still told the rejected value "is stored".  Returns the ask's
        row, or None when there is no ask.

        A phone (device_access) has one ask per account it asks as, and a
        no is a no to that account: the ask ``ask`` (the one the answer
        named), else every one waiting for the phone (the privacy page's
        Don't allow names only the phone), is declined, allowed or not, and
        any grant stays on file.  Measured on 2c18dc713, a no to the
        confirm card revoked every grant for the phone (rv_2c18); on
        f7f073cad, a no to one account of a phone not yet allowed refused
        its other account too, whose card stayed open (rv_66auth).  Taking
        the phone away is a revoke.

        Don't allow by scope alone of a phone no grant allows is also a no
        to the phone, a row naming no account, which device_refused reads:
        the phone writes its account into its own token, and on 4486f2dad
        it signed in as an account it had never asked as and got a fresh
        card (rv_66auth)."""
        phone = consent_type == 'device_access' and agent_id is None
        bound = (ConsentService.active_grant(db, user_id, consent_type, scope)
                 if phone else None)
        rows = [] if not phone else [ask] if ask is not None else _ask_rows(
            db, user_id, consent_type, scope, agent_id)
        if bound is not None or rows:
            now = datetime.utcnow()
            for row in rows:
                row.granted = False
                row.revoked_at = now
            if (ask is None and bound is None
                    and not ConsentService.device_refused(db, user_id, scope,
                                                          None)):
                db.add(UserConsent(
                    id=str(uuid.uuid4()), user_id=user_id, agent_id=None,
                    consent_type=consent_type, scope=scope, granted=False,
                    revoked_at=now, label=rows[0].label))
            db.flush()
            _drop_asks(db, user_id, [row.id for row in rows])
            return rows[0] if rows else None
        # The asks still open, read before the no is written: only they can
        # have a card on a screen or a phone.  A no for an agent that never
        # asked (allow-all covered it) writes that agent's row now, and a
        # second no finds that row already declined: neither has a card to
        # drop or a phone to tell (review of 0be16584a: the second no sent a
        # phone drop for a card never shown).
        asked = [ask.id for ask in _ask_rows(
            db, user_id, consent_type, scope, agent_id)]
        row = ConsentService.revoke_consent(db, user_id, consent_type,
                                            scope, agent_id)
        # The no answers this combination's ask: its card drops everywhere.
        _drop_asks(db, user_id, asked)
        if (consent_type == 'credential' and agent_id is not None
                and ConsentService.active_grant(db, user_id, consent_type,
                                                scope) is not None):
            ConsentService.revoke_consent(db, user_id, consent_type, scope)
        return row

    @staticmethod
    def reopen(db, user_id: str, consent_type: str, scope: str = '*'):
        """Take back a "no": every standing revocation for (user, type,
        scope), for any agent, is marked reopened now, so declined() is
        False and the next request_consent shows the card.  Returns how many
        rows.

        The way back for an ask with no on/off card to grant from (a
        credential: the privacy page's "Allow asking again").  It is not a
        yes and it rewrites no history: granted, granted_at and revoked_at
        (when the no was given) stay as they were; reopened_at records the
        reopen, and a later no (a newer revoked_at) stands again.  Every
        surface hears consent.reopened, after the commit, as it hears
        consent.granted and consent.revoked.
        """
        _validate_consent_type(consent_type)
        rows = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            standing_revocation(),
        ).all()
        if not rows:
            return 0
        now = datetime.utcnow()
        for row in rows:
            row.reopened_at = now
        db.flush()
        _audit('consent', actor_id=user_id,
               action=f'consent.reopened:{consent_type}',
               detail={'scope': scope, 'rows': len(rows)})
        data = {'user_id': user_id, 'consent_type': consent_type,
                'scope': scope, 'agent_id': None}
        after_commit(db, lambda: _emit('consent.reopened', data))
        return len(rows)

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
        agents") given after the no covers that agent again, so ask this only
        after check_consent failed (turned_off_for asks both).
        """
        _validate_consent_type(consent_type)
        return db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.scope == scope,
            UserConsent.agent_id == agent_id,
            standing_revocation(),
        ).first() is not None

    @staticmethod
    def grant_consent(db, user_id: str, consent_type: str,
                      scope: str = '*', agent_id=None, paired_user_id=None):
        """Grant consent — APPEND-ONLY.

        A phone's grant (device_access) admits it as one account: the one
        on the ask the owner answered (``paired_user_id``, which the
        answer route passes from the ask its request_id names), else
        paired_on_file's.  A grant for another account than the phone's
        open link acts as closes that link after the commit (rv_2c18).

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

        # PROMOTE A NEVER-ANSWERED ASK instead of inserting beside it.
        #
        # request_consent files a PENDING row on the same
        # (user_id, agent_id, consent_type, scope) key the UNIQUE constraint
        # covers. With a non-NULL agent_id those keys do NOT stack (SQL treats
        # only NULL as distinct), so a per-agent consent could be ASKED and then
        # never GRANTED: the insert below raised IntegrityError, and the
        # documented workaround — revoke first — cannot help, because revoking
        # updates the row rather than freeing the key. That is why
        # record_capability_decision grants BLANKET: it sidesteps this rather
        # than hitting it. A per-agent ask (agent_contact, and any future one)
        # needs the answer to land ON the ask.
        #
        # The append-only invariant is preserved exactly: promotion is allowed
        # ONLY while granted_at IS NULL, i.e. this row has never been granted,
        # so no granted_at is ever rewritten and no history is lost. A row that
        # was granted before (even if since revoked) still takes the insert path
        # and still raises, which is the documented re-grant semantics.
        # ONLY for a non-NULL agent_id, i.e. only where appending is impossible.
        # With agent_id NULL the keys are distinct (SQL), so the append-only
        # semantic still holds exactly as acd11f55 specified: the pending row
        # stays as audit history and a new granted row is inserted beside it.
        # Narrowing it this way is what keeps that invariant — and its test —
        # untouched while still making a per-agent ask answerable.
        # no_autoflush on principle, NOT on a measurement.  Adding a SELECT
        # inside a writer changes flush timing: SQLAlchemy autoflushes the
        # caller's pending state before answering a query, so without this the
        # lookup would flush whatever the caller had in flight at a point it
        # never used to.  That is a real hazard and the guard is cheap, so it
        # stays.
        #
        # Honesty about the evidence: I first wrote that this guard FIXED a
        # measured 19-suite regression.  It did not, and the claim was wrong —
        # the two runs I compared (253 passed/1 error vs 3 failed/247/7 errors)
        # turned out to be THE SAME CODE.  The consent sweep is not hermetic:
        # some suites pin HEVOLVE_DB_PATH=':memory:' and two do not,
        # models.py reads that env ONCE at import and caches DB_PATH, so
        # whichever test module imports first decides the database for the whole
        # process — and agent_data/hevolve_database.db (44MB, real file) gets
        # written by the runs that lose that race.  Cross-run comparisons in
        # this area are therefore not evidence of anything until the DB is
        # pinned and fresh.
        # The asks still open before this grant, read before it is written:
        # an ask it answers is closed once it is (open_ask), and the ones it
        # answers are dropped after the commit (_answered_by_grant).
        with db.no_autoflush:
            waiting = _ask_rows(db, user_id, consent_type)
        _pending = None
        if agent_id is not None:
            with db.no_autoflush:
                _pending = db.query(UserConsent).filter(
                    UserConsent.user_id == user_id,
                    UserConsent.agent_id == agent_id,
                    UserConsent.consent_type == consent_type,
                    UserConsent.scope == scope,
                    UserConsent.granted_at.is_(None),
                ).first()
        if _pending is not None:
            _pending.granted = True
            _pending.granted_at = now
            _pending.revoked_at = None
            db.flush()
            _audit('consent', actor_id=user_id,
                   action=f'consent.granted:{consent_type}',
                   detail={'scope': scope, 'agent_id': agent_id,
                           'promoted_pending_ask': True})
            _announce_grant(db, user_id, consent_type, scope, agent_id,
                            answered=[_pending.id] + _answered_by_grant(
                                db, user_id, consent_type, waiting))
            return _pending

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
            # ... and the account it asked as, the one it is admitted as (#306)
            paired_user_id=((str(paired_user_id) if paired_user_id is not None
                             else ConsentService.paired_on_file(db, user_id, scope))
                            if consent_type == 'device_access' else None),
        )
        db.add(consent)
        db.flush()

        _audit('consent', actor_id=user_id,
               action=f'consent.granted:{consent_type}',
               detail={'scope': scope, 'agent_id': agent_id})
        # The broadcast and the feed wait for the commit (after_commit): run
        # here they held SQLite's write lock for as long as they took.
        _announce_grant(db, user_id, consent_type, scope, agent_id,
                        answered=_answered_by_grant(db, user_id, consent_type,
                                                    waiting))
        if consent.paired_user_id is not None:
            bound = consent.paired_user_id
            after_commit(db, lambda: _device_links_from_consent(
                scope, 'account_switched', keep_user=bound))

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
            except Exception as e:
                # Best-effort (the grant stands), never silent: a user who made
                # their agents public otherwise saw them never appear.
                _logger.warning(
                    "public_exposure granted for %s but its agents were not "
                    "re-synced to central: %s", user_id, e, exc_info=True)

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
    def device_ask(db, user_id: str, scope: str, account):
        """The ask the phone with device scope ``scope`` filed with
        ``user_id`` as ``account`` (its paired_user_id; None: an ask filed
        before v60, which names none), newest first, or None.  A phone has
        one ask per account it asks as (request_consent, rv_2c18)."""
        return db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == 'device_access',
            UserConsent.scope == scope,
            UserConsent.agent_id.is_(None),
            UserConsent.granted_at.is_(None),
            UserConsent.paired_user_id == account if account is not None
            else UserConsent.paired_user_id.is_(None),
        ).order_by(UserConsent.created_at.desc()).first()

    @staticmethod
    def device_ask_declined(db, user_id: str, scope: str, account) -> bool:
        """The owner said no to the phone's ask as ``account``, and the no
        stands (a switch the owner did not allow, rv_2c18)."""
        row = ConsentService.device_ask(db, user_id, scope, account)
        return row is not None and _says_no(db, row)

    @staticmethod
    def device_refused(db, user_id: str, scope: str, account) -> bool:
        """A no stands for the phone with device scope ``scope``, allowed by
        no grant now, signed in as ``account``: the phone was taken away (a
        grant of its, revoked), the owner said no to it before its asks
        named an account (v60), or said no to its ask as ``account``.  A no
        to its ask as another account is that account's alone (rv_66auth:
        on f7f073cad it refused every account at once)."""
        from sqlalchemy import or_
        return db.query(UserConsent.id).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == 'device_access',
            UserConsent.scope == scope,
            UserConsent.agent_id.is_(None),
            standing_revocation(),
            or_(UserConsent.granted_at.isnot(None),
                UserConsent.paired_user_id.is_(None),
                UserConsent.paired_user_id == account),
        ).first() is not None

    @staticmethod
    def paired_on_file(db, user_id: str, scope: str):
        """The account a grant of the phone with device scope ``scope``
        admits it as when the answer names no ask (an Allow by scope alone:
        an older card, the privacy page): the account of the phone's ask
        still waiting; with none waiting, the account it was last allowed
        as, else the one its newest ask named; None when nothing names one
        (filed before v60).  A yes never takes an account no card of the
        owner's showed (#306).  Two asks waiting for two accounts leave it
        unknown which one the owner meant: ValueError, and the owner
        answers on the card, which names its ask (request_id).

        A phone taken away (a grant on file, none standing: the privacy
        page's Block) and allowed again by scope alone is allowed as the
        account its newest grant names, never as one an ask still waiting
        names: that account was approved on no card (rv_66auth, measured on
        f7f073cad: a family phone signed in once as the owner, then Block
        and Allow, was bound as the owner).  A switch stays its own card's
        to allow."""
        if ConsentService.active_grant(db, user_id, 'device_access',
                                       scope) is None:
            last = db.query(UserConsent).filter(
                UserConsent.user_id == user_id,
                UserConsent.consent_type == 'device_access',
                UserConsent.scope == scope,
                UserConsent.granted_at.isnot(None),
            ).order_by(UserConsent.granted_at.desc()).first()
            if last is not None:
                return last.paired_user_id
        named = UserConsent.paired_user_id.isnot(None)
        rows = db.query(UserConsent).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == 'device_access',
            UserConsent.scope == scope,
            named,
        )
        waiting = {row.paired_user_id for row in rows.filter(
            UserConsent.granted_at.is_(None), open_ask())}
        if len(waiting) > 1:
            raise ValueError("this phone asks to be allowed as more than one "
                             "account: answer the card for the one to allow")
        if waiting:
            return waiting.pop()
        row = (rows.filter(UserConsent.granted_at.isnot(None))
               .order_by(UserConsent.granted_at.desc()).first()
               or rows.order_by(UserConsent.created_at.desc()).first())
        return row.paired_user_id if row is not None else None

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
                    consent and re-granting requires a fresh user action,
                    or the type is one only an explicit yes grants
                    (ASK_ONLY_CONSENT_TYPES); ask with request_consent.
        """
        _validate_consent_type(consent_type)
        if consent_type in ASK_ONLY_CONSENT_TYPES:
            _logger.warning(
                "consent %s for user %s was not auto-granted: only the "
                "person's own yes grants it (ask with request_consent)",
                consent_type, user_id)
            return False

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
        # After the commit, like the grant's own consent.granted (and after
        # it): a notice of a grant that rolled back would offer to revoke a
        # consent that does not exist.  The name is looked up now, while the
        # session can still query.
        notice = _named(db, {
            'user_id': user_id,
            'consent_type': consent_type,
            'scope': scope,
            'agent_id': agent_id,
            'reason': reason or (
                f"Auto-granted {consent_type}/{scope} so your request "
                f"could be served.  Tap to review or revoke in settings."
            ),
            'revoke_action': 'consent.revoke',
        }, agent_id)
        after_commit(db, lambda: _emit('consent.auto_granted', notice))
        return True

    @staticmethod
    def revoke_consent(db, user_id: str, consent_type: str,
                       scope: str = '*', agent_id=None,
                       keep_the_no: bool = False):
        """Revoke consent: end every active grant for the combination.

        grant_consent is append-only, so two grants are two rows and both
        must end.  This used to take ``.first()`` of all rows, which after
        an ask is the pending ask row, so the grant stayed and check_consent
        kept passing (tests/unit/test_consent_revoke_is_honoured.py).  When
        nothing was granted, the first row is marked as before, which
        records a declined ask and stops request_consent re-asking.

        A no for ONE agent while "Allow ALL agents" stands (a blanket grant;
        that agent may never have asked, so it has no row) is recorded as
        that agent's own declined row: the grant stays for every other
        agent and check_consent refuses this one (owner 2026-11-21: a kept
        consent ends for an agent when the user turns it off for that
        agent).  The others keep the camera and the screen too: that no
        does not close the feed (announce_revocation) and is not read as
        the feed's last answer (feed_said_no).  Every surface hears it
        (consent.revoked), so a page showing that agent's permissions shows
        a no given in chat; no card was ever shown for such a row, so none
        is dropped (decline).  With no blanket grant and no row there is
        still nothing to mark.

        ``keep_the_no``: a no for every agent with NOTHING on file is written
        as a declined row of its own, where it used to be announced and
        forgotten.  The owner's Deny on a capability passes it
        (record_capability_decision): the copilot switched off from a
        settings page, and a camera or screen Deny given before any ask or
        grant, then outlive a restart, because the last answer on file is
        what the gates are restored from (feed_said_no).  Nobody is asked
        and no phone rings.  Without it, as before: None and no row.

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
            if first is None and (
                    (agent_id is not None and ConsentService.active_grant(
                        db, user_id, consent_type) is not None)
                    or (agent_id is None and keep_the_no)):
                first = UserConsent(
                    id=str(uuid.uuid4()), user_id=user_id, agent_id=agent_id,
                    consent_type=consent_type, scope=scope, granted=False)
                db.add(first)
            rows = [first] if first else []
        if not rows:
            return None

        now = datetime.utcnow()
        for row in rows:
            row.granted = False
            row.revoked_at = now
        db.flush()

        ConsentService.announce_revocation(user_id, consent_type, scope,
                                           agent_id, db=db)
        return rows[0]

    @staticmethod
    def announce_revocation(user_id: str, consent_type: str,
                            scope: str = '*', agent_id=None, db=None):
        """Fire the immutable-audit entry + ``consent.revoked`` broadcast for a
        revocation. PUBLIC on purpose: a surface that revokes by its own row
        model (the append-only ``consent_api`` UI path cannot delegate the WRITE
        without corrupting its audit trail) still gets IDENTICAL observability by
        calling this, instead of reaching into this module's underscore-private
        ``_audit``/``_emit``. That keeps the grant/revoke side-effect parity
        STRUCTURAL — a caller cannot get the row and silently skip the audit +
        broadcast. Best-effort (never raises).

        The camera and the screen are shared: the feed runs while any agent
        that needs it is allowed.  A no that names ONE agent while "Allow
        ALL agents" stands is that agent's alone (check_consent refuses it),
        so it does not stop the feed for the others.  That needs ``db`` to
        see the standing grant; without it, or when the read fails, the feed
        stops as before, the safe side.

        Every no is broadcast, also one for an agent that never asked
        (revoke_consent wrote its row just now): a page that shows what an
        agent is allowed has to hear a turn-off made in chat (review of
        0be16584a: the first such no was silent and the second was not).
        consent.revoked rings no phone; dropping a card from the phone is
        decline's, and only for an ask that was open."""
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
        others_keep_it = False
        if agent_id is not None and db is not None:
            try:
                others_keep_it = ConsentService.active_grant(
                    db, user_id, consent_type) is not None
            except Exception:
                _logger.warning("consent %s: could not read whether allow-all "
                                "stands; the feed stops", consent_type,
                                exc_info=True)
        if not others_keep_it:
            _embodied_feed_from_consent(consent_type, False)
        # Writes goals, and reads which credential grants are left, so after
        # the revoke's commit when the caller hands its session (both revoke
        # paths do); a caller outside any transaction gets it at once.
        if db is not None:
            after_commit(db, lambda: _mailbox_goals_from_consent(
                user_id, consent_type, scope, False))
            after_commit(db, lambda: _vault_value_from_consent(
                consent_type, scope))
        else:
            _mailbox_goals_from_consent(user_id, consent_type, scope, False)
            _vault_value_from_consent(consent_type, scope)
        # A phone taken away stops acting over the link it has open
        # (rv_2c18): the link is closed and told why.
        if consent_type == 'device_access':
            if db is not None:
                after_commit(db, lambda: _device_links_from_consent(
                    scope, 'consent_revoked'))
            else:
                _device_links_from_consent(scope, 'consent_revoked')

    @staticmethod
    def feed_said_no(db, consent_type: str, user_id=None) -> bool:
        """True when the latest answer on file for a camera/screen feed is a
        No, whoever gave it and for whichever agent or scope.

        ``user_id``: only that person's answers count.  The node-wide default
        is what the capture gate follows; ask_owner passes the owner, so a
        No given by a previous owner of this computer is not read as the
        current owner's.

        The feed is this node's hardware and its capture gate is node-wide:
        every answer, from any surface, reaches the same gate
        (_embodied_feed_from_consent) and the last one stands.  A process
        restoring the gate at startup (vision_service.restore_feed_answers)
        reads that same last answer here.  An answer is a grant (granted_at)
        or a No (revoked_at: revoke_consent, the privacy page's revoke, a
        declined ask); a pending ask is neither.  On a tie the No wins.

        One No is not the feed's: a No that names an agent, given by a
        person whose "Allow ALL agents" for this feed still stands.  It ends
        the feed for that one agent (check_consent) and every other agent
        keeps it, so it must not close the gate for them, now or at the next
        start (review of 59ff12f67: it did, and screen capture has standing
        allow-all grants on real desktops).  Without a standing allow-all a
        No for one agent counts as before.

        A database with no consent table has no answer on file: False.  It
        is a fresh install read before init_db creates the schema, or a file
        whose migrations never ran; no answer can have been recorded in it,
        so this is not a read error (those raise, and the caller decides).
        """
        _validate_consent_type(consent_type)
        from sqlalchemy import func, inspect
        if not inspect(db.get_bind()).has_table(UserConsent.__tablename__):
            return False
        from sqlalchemy import or_
        rows = db.query(UserConsent).filter(
            UserConsent.consent_type == consent_type)
        if user_id is not None:
            rows = rows.filter(UserConsent.user_id == str(user_id))
        allowing_all = db.query(UserConsent.user_id).filter(
            UserConsent.consent_type == consent_type,
            UserConsent.agent_id == None,
            UserConsent.scope == '*',
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        )
        last_yes = rows.with_entities(func.max(UserConsent.granted_at)).scalar()
        last_no = rows.filter(or_(
            UserConsent.agent_id == None,
            UserConsent.user_id.notin_(allowing_all),
        )).with_entities(func.max(UserConsent.revoked_at)).scalar()
        return last_no is not None and (last_yes is None or last_no >= last_yes)

    @staticmethod
    def active_grant(db, user_id: str, consent_type: str,
                     scope: str = '*', agent_id=None):
        """The granted, unrevoked row for EXACTLY this combination, the
        newest when grants stacked, or None.

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
        ).order_by(UserConsent.granted_at.desc()).first()

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
          3. Blanket consent (agent_id=None, scope='*'), unless the owner
             said no to this agent at or after that grant (_said_no_since):
             the no ends the kept consent for this one agent and leaves it
             for the others.  A blanket grant given after the no covers the
             agent again, and so does taking the no back (reopen).
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
            blanket = ConsentService.active_grant(db, user_id, consent_type)
            if blanket is not None and not ConsentService._said_no_since(
                    db, user_id, consent_type, scope, agent_id,
                    blanket.granted_at):
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
    def _said_no_since(db, user_id: str, consent_type: str, scope: str,
                       agent_id, since) -> bool:
        """True when the owner said no to ``agent_id`` for ``scope`` (or
        for every scope) at or after ``since``, and that no still stands
        (standing_revocation).  A no before ``since`` is covered by the
        grant given then; a grant with no time on it covers as it always
        did.  On a tie the no wins, the one tie rule for consent
        (feed_said_no has it too): two answers can carry one time (a store
        that keeps whole seconds), and refusing is the safe side."""
        if since is None:
            return False
        return db.query(UserConsent.id).filter(
            UserConsent.user_id == user_id,
            UserConsent.consent_type == consent_type,
            UserConsent.agent_id == agent_id,
            UserConsent.scope.in_([scope, '*']),
            standing_revocation(),
            UserConsent.revoked_at >= since,
        ).first() is not None

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
    def granted_by(db, consent_type: str, scope: str, agent_id=None,
                   prefix: bool = False) -> list:
        """The user ids holding an active grant of ``consent_type`` for
        ``scope`` (or, with ``prefix``, any scope starting with it) and
        ``agent_id``, oldest grant first: the reverse of active_grant, for a
        grant whose scope names who it lets in (chat_join: whose chats a
        joiner may use)."""
        _validate_consent_type(consent_type)
        q = db.query(UserConsent.user_id).filter(
            UserConsent.consent_type == consent_type,
            UserConsent.agent_id == agent_id,
            UserConsent.granted == True,
            UserConsent.revoked_at.is_(None),
        )
        q = q.filter(UserConsent.scope.startswith(scope, autoescape=True) if prefix
                     else UserConsent.scope == scope)
        out = []
        for (uid,) in q.order_by(UserConsent.granted_at.asc()).all():
            if uid not in out:
                out.append(uid)
        return out

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


# ── What a tool asks before it hands an agent a frame ─────────────────────

#: What agent_state reads for one agent.
AGENT_ALLOWED = 'allowed'          # something on file allows the agent
AGENT_TURNED_OFF = 'turned_off'    # nothing allows it, and its own no stands


def agent_state(db, user_id: str, consent_type: str, scope: str = '*',
                agent_id=None):
    """Where ``agent_id`` stands for ``consent_type``: AGENT_ALLOWED when
    something on file allows it (check_consent), AGENT_TURNED_OFF when
    nothing does and its own no stands (declined), None when nobody
    answered for it.

    The one reading of "is it off for this agent": the tools that hand out a
    frame ask it (turned_off_for), and GET /consent puts it on each row
    (consent_api.list_consents), so a page shows what the tools enforce and
    keeps no rule of its own (review of Nunba b0849302: the page's copy
    disagreed with check_consent for two answers given in one millisecond).
    Reads only; a store error is the caller's to handle."""
    if ConsentService.check_consent(db, user_id, consent_type, scope=scope,
                                    agent_id=agent_id):
        return AGENT_ALLOWED
    if ConsentService.declined(db, user_id, consent_type, scope, agent_id):
        return AGENT_TURNED_OFF
    return None


def turned_off_for(consent_type: str, prompt_id) -> bool:
    """True when this computer's owner said no to ``consent_type`` for the
    agent ``prompt_id`` names, and that no stands: nothing allows the agent
    (check_consent) and its own no is on file (declined), as agent_state
    reads them.

    The camera and the screen are shared and stay on for the agents still
    allowed, so the tool that hands out a frame asks this for its own
    agent first (hartos.helper.get_user_camera_inp,
    hart_intelligence_entry._handle_screenshot_tool).  Only an explicit no
    refuses: an agent nobody answered for reads False, as before this
    check existed.  Reads only.  No owner, no agent, or a store that cannot
    be read (logged) is False."""
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    agent = known_agent_id(prompt_id)
    if not owner or agent is None:
        return False
    try:
        from .models import db_session
        with db_session(commit=False) as db:
            return agent_state(db, owner, consent_type,
                               agent_id=agent) == AGENT_TURNED_OFF
    except Exception:
        _logger.warning("%s: could not read whether it is turned off for "
                        "agent %s", consent_type, agent, exc_info=True)
        return False


# ── Asking this computer's owner for a capability ─────────────────────────

#: What ask_owner found.
OWNER_ALLOWED = 'allowed'      # the consent is on file
OWNER_SAID_NO = 'said_no'      # the owner said no; nothing was asked
OWNER_ASKED = 'asked'          # the ask is on file (filed now, or sent again)
NO_OWNER = 'no_owner'          # nobody is signed in on this computer
NOT_THE_OWNER = 'not_owner'    # the turn is not the owner's; nobody was asked
NOT_HERE = 'not_here'          # central has no such part (central_lacks)
ASK_FAILED = 'failed'          # the consent store could not be read or written


def ask_owner(consent_type: str, reason: str = '', prompt_id=None, *,
              turn_user=None, stop_at_last_no: bool = False) -> str:
    """Ask this computer's owner for ``consent_type``; return what came of it.

    The ask shared by the Request_Camera_Access / Request_Screen_Access tools
    (hart_intelligence_entry._request_consent) and by the camera tool when it
    finds no frame (hartos.helper.get_user_camera_inp), so those two cannot
    drift apart.  The screen-capture loop, /visual_agent and computer
    control still ask by themselves: they carry a scope or a requester name
    this function does not take.  Whose permission: the desktop's owner
    (HEVOLVE_OWNER_USER_ID, which Nunba exports at boot and keeps in step
    with sign-in), never the caller.  Which agent asks: the turn's prompt id
    (known_agent_id).  The ask itself is request_consent: one row, one card,
    and nothing asked again once the owner answered.

    ``turn_user``: the user whose turn needs the part.  When given and not
    the owner, nobody is asked (NOT_THE_OWNER): the camera's frames are keyed
    by the owner's own window, so no answer could serve that turn.

    ``stop_at_last_no``: for an ask the agent did not make in so many words
    (the camera tool asking on its own).  When the owner's latest answer for
    this feed is a No (feed_said_no for the owner: any agent, any surface, a
    "Don't allow" or a revoke), nothing new is filed and the outcome is
    OWNER_SAID_NO -- unless this agent's own ask is still on screen,
    unanswered (it asked in so many words after that No): then the outcome
    is OWNER_ASKED and the card is sent again.  Without the flag a No to one
    agent stays that agent's (declined), as the card promises.

    Returns one of the outcomes above.  Never raises: a store error is logged
    here and returned as ASK_FAILED.
    """
    if central_lacks(consent_type):
        return NOT_HERE
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    if not owner:
        return NO_OWNER
    if turn_user is not None and str(turn_user).strip() != owner:
        return NOT_THE_OWNER
    agent = known_agent_id(prompt_id)
    try:
        from .models import db_session
        with db_session(commit=True) as db:
            if ConsentService.check_consent(db, owner, consent_type,
                                            agent_id=agent):
                return OWNER_ALLOWED
            if stop_at_last_no and ConsentService.feed_said_no(
                    db, consent_type, user_id=owner):
                waiting = (ConsentService.ask_on_file(
                               db, owner, consent_type, agent_id=agent) is not None
                           and not ConsentService.declined(
                               db, owner, consent_type, agent_id=agent))
                if not waiting:
                    return OWNER_SAID_NO
            ConsentService.request_consent(db, owner, consent_type,
                                           agent_id=agent, reason=reason)
            if ConsentService.declined(db, owner, consent_type, agent_id=agent):
                return OWNER_SAID_NO
    except Exception:
        _logger.exception("%s ask for the owner could not be filed", consent_type)
        return ASK_FAILED
    return OWNER_ASKED


# ── Turning a consent off for one agent, from its own chat ────────────────

#: What turn_off_for_agent found (beside NO_OWNER, NOT_THE_OWNER, ASK_FAILED).
TURNED_OFF = 'turned_off'      # this agent's no is on file; the others keep theirs
NOT_ON = 'not_on'              # nothing allowed this agent, so nothing to end
NO_AGENT = 'no_agent'          # the turn names no agent to turn it off for
BACKGROUND_TURN = 'background_turn'    # a goal or peer turn: no person said it

#: The consents an agent can be told in chat to stop using, by the words a
#: person uses for them.  Only kinds an agent holds for itself AND whose
#: every reader asks for its own agent (check_consent with the agent,
#: turned_off_for): the copilot is one switch for the whole computer, and a
#: credential, a phone or a mailbox is not an agent's to end.  Data access
#: is not here: what reads it (the microphone and chat learning,
#: whisper_tool._mic_learning_consented and world_model_bridge
#: ._ingest_user_utterance) hears a person, not an agent, so a no for one
#: agent would change nothing it does (review of f41434b5a).
CHAT_OFF_TYPES = {
    'camera': 'camera_capture',
    'screen': 'screen_capture',
    'computer control': 'computer_control',
}

#: Where the owner turns a permission off or back on for one agent, and what
#: turning it back on does, as every answer below words them.
IN_ADMIN_FOR_ONE_AGENT = "in Admin, under Agents, this agent, Permissions"
_OFF_IN_ADMIN = IN_ADMIN_FOR_ONE_AGENT
_BACK_ON_IS_FOR_EVERY_AGENT = (
    "turning it back on turns it back on for every agent they turned it off "
    "for, not only this one")


def chat_off_type(text):
    """The consent type ``text`` names for the chat off switch, or None:
    one of CHAT_OFF_TYPES' words, or the type's own name, anywhere in the
    text, whatever the case and whether written with spaces or
    underscores."""
    if not isinstance(text, str):
        return None
    said = ' '.join(re.sub(r'[^a-z]+', ' ', text.lower()).split())
    for words, consent_type in CHAT_OFF_TYPES.items():
        if re.search(rf'\b({words}|{consent_type.replace("_", " ")})\b', said):
            return consent_type
    return None


def _names_data_access(text) -> bool:
    """Whether ``text`` asks for data access, which the chat switch does
    not take (CHAT_OFF_TYPES says why)."""
    if not isinstance(text, str):
        return False
    said = ' '.join(re.sub(r'[^a-z]+', ' ', text.lower()).split())
    return re.search(r'\bdata access\b', said) is not None


def _what_went_wrong(error: BaseException) -> str:
    """The store's own error as an agent may read it: its kind and its
    words, with secrets and personal details masked (owner 2026-11-22: "Real
    failure reasons should be available to the agent so that it can ask
    correct questions").  When the masking cannot run, the kind alone."""
    kind = type(error).__name__
    try:
        from security.edge_privacy import scrub_text
        words = scrub_text(str(error)).strip()
    except Exception:
        _logger.warning("a consent error's words could not be masked; only "
                        "its kind is told", exc_info=True)
        return kind
    return f"{kind}: {words[:200]}" if words else kind


def _turn_off(consent_type: str, prompt_id, turn_user):
    """turn_off_for_agent's outcome and, with it, why: the detail the
    answer needs to give the real reason.
    NOT_THE_OWNER: 'no_user' (the turn names nobody) or 'another_person';
    NOT_ON: 'already_off' (this agent's own no stands) or 'never_allowed';
    ASK_FAILED: the error itself, masked (_what_went_wrong);
    else None."""
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    if not owner:
        return NO_OWNER, None
    if turn_user is None or not str(turn_user).strip():
        return NOT_THE_OWNER, 'no_user'
    if str(turn_user).strip() != owner:
        return NOT_THE_OWNER, 'another_person'
    agent = known_agent_id(prompt_id)
    if agent is None:
        return NO_AGENT, None
    try:
        from core.agent_tools import recall_runs_on_a_background_turn
        if recall_runs_on_a_background_turn():
            return BACKGROUND_TURN, None
        from .models import db_session
        with db_session(commit=True) as db:
            state = agent_state(db, owner, consent_type, agent_id=agent)
            if state != AGENT_ALLOWED:
                return NOT_ON, ('already_off' if state == AGENT_TURNED_OFF
                                else 'never_allowed')
            if ConsentService.decline(db, owner, consent_type,
                                      agent_id=agent) is None:
                return NOT_ON, 'never_allowed'
    except Exception as e:
        _logger.exception("%s could not be turned off for agent %s",
                          consent_type, agent)
        return ASK_FAILED, _what_went_wrong(e)
    return TURNED_OFF, None


def turn_off_for_agent(consent_type: str, prompt_id=None, *,
                       turn_user=None) -> str:
    """End ``consent_type`` for the turn's own agent because its person said
    so in chat (owner 2026-11-21: a kept consent stays "unless explicitly
    asked to be turned off by user").  The same owner as ask_owner
    (HEVOLVE_OWNER_USER_ID), the same agent (known_agent_id of the turn's
    prompt id), and the write is ConsentService.decline, the consent card's
    "Don't allow <agent>": this agent's no goes on file and every other
    agent keeps what it had.

    Unlike ask_owner this writes, so it takes the owner's own turn and
    nothing less (review of f41434b5a):
    - ``turn_user`` must be the owner.  A turn with no user on it is nobody's
      (NOT_THE_OWNER), where ask_owner reads it as "no turn check".
    - a goal or peer turn runs under the owner's id with no person in it
      (core.agent_tools.recall_runs_on_a_background_turn, the one rule for
      that): BACKGROUND_TURN, nothing written.

    Returns TURNED_OFF, NOT_ON (nothing allowed this agent: never granted,
    or already off), NO_AGENT, NO_OWNER, NOT_THE_OWNER, BACKGROUND_TURN or
    ASK_FAILED (the store or the turn could not be read, or the store not
    written; logged).  Never raises.
    """
    return _turn_off(consent_type, prompt_id, turn_user)[0]


def _allow_all_stands(consent_type: str):
    """Whether the owner's "Allow ALL agents" for ``consent_type`` stands:
    True, False, or None when it could not be read (logged)."""
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    try:
        from .models import db_session
        with db_session(commit=False) as db:
            return ConsentService.active_grant(db, owner, consent_type) is not None
    except Exception:
        _logger.warning("%s: could not read whether allow-all stands",
                        consent_type, exc_info=True)
        return None


def way_back_on(consent_type: str, name: str) -> str:
    """Where the owner turns ``name`` back on for an agent they turned it
    off for, true for what is on file now: the sentence a tool's refusal
    gives its agent (the camera tools, the screenshot tools), so the agent
    can tell the person where to act.  Under "Allow ALL agents" the agent
    has its switch in Admin, and it is for every agent.  With none there is
    no switch for one agent: the owner allows it for all agents."""
    allow_all = _allow_all_stands(consent_type)
    if allow_all:
        return (f"The owner turns it back on {IN_ADMIN_FOR_ONE_AGENT}, which "
                f"turns it back on for every agent they turned it off for")
    if allow_all is False:
        return (f"Nothing turns it back on for this agent alone: the owner "
                f"allows {name} for all their agents in Privacy settings")
    return f"The owner can allow {name} again in Privacy settings"


def turn_off_in_chat(input_text, prompt_id=None, *, turn_user=None) -> str:
    """What the Turn_Off_Permission tool answers its agent: the person said
    in chat to stop using the permission ``input_text`` names.  The write is
    turn_off_for_agent's, for the turn's agent only; every answer says what
    happened, what was not changed and what to tell the person, so the model
    never has to guess (owner 2026-11-09: a refusal says what to do next).

    Every refusal gives its real reason, never one sentence for several
    (owner 2026-11-22: "Real failure reasons should be available to the
    agent so that it can ask correct questions"): which kind of turn this
    is, whether the permission was never allowed or already turned off, and
    the store's own error with secrets masked.

    The answer to a turn-off says, before anyone turns it back on, that
    turning it back on is for the whole kind (owner 2026-11-22: POST
    /consent/reopen takes back every agent's no for it, and the switch says
    so)."""
    consent_type = chat_off_type(input_text)
    if consent_type is None:
        if _names_data_access(input_text):
            return ("Nothing was turned off: data access cannot be turned "
                    "off for one agent. What reads it, the microphone and "
                    "chat learning, serves the person whichever agent they "
                    "talk to, so a no for this agent would change nothing. "
                    "No page has a switch for it yet. Tell the person that; "
                    f"this tool takes: {', '.join(CHAT_OFF_TYPES)}.")
        return ("Nothing was turned off: the input names no permission. "
                f"Call this tool again with one of: {', '.join(CHAT_OFF_TYPES)}. "
                "If it is not clear which one the person means, ask them.")
    name = next(w for w, t in CHAT_OFF_TYPES.items() if t == consent_type)
    outcome, why = _turn_off(consent_type, prompt_id, turn_user)
    if outcome == TURNED_OFF:
        done = (f"Done: {name} is now turned off for this agent only; the "
                f"person's other agents are not changed. Do not use {name} "
                f"again in this chat. Tell the person it is off for this "
                f"agent")
        # The way back that is true for what is on file now (review of
        # 2dd5600a9: "Privacy settings or Admin" was said to an agent that
        # held its own grant, which Admin does not list once ended).
        allow_all = _allow_all_stands(consent_type)
        if allow_all:
            return (f"{done}, and that {_BACK_ON_IS_FOR_EVERY_AGENT}: they "
                    f"do that {_OFF_IN_ADMIN}.")
        if allow_all is False:
            return (f"{done}. It was allowed for this agent alone, and "
                    f"nothing turns it back on for this agent alone: to use "
                    f"it again they allow {name} for all their agents in "
                    f"Privacy settings.")
        return f"{done}, and that they can allow it again in Privacy settings."
    if outcome == NOT_ON:
        if why == 'already_off':
            return (f"Nothing was changed: {name} is not allowed for this "
                    f"agent: the owner already turned it off for it, so "
                    f"there was nothing to turn off. Tell the person it is "
                    f"already off for this agent.")
        return (f"Nothing was changed: {name} is not allowed for this agent: "
                f"nobody has allowed it yet, so there was nothing to turn "
                f"off. Tell the person it is already off for this agent, and "
                f"that the agent asks them the first time it needs it.")
    if outcome == NOT_THE_OWNER:
        who = ("this turn names no person, so it cannot be the owner's"
               if why == 'no_user' else
               "the person in this chat is not the owner")
        return (f"Nothing was changed: only the owner of this computer can "
                f"turn {name} off, by telling this agent in their own chat "
                f"or {_OFF_IN_ADMIN}, and {who}. Tell the person that.")
    if outcome == BACKGROUND_TURN:
        return (f"Nothing was changed: this turn runs in the background with "
                f"no person in it, and only the owner's own words turn "
                f"{name} off. Do not call this tool again on this turn. The "
                f"owner turns it off by telling this agent in their own chat "
                f"or {_OFF_IN_ADMIN}.")
    if outcome == NO_OWNER:
        return (f"Nothing was changed: nobody is signed in on this computer "
                f"whose {name} permission could be turned off.")
    if outcome == NO_AGENT:
        return (f"Nothing was changed: this chat is not with a named agent, "
                f"so there is no agent to turn {name} off for. Tell the "
                f"person to turn it off in Privacy settings.")
    return (f"Nothing was changed: {name} could not be turned off because "
            f"the permission store failed ({why}). It is still on for this "
            f"agent. Tell the person that, and that they can turn it off "
            f"{_OFF_IN_ADMIN}.")


# ──────────────────────────────────────────────────────────────────────
# Legacy ``register_consent_routes`` was REMOVED in the consent-surface
# consolidation (orchestrator review acd11f55, 2026-04-25).  The HTTP
# write surface for consent now lives at ``integrations.social.consent_api``
# (JWT-authed, append-only, mounted at ``/api/social/consent``).
# Internal services that need direct DB access continue to use the
# ``ConsentService`` static methods above — those are unchanged in
# signature, with grant_consent flipped to APPEND-ONLY.
# ──────────────────────────────────────────────────────────────────────
