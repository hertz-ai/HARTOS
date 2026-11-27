"""
HevolveSocial - User Consent Blueprint (W0c F3 prereq).

JWT-authed, append-only consent surface for the UserConsent UI.

Single HTTP surface for consent writes (orchestrator review acd11f55,
2026-04-25): the legacy ``/api/consent/<user_id>/*`` route family in
``consent_service.py`` was deprecated in this commit and this module
is now the only HTTP write path for the ``user_consents`` table.
``ConsentService`` static methods on ``consent_service.py`` are still
the in-process write API for internal services and now share this
module's APPEND-ONLY semantics.

This is the WRITE path the cloud_capability scope (encounter_icebreaker
agent + future cloud capabilities) reads at runtime via
encounter_api._has_cloud_drafting_consent (encounter_api.py:616).

Endpoints (all mounted at /api/social/consent*, JWT auth required):

  POST /api/social/consent          grant — APPEND a NEW row
  POST /api/social/consent/revoke   revoke — set revoked_at on every
                                    active row (granted_at preserved)
  POST /api/social/consent/decline  decline — say no to a pending ask,
                                    for the ask's agent only
                                    (ConsentService.revoke_consent)
  POST /api/social/consent/reopen   take a no back: the combination is
                                    undecided again, nothing is granted
                                    (ConsentService.reopen)
  GET  /api/social/consent          list — newest-first; supports
                                    consent_type + active_only filters;
                                    ?request_id= reads the caller's one
                                    open ask instead of the list

Append-only auditability:
  * Each grant creates a NEW UserConsent row.  Revoked rows are
    PRESERVED with their original granted_at intact and revoked_at
    set; subsequent grants do NOT reuse revoked rows.  Two rows with
    the same (user_id, agent_id=NULL, consent_type, scope) coexist
    because SQL treats NULL as distinct in the unique constraint.
  * Revoke flips revoked_at on the most-recent row where
    granted=True AND revoked_at IS NULL.  granted_at is never
    rewritten — the audit trail of WHEN the consent was granted is
    immutable.

Privacy invariants:
  * All routes scope by g.user_id — user A cannot see, grant, or
    revoke user B's consents.
  * 404 responses on revoke-with-no-active-row are neutral (no leak
    about whether the user has any consent rows at all).

Relationship to integrations/social/consent_service.py:
  * ``consent_service.py`` exposes the in-process ``ConsentService``
    static methods (``grant_consent``, ``revoke_consent``,
    ``check_consent``, ``list_consents``, ``set_payment_id``) used by
    internal services (``revenue_tracker``, ``ai_governance``,
    ``federated_aggregator``, ``lifecycle_hooks``).
  * Both surfaces share APPEND-ONLY write semantics as of the
    consolidation: every grant inserts a NEW row; ``granted_at`` is
    never rewritten on an existing row.
  * The legacy HTTP route family ``/api/consent/<user_id>/*`` from
    ``consent_service.py`` was REMOVED in the consolidation; this
    module is the single JWT-authed HTTP surface for consent writes.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

from flask import Blueprint, g, jsonify, request

from .auth import require_auth
from .models import UserConsent
from .consent_service import (
    ConsentService, agent_display_name, agent_state, device_fingerprint,
    known_agent_id)

logger = logging.getLogger('hevolve_social')

consent_bp = Blueprint('consent', __name__, url_prefix='/api/social')


# ──────────────────────────────────────────────────────────────────────
# Tiny helpers — response shape mirrors the rest of /api/social/*.
# ──────────────────────────────────────────────────────────────────────

from .api_common import _ok, _err  # single-sourced envelope helpers (#97)


def _json() -> dict[str, Any]:
    return request.get_json(force=True, silent=True) or {}


def _user_id() -> Optional[str]:
    """Resolve current user_id as string (matches schema's String(64))."""
    user = getattr(g, 'user', None)
    if user is None:
        return None
    uid = getattr(user, 'id', None)
    if uid is None and isinstance(user, dict):
        uid = user.get('id')
    return str(uid) if uid is not None else None


def _row_to_dict(row: UserConsent, agent_name=None) -> dict[str, Any]:
    """Subset projection that mirrors the F3 UI's expected shape.

    Avoids leaking columns the UI doesn't need (created_at,
    updated_at) while staying compatible with
    UserConsent.to_dict() callers elsewhere in the codebase.

    ``agent_id`` says which agent the row is for (None: every agent) and
    ``agent_name`` is the name its person knows it by (the caller looks it
    up, consent_service.agent_display_name), so a page can show and switch
    one agent's consent (owner 2026-11-21: a kept consent is turned off "in
    nunba admin for that agent").

    A device_access row is the trusted-phones listing (#111): its scope is
    the phone's key, unreadable, so it carries the ``label`` the phone
    signed into its ask (self-asserted) and the key's ``fingerprint``, the
    thing to match against the phone.
    """
    out = {
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'granted': bool(row.granted),
        'granted_at': row.granted_at.isoformat() if row.granted_at else None,
        'revoked_at': row.revoked_at.isoformat() if row.revoked_at else None,
        # A no taken back ("Allow asking again") keeps its revoked_at; the
        # row is declined only while revoked_at is newer than this.
        'reopened_at': (row.reopened_at.isoformat()
                        if getattr(row, 'reopened_at', None) else None),
        'agent_id': row.agent_id,
        'agent_name': agent_name if row.agent_id is not None else None,
    }
    if row.consent_type == 'device_access':
        out['label'] = row.label
        out['fingerprint'] = device_fingerprint(row.scope)
    return out


def _ask_to_dict(row: UserConsent, agent_name=None) -> dict[str, Any]:
    """An open ask as the client that will answer it reads it (GET
    /consent?request_id=): what is asked (consent_type, scope), by which
    agent, and on which node (fleet_command.self_node_keys).  A credential
    card is built from this and from nothing a push or an event carried, so
    its label is the scope this node holds and its answer names this node.
    Exactly these keys; a row holds no secret value and none is added."""
    from .fleet_command import self_node_keys
    node = self_node_keys()
    return {
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'agent_id': row.agent_id,
        'agent_name': agent_name if row.agent_id is not None else None,
        'node_id': node.get('node_id'),
        'node_tier': node.get('node_tier'),
        'node_name': node.get('node_name'),
    }


# The longest credential value a card may send.  Generous for any password
# or API key; it only stops a body from filling the vault.
CREDENTIAL_VALUE_MAX = 4096


def _open_ask(uid: str, request_id: str,
              scope: Optional[str] = None) -> Optional[UserConsent]:
    """The ask ``request_id`` names, when it is uid's and still waiting for
    its answer (consent_service.open_ask: never granted, no no standing on
    it, no yes since it was last sent).  An ask answered once is closed, so
    a second answer to it is refused and cannot write the vault again; an
    ask sent again after its answer (a credential the site refused) is open
    again.  request_id is the row id the ask's push carries
    (consent_service.consent_prompt_push), so a phone that only got the push
    answers exactly the row it was asked about.  None for anything else, so
    a 404 says nothing about other users' rows.

    ``scope``: the scope the answer says it is for (_answered_ask).  A row
    with another scope is not the ask the person saw, so it is None too."""
    from .consent_service import open_ask
    query = g.db.query(UserConsent).filter(
        UserConsent.id == request_id,
        UserConsent.user_id == uid,
        open_ask(),
    )
    if scope is not None:
        query = query.filter(UserConsent.scope == scope)
    return query.first()


# Why an answer was not taken, as the agent that filed the ask is told
# (_tell_the_agent).  The caller gets the neutral 404 and none of this.
NOT_NAMED = ("it did not say which ask and which computer it was for, which "
             "an app that needs updating sends. The ask is still open: the "
             "owner can answer it on this computer or update the app.")
OTHER_NODE = ("it was meant for another computer's ask and reached this one. "
              "The ask on this computer is still open.")
OTHER_SCOPE = ("it named another credential than the one this ask is for. "
               "The ask is still open.")
ASK_CLOSED = ("the ask had already been answered or declined when it "
              "arrived.")


def _tell_the_agent(uid: str, request_id: str, reason: str) -> None:
    """Record why an answer to a credential ask was not taken, for the agent
    that filed it (owner 2026-11-22: "Real failure reasons should be
    available to the agent so that it can ask correct questions").
    hartos.ai_key_vault.note_refused_answer keeps it by the credential's
    name and request_credential says it in the agent's next tool result.

    Only for the caller's own credential ask: another person's id records
    nothing, so a stranger cannot write into an agent's tool result.
    ``reason`` is this module's own words; nothing the caller sent and
    nothing of the value goes in.  Best-effort: it never changes what the
    route answers."""
    try:
        from .consent_service import CREDENTIAL_SCOPE_PREFIX
        row = g.db.query(UserConsent).filter(
            UserConsent.id == request_id,
            UserConsent.user_id == uid,
            UserConsent.consent_type == 'credential',
        ).first()
        scope = (row.scope or '') if row is not None else ''
        if not scope.startswith(CREDENTIAL_SCOPE_PREFIX):
            return
        from hartos.ai_key_vault import get_ai_key_vault, note_refused_answer
        name = get_ai_key_vault().credential_name(
            scope[len(CREDENTIAL_SCOPE_PREFIX):])
        if name:
            note_refused_answer(name, reason)
    except Exception as e:
        logger.warning('consent: the reason an answer to ask %s was not taken '
                       'could not be kept for its agent: %s',
                       request_id, type(e).__name__)


def _answered_ask(uid: str, request_id: str, body: dict) -> Optional[UserConsent]:
    """The open ask an answer by ``request_id`` is for, or None (the route's
    neutral 404) when the answer is not bound to that ask on this node.

    An id alone ties nothing: the ask's note is published on a router other
    sessions can read, and a goal mailbox on central and on a desktop asks
    for the same secret name.  So an answer may name what the person saw:

      scope    the scope of the ask the card showed.  The row must have it,
               so a value is stored under the name the person read.
      node_id  the node the client chose to answer.  It must be this node
               (fleet_command.self_node_id), so an answer routed to the
               wrong node is refused even when that node holds an open row
               with the same id and the same scope.  A node with no id of
               its own matches nothing.

    Each is checked when it is given.  An answer that names one of them
    names both when it carries a ``value``; a yes or a no without one is
    held only to what it names.  All of it is decided from the ask's names
    alone, before the value is looked at, so a refused answer stores
    nothing and says nothing about the value.  The caller hears the same
    404 for every one of them; the agent that filed the ask is told which
    it was (_tell_the_agent).

    An answer that names neither is from an app built before these names
    were sent: the phone apps in the field (Android ConsentAnswer sends
    {consent_type, request_id[, value]}).  It is taken by request_id alone,
    the caller's own open ask with that id, as every answer was taken
    before the names existed, with or without a value, and one log line
    says so.  Owner, 22 Nov: "functionalities shd work ... all should work
    the way its supposed to work during demo"."""
    node_id, scope = body.get('node_id'), body.get('scope')
    carries_value = body.get('value') is not None
    carries_values = body.get('values') is not None
    if carries_values and node_id is None and scope is not None:
        # The values of a channel's one card for several values
        # (grant_consent ``values``) name the node they are for (Android's
        # card: node_id from the push), or nothing, as the desktop card's
        # {consent_type, request_id, values} does, Android's 3-field body
        # (f2d640b49), taken by request_id below.  A scope without its node
        # is refused, as for a value.
        _tell_the_agent(uid, request_id, NOT_NAMED)
        return None
    if node_id is None and scope is None:
        logger.info('consent: the answer to ask %s names neither its scope '
                    'nor its node (an app from before both were sent), %s a '
                    'value; taken by its request_id alone', request_id,
                    'with' if carries_value or carries_values else 'without')
    elif carries_value and (node_id is None or scope is None):
        _tell_the_agent(uid, request_id, NOT_NAMED)
        return None
    if node_id is not None:
        from .fleet_command import self_node_id
        here = self_node_id()
        if (not here or not isinstance(node_id, str)
                or node_id.strip().lower() != here):
            _tell_the_agent(uid, request_id, OTHER_NODE)
            return None
    if scope is not None:
        if not isinstance(scope, str):
            _tell_the_agent(uid, request_id, OTHER_SCOPE)
            return None
        scope = scope.strip()
    ask = _open_ask(uid, request_id, scope)
    if ask is None:
        # Closed, or open under another scope than the answer named.
        _tell_the_agent(uid, request_id, ASK_CLOSED if _open_ask(
            uid, request_id) is None else OTHER_SCOPE)
    return ask


def _answer_record(uid: str, ask: UserConsent) -> dict:
    """Who answered a credential ask, and by which road, for the vault's one
    ``credential_stored`` audit event (AIKeyVault.store_credential writes it
    as the event's detail, at the node that stored):

      answered_by  the user this route resolved
      leg          'phone_device_link': the owner's phone over its device
                   link (the gate marked the call auth_source 'device', or
                   it came through the mobile adapter);
                   'local_page': a caller on this machine;
                   'cloud': an account token from another machine
      device       the answering phone's key fingerprint
                   (consent_service.device_fingerprint), on the device leg
                   only: the key the gate verified, or, where no gate ran
                   (/api/social/ is outside the API gate), the key the
                   link's own handshake verified
      node_id      this node, the one that stores
      ask          the ask's request_id

    Names and ids only.  Nothing of the value is ever in it: not the value,
    not its length, not a hash."""
    from core.auth_local import _is_local_request
    from integrations.mobile_adapter.adapter import REMOTE_ADDR as DEVICE_LINK
    from .fleet_command import self_node_id
    record = {'answered_by': uid, 'node_id': self_node_id(), 'ask': ask.id}
    via = str(request.remote_addr or '')
    peer = via[len(DEVICE_LINK) + 1:] if via.startswith(DEVICE_LINK + '/') else None
    if getattr(g, 'auth_source', None) == 'device' or peer is not None:
        key = getattr(g, 'device_public_key', None)
        if not key and peer:
            # The mobile adapter names the link's peer in REMOTE_ADDR; the
            # link holds the key its handshake checked the HELLO against.
            try:
                from core.peer_link.link_manager import get_link_manager
                link = get_link_manager().get_device_link(peer)
                key = getattr(link, 'peer_ed25519_public', None)
            except Exception as e:
                logger.warning('answer record: no key for the device link '
                               '(%s)', type(e).__name__)
        record['leg'] = 'phone_device_link'
        record['device'] = device_fingerprint(key)
    elif _is_local_request():
        record['leg'] = 'local_page'
    else:
        record['leg'] = 'cloud'
    return record


def _store_failure(e: BaseException) -> str:
    """Why a typed value could not be stored, as the owner's card and the
    asking agent are told (owner 2026-11-22: real failure reasons): the
    error's type and what to do about it.  Never the error's text, which
    could quote the value."""
    import errno
    if isinstance(e, PermissionError):
        todo = ("another program had this computer's credential file open, "
                "or HARTOS may not write it. Answer again in a moment; if it "
                "keeps failing, check that HARTOS can write to its data folder.")
    elif isinstance(e, OSError) and e.errno == errno.ENOSPC:
        todo = "this computer's disk is full. Free some space and answer again."
    elif isinstance(e, OSError):
        todo = ("this computer could not write its credential file. Answer "
                "again; if it keeps failing, its log says why.")
    else:
        todo = ("answer again; if it keeps failing, this computer's log names "
                "the error.")
    return (f'the credential could not be stored on this computer '
            f'({type(e).__name__}): {todo}')


def _store_owner_credential(uid: str, ask: UserConsent, value) -> tuple:
    """Put the value the owner typed on a card into this node's vault, for
    the credential ask ``ask``.  Returns (the vault's name for it, None)
    when stored, else (None, (message, status)) to refuse with.

    The phone's overlay has no local vault to put it in (the desktop card
    stores through Nunba's localhost /api/vault/store and only then grants),
    so the value comes with the answer, to the node that asked (owner
    ruling 2026-11-22: "Yes it can send password over internet"): only for
    this node's owner (HEVOLVE_OWNER_USER_ID, whose vault request_credential
    asks for), only on a credential ask still open, never a node secret.
    The value is never logged or returned.

    The name is the vault's one spelling of the scope's credential
    (AIKeyVault.credential_name, which _granted_names reads grants with
    too), so it is the one checked against the node's secrets and read
    back, and a scope written another way ('secret:site_password')
    resolves once granted.  The vault answers a name it refuses (one this
    process holds and never stored: PATH, a proxy, NUNBA_CI) exactly as
    one it stored, writing nothing; granting then would name that setting
    an owner credential and its {{secret:NAME}} would resolve to the
    system's value.  So the value a tool would get is read back, and
    anything but what was typed refuses the answer before the grant (409:
    the node already holds that name).  The value is stored
    ``granted_only``, and every reader of the vault in this process
    (get_tool_key, SecretsManager.get_secret, os.environ, its alias) stops
    getting it once the answer is refused here, its grant fails
    (grant_consent) or its grant is revoked, after a restart too
    (AIKeyVault.take_back_answer puts back what the vault held before the
    answer; the record of the answer is saved in the vault with it).  The
    limits: a value another writer stored since the answer is left; when
    the vault's save fails, secrets.enc keeps the value until a later save
    succeeds; and the value stays masked in this process's logs."""
    from .consent_service import CREDENTIAL_SCOPE_PREFIX
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner or str(owner) != uid:
        return None, ('only the owner of this computer can enter its credentials', 403)
    scope = ask.scope or ''
    from hartos.ai_key_vault import get_ai_key_vault, is_node_secret
    vault = get_ai_key_vault()
    name = vault.credential_name(scope[len(CREDENTIAL_SCOPE_PREFIX):])
    if ask.consent_type != 'credential' or not scope.startswith(
            CREDENTIAL_SCOPE_PREFIX) or not name:
        return None, ('a value answers a credential ask only', 400)
    if is_node_secret(name):
        return None, ('a value answers a credential ask only', 400)
    if not isinstance(value, str):
        return None, ('value must be text: the password or key as typed', 400)
    value = value.strip()
    if not value:
        return None, ('value required', 400)
    if len(value) > CREDENTIAL_VALUE_MAX:
        return None, (f'value exceeds {CREDENTIAL_VALUE_MAX} chars', 400)
    # Before the store: a take-back below removes only what THIS answer
    # wrote (take_back_answer typed/since); a store the vault refused wrote
    # nothing, and an earlier granted answer must stay (review rv4a1).
    since = time.time()
    try:
        vault.store_credential(name, value, granted_only=True,
                               answered=_answer_record(uid, ask))
        held = vault.get_tool_key(name) == value
    except Exception as e:
        # The type only: a message could carry the value.
        logger.warning('consent.credential %s not stored: %s',
                       name, type(e).__name__)
        vault.take_back_answer(name, typed=value, since=since)
        return None, (_store_failure(e), 500)
    if not held:
        vault.take_back_answer(name, typed=value, since=since)
        logger.warning('consent.credential %s not stored: the process already '
                       'holds a value under that name, so it is not granted', name)
        return None, (f'this computer already holds a value named {name} that '
                      f'was not entered for an agent, so it was not replaced '
                      f'and nothing was granted. To give it here, remove '
                      f'{name} from where this computer\'s environment sets it '
                      f'and answer again, or have the agent ask for it under '
                      f'another name.', 409)
    return name, None


def _setup_ask_channel(consent_type: str, scope: str) -> Optional[str]:
    """The channel whose one card for several values a ``consent_type`` /
    ``scope`` names (a 'credential' ask named by
    agent_tools.setup_ask_channel), or None for any other."""
    from .consent_service import CREDENTIAL_SCOPE_PREFIX
    scope = scope or ''
    if consent_type != 'credential' or not scope.startswith(
            CREDENTIAL_SCOPE_PREFIX):
        return None
    from integrations.channels.agent_tools import setup_ask_channel
    return setup_ask_channel(scope[len(CREDENTIAL_SCOPE_PREFIX):])


def _start_channel_from_answer(uid: str, channel: str, values) -> Optional[tuple]:
    """Start ``channel`` from the values the owner typed on its one card
    (agent_tools.setup_fields), through register_channel, the path every
    channel start takes: it keeps them sealed on the channel's binding, where
    boot restore reads them, as the connect form does, and watches the start,
    telling the owner how it ended.  No second copy goes into the vault: the
    binding is the one store of a channel's settings.  None when the start
    was scheduled, else (message, status) to refuse with, nothing started.

    Only this node's owner (HEVOLVE_OWNER_USER_ID) starts its channels, as
    only the owner fills its vault (_store_owner_credential).  The answer
    names exactly the card's fields, each a non-empty text; anything else
    starts nothing.  The values are never logged or returned, and are masked
    out of what register_channel says."""
    import json
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner or str(owner) != uid:
        return ('only the owner of this computer can enter its credentials', 403)
    from integrations.channels.agent_tools import (
        build_channel_tool_closures, setup_fields, start_verdict)
    names = [f['name'] for f in setup_fields(channel)]
    if not isinstance(values, dict) or set(values) != set(names):
        return (f"this ask is answered with exactly these values: "
                f"{', '.join(names)}", 400)
    clean = {}
    for name in names:
        typed = values[name]
        if not isinstance(typed, str) or not typed.strip():
            return (f'{name} is needed: the value as typed, not empty', 400)
        if len(typed.strip()) > CREDENTIAL_VALUE_MAX:
            return (f'{name} exceeds {CREDENTIAL_VALUE_MAX} chars', 400)
        clean[name] = typed.strip()
    register = next(t[2] for t in build_channel_tool_closures(
        {'user_id': owner}) if t[0] == 'register_channel')
    before = start_verdict(channel)
    said = str(register(channel, json.dumps(clean)))
    after = start_verdict(channel)
    if after is None or (before is not None and after['start'] == before['start']):
        for typed in clean.values():
            said = said.replace(typed, '***')
        logger.warning('consent: %s was not started from the owner\'s answer',
                       channel)
        from integrations.channels.flask_integration import (
            FlaskChannelIntegration)
        if FlaskChannelIntegration.build_refusal(channel):
            # A component missing from this build is nothing the card can be
            # answered with: the owner is told so, not left to type the same
            # values again (rv_mc on 0e841af43).
            said += (' Answering this card again will not start it until '
                     'that component is installed.')
        return (f'{channel} was not started: {said}', 409)
    return None


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent — grant (APPEND a NEW row)
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent', methods=['POST'])
@require_auth
def grant_consent():
    """Grant a consent.  ALWAYS appends a new row — never updates an
    existing one — so the audit log of grant events is immutable.

    Body: {consent_type: str, scope: str (default '*')}
       or {request_id: str[, scope: str, node_id: str][, value: str]}: the
          answer to the ask whose push carried request_id (the phone's
          overlay); type and scope are the ask's.  ``scope`` and
          ``node_id`` say which ask on which node the person answered
          (_answered_ask): checked when given; with a ``value``, one
          names both.  An answer naming neither (an app from before
          them) is taken by request_id alone, as before.
          ``value`` is what the owner typed on a credential ask: it
          is stored in this node's vault before the grant (only for this
          node's owner, only while the ask is open), taken back out if the
          grant fails, and never logged or returned.
       or {request_id[, node_id[, scope]], values: {name: value}}: the
          answer to a channel's one card for several values (its push and
          its note carry data.fields).  Android's card names the node; the
          desktop card names neither, {consent_type, request_id, values},
          as Android's 3-field body does, and is taken by request_id; a
          scope without the node is refused.  Exactly the card's fields,
          which start the channel (_start_channel_from_answer) before the
          grant; never stored in the vault, never logged or returned.  Such
          an ask is answered this way only: one value, or a yes by type and
          scope, is refused (400) and leaves it open.
    Returns: {id, consent_type, scope, granted_at}
    Errors: 404 — no open ask for request_id (neutral message)
                  The same 404 when the answer names another node or
                  another scope, decided before the value is read.
            409 — the node already holds a value under the credential's
                  name that was not entered for an agent
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    request_id = str(body.get('request_id') or '').strip()
    ask = None
    if request_id:
        ask = _answered_ask(uid, request_id, body)
        if ask is None:
            return _err('no such ask', 404)
    value = body.get('value')
    values = body.get('values')
    if ask is not None:
        consent_type, scope = ask.consent_type, ask.scope
    else:
        if value is not None or values is not None:
            return _err('a value answers an ask: request_id required')
        consent_type = str(body.get('consent_type', '')).strip()
        scope = str(body.get('scope', '*')).strip() or '*'
    channel = _setup_ask_channel(consent_type, scope)
    if channel is not None:
        # A channel's one card for several values is answered on its ask with
        # every one of them: never with one value, and never by type and
        # scope alone, whose yes would start nothing and stand as its answer.
        if ask is None or value is not None or values is None:
            return _err('this ask is answered with every value its card '
                        'names: request_id and values')
    elif values is not None:
        return _err("values answer a channel's ask for several values only")
    # A yes with nothing typed would name a credential that holds no
    # value; the card's Accept always carries one.
    elif ask is not None and consent_type == 'credential' and value is None:
        return _err('a credential ask is answered with its value')

    if not consent_type:
        return _err('consent_type required')
    # Defensive caps that match the schema column widths so writes
    # never silently truncate at the DB layer.
    if len(consent_type) > 30:
        return _err('consent_type exceeds 30 chars')
    if len(scope) > 100:
        return _err('scope exceeds 100 chars')
    # A phone is allowed by its key, never by a blanket: the gate reads the
    # key back from the granted row (auth.verify_device_jwt), so a '*' row
    # admits no phone and would only look like "all phones allowed".
    if consent_type == 'device_access' and device_fingerprint(scope) is None:
        return _err('device_access is granted per phone: scope must be device:<key>')

    # Delegate the WRITE to the canonical ConsentService so this UI surface gets
    # the SAME immutable-audit entry, `consent.granted` event, consent-type
    # validation, and public_exposure up-sync that internal callers get. This
    # route used to INSERT the UserConsent row inline, so UI-driven grants left
    # NO audit trail, emitted NO event, skipped up-sync, and accepted arbitrary
    # consent_type strings — a drifted parallel path (audit gap #4). grant is
    # append-only in both, so the delegation is behaviour-preserving for the row.
    if channel is not None:
        # Started before the grant: a start refused here keeps the ask open
        # for a corrected answer.  A component missing from this build is
        # not fixed by any answer, and the refusal says so
        # (_start_channel_from_answer).  The ask is not closed: closing it
        # here is a no (decline), which the owner did not give, and after
        # which the agent is told "the owner said no" and its card does not
        # come back once the component is installed.
        refused = _start_channel_from_answer(uid, channel, values)
        if refused:
            _tell_the_agent(uid, ask.id, 'this computer refused it: '
                            + refused[0])
            return _err(*refused)
    stored = None
    answered_at = time.time()   # before the store: take_back_answer(since=)
    if value is not None:
        stored, refused = _store_owner_credential(uid, ask, value)
        if refused:
            # The route's own words for it (no value in any of them): the
            # vault refused the name, the value was empty or too long, the
            # caller is not this computer's owner.
            _tell_the_agent(uid, ask.id, 'this computer refused it: '
                            + refused[0])
            return _err(*refused)
    try:
        # A phone's yes admits it as the account the card it answers showed
        # (the ask's paired_user_id, #306 / rv_2c18).
        row = ConsentService.grant_consent(
            g.db, uid, consent_type, scope,
            paired_user_id=(ask.paired_user_id if ask is not None
                            and consent_type == 'device_access' else None))
        if stored:
            # The value stands only with its grant on disk: committed here,
            # not by require_auth after this returns, so a commit that
            # fails still finds the value to take back below.
            g.db.commit()
    except Exception as e:
        if stored:
            from hartos.ai_key_vault import get_ai_key_vault
            # Its own write only, the value as _store_owner_credential
            # stored it (stripped).
            get_ai_key_vault().take_back_answer(
                stored, typed=value.strip(), since=answered_at)
            _tell_the_agent(uid, ask.id, 'the value was entered but its '
                            'permission could not be saved, so it was taken '
                            'back out. The owner can enter it again.')
        if isinstance(e, ValueError):
            return _err(str(e))  # unknown consent_type -> 400, not a masked 500
        raise
    if stored:
        # Only now does the value stand: whoever set something aside for
        # want of this key hears of it (the name only, never the value).
        from hartos.ai_key_vault import announce_credential_stored
        announce_credential_stored(stored)

    if stored:
        from hartos.ai_key_vault import forget_refused_answer
        forget_refused_answer(stored)
    logger.info(
        'consent.grant user=%s type=%s scope=%s id=%s',
        uid, consent_type, scope, row.id,
    )
    return _ok({
        'id': row.id,
        'consent_type': row.consent_type,
        'scope': row.scope,
        'granted_at': row.granted_at.isoformat(),
    }, status=201)


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/revoke — set revoked_at on the active row
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/revoke', methods=['POST'])
@require_auth
def revoke_consent():
    """Revoke every active consent for (user, type, scope).

    Active = granted=True AND revoked_at IS NULL.  The response names the
    most recent of them.

    Body: {consent_type: str, scope: str (default '*')}
    Returns: {id, revoked_at}
    Errors:
      400 — missing consent_type
      404 — no active consent for this triple (neutral message; no
            leak about whether the user has any rows at all)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    consent_type = str(body.get('consent_type', '')).strip()
    scope = str(body.get('scope', '*')).strip() or '*'

    if not consent_type:
        return _err('consent_type required')

    # Every active row.  A grant appends a row, so two Allow clicks are two
    # rows; revoking only the newest left the older one passing
    # check_consent (tests/unit/test_consent_revoke_is_honoured.py).
    # Newest first, so the response still names the most recent grant.
    rows = g.db.query(UserConsent).filter(
        UserConsent.user_id == uid,
        UserConsent.consent_type == consent_type,
        UserConsent.scope == scope,
        UserConsent.granted == True,  # noqa: E712 — SQLAlchemy idiom
        UserConsent.revoked_at.is_(None),
    ).order_by(UserConsent.granted_at.desc()).all()

    if not rows:
        return _err('no active consent', 404)

    # Audit-evidence-discipline: NEVER overwrite granted_at.  The
    # event of "this consent was granted at T" is immutable history.
    now = datetime.utcnow()
    for r in rows:
        r.revoked_at = now
    g.db.flush()
    row = rows[0]

    # Parallel-path parity (audit #4): this UI surface keeps its OWN append-only
    # row model on purpose — granted stays True and revoked_at is the tombstone
    # (see the module docstring), which is deliberately NOT the same as
    # ConsentService.revoke_consent's granted=False + agent_id-keyed row model,
    # so we do not delegate the WRITE (that would corrupt the append-only audit
    # trail). But the observable SIDE EFFECTS must not drift between the two
    # writers, so fire them through the service's PUBLIC announce_revocation —
    # the same entry point ConsentService.revoke_consent uses — rather than
    # reaching into its underscore-private _audit/_emit. Previously a UI revoke
    # left no audit entry and never reached WAMP/SSE listeners or the user's
    # other devices. announce_revocation is best-effort (never raises), so this
    # cannot fail the revoke.
    from .consent_service import ConsentService
    ConsentService.announce_revocation(
        uid, consent_type, scope, getattr(row, 'agent_id', None), db=g.db)

    logger.info(
        'consent.revoke user=%s type=%s scope=%s id=%s',
        uid, consent_type, scope, row.id,
    )
    return _ok({
        'id': row.id,
        'revoked_at': row.revoked_at.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/decline — say no to a pending ask
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/decline', methods=['POST'])
@require_auth
def decline_consent():
    """Say no to a pending ask: the "Don't allow" answer on a consent card.

    The write is ConsentService.revoke_consent.  With no active grant it
    marks the ask declined, which stops request_consent asking again and
    makes ConsentService.declined true, so a gate that is waiting refuses at
    once.  agent_id is the ask's agent: a no to one agent's ask leaves every
    other agent's ask open.  While "Allow ALL agents" stands, a no that names
    an agent turns the consent off for that agent only, asked or not: the
    grant stays for the others.  The way back is a grant (POST /consent),
    which covers every agent, or POST /consent/reopen.

    Body: {consent_type: str, scope: str (default '*'), agent_id: str|null}
       or {request_id: str[, scope: str, node_id: str]}: the no to the
          ask whose push carried request_id (the phone's overlay); type,
          scope and agent are the ask's.  ``scope`` and ``node_id`` are
          checked when given (_answered_ask).
    Returns: {declined: true, id}
    Errors:
      400 — missing or unknown consent_type
      404 — no ask for this combination (neutral message)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    request_id = str(body.get('request_id') or '').strip()
    if request_id:
        ask = _answered_ask(uid, request_id, body)
        if ask is None:
            return _err('no such ask', 404)
        consent_type, scope, agent_id = ask.consent_type, ask.scope, ask.agent_id
    else:
        consent_type = str(body.get('consent_type', '')).strip()
        scope = str(body.get('scope', '*')).strip() or '*'
        agent_id = body.get('agent_id')
        if agent_id is not None:
            agent_id = str(agent_id).strip() or None

    if not consent_type:
        return _err('consent_type required')
    # The same caps as the grant route: a no can write a row (an agent's own
    # no under "Allow ALL agents"), and a row never truncates at the store.
    if len(consent_type) > 30:
        return _err('consent_type exceeds 30 chars')
    if len(scope) > 100:
        return _err('scope exceeds 100 chars')
    if agent_id is not None and len(str(agent_id)) > 64:
        return _err('agent_id exceeds 64 chars')

    try:
        # The ask the answer named, so a no to a phone's switch declines
        # that ask alone (rv_2c18).
        row = ConsentService.decline(g.db, uid, consent_type, scope, agent_id,
                                     ask=ask if request_id else None)
    except ValueError as e:
        return _err(str(e))  # unknown consent_type -> 400
    if row is None:
        return _err('no such ask', 404)

    logger.info(
        'consent.decline user=%s type=%s scope=%s agent=%s id=%s',
        uid, consent_type, scope, agent_id, row.id,
    )
    return _ok({'declined': True, 'id': row.id})


# ──────────────────────────────────────────────────────────────────────
# POST /api/social/consent/reopen — take a "no" back
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent/reopen', methods=['POST'])
@require_auth
def reopen_consent():
    """Take back a "no" (ConsentService.reopen): the privacy page's "Allow
    asking again" for an ask with no on/off card to grant from, such as a
    credential.  The combination is undecided again, for every agent, and
    the next ask shows the card.  Nothing is granted.

    Body: {consent_type: str, scope: str (default '*')}
    Returns: {reopened: n}
    Errors:
      400 — missing or unknown consent_type
      404 — nothing declined for this combination (neutral message)
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    body = _json()
    consent_type = str(body.get('consent_type', '')).strip()
    scope = str(body.get('scope', '*')).strip() or '*'
    if not consent_type:
        return _err('consent_type required')

    try:
        n = ConsentService.reopen(g.db, uid, consent_type, scope)
    except ValueError as e:
        return _err(str(e))  # unknown consent_type -> 400
    if not n:
        return _err('nothing declined', 404)

    logger.info('consent.reopen user=%s type=%s scope=%s rows=%d',
                uid, consent_type, scope, n)
    return _ok({'reopened': n})


# ──────────────────────────────────────────────────────────────────────
# GET /api/social/consent — list this user's consents
# ──────────────────────────────────────────────────────────────────────

@consent_bp.route('/consent', methods=['GET'])
@require_auth
def list_consents():
    """List the caller's consent rows (newest-first by granted_at).

    Query params:
      consent_type — filter by exact match (optional)
      active_only  — if 'true', return only granted=True AND
                     revoked_at IS NULL (default: return all rows)
      agent_id     — the agent a page asks about (optional); it changes
                     only what agent_state says on every-agent rows
      request_id     read ONE ask instead of the list: the caller's own
                     open ask with that id as _ask_to_dict gives it (id,
                     consent_type, scope, agent_id, agent_name, node_id,
                     node_tier, node_name), or no rows for anything else
                     (another person's ask, an answered one, an unknown
                     id).  The read a client makes before it shows a
                     credential card.  Never the list, even when empty.

    Each row also carries agent_id (null: every agent) and agent_name, and
    agent_state: 'allowed', 'turned_off' or null (nobody answered), as
    consent_service.agent_state reads it for the row's type and scope.  It
    is the state of the row's own agent; on a row for every agent it is the
    state of the agent the request names (agent_id), else of a caller with
    no agent.  A page shows this and keeps no rule of its own (review of
    Nunba b0849302).

    Returns: {consents: [{id, consent_type, scope, granted,
                          granted_at, revoked_at}, ...]}; a device_access
             row also carries label + fingerprint (#111).
    """
    uid = _user_id()
    if uid is None:
        return _err('unauthenticated', 401)

    if 'request_id' in request.args:
        request_id = request.args.get('request_id', '').strip()
        ask = _open_ask(uid, request_id) if request_id else None
        asks = []
        if ask is not None:
            asks.append(_ask_to_dict(ask, agent_display_name(g.db, ask.agent_id)
                                     if ask.agent_id is not None else None))
        return _ok({'consents': asks, 'count': len(asks)})

    consent_type = request.args.get('consent_type')
    active_only = request.args.get('active_only', '').lower() in {
        'true', '1', 'yes',
    }

    q = g.db.query(UserConsent).filter(UserConsent.user_id == uid)
    if consent_type:
        q = q.filter(UserConsent.consent_type == consent_type)
    if active_only:
        q = q.filter(
            UserConsent.granted == True,  # noqa: E712
            UserConsent.revoked_at.is_(None),
        )
    # Newest-first by granted_at.  Rows that were created via
    # request_consent (granted=False, granted_at=NULL) sort last
    # naturally because NULLs are LAST in ASC and FIRST in DESC on
    # most engines — push them to the bottom by also sorting on
    # created_at DESC as a stable secondary key.
    rows = q.order_by(
        UserConsent.granted_at.desc().nullslast()
        if hasattr(UserConsent.granted_at.desc(), 'nullslast')
        else UserConsent.granted_at.desc(),
        UserConsent.created_at.desc(),
    ).all()

    # One name lookup per agent, not per row.
    names = {}
    for r in rows:
        if r.agent_id is not None and r.agent_id not in names:
            names[r.agent_id] = agent_display_name(g.db, r.agent_id)

    # One reading per (type, scope, agent), by the rule the tools enforce.
    asked_about = known_agent_id(request.args.get('agent_id'))
    states = {}

    def read(consent_type, scope, agent):
        key = (consent_type, scope, agent)
        if key not in states:
            try:
                states[key] = agent_state(g.db, uid, consent_type, scope, agent)
            except ValueError:
                # A row of a type this build no longer knows: no state.
                states[key] = None
        return states[key]

    def state_of(r):
        if r.agent_id is not None:
            return read(r.consent_type, r.scope, r.agent_id)
        # A row for every agent, asked about one: that agent's state, and
        # when nobody answered for it, the row's own, so an "Allow ALL
        # agents" that was ended reads turned_off with or without the
        # agent named (review of 2811bea72).
        said = read(r.consent_type, r.scope, asked_about)
        if said is None and asked_about is not None:
            said = read(r.consent_type, r.scope, None)
        return said

    consents = []
    for r in rows:
        out = _row_to_dict(r, names.get(r.agent_id))
        out['agent_state'] = state_of(r)
        consents.append(out)

    return _ok({
        'consents': consents,
        'count': len(rows),
    })
