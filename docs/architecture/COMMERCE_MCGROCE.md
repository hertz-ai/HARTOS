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
- **Nothing is charged for an order that cannot be placed:** before the shopper is asked to approve, and again right before the money moves, `_why_no_order` checks the shared secret, the shopper's link, the McGroce cart id, a SKU on every line, and that McGroce takes agent orders at all (an unsigned `POST /cart/checkout/ap2` must be refused with 403 `AP2_MANDATE_REJECTED`; today's McGroce answers 404). McGroce is asked each time, never remembered, so a McGroce rolled back after the card was shown is not paid. An approval card shown by an older build names no cart and is never paid; the shopper approves again.
- **One cart, one payment:** each mandate records and signs the McGroce cart id it pays for (`cart_ref`). `commerce_prepare_checkout` withdraws the shopper's earlier pending or approved checkouts (`MandateStore.supersede`, voiding one only when its payment really was cancelled or had already ended with nothing taken, such as a declined card) and refuses while an earlier payment is being taken or is with the gateway, or while this cart, whatever it holds now, was paid for and has no order. A payment whose money was taken but whose order is not placed holds up its own cart and nothing else, and is never voided. A PhonePe capture of a different amount is FAILED with the `money_taken` flag (`ap2_protocol.MONEY_TAKEN`): it counts as paid, never as a declined card, so its cart is not offered for a second payment, before or after the mandate's TTL. `MandateStore.begin_settle` applies the same rule at the moment of payment, so two approvals that raced past prepare never both charge.
- **Paid but not ordered:** `commerce_checkout` on a consumed mandate retries only the order step (`retry_order`); it never charges again, and places the order only while the live cart is the same cart with the same contents. When it is not (changed, another cart, or gone: McGroce checks a cart out when it places an order, so this is also what an order whose answer was lost, or whose record was not written, looks like), McGroce is asked first whether the mandate already became an order, with a **probe** attestation naming the paid cart and no lines (`mandate_attestation_for` with no view, `_order_for_mandate`): McGroce answers a mandate that one of the customer's placed orders carries with that order before any cart check (`Ap2CheckoutServiceImpl.completeAp2`), and refuses an attestation without lines for any other mandate (`Ap2MandateVerifierImpl`: "items"), so a probe can never place an order. Only a refused probe holds the payment; a probe McGroce does not answer holds it without a mark of its own. On an approved mandate whose payment completed but whose order step never ran (a callback that died midway) it runs that step now. One order step per mandate runs at a time (`begin_settle`), so a retry racing the callback is told it is in progress and sends no second order call. A mandate consumed by an older build (no `cart_ref`) is never retried: its order may exist.
- **Redirect gateways:** PhonePe settles later, in its S2S callback. The money is taken, so the callback uses the mandate up first, then runs the same order step as `retry_order`: the order goes in only for the cart the approval named, still holding what was approved. Otherwise (McGroce down, the cart changed, a card approved by an older build, a node that cannot sign its records) the order is **held** through the one hold rule, `commerce_tools.order_on_hold`, and `commerce_checkout` retries it later without a new charge. The rule, for every held payment whatever the cause: the shopper's notice is built from the payment's own ledger record (no mandate store, so a node that cannot read its key still tells them) with the real amounts ("We received ₹X but your order comes to ₹Y" when PhonePe gave its figure, "a different amount" when the ledger only knows that one was taken), and this node's owner (`HEVOLVE_OWNER_USER_ID`, the same identity every other ask of the machine goes to) is told what a person must do: the payment, the shopper, the amounts, the cause and the next step (place the order, or refund it from the operator's merchant account on that rail; nothing is refunded automatically). Both notices are notification rows (`NotificationService`, type `held_payment`: the bell on every client of this node, kept across a restart) for whoever is a user of this node, with the live card pushed on top; a McGroce-only shopper (`mcg-<id>`, bound but with no user row here) gets the live card alone. One row per person, payment and sentence: a retry that holds again for the same reason adds nothing to the bell (the agent is told to retry, so holds repeat), the live card may repeat, and a new reason is a new row. The owner counts as told only when their row was written, which needs the configured owner to be a user of this node (central's owner id had no user row on 2026-10-19, so a card pushed to it reached nobody); the shopper's sentence ends "we will sort it out with you" only then, otherwise "reply here or contact the shop". Whether an order call left this node decides the shopper's sentence, from the code path and never from an error text: after a call McGroce neither did nor refused (timeout, transport error, 5xx: the client's `unconfirmed` mark on either of the two attempts; or an order step that raised midway) the shopper hears "we could not confirm your order" and the owner's notice says to check McGroce for the order before refunding; "not placed yet" is said only when no call ever left for this mandate (the cart could not be read or had changed, McGroce refused with a 4xx, the record failed its signature, the key pause). That mark outlives the attempt: it is kept on the mandate, signed (`CartMandate.order_sent`, `MandateStore.record_order_sent`), so every later hold for the mandate says "could not confirm" and sends a person to McGroce first, whether that later attempt sent nothing or its probe was refused, until McGroce answers with the order; a probe that goes unanswered sets no mark, since it can place nothing. The hold is raised at the failure site (`retry_order`, `_submit_order`, `_order_for_mandate`, `_consume_paid`), so the card path (a Stripe approval settled at once) holds the same way as the PhonePe callback. An order McGroce placed that this node could not record (the key stopped reading in between) is an order, never a hold: the shopper gets its tracking and the next retry's probe finds the same order at McGroce. McGroce keeps one open cart per shopper until an order is placed, so the held cart is the shopper's live cart: when it changed while they paid, the notice tells them to put it back as approved and check out, which places the order with the payment already made. A new cart (once that one is ordered another way) is not held up. The PhonePe callback answers 200 even when its order step fails or raises: the money is recorded, a 5xx would only redeliver into the same failure, and the shopper and owner have been told (`complete_redirect_checkout` answers anything raised mid-step as a hold itself; the callback's own catch is the net behind it).
- **Records are checked before they are signed:** an order is attested only for a mandate whose own signature holds and whose ledger payment took exactly its amount and currency, and a mandate edited outside the store is never consumed or re-signed, expiry included. When a paid mandate's record fails its signature the shopper is told the order is on hold, the cause is logged for a person, and only that cart waits. The node's mandate key (`.ap2_mandate_key` beside the mandate file) is one key per node, read as it is on disk, created once and never replaced, because a replaced key would leave every paid record unverifiable. Creation runs under an OS lock on `.ap2_mandate_key.lock` (one writer at a time across threads and processes, so two workers at first boot end with one key), writes a temp file beside it, fsyncs, swaps it in with `os.replace` and reads it back with the one parser; a key re-saved by an editor (BOM, CRLF) is still read. A key that cannot be read or saved, or a non-empty file that holds no key, raises `MandateKeyError` and nothing is signed; a store that cannot sign refuses before it touches the ledger or a record. Where no money has moved (the approval answer, `commerce_prepare_checkout`, a checkout before the payment is taken, `GET /api/commerce/mandates/<id>`) the answer is `ap2_mandate.PAYMENTS_PAUSED`, "nothing was charged", as a 503 with the card left open. Where money HAS moved, the raise site cannot know but the catcher can, from the payment's own record (`ap2_protocol.money_taken`, the one reading): `commerce_checkout` and the callback's order step then go through the hold rule above, never the pause sentence, and the order is placed once the key reads again. The cause is logged for a person in both cases. Whether a file that holds no key should instead mint a fresh key is the owner's open call.
- **Drafts:** merchant and product drafts are submitted to the McGroce admin API only on approval.

An agent can never authorize a payment:
- `authorize_payment` only shows the card.
- `PaymentLedger.authorize_payment` refuses `system`/empty/agent ids and the requesting agent itself.
- `commerce_checkout` refuses anything the owner has not approved.
