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

Pure: the JWT decoder, the tenant name, the system identities and the
genuine-user rule are passed in by the caller (hart_intelligence_entry's
/chat), so core imports nothing from integrations and the rule is testable
without the app.  The lane names are the ones core.llm_outbound_logger stamps
on the wire.
"""
from core.llm_outbound_logger import (
    LANE_REVENUE, LANE_USER, lane_for_request, register_request_lane,
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
                       tenant=None, system_identities=(), is_genuine=None):
    # type: (...) -> object
    """/chat's one registration decision.  Returns the lane the turn runs on
    when this call decided or confirmed it, or None when the turn is left to
    the prefix rule and to earlier registrations."""
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
        return None                                   # the node's own daemon turn
    claim = body.get('lane') if isinstance(body, dict) else None
    if internal and not claim:
        return lane_for_request(request_id) or None   # the node's own inner call
    lane = lane_for_chat(bearer_token, body, auth_source, decode_jwt=decode_jwt,
                         tenant=tenant, system_identities=system_identities,
                         jwt_payload=payload)
    register_request_lane(request_id, lane)
    return lane
