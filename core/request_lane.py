"""Which lane a /chat turn runs on, decided from what the request proves.

Owner policy 2026-10-12: a person's conversation runs on the free model
unless it is revenue-bearing; a revenue-bearing turn may use the expert
model and is never throttled or budget-blocked, because it pays for itself.

Two facts make a turn revenue-bearing, and /chat can verify both itself:

  1. The caller's JWT carries the commerce tenant claim (``tid``): a McGroce
     shopper or store owner whose token POST /api/commerce/session minted.
  2. The metered API endpoints' own self-POST to /chat says ``lane: revenue``
     for a paying tier (commercial_api._metered_backend_chat), AND the
     request authenticated with the node's own key (``auth_source ==
     'api_key'``), which no outside client holds.  The same word in a body
     that arrived over a JWT or body auth is just a word.

Everything else is a user turn.  The daemon lane is not decided here: the
request id's ``daemon_`` prefix decides it on the wire
(dispatch.is_genuine_user_request), the rule every other caller uses.

Pure: the JWT decoder and the tenant name are passed in by the caller
(hart_intelligence_entry's /chat), so core imports nothing from integrations
and the rule is testable without the app.  The lane names are the ones
core.llm_outbound_logger stamps on the wire.
"""
from core.llm_outbound_logger import LANE_REVENUE, LANE_USER


def lane_for_chat(bearer_token, body, auth_source, decode_jwt=None, tenant=None):
    # type: (str, object, object, object, object) -> str
    """'revenue' or 'user' for one /chat request; never raises."""
    if bearer_token and decode_jwt is not None and tenant:
        try:
            payload = decode_jwt(bearer_token) or {}
            if isinstance(payload, dict) and payload.get('tid') == tenant:
                return LANE_REVENUE
        except Exception:
            pass
    if (isinstance(body, dict) and body.get('lane') == LANE_REVENUE
            and auth_source == 'api_key'):
        return LANE_REVENUE
    return LANE_USER
