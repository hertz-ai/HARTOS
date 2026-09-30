# McGroce commerce (WP-H): open follow-ups

Companion to [COMMERCE_MCGROCE.md](COMMERCE_MCGROCE.md). This is what the WP-H work
(PR #125, branch `mcgroce-agentic-commerce`) did **not** finish. Nothing here
is a claim that the item is broken in production; each entry says what is known
and what was never measured.

Legend:
- **P1**: a correctness or security gap in shipped code.
- **P2**: needed before a live McGroce launch.
- **P3**: hardening or nice-to-have.

## 1. Gaps in the shipped code

| # | Pri | Item | Where | What to do |
|---|---|---|---|---|
| 1.1 | P1 | The generic AP2 `process_payment` tool can charge ANY `AUTHORIZED` payment by id. This includes a McGroce checkout mandate whose settlement was refused because the cart changed after approval (1.2). Money would move with no McGroce order placed. | `integrations/ap2/ap2_protocol.py` `create_payment_processing_function` | Refuse payments that carry a `mandate_id` in metadata. Those settle only through `ap2_mandate.settle` or `decide_payment`. Add a test. |
| 1.2 | P1 | Approving a checkout whose cart changed since approval leaves the payment `AUTHORIZED` (nothing charged, shopper told to re-approve). It is cancelled only when the mandate is next read after its 15-minute TTL (`_expire_if_due` is lazy). There is no sweeper. | `integrations/ap2/ap2_mandate.py`, `integrations/commerce/commerce_tools.py` `settle_checkout` | On a hash mismatch, reject the mandate and cancel the payment at once. Also add a periodic expiry sweep, or expire on ledger load. |
| 1.3 | P2 | Any signed-in user can file a merchant store draft (`commerce_onboard_merchant` has no merchant-role check). Only the drafter can approve it. | `commerce_tools.commerce_onboard_merchant` | Decide the policy: require a McGroce merchant binding, or rate-limit per user. |
| 1.4 | P2 | Commerce PhonePe payments carry no `redirect_url` / `callback_url` in metadata, so PhonePe falls back to the hevolve.ai intelligence routes baked into `PhonePePaymentGateway`. | `ap2_mandate.create_cart_mandate` metadata | Add config (for example `COMMERCE_PHONEPE_REDIRECT_URL`) pointing back at the McGroce SPA. The callback already completes commerce payments. |
| 1.5 | P2 | Bindings, mandates and drafts are JSON files under `agent_data/`. Each process keeps an in-memory copy, so with more than one server worker the copies diverge. Old records are never pruned. | `commerce/bindings.py`, `ap2/ap2_mandate.py`, `commerce/drafts.py` | Run the commerce gateway single-process, or move these to the DB. Add pruning. |
| 1.6 | P2 | Possible double delivery on the Nunba web. `main` now registers a headless `LiquidUIService` in the backend, so `push_agent_ui` sends a card via `agent.ui.update` (SSE) **and** via per-user `chat.social`. Not measured. | `liquid_ui_service.push_agent_ui` | Check which events the Nunba Demopage and the embed subscribe to, and drop the redundant leg. |
| 1.7 | P3 | The intent cap is whatever the model passes to `commerce_prepare_checkout(cap=...)`. There is no standing, user-set spend limit. | `ap2_mandate.IntentMandate` | Optional: a user-owned intent mandate written from the SPA and checked at prepare time. |
| 1.8 | P3 | `/chat` is rate-limited at 30 requests/min **per IP** (per hartos_commerce.md §5; not re-measured here). Shoppers behind one NAT (or an SPA gateway) share that budget. | `integrations.social.rate_limiter` | Key the limit on the JWT user for commerce tokens. |

## 2. Planned but not implemented

| # | Source | Item | Why not / status |
|---|---|---|---|
| 2.1 | PLAN §5.2 `commerce_checkout` | McGroce `POST /api/v1/spa/checkout/ap2` is not used. HARTOS records the payment through the Basic API (`POST /cart/checkout/payment` with `referenceNumber` = mandate id, then `POST /cart/checkout`). | The `/spa/**` chain is session-authenticated only; HARTOS calls server-to-server with Basic auth. So the spa endpoint's `amount == cart.total` 409 guard does not protect this path, and McGroce must accept a `THIRD_PARTY_ACCOUNT` / `Passthrough` payment there. Unverified (see 3.1). |
| 2.2 | hartos_commerce §7.2 | `commerce_voice_order` (audio upload to `/audioorder/upload`) | Not in PLAN §5.2's binding tool list; not built. Voice uses the existing whisper path. |
| 2.3 | rest_api §2 | Setting the cart's store from an agent | McGroce has no REST endpoint for it (`PUT /spa/cart/store` is session-only). The store comes only from the binding written at `/api/commerce/session`. |
| 2.4 | PLAN §6 (WP-I) | Adding `integrations.commerce` to Nunba `scripts/setup_freeze_nunba.py` `packages[]` (Gate 6) | That file belongs to WP-I in the Nunba repo. Without it, the frozen desktop build raises `ModuleNotFoundError` for the commerce package on first boot. |
| 2.5 | Owner decision | Commerce tools on the channel runtime (`reuse_recipe` time_agent path) | Deliberately not registered: anyone who can message the bot on Discord, Telegram and similar reaches that runtime. Needs an owner ruling. |
| 2.6 | rest_api §1 | Guest or anonymous carts | McGroce REST requires a customer; not supported. |

## 3. Could not be done in the build environment

| # | Item |
|---|---|
| 3.1 | No live McGroce (Tomcat, Solr, MySQL) was reachable. Unverified:<br>• every endpoint path and response shape (`OrderWrapper`, `Money`, `ErrorWrapper`)<br>• the query-param binding of `OrderPaymentWrapper`<br>• the https redirect<br>• the G1 415 behaviour on GET without a JSON Content-Type |
| 3.2 | No Stripe or PhonePe credentials: only the Mock gateway and test doubles ran. The PhonePe callback was exercised with a signature double. |
| 3.3 | No LLM: no real `/chat` turn ran with the commerce tools. Tag detection, registration and schema were tested; an agent actually choosing the tools was not. |
| 3.4 | Android rendering (`Hevolve_React_Native` `AgentOverlayBridge`) was not read or run. Fragment prop names follow PLAN §11 as written. |
| 3.5 | Nunba web LiquidUI and the `hart-agent` embed were not run. WAMP/Crossbar delivery was mocked (`publish_event`). |
| 3.6 | The `hartos` and `trueflow` MCP servers failed to connect in the session, so they were not used. |
| 3.7 | The admin API (WP-G) that `onboard_merchant` / `create_product` call did not exist yet. Payload shapes (`displayName`, `deliveryRadiusKm`, ...) are HARTOS's proposal and must be matched on the McGroce side. |

## 4. PR #125 housekeeping

- The PR description still describes the other session's first version (`search_products`, `tests/unit/test_commerce.py`, HMAC-signed approval records). The merged code is described in `COMMERCE_MCGROCE.md` and in merge commit `18eb79e`'s message. Update the description before review.
- PR #125 conflicts with `main` (174 commits ahead) in `liquid_ui_service.agent_ui_update`. `main` independently added the same `user_id` parameter; take main's hunk and docstring.
- `tests/unit/test_approval_ap2_branch.py` imports the real entry app, which sets process-wide logging (root INFO, a handler, `RequestLogRecord`). Run in one interpreter before `test_bind_game_sound.py`, the still-running case fails with `StopIteration`. Its module fixture should restore the root level, handlers and record factory afterwards. CI runs one interpreter per file, so it is not red there.
