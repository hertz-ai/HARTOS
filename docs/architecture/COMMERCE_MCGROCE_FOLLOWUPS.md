# McGroce commerce (WP-H): open follow-ups

Companion to [COMMERCE_MCGROCE.md](COMMERCE_MCGROCE.md).  This file records
what WP-H has NOT done yet, as of branch `mcgroce-agentic-commerce` after
merge `18eb79e` (the reconciliation of the two parallel WP-H implementations)
and merge `cf93e99` (main brought in).
Delete an entry when the change that closes it lands.

## Resolved by the reconciliation merge (kept for the record)

| Earlier gap | How the merge closed it |
|---|---|
| McGroce REST paths were guesses (`/agent/*`, `/search/{q}`) | Client now calls the real McGroce REST: `/cart`, `/cart/items/{id}`, `/cart/checkout/payment`, `/catalog/search?q=` |
| `/api/commerce/session` request body unknown (six accepted aliases) | Contract fixed to `{customerId, username, role, storeId}` returning a HARTOS JWT (`tid=mcgroce`) + `userId` |
| Commerce payments stuck on the mock gateway (PhonePe is two-step) | `PaymentLedger.select_gateway`; PhonePe redirect + S2S callback completes commerce payments |
| Backend `/api/agent/approval` commerce branch only compile-checked | `tests/unit/test_approval_ap2_branch.py` drives the real `hart_intelligence_entry` app |
| PR #125 conflicted with `main` in `liquid_ui_service.agent_ui_update` (main independently added the same `user_id` parameter) | `cf93e99`: took main's hunk and docstring, dropped the branch's duplicate paragraph |
| `test_approval_ap2_branch.py` importing the entry app left root logging at INFO, with its handler and `RequestLogRecord`; `test_bind_game_sound` run after it in one interpreter failed (`StopIteration`) | `cf93e99`: the module fixture restores root level, handlers and record factory |
| `process_payment` could charge a checkout mandate outside its settler (P1) | The tool refuses any payment whose metadata carries a `mandate_id`; only `ap2_mandate.settle` / `decide_payment` take it |
| A refused settlement left the payment `AUTHORIZED`; expiry was lazy only (P1) | `MandateStore.withdraw` rejects the mandate and cancels the payment on a cart-hash mismatch; `sweep_expired` runs on every new mandate |

## Open

### Needs another repository (cannot be done from HARTOS)

1. **Nunba freeze list (CLAUDE.md Gate 6).**  Add the new modules to
   `Nunba-HART-Companion/scripts/setup_freeze_nunba.py` `packages[]`:
   `integrations.commerce` (`__init__`, `approvals`, `bindings`,
   `commerce_api`, `commerce_tools`, `drafts`, `mcgroce_client`) and
   `integrations.ap2.ap2_mandate`.  Without it the installed Nunba build hits
   `ModuleNotFoundError` the first time a commerce or AP2-mandate import runs.
2. **Possible duplicate card in bundled Nunba.**  `push_agent_ui` sends a
   card on the shell leg (`agent.ui.update`, which also reaches SSE) AND on
   the per-user leg (`chat.social`, SSE event `chat.social`).  Whether
   Nunba's web LiquidUI renders both was not checked (Nunba source not in
   this session).  Fix on the Nunba side (dedupe on `_agent_id` + `_ts`) or
   drop one leg when the shell leg delivered.
3. **Cross-check against the McGroce plan.**  The session that wrote the
   reconciliation merge read `hertz-ai/McGroce` `doc/agentic-commerce/PLAN.md`
   and `hartos_commerce.md` in full, and `rest_api.md` in part.
   `SpaAgentTokenEndpoint.java` and the McGroce admin API (WP-G) were not
   read, so the `/api/commerce/session` body and the merchant/product
   payloads (`displayName`, `deliveryRadiusKm`, ...) are HARTOS's proposal
   and must be matched on the McGroce side.  An independent read-through of
   §5 / §8 WP-H / §10 / §11 against the merged code is still worth doing.

### HARTOS

4. **`/api/commerce/session` from another machine on a bundled node.**  With
   `NUNBA_BUNDLED` set, `security/middleware.check_api_auth` admits only local
   callers and exempt prefixes without a credential, and `/api/commerce/` is
   not exempt.  The McGroce site's server-to-server call carries
   `X-Commerce-Secret`, not a Bearer, so it gets 401 on a bundled desktop.
   The cloud gateway (not bundled) is unaffected.  Decide: exempt
   `/api/commerce/session` (it authenticates itself with the shared secret)
   or document that McGroce only ever talks to a gateway node.
