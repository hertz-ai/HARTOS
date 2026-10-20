# McGroce agentic commerce in HARTOS (WP-H)

Code: `integrations/commerce/`, `integrations/ap2/ap2_mandate.py`,
`integrations/ap2/ap2_protocol.py`.
Tests: `tests/unit/test_commerce_{client,tools,api,wiring,signing}.py`,
`tests/unit/test_ap2_{mandate,protocol_fixes}.py`,
`tests/unit/test_approval_ap2_branch.py`, `tests/unit/test_phonepe_callback_kinds.py`.

## Configuration (env first, via `core.config_cache.get_secret`)

| Variable | Meaning |
|---|---|
| `MCGROCE_API_URL` | McGroce site REST base, e.g. `https://shop.example/api/v1`. https only (plain http only for a loopback host with `MCGROCE_ALLOW_HTTP=1`) |
| `MCGROCE_API_USER` / `MCGROCE_API_PASSWORD` | McGroce service account (HTTP Basic). Never reaches a browser |
| `MCGROCE_ADMIN_API_URL` / `MCGROCE_ADMIN_API_KEY` | McGroce admin API + Bearer key, for merchant / product creation |
| `COMMERCE_SESSION_SECRET` | Shared with McGroce (`hartos.commerce.secret`); 32+ random bytes. It never travels: both sides derive a request key and a mandate key from it (`integrations/commerce/signing.py`) |
| `MCGROCE_ORIGINS` | McGroce storefront origins, joined to the one CORS allowlist (`security.middleware.cors_allowed_origins`) |
| `AP2_ALLOW_LLM_AUTHORIZE` | Default off. On, `authorize_payment` authorizes directly (still never as `system` or the requesting agent) |

Payments use `PaymentLedger.select_gateway(currency)`: INR goes to PhonePe if live, else Stripe, else Mock. A Mock choice settles nothing for a shopper: `commerce_prepare_checkout` refuses before any approval card is shown, and `settle_checkout` refuses and cancels the payment, unless the node is in dev mode (`ap2_protocol.mock_would_settle`, the one spelling the paid-tier upgrade route, the cart settler and the generic mandate settler all read). A node with no gateway keys therefore cannot place a McGroce order "paid" with no money taken; `ap2_mandate.settle` applies the same rule to every other mandate kind (any agent's `request_payment`), and each refusal logs which setting is missing for the operator.

## Session contract (McGroce `SpaAgentTokenEndpoint` → HARTOS)

```
POST {hartos}/api/commerce/session
X-Commerce-Timestamp: <unix seconds>
X-Commerce-Nonce: <32 hex, never reused>
X-Commerce-Signature: hex HMAC-SHA256(request key, "POST\n/api/commerce/session\n<ts>\n<nonce>\n" + hex sha256(body))
{"customerId": 4242, "username": "asha@example.com", "role": "customer", "storeId": 7}
→ 200 {"token": "<HARTOS JWT, tid=mcgroce>", "expiresIn": 3600, "userId": "mcg-4242"}
   X-Commerce-Signature: hex HMAC-SHA256(request key, "RESP\n<nonce>\n" + hex sha256(answer body))
```

- **Keys:** request key = HMAC-SHA256(secret, `mcgroce-hartos/request/v1`). The route signed is the declared one (`/api/commerce/session`), whatever prefix a proxy adds.
- **Errors:** 403 for a missing or wrong signature, a timestamp more than 300 s off, a nonce already used, or a request signed before this process started (a restart empties the nonce memory). The plain `X-Commerce-Secret` header is refused. An unconfigured secret returns 503; a signed body that is not a JSON object returns 400.
- **Answer:** McGroce uses the token only when the answer's signature matches its own nonce.
- **Binding:** the call writes the binding `mcg-4242` → McGroce customer 4242. Merchants map to `mcg-m-<id>`, and unsafe ids are hashed.
- **Customer id:** it always comes from that binding (`customerId` header), never from a tool argument.
- **The token:** it is an ordinary HARTOS access JWT, so the SPA/embed sends it as `Bearer` to `POST /chat` (`prompt_id` `mcgroce_shopper` / `mcgroce_merchant`) and to `/api/agent/approval`.

## Order contract (HARTOS → McGroce, after the money is taken)

```
POST {MCGROCE_API_URL}/cart/checkout/ap2         service account Basic + customerId (from the binding)
X-Commerce-Mandate: B64 "." hex HMAC-SHA256(mandate key, B64)
   B64 = unpadded base64url of compact sorted-key JSON:
   {"v":1,"mid":"mdt_…","pid":"pay_…","cid":"4242","oid":"555",
    "lines":[["11",2,"40.00"],["12",1,"160.00"]],"amt":"240.00","cur":"INR",
    "st":"consumed","iat":…,"exp":iat+120,"gw":"stripe","txn":"…"}
→ 200 {"orderNumber": "…"}
```

- **Mandate key:** HMAC-SHA256(secret, `mcgroce-hartos/mandate/v1`), so a request signature can never pass as an attestation.
- **McGroce checks, before it records anything:** the MAC (constant time), `v`, `st`, `exp`; `cid` is the cart's customer and `oid` the cart; the repriced cart's SKUs and quantities equal `lines`; its total equals `amt` and its currency `cur` exactly. Each mandate becomes one order at most: a mandate this customer's submitted order already carries is answered with that order (200), so HARTOS's retry after a lost answer never places a second one.
- **HARTOS retries** once at once on a transport error or 5xx, and again when `commerce_checkout` is asked; a 4xx is not retried and the payment is flagged `needs_attention`. Nothing is refunded automatically (the ledger has no refund step yet).
- **Fixed vectors** for the request MAC, the answer MAC and an attestation are in `tests/unit/test_commerce_signing.py`; McGroce's tests carry the same.

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
- **Payments:** `ap2_mandate.decide_payment` approves the CartMandate and **settles it at once**. McGroce checkout's settler re-hashes the live cart against the approved one, takes the payment, consumes the mandate, and asks McGroce to place the order with a signed attestation of it (order contract below). HARTOS is the only payer and the only order placer.
- **One live checkout per shopper:** `commerce_prepare_checkout` withdraws the shopper's earlier pending or approved checkouts (`MandateStore.supersede`), so two approval cards for one cart can never both be paid. It refuses outright while an earlier payment is with the gateway, or when this same cart is already paid for and its order is not placed yet.
- **Paid but not ordered:** `commerce_checkout` on a consumed mandate retries only the order step (`retry_order`); it never charges again, and refuses if the live cart is no longer the one paid for.
- **Redirect gateways:** PhonePe settles later, in its S2S callback.
- **Drafts:** merchant and product drafts are submitted to the McGroce admin API only on approval.

An agent can never authorize a payment:
- `authorize_payment` only shows the card.
- `PaymentLedger.authorize_payment` refuses `system`/empty/agent ids and the requesting agent itself.
- `commerce_checkout` refuses anything the owner has not approved.
