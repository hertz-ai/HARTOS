# McGroce agentic commerce in HARTOS (WP-H)

Code: `integrations/commerce/`, `integrations/ap2/ap2_mandate.py`.
Tests: `tests/unit/test_commerce.py`, `tests/unit/test_commerce_wiring.py`,
`tests/unit/test_ap2_mandate.py`.

## Configuration (env first, via `core.config_cache.get_secret`)

| Variable | Meaning |
|---|---|
| `MCGROCE_API_URL` | McGroce site API base URL (e.g. `https://shop.example/api/v1`) |
| `COMMERCE_SESSION_SECRET` | Shared with McGroce. McGroce sends it as `X-Commerce-Secret`; HARTOS sends it back on its calls to McGroce |
| `COMMERCE_SESSION_TTL` | Session token lifetime in seconds (default 3600, min 60) |
| `COMMERCE_PAYMENT_GATEWAY` | AP2 gateway for orders (`mock` default; used only if registered on the ledger) |
| `MCGROCE_ORIGINS` | Comma-separated McGroce storefront origins, added to the one CORS allowlist (`security/middleware.cors_allowed_origins`) |

## Session contract (McGroce `SpaAgentTokenEndpoint` → HARTOS)

```
POST {hartos}/api/commerce/session
X-Commerce-Secret: <COMMERCE_SESSION_SECRET>
{"customerId": "<id>"}          # also accepted: customer_id, userId, user_id, username, email
→ 200 {"token": "<jwt>", "expiresIn": 3600}
```

Wrong or missing secret gives 403. No secret configured on HARTOS gives 503. The token names the
identity `mcg-<id>` (`bindings.mcgroce_identity`) and is signed with a key
derived from the shared secret, so it is never valid as a HARTOS login.

## Live cards: what the embed subscribes to

No new stream. Every commerce card goes through
`liquid_ui_service.push_agent_ui(agent_id, card, user_id)`:

1. When the process serves the desktop shell: `LiquidUIService.agent_ui_update`,
   the shell SSE (`/api/notifications/stream` on :6800), and
   `agent.ui.update` (Android `AgentOverlayBridge`).
2. Always, per user: `publish_event('chat.social', {"type": "agent_ui_update",
   "agent_id": ..., "component": {...}}, user_id)`.

**The embed subscribes to WAMP `com.hertzai.hevolve.social.<user_id>`**, the
per-user topic web and React Native already use, and keeps messages whose
`type == "agent_ui_update"`. In bundled Nunba, the same message arrives as the
per-user SSE event `chat.social`. `GET /api/commerce/stream` (Bearer) returns
these names for the caller's identity.

## Approvals

Cards carry `action = "ap2_pay:<payment_id>"` or
`"merchant_onboard:<request_id>"`. The user's answer is posted to
`/api/agent/approval` with `Authorization: Bearer <commerce token or HARTOS
login>`. The approver is that token's identity, never a body field, and only
the owner of the payment or request may decide it. Both the backend route and
the shell route call `commerce_api.handle_commerce_approval`.

An agent can never authorize a payment. `authorize_payment` and `checkout` only request
approval. `PaymentLedger.authorize_payment` refuses a mandate-bearing payment
without the owner's HMAC-signed approval, and only `ap2_mandate.decide_payment`
writes that approval.
