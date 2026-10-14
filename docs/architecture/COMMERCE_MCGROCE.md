# McGroce agentic commerce in HARTOS (WP-H)

Code: `integrations/commerce/`, `integrations/ap2/ap2_mandate.py`,
`integrations/ap2/ap2_protocol.py`.
Tests: `tests/unit/test_commerce_{client,tools,api,wiring}.py`,
`tests/unit/test_ap2_{mandate,protocol_fixes}.py`,
`tests/unit/test_approval_ap2_branch.py`, `tests/unit/test_phonepe_callback_kinds.py`.

## Configuration (env first, via `core.config_cache.get_secret`)

| Variable | Meaning |
|---|---|
| `MCGROCE_API_URL` | McGroce site REST base, e.g. `https://shop.example/api/v1`. https only (plain http only for a loopback host with `MCGROCE_ALLOW_HTTP=1`) |
| `MCGROCE_API_USER` / `MCGROCE_API_PASSWORD` | McGroce service account (HTTP Basic). Never reaches a browser |
| `MCGROCE_ADMIN_API_URL` / `MCGROCE_ADMIN_API_KEY` | McGroce admin API + Bearer key, for merchant / product creation |
| `COMMERCE_SESSION_SECRET` | Shared with McGroce's `SpaAgentTokenEndpoint` (`X-Commerce-Secret`) |
| `MCGROCE_ORIGINS` | McGroce storefront origins, joined to the one CORS allowlist (`security.middleware.cors_allowed_origins`) |
| `AP2_ALLOW_LLM_AUTHORIZE` | Default off. On, `authorize_payment` authorizes directly (still never as `system` or the requesting agent) |

Payments use `PaymentLedger.select_gateway(currency)`: INR goes to PhonePe if live, else Stripe, else Mock.

## Session contract (McGroce `SpaAgentTokenEndpoint` → HARTOS)

```
POST {hartos}/api/commerce/session
X-Commerce-Secret: <COMMERCE_SESSION_SECRET>
{"customerId": 4242, "username": "asha@example.com", "role": "customer", "storeId": 7}
→ 200 {"token": "<HARTOS JWT, tid=mcgroce>", "expiresIn": 3600, "userId": "mcg-4242"}
```

- **Errors:** a wrong or missing secret returns 403. An unconfigured secret returns 503.
- **Binding:** the call writes the binding `mcg-4242` → McGroce customer 4242. Merchants map to `mcg-m-<id>`, and unsafe ids are hashed.
- **Customer id:** it always comes from that binding (`customerId` header), never from a tool argument.
- **The token:** it is an ordinary HARTOS access JWT, so the SPA/embed sends it as `Bearer` to `POST /chat` (`prompt_id` `mcgroce_shopper` / `mcgroce_merchant`) and to `/api/agent/approval`.

## Live cards

Every card goes through `liquid_ui_service.push_agent_ui(agent_id, card, user_id)`:

1. **Shell leg.** When this process serves the desktop shell, the card goes through `LiquidUIService.agent_ui_update`, which applies the allowlist, kill switch, rate cap and XSS gate. It is then published as `agent.ui.update`, which Android's `AgentOverlayBridge` receives.
2. **Per-user leg.** When a user is named, the card is also sent as `publish_event('chat.social', {"type": "agent_ui_update", ...}, user_id)`. That reaches WAMP `com.hertzai.hevolve.social.<user_id>`, and in bundled Nunba the per-user SSE event `chat.social`.

`GET /api/commerce/stream` (Bearer) returns these names for the caller.

Only the existing fragment types are emitted: `product_card` (`image` and `image_url`), `cart`, `checkout`, `approval`, `payment_status`, `order_tracking`, `form`, `list`, `notification`. Currency is INR.

## Approvals

Cards carry one of three actions:
- `ap2_pay:<payment_id>`
- `merchant_onboard:<draft_id>`
- `merchant_sku:<draft_id>`

The person's answer goes to `/api/agent/approval`, on the backend or on the shell, with their Bearer token. Both routes call `integrations.commerce.approvals.answer_commerce_approval`:
- **Approver:** the token's identity (`approver_from_request`), never a body field.
- **Ownership:** only the owner may answer.
- **Payments:** `ap2_mandate.decide_payment` approves the CartMandate and **settles it at once**. McGroce checkout's settler re-hashes the live cart against the approved one, takes the payment, records it on the McGroce cart (`referenceNumber` = mandate id), and submits the order.
- **Redirect gateways:** PhonePe settles later, in its S2S callback.
- **Drafts:** merchant and product drafts are submitted to the McGroce admin API only on approval.

An agent can never authorize a payment:
- `authorize_payment` only shows the card.
- `PaymentLedger.authorize_payment` refuses `system`/empty/agent ids and the requesting agent itself.
- `commerce_checkout` refuses anything the owner has not approved.
