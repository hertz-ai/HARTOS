# McGroce commerce (WP-H): open follow-ups

Companion to [COMMERCE_MCGROCE.md](COMMERCE_MCGROCE.md).  This file records
what WP-H has NOT done yet, as of branch `mcgroce-agentic-commerce` at merge
`18eb79e` (the reconciliation of the two parallel WP-H implementations).
Delete an entry when the change that closes it lands.

## Resolved by the reconciliation merge (kept for the record)

| Earlier gap | How the merge closed it |
|---|---|
| McGroce REST paths were guesses (`/agent/*`, `/search/{q}`) | Client now calls the real McGroce REST: `/cart`, `/cart/items/{id}`, `/cart/checkout/payment`, `/catalog/search?q=` |
| `/api/commerce/session` request body unknown (six accepted aliases) | Contract fixed to `{customerId, username, role, storeId}` returning a HARTOS JWT (`tid=mcgroce`) + `userId` |
| Commerce payments stuck on the mock gateway (PhonePe is two-step) | `PaymentLedger.select_gateway`; PhonePe redirect + S2S callback completes commerce payments |
| Backend `/api/agent/approval` commerce branch only compile-checked | `tests/unit/test_approval_ap2_branch.py` drives the real `hart_intelligence_entry` app |

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
3. **Cross-check against the McGroce plan.**  This session could not read
   `hertz-ai/McGroce` `doc/agentic-commerce/PLAN.md`, `hartos_commerce.md` or
   `SpaAgentTokenEndpoint.java` (repository access was not granted).  The
   reconciliation merge was written against them; an independent read-through
   of §5 / §8 WP-H / §10 / §11 against the merged code is still worth doing.

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

### Pre-existing failures (not caused by WP-H, not fixed)

6. These fail identically on `main` without WP-H (measured on this branch
   and on a stash of it, same 6 in the same targeted run):
   - `tests/unit/test_eventbus_wamp_tts.py::TestLifecycleEventEmission::test_auto_sync_emits_event`
   - `tests/unit/test_phase4_layer_shell_host.py::TestPhase4NixosTest::test_has_a_fresh_gtk4_paint_proof_node`
   - `tests/unit/test_shell_dismiss_sheets.py::test_sheets_dismiss_through_one_set_js`
   - `tests/unit/test_shell_poll_diet.py::test_shell_poll_diet_js`
   - `tests/unit/test_ws12_security_wiring.py::TestAuditLogAppInstaller::test_audit_log_called_on_successful_install` (order-dependent)
   - `tests/integration/shell_surface/test_flow_01_boot_and_first_paint.py::test_02_first_paint_serves_theme_tokens_and_gpu_floor` (order-dependent)
