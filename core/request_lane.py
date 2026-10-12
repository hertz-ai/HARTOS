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

``register_chat_lane`` is /chat's ONE registration decision (review of
7086adff8): the node's own daemon turn is left to the prefix rule and to the
lane dispatch_goal registered for a goal that earns, and every other caller
is registered by this rule, OVERWRITING whatever a request id already held.
That is what stops a stranger from inheriting a goal's revenue lane by naming
its request id.

Pure: the JWT decoder, the tenant name, the system identities and the
genuine-user rule are passed in by the caller (hart_intelligence_entry's
/chat), so core imports nothing from integrations and the rule is testable
without the app.  The lane names are the ones core.llm_outbound_logger stamps
on the wire.
"""
from core.llm_outbound_logger import LANE_REVENUE, LANE_USER, register_request_lane


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
    """/chat's one registration decision.  Returns the lane registered, or
    None when the turn is the node's own daemon turn (left to the prefix
    rule and to dispatch_goal's registration)."""
    payload = _payload(bearer_token, decode_jwt)
    internal = is_internal_caller(auth_source, payload, system_identities)
    genuine = True
    if is_genuine is not None:
        try:
            genuine = bool(is_genuine(request_id))
        except Exception:
            genuine = True
    if internal and not genuine:
        return None
    lane = lane_for_chat(bearer_token, body, auth_source, decode_jwt=decode_jwt,
                         tenant=tenant, system_identities=system_identities,
                         jwt_payload=payload)
    register_request_lane(request_id, lane)
    return lane