5. **Tier-2 registration not driven end to end.**  `register_commerce_tools`
   is tested directly and `detect_goal_tags` is tested for the `commerce`
   tag, but no test runs `create_agents` / `create_agents_for_user` and asserts
   the commerce tools land on the agent.

8. **Merchant drafts are open to any signed-in user.**
   `commerce_onboard_merchant` has no merchant-role check (only the drafter
   can approve).  Decide: require a McGroce merchant binding, or rate-limit.
9. **PhonePe return URLs for commerce.**  Commerce payments carry no
   `redirect_url` / `callback_url` in metadata, so `PhonePePaymentGateway`
   falls back to the hevolve.ai intelligence routes.  Add config pointing the
   redirect back at the McGroce SPA.  The S2S callback already completes
   commerce payments.
10. **Single-process state.**  Bindings, mandates and drafts are JSON files
    under `agent_data/` with an in-memory copy per process.  With more than
    one server worker the copies diverge.  Old records are never pruned.
    Run the commerce gateway single-process, or move them to the DB.
11. **Intent cap is model-supplied.**  `commerce_prepare_checkout(cap=...)`
    takes whatever the model passes.  There is no standing, user-set spend
    limit (`IntentMandate` is recorded, not user-owned).
12. **`/chat` rate limit is per IP** (30/min, per hartos_commerce.md §5; not
    re-measured).  Shoppers behind one NAT or gateway share it.  Key it on the
    JWT user for commerce tokens.

### Planned but not implemented

13. **McGroce `POST /api/v1/spa/checkout/ap2` is not used.**  HARTOS records
    the payment server-to-server on the Basic API (`POST
    /cart/checkout/payment`, `referenceNumber` = mandate id, then `POST
    /cart/checkout`), because the `/spa/**` chain is session-authenticated
    only.  The spa endpoint's `amount == cart.total` 409 guard therefore does
    not protect this path, and McGroce must accept a `THIRD_PARTY_ACCOUNT` /
    `Passthrough` payment there.  Unverified.
14. **`commerce_voice_order`** (audio to `/audioorder/upload`,
    hartos_commerce.md §7.2) was not built.  It is not in PLAN §5.2's tool
    list; voice uses the existing whisper path.
15. **No way for an agent to set the cart's store.**  McGroce has no REST
    endpoint for it (`PUT /spa/cart/store` is session-only).  The store comes
    only from the binding written at `/api/commerce/session`.
16. **Commerce tools on the channel runtime** (the `reuse_recipe` time_agent
    path) were deliberately not registered.  Anyone who can message the bot on
    Discord, Telegram and similar reaches that runtime.  Owner decision needed.
17. **Guest or anonymous carts** are not supported (McGroce REST requires a
    customer).

### Could not be done in the build environment

18. No live McGroce (Tomcat, Solr, MySQL).  Unverified: endpoint paths and
    response shapes (`OrderWrapper`, `Money`, `ErrorWrapper`), the
    query-param binding of `OrderPaymentWrapper`, the https redirect, and the
    415 on GET without a JSON Content-Type.
19. No Stripe or PhonePe credentials: only the Mock gateway and test doubles
    ran.  The PhonePe callback ran against a signature double.
20. No LLM: no real `/chat` turn chose the commerce tools.  Tag detection,
    registration and schema are tested.
21. Android `AgentOverlayBridge` (Hevolve_React_Native) was not read or run.
    Fragment prop names follow PLAN §11 as written.  Nunba web LiquidUI and
    `<hart-agent>` were not run, and WAMP delivery (`publish_event`) was
    mocked.

### Pre-existing failures (not caused by WP-H, not fixed)

22. These fail identically on `main` without WP-H (measured on this branch
   and on a stash of it, same 6 in the same targeted run):
   - `tests/unit/test_eventbus_wamp_tts.py::TestLifecycleEventEmission::test_auto_sync_emits_event`
   - `tests/unit/test_phase4_layer_shell_host.py::TestPhase4NixosTest::test_has_a_fresh_gtk4_paint_proof_node`
   - `tests/unit/test_shell_dismiss_sheets.py::test_sheets_dismiss_through_one_set_js`
   - `tests/unit/test_shell_poll_diet.py::test_shell_poll_diet_js`
   - `tests/unit/test_ws12_security_wiring.py::TestAuditLogAppInstaller::test_audit_log_called_on_successful_install` (order-dependent)
   - `tests/integration/shell_surface/test_flow_01_boot_and_first_paint.py::test_02_first_paint_serves_theme_tokens_and_gpu_floor` (order-dependent)
