"""Which lane a /chat turn runs on, decided from what the request proves.

Owner policy 2026-10-12: a person's conversation runs on the free model
unless it is revenue-bearing; a revenue-bearing turn may use the expert
model and is never throttled or budget-blocked, because it pays for itself.

Two facts make a turn revenue-bearing, and /chat can verify both itself:

  1. The caller's JWT carries the commerce tenant claim (``tid``): a McGroce
     shopper or store owner whose token POST /api/commerce/session minted.
  2. The body says ``lane: revenue`` AND the caller is the node itself: the
     metered API endpoints' self-POST for a paying tier
     (commercial_api._metered_backend_chat) arrives either with the node's
     own key (``auth_source == 'api_key'``) or with the JWT the node mints
     for its own system identity (dispatch._internal_auth_headers).  Nobody
     else holds either.  The same word in any other body is just a word.

Everything else is a user turn.  The daemon lane is not decided here: the
request id's ``daemon_`` prefix decides it on the wire
(dispatch.is_genuine_user_request), the rule every other caller uses.

``register_chat_lane`` is /chat's ONE registration decision (reviews of
7086adff8 and 8947e2753).  In order:

  * The node's own daemon turn is left alone: a ``daemon_`` id from the node
    itself (its key, its system JWT, or no credential at all, which is how
    the in-process adapter on a bundled desktop and dispatch's local path
    call /chat) keeps whatever dispatch_goal registered for the goal, and a
    plain goal's turn stays unregistered so the prefix rule says daemon.
  * The node calling itself for a turn that is already decided is left
    alone too: the speculative dispatcher's inner expert call carries the
    person's request id, the node's credential and NO lane claim, and must
    not re-register the shopper's revenue turn as a user's.
  * Everyone else (a person's JWT, a device token, a stranger naming a
    goal's request id) is registered by what the request proves, REPLACING
    what the id held.  That is what stops a stranger from inheriting a
    goal's revenue lane by naming its request id.

A turn that keeps to this node (``local_only``) is the third input.  /chat
decides it from ONE rule (the person's own local_only toggle, or the goal
tags of the turn's agent, e.g. a Reel Studio agent, decision U4) and passes
it here; it is never a second decision beside this module (amendment A9 e,
ruled with the lanes lane 2026-10-13).  Such a turn never runs on, and is
never left on, the revenue lane, the one lane the configured expert is
offered to.  ``chat_turn_models`` is the other half of the same decision:
the model tier and speculation the turn's body asks for, refused for a turn
that keeps to this node.  /chat sets neither itself.

Pure: the JWT decoder, the tenant name, the system identities and the
genuine-user rule are passed in by the caller (hart_intelligence_entry's
/chat), so core imports nothing from integrations and the rule is testable
without the app.  The lane names are the ones core.llm_outbound_logger stamps
on the wire.
"""
from core.llm_outbound_logger import (
    LANE_DAEMON, LANE_REVENUE, LANE_USER, lane_for_request, register_request_lane,
)

#: auth_source values that mean "no credential was presented": the desktop's
#: own callers on a bundled node and any caller on a flat node, where body
#: auth is the design (hart_intelligence_entry /chat, security.middleware).
CREDENTIALLESS_AUTH = frozenset({None, '', 'none', 'body'})


def _payload(bearer_token, decode_jwt):
    if not bearer_token or decode_jwt is None:
        return {}
    try:
        payload = decode_jwt(bearer_token)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def is_internal_caller(auth_source, jwt_payload, system_identities=()):
    # type: (object, object, object) -> bool
    """Is this request the node talking to itself?  The node's own key, or a
    JWT whose user is one of the node's system identities (only the node's
    secret mints one)."""
    if auth_source == 'api_key':
        return True
    uid = (jwt_payload or {}).get('user_id') if isinstance(jwt_payload, dict) else None
    return bool(uid) and str(uid) in set(str(s) for s in (system_identities or ()))


def lane_for_chat(bearer_token, body, auth_source, decode_jwt=None, tenant=None,
                  system_identities=(), jwt_payload=None):
    # type: (...) -> str
    """'revenue' or 'user' for one /chat request; never raises."""
    payload = jwt_payload if isinstance(jwt_payload, dict) else _payload(bearer_token, decode_jwt)
    if tenant and payload.get('tid') == tenant:
        return LANE_REVENUE
    if (isinstance(body, dict) and body.get('lane') == LANE_REVENUE
            and is_internal_caller(auth_source, payload, system_identities)):
        return LANE_REVENUE
    return LANE_USER


def register_chat_lane(request_id, bearer_token, body, auth_source, decode_jwt=None,
                       tenant=None, system_identities=(), is_genuine=None,
                       local_only=False):
    # type: (...) -> object
    """/chat's one registration decision.  Returns the lane the turn runs on
    when this call decided or confirmed it, or None when the turn is left to
    the prefix rule and to earlier registrations.

    ``local_only``: the turn keeps to this node.  Wherever the answer would
    be revenue (what the request proves, or what the id already holds), the
    turn is registered on its own lane instead, replacing the revenue
    registration: 'daemon' for the node's own daemon turn, 'user' for anyone
    else's.  Every other answer is unchanged, and False (the default) changes
    nothing."""
    payload = _payload(bearer_token, decode_jwt)
    internal = is_internal_caller(auth_source, payload, system_identities)
    credentialless = auth_source in CREDENTIALLESS_AUTH
    genuine = True
    if is_genuine is not None:
        try:
            genuine = bool(is_genuine(request_id))
        except Exception:
            genuine = True
    if not genuine and (internal or credentialless):
        # The node's own daemon turn.
        if local_only and lane_for_request(request_id) == LANE_REVENUE:
            register_request_lane(request_id, LANE_DAEMON)
            return LANE_DAEMON
        return None
    claim = body.get('lane') if isinstance(body, dict) else None
    if internal and not claim:
        # The node's own inner call.
        lane = lane_for_request(request_id) or None
        if local_only and lane == LANE_REVENUE:
            register_request_lane(request_id, LANE_USER)
            return LANE_USER
        return lane
    lane = lane_for_chat(bearer_token, body, auth_source, decode_jwt=decode_jwt,
                         tenant=tenant, system_identities=system_identities,
                         jwt_payload=payload)
    if local_only and lane == LANE_REVENUE:
        lane = LANE_USER
    register_request_lane(request_id, lane)
    return lane


def chat_turn_models(body, local_only=False):
    # type: (object, bool) -> tuple
    """``(model_config, speculative)`` for one /chat turn: the model tier its
    body names for the turn's autogen agents (the speculative dispatcher's
    expert reroute and dispatch.py send one) and whether to hand the turn to
    the speculative dispatcher's background expert.  A turn that keeps to
    this node (``local_only``) gets neither: ``(None, False)``.  Otherwise
    the body's own values, as /chat read them before (absent: None, False).
    Never raises."""
    if local_only or not isinstance(body, dict):
        return None, False
    return body.get('model_config', None), body.get('speculative', False)
