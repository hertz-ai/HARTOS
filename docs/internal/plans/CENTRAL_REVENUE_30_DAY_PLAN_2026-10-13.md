# Central revenue: the 30-day plan (2026-10-13 to 2026-11-12)

One funnel, made real end to end, then fed. Everything below is either
**measured** (with the time), **read** in code, or marked **not checked**.
This repository is public, so this file names no internal hosts, ports,
private addresses or operator paths (only the public product hosts and the
two model-provider hosts), and states each open security or billing weakness
in one line with its task number, never as steps; the detail lives in the
owner's runbooks and the session notes.

Companion documents, not repeated here except for the numbers section 0
needs: `docs/plans/REVENUE_LANES_PLAN_2026-10-12.md` (the lane policy, the
journeys, and the **authoritative owner-actions list**: key rotation for the
GPT3 config, the gpt3 redeploy and `kong_onboard --llm`, `COMMERCE_SESSION_SECRET`
and the McGroce settings, pointing the LLM, `OPENAI_BASE_URL` and
`HEVOLVE_LOCAL_LLM_URL` at Kong, payment links and DNS; it still says $6 a day
for Claude in three places, the configured value is 3.30), `deploy/go_live_stripe.md`
(Stripe steps), `deploy/shortest_path_to_first_9.md` (payment links without any
backend), `deploy/verify_revenue_rails.sh` (the existing read-only probe of the
revenue stack), and `docs/internal/verification/USER_JOURNEYS_LIVE_2026-10-13.md`
(what works today, measured).

## 0. Where central is today (measured 2026-10-13, 03:00 to 06:05 UTC, or read in code where a row says so)

| Piece | State |
|---|---|
| Central build | 903cd58c (deployed 04:17:49 UTC), `/status` running (measured) |
| Configured LLM | `openrouter/free` (owner's pick). **The OpenRouter account is free tier with a 50-request-per-day cap** (read from its key endpoint at 05:40 UTC, and a plain request answered 429 "free-models-per-day"). The new container's log (the deploy recreates the container, so it starts at 04:17) shows its first 429 at 04:20 UTC; by 05:40 that log held 22 x HTTP 400, 18 x HTTP 429 and 0 x HTTP 200 outbound. The 400 is "Reasoning is mandatory for this endpoint and cannot be disabled": the router's picks for our request shape include a reasoning-only model, which rejects the thinking-off field we send; 1 of 8 direct probes without tools got it, and all 4 REUSE turns (05:19 to 05:20 UTC, after the quota ran out) got it, so whether tools make it certain is not isolated (task #115). All measured. |
| Public API host | `https://api.hevolve.ai` reaches central: `/status` 200, `/api/social/auth/login` answered by HARTOS, the public feed read 200. **`/api/v1/intelligence/*` has no Kong route** (404 "no Route matched"). `/chat` on that host is captured by an unrelated OAuth2-gated route and never reaches HARTOS (task #119). `hevolve.ai` serves the web app, and also HARTOS's `/prompts` and `/prompts/public`. All measured. |
| Paid API behaviour | A customer question returns agent-building text: 12 chat/analyze probes were saved on 2026-10-13, 11 with their reply text kept (3 at 03:25 UTC on the previous build e829b072f, whose metered path is identical; 3 at 04:20, 2 at 05:00 and 3 at 06:00 on 903cd58c): 9 came back as agent-building text, 1 as the "internal snag" sentence (04:20) and 1 as the prompt echoed (03:25), each `success: true` and counted as a call (every saved key was free tier, so cost 0.0: counted, not charged); the 12th, at 03:20 on e829b072f, got a 3-word reply the probe did not keep (not checked). Measured. Fixed in local commits faf7c21a5 + e71ce92e0 + b73892b1b (the last APPROVED), not yet deployed. |
| Pricing page | `hevolve.ai/pricing` serves bundle `main.8db4b110.js`; across all 137 deployed JS files there are 0 occurrences of `upgrade/checkout`, `api.hevolve.ai`, `intelligence/keys` or `upgrade-success` (measured). The page with the real flow is `Nunba-HART-Companion/landing-page/src/pages/CommercialApiPricing.js` (default base `https://api.hevolve.ai`) plus `UpgradeSuccess.js` (read). Which service serves the public web app behind Kong's root route: not checked. |
| Payments | `STRIPE_API_KEY` unset (measured): `select_gateway('USD')` is the mock, which customers are refused (503, measured). PhonePe routes exist (`/keys/<id>/upgrade/phonepe`, `/phonepe/callback`) and answer 503 "not registered" without `PHONEPE_MERCHANT_ID` + `PHONEPE_SALT_KEY` (measured). **There is no Stripe webhook (read); the PhonePe redirect and callback defaults point at `hevolve.ai` (read), the web-app host, which answers those paths with a 404 (measured), so a buyer can be charged and never upgraded** (task #122). `ap2_protocol.py:610` names a `/phonepe/refund-callback` route that exists nowhere (read; folded into #122). |
| Identity | `HEVOLVE_API_KEY` unset (measured): a paying customer's turn runs as `system_daemon`; with the conversation fix that also means one shared memory buffer (task #118). |
| Rate limits | Task #123 (read, not measured; the metered self-POSTs share one `/chat` bucket by reading). |
| Admin surface | The channels admin API accepted any registered user's token on central (measured); fixed in 8ae03de71 + 70d65db24, which lead this chain and are not yet deployed (task #121). Writes were not exercised against production. |
| Expert tier | Endpoint (api.anthropic.com), model `claude-sonnet-5-5`, cap 3.30 USD/day set; `HEVOLVE_EXPERT_LLM_API_KEY` unset (measured), so no revenue turn uses Claude yet. The budget gate fails closed (read). The 2026-10-12 ruling said no more than $6 a day and then chose 3.30; the code default is still 6 (`budget_gate.py:644`, read). |
| Marketing | `HEVOLVE_AUTONOMOUS_MARKETING=1` deployed (measured). The standing `public_exposure` consent for the system identities has existed since 2026-07-21 (measured). 0 channels are configured on central (measured), so nothing posts externally; 4 daemon goals are active (two of them named in the journey record's section 7) and one system user posts on-platform. |
| Reporting | `revenue_tools.register_revenue_tools` defines `get_api_revenue_stats` as a closure (an autogen tool, not reachable from outside the registration), and `revenue_aggregator.query_revenue_streams(db, period_days)` exists and writes nothing (read). The `usage/admin` URL named in `deploy/go_live_stripe.md` Step 7 and `deploy/enable_revenue_rails.md:172` exists in no code, and the "nightly settlement" the same docs promise does not exist either (read; W1-8 fixes the docs, W3-5 builds the payout). |
| Billing honesty | A failed turn (the "internal snag" sentinel) is counted as a call and reported `success: true` (cost 0.0 on the free keys measured; a paid key accrues cost per token, whether those credits are invoiced: not checked); hivemind (`result: null`) and generate (`status: unavailable`) are counted too (task #110, measured). Fixed in local commit 2efb73519 and its review follow-ups (the latest under review), not yet deployed. |

## 0b. The product is the Hive API, and it is not wired to the hive yet (measured 04:50 to 05:40 UTC, or read where a row says so)

The owner's standard: people do not want just an LLM API. What the code calls
the commercial or intelligence API is the entry to the hive and is sold as
the Hive API. Today:

| Hive leg | State |
|---|---|
| Who answers a paid call | Central's own /chat, central's configured model. The metered reply carries only `success`, `response` and `usage` (read); the `served_by: cloud` evidence comes from direct `/chat` turns (measured). No peer served any turn. |
| `GET /api/v1/intelligence/hivemind` | `{"result": null, "success": true, "usage": {"compute_ms": ...}}`, HTTP 200, counted as a call (measured). Central's boot log says the hevolve core is not installed (measured). Broken (task #129); the billing half is task #110. |
| Peers central knows | `GET /api/social/peers`: itself plus two flat desktops on the owner's LAN (measured). No remote hive node. |
| Hive delegation | Measured submit-only on 2026-10-09: the result does not come back into the turn. |
| Listed agents | `GET /prompts/public` lists 4 agents, all local files on central (61 prompt files on the node; 3 of the 4 listed have recipes); none is a system agent (measured). REUSE of two of them enters Reuse Mode and fails on the router's 400 (measured, above). |
| Agent-less turn | `get_tools(is_first=False)`: the default tool set, which includes `Create_Agent` and `Agentic_Router` (read; so a paid turn can still become an agent interview, a Review Mode agent saved under the customer's id, or a Plan Mode consent question the API cannot answer, task #120; withheld from the metered turn in local commit 5a66a3308 and its review follow-up, not yet deployed); never the casual set, and not the draft unless the node's env forces `HEVOLVE_DRAFT_FIRST=1` (e71ce92e0; the deploy scripts do not set it, read). |

Of the four legs, this month plans one: a listed agent runs by id through the
Hive API (W1-9). The other three are NOT planned for this month: two carry
task numbers instead of steps, the hive leg returning answers into the turn
(task #128) and hivemind backed by a real core on central (task #129; the
core is the owner's closed-source build and is not installed there); the
third, serving nodes beyond the two LAN desktops, has neither a task nor a
step (the channels of W2-2 and W2-3 are the only growth lever planned). Until
then a paid call is answered by
central's own /chat and its configured model, and the plan says so.

## 1. The money path (the only funnel this month)

```
post or link  ->  hevolve.ai/pricing  ->  sign up  ->  free key
  ->  first call ANSWERS  ->  "Choose Starter"  ->  Stripe Checkout (hosted)
  ->  Stripe webhook or /upgrade/checkout/complete  ->  key tier = starter/pro
  ->  AP2 ledger row  ->  revenue-lane turns (Claude under the 3.30/day cap)
  ->  90/9/1 payout (W3-5 builds it: nothing pays out customer revenue today)
```

Each arrow is a step below with a check. A step without its check is not done.

## 2. Week 1 (Oct 13 to 19): a model that answers, the funnel's plumbing, the first dollar by our own hand

Order matters: W1-0 and W1-4 before anything is exposed. The admin-gate
commits (8ae03de71, 70d65db24) lead the local chain and do not depend on
W1-4, so they can be pushed first on the owner's word.

| # | Who | Step | Check | Expected | Tolerance |
|---|---|---|---|---|---|
| W1-0 | owner | Decide the model (task #115): add $10 OpenRouter credits (free cap 1000/day), or pin a free non-reasoning model under 50/day. A paid pin as the node's default (`qwen/qwen3.8-27b`, ~$0.0012 per turn measured 10-12) would put every person's turn on a paid model, which the 10-12 ruling allows only where revenue is earned; the revenue lane already has its paid model (the expert), so a paid default is not an option here. Eng then ships the openrouter.ai fallback list (W1-7) so a 400 or 429 falls to the next model. | the tools probe on the dev box (13 probes with a tool schema) and REUSE of agent 66722327257 ("Hive Model Trainer Incremental Improvement" in the public listing) | answers on every probe; a real REUSE reply | 0 x 400, 0 x 429 within the chosen cap |
| W1-4 | owner | Set `HEVOLVE_API_KEY` (any long random value) and `HEVOLVE_EXPERT_LLM_API_KEY` (the Anthropic key funded by the startup credits), both as repo secrets: the deploy workflow writes them into the node's env on every deploy. Do NOT set `TRUSTED_PROXY`: see task #123. | a paid call's `api_...` request id in the container log | the customer's own user id; the expert call goes to api.anthropic.com; a USD row in the budget ledger | cap 3.30 holds |
| W1-1 | eng | Push: the admin-gate pair now, on the owner's word, BY SHA (`git push origin 70d65db24:main`), because the deploy workflow runs on every push to main with no path filter and the conversation fix (faf7c21a5 + e71ce92e0 + b73892b1b) sits above the pair in the chain; that fix, this plan, the #110 fix and the #120 fix (5a66a3308 and its review follow-up) go together after their reviews and AFTER W1-4 is live. The pair's gate keys on `HEVOLVE_NODE_TIER`, the same env the network gate reads; whether that counts as a node-tier branch under the 10-12 ruling is the owner's call. | the pair: a plain user's JWT on `GET /api/admin/channels` after the deploy; the fix: `probe_metered.sh` (the persistent validation account and its one key, on the dev box, against the LAN node) | 403 for the plain user, 200 for the operator; real answers to the three questions | 0 plain-user 200s; 0 agent-building replies in 3 |
| W1-2 | eng | Kong route for the Hive API: `paths=['/api/v1/intelligence']`, `strip_path=false`, service `hartos-social`, no host restriction; added to `integrations/gateway/kong_onboard.py` as an onboarding function and applied against Kong's admin API on the box (the lanes plan has the flag). **Two preconditions**: W1-4 (before the key, the public route would put one shared identity and memory on the internet, and `/analyze` takes the first 5,000 characters of any document) and W1-2a below. Needs the owner's OK: it exposes the paid API. | `curl https://api.hevolve.ai/api/v1/intelligence/pricing` from outside the LAN | the four tiers | 200 |
| W1-2a | eng | The per-customer `/chat` bucket for metered calls (the metered half of task #123, pulled out of W2-0 into week 1): today every metered self-POST comes from the loopback address and shares one 30-per-minute `/chat` bucket, so one free key could push paying calls into 503s, and the 10-12 ruling says revenue service never breaks. Key the bucket by the customer named in the body, trusted only when the request carries the node's own credential (`core.request_lane.is_internal_caller`), in the one place `/chat` keys its limiter. | two paid keys driven at once on a dev node while a free key hammers | both paid keys answered | 0 x 503 on a paid key |
| W1-3 | owner | `STRIPE_API_KEY` as a repo secret (the deploy already carries this name into the node's env): the test key first, the live key once W1-5's test-mode run passes and before its live-mode run. The boot log line "Stripe gateway auto-registered" may not print on central (its sibling INFO line never has), so the check is the route, not the log. | an upgrade attempt | the 503 "No live USD payment gateway" is gone | 0 upgrade attempts answered by the mock or by that 503 |
| W1-5 | eng + owner | First dollar, by our own hand: the owner signs up, gets a free key, `POST /keys/<id>/upgrade/checkout {"target_tier":"starter"}`, pays on the Stripe test page, then **POSTs `/upgrade/checkout/complete {"session_id"}` by hand** (no webhook and no success page yet, task #122). Once in live mode with a real card, refunded in the dashboard; a dashboard refund leaves the key at starter and the AP2 row COMPLETED, so note the refund for W2-4 and W3-5 or reverse it by hand. | the AP2 ledger and the key row | one COMPLETED tier_upgrade request for 9 USD, key `tier=starter`, limits bumped | 0 DB edits |
| W1-6 | eng | Task #110: the metered endpoints must not bill or say `success` for the failure sentinel, for `hivemind` returning null, or for `generate` returning unavailable; they already have the honest 503 path for transport errors. Done locally on 2026-10-13 (the commit after this plan in the chain, `tests/unit/test_metered_failed_turn_is_not_billed.py`), not yet deployed. | that test file + a live failed turn after deploy | billing row status != 200, `success: false`, 0 billable tokens | 0 billed failed calls |
| W1-7 | eng | Task #115 code side: for host openrouter.ai send OpenRouter's `models` fallback list with `route: fallback` so the classifier and reasoning-only picks never win, and a 429 on one model falls to the next. | the tools probe above | answers on every probe | 0 classifier verdicts, 0 400s |
| W1-8 | eng | Point every doc and default at the API host and the real routes: `docs/developer/user-journey.md` (lines 20 and 25 also use `/api/social/register` and `/login`; the routes are `/api/social/auth/register` and `/auth/login`, so its own curl fails at step 1), `docs/developer/widget-embed.md` (lines 148, 165, 212, 217, 222; 212 and 217 have the same route bug), Nunba's `docs/reference/commercial-api.md`, the `usage/admin` URL in `deploy/go_live_stripe.md` and `deploy/enable_revenue_rails.md`, the "nightly settlement" those same docs promise (`go_live_stripe.md` lines 247 and 254, `shortest_path_to_first_9.md:147`, `enable_revenue_rails.md:191`: no such payout exists, see W3-5; the comment in `complete_upgrade_checkout` in `commercial_api.py` makes the same false claim, that `settle_metered_api_costs` reads the ledger row), the PhonePe redirect/callback defaults in `commercial_api.py` and `ap2_protocol.py` (task #122), and the dangling refund-callback name at `ap2_protocol.py:610`. | the curl in each doc, copy-pasted | a key | 0 curls in the docs that fail against the API host |
| W1-9 | eng | Hive API, first leg: an optional `agent_id` on `POST /api/v1/intelligence/chat` selects a LISTED public agent on the node and the self-POST carries it as `prompt_id` (REUSE; the cloud pull-on-demand fetches the recipe bundle when the node lacks it); an id that is not listed answers 404, never CREATE. The metered reply gains `served_by` and `agent_status` (additive) so the check below can read them. | a listed agent's id through the validation key | the recipe's answer; `agent_status` Reuse Mode | 0 CREATE turns from the Hive API, incl. the model picking `Create_Agent` (task #120) |

End of week 1: WE can buy Starter at `api.hevolve.ai` with a manual completion
call, and the key answers questions. A stranger cannot yet: that needs W2-1.

## 3. Week 2 (Oct 20 to 26): a buyer who never saw curl, and the first outside buyers

| # | Who | Step | Check | Expected | Tolerance |
|---|---|---|---|---|---|
| W2-0 | eng | Tasks #123 (its public half; the metered half is W1-2's precondition) and #125 and #127 first: measure the client address central sees (the docstring in `core/auth_local.py` says how) and whether the gateway can see the real one; then put per-client limits for register, login and the Hive API where the real address is known. Measure #125 on a dev node with two users and fix it the way `/chat` ties the body to the token; scope the goals listing (#127) by the same owner rule the single-goal route already applies. | two external clients' register buckets; user A with user B's id; user A listing goals | independent buckets; the cross-user write refused; only A's goals | 0 shared buckets, 0 cross-user writes, 0 foreign goals listed |
| W2-1 | eng | Put the checkout page on hevolve.ai: find which service serves the public web app (not checked), deploy the landing-page build that carries `CommercialApiPricing.js` and `UpgradeSuccess.js`, and add the Stripe webhook (`checkout.session.completed` calling the same completion code as `/upgrade/checkout/complete`). | "Choose Starter" on hevolve.ai/pricing with a test card | Stripe page, redirect to `/upgrade-success`, tier bumped with no manual call | 0 manual steps; the tier bumped within 60 s of the payment |
| W2-2 | owner + eng | Pick two token-only external channels (Telegram or Discord: their only credential is the bot token, `_ENV_FALLBACKS` in `integrations/channels/flask_integration.py`; `_CHANNEL_SPECS` lists seventeen channels that need more than a token, Slack, Matrix and Twitter among them, read) and put the tokens where central's boot reads them: as named repo secrets (`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`) carried into the node's env through the deploy's three env-name places (the workflow's `env:` block, its `envs:` list, and the loop in `deploy/deepbox_deploy.sh`; the same route W3-1 uses for `PHONEPE_*`, so eng adds the names first), never through the one multi-line OAuth secret the deploy writes verbatim (re-entering it to add a line risks the OAuth client lines it already holds). A deploy must run before boot registers them: central's entry point calls the channel integration's `start()`, which registers every env-credential channel and whose loop thread starts the adapters (read); that survives a redeploy, which recreates the container. The channels admin API is NOT the path on central: its activate route registers an adapter without starting it, the admin-config loop that would start it at boot runs only in the bundled desktop's bootstrap, and the stored token would be plain text (read). Before the bot exists: the channel send route accepts any registered user's token once a channel is registered (task #132), to be gated first; and task #130, the marketing goal's `post_to_channel` builds an extension adapter with no configuration and never consults the live registry, so no configured channel can carry its post until that is fixed. The owner also names the target chat or channel the bot posts to, and knows that a live bot answers anyone's direct messages with turns on central's model. Names only in any log. The browser poster needs a desktop browser and is not for central. | one external post carrying the buy link, made by the marketing goal through the boot-registered adapter | the post exists; the consent gate line is in the log | 0 posts without the consent gate line |
| W2-3 | owner | Post the launch copy on your own handles (the templates are in an untracked file on the dev box). Until W2-1 lands the link is the static buy page from `deploy/shortest_path_to_first_9.md`, whose payment links must be minted and whose page must be hosted first; a buyer through it gets a key delivered by hand, which is why the Oct 26 target below reads "no manual step" only for the W2-1 path. | Stripe dashboard | first payment from someone who is not us | a target, not a tolerance: 1 or more by Oct 26 |
| W2-4 | eng | Daily revenue line to the owner: lift `get_api_revenue_stats` to a module-level function (the autogen tool wraps it; today it is a closure, not reachable from outside the tool registration) and run it with `query_revenue_streams(db, 30)` at 09:00 IST through the existing notification path: keys by tier and calls (minus the validation key and its calls, by key id), paid USD minus refunds, Claude USD that day, sign-ups and pricing-page hits. | the message arrives | one line a day, numbers match the DB | 0 missed days once live; 0 mismatches against a manual DB count |
| W2-5 | eng | Task #111: when the hosted model dies (429 or 400), `/status` says degraded and the metered path answers the honest 503, not "running" plus sentinels. | point the model at a dead slug on a dev node | `/status` degraded, calls 503, nothing billed | 0 billed calls while degraded |

End of week 2: at least one payment from outside the company (target, not a
forecast), and a daily number nobody has to ask for.

## 4. Week 3 (Oct 27 to Nov 2): second rail, commerce lane, conversion

| # | Who | Step | Check | Expected | Tolerance |
|---|---|---|---|---|---|
| W3-1 | owner + eng | Decide PhonePe (INR). The routes exist; what is missing is the merchant account (`PHONEPE_MERCHANT_ID`, `PHONEPE_SALT_KEY`, `PHONEPE_SALT_INDEX`, `PHONEPE_ENV`), the callback host fix from W1-8, and an eng step first: the deploy carries a fixed list of env names into the node, in three places (the workflow's `env:` block, its `envs:` list, and the loop in `deploy/deepbox_deploy.sh`), and no `PHONEPE_*` name is in any of them, so a repo secret alone reaches nothing; add the four names in all three, then the owner sets the secrets. There is no PhonePe button on the public page today, so nothing to hide if the answer is no. | an INR upgrade end to end in the sandbox | callback reaches HARTOS, tier bumped | 0 charged-not-upgraded in the sandbox |
| W3-2 | eng | Task #113: Stripe mandates attach no payment method, so a McGroce cart on a Stripe node always fails. Either attach a payment method (SetupIntent) in the mandate flow, or keep the cart lane off until the first merchant exists (owner's merchant-fee decision in the lanes plan). | a cart checkout on a dev node with a test card | charged, or the lane clearly off | 0 carts that "settle" with no money taken |
| W3-3 | eng | Upgrade nudges: at 80 of the free tier's 100 daily calls, the existing notification path tells the user what Starter costs, with the checkout link. | a free key driven to 80 calls | one nudge, once | exactly 1 per key per day |
| W3-4 | owner + eng | Enterprise: the `sales` goal type exists (CRM and outreach tools). Ten target companies; it drafts, a human sends. | ten drafts in the CRM | ten, reviewed before sending | 0 sent without a human's review |
| W3-5 | eng | Build the payout of paid revenue; none exists today (read): `query_revenue_streams` computes the 90/9/1 shares of usage credits and ad Spark and writes nothing, `settle_metered_api_costs` (run by the daemon) pays compute operators Spark for cost-recovery rows and never reads the AP2 ledger, and `distribute_ad_revenue` has no caller; the "nightly settlement" the deploy docs promise is one of W1-8's corrections. The step: read COMPLETED `tier_upgrade` payments from the AP2 ledger (what `get_api_revenue_stats` already reads), split each in USD with the ONE existing helper `commercial_api._revenue_split_usd` (90% to the contributors' pool, 9% to the infrastructure pool, 1% to central, the constants `query_revenue_streams` uses; no second split), write USD payout rows per pool (the existing reward path pays Spark and is not this), exclude refunds, and run it once on real revenue. Who inside the contributors' pool receives what is the contribution-score rule and is not this step. | the payout rows against the ledger | the three pools' USD for every COMPLETED upgrade, refunds excluded | to the cent |

## 5. Week 4 (Nov 3 to 12): keep what converts, keep Claude under revenue

No new steps are planned for week 4 in advance; it is a weekly review, every
Monday, from the daily lines: sign-ups, free keys, calls per day, paid
conversions, MRR; Claude USD per day against paid USD per day (rule of the
house: Claude only where it earns and never its own blocker; proposal: raise
`HEVOLVE_CLAUDE_BUDGET_USD_DAY` only while the revenue lane's Claude spend is
under half of paid revenue, lower it the week that stops being true);
failed-turn rate (task #110), router bad-turn rate (task #115), and the one
thing a buyer asked for that we do not have. Each action the review picks
gets its own check, expected value and tolerance when it is written down.

## 6. Owner inputs with dates

This table orders the inputs this plan needs; the full owner-actions list
(key rotation, gpt3, Kong for the LLM, McGroce, payment links, DNS) is in the
lanes plan and is not repeated here.

| Needed by | Input |
|---|---|
| Oct 14 | "push" for the admin-gate pair (no dependency on keys) |
| Oct 14 | model decision (task #115) |
| Oct 14 | `HEVOLVE_API_KEY`, `HEVOLVE_EXPERT_LLM_API_KEY` |
| Oct 15 | "push" for the conversation fix after its reviews and W1-4 |
| Oct 15 | `STRIPE_API_KEY` (test, then live) |
| Oct 16 | OK for the Kong route on `api.hevolve.ai` (after W1-4) and for untangling `/chat` (task #119) |
| Oct 20 | two marketing channels and their bot tokens, as named deploy secrets (W2-2) |
| Oct 20 | your own launch posts |
| Oct 27 | PhonePe yes or no (if yes: the four `PHONEPE_*` secrets after eng adds their names to the deploy, W3-1); merchant fee yes or no |
| weekly | the Monday review |

## 7. The ten-minute daily check on central

`deploy/verify_revenue_rails.sh` is the existing read-only probe of the
revenue stack: container, volume mount, payment env vars, `/status`, the
pricing tiers, the two PhonePe routes and the ledger file (its `--remote`
target predates the `api.hevolve.ai` host). Extend it with the steps below
rather than adding a second script, after fixing it: its probes 1, 2 and 5
call its own `run_remote` shell function from a child `bash -c`, which cannot
see an unexported function, so those three always fail and the script always
exits 1 (read). Until it is extended (by Oct 26, with W2-4) the probe scripts
live on the dev box. `probe_metered.sh` targets the LAN node (the paid paths
have no public route yet) and uses the persistent validation account and its
one key: it creates no accounts and no keys after its first run, so it does
not inflate the funnel counts; it does make three metered calls a day on that
key, which W2-4 excludes by key id.

1. the container is up on the current sha (the deploy workflow's log, or the box);
2. `curl -s https://api.hevolve.ai/status` says running;
3. `curl -s https://api.hevolve.ai/api/v1/intelligence/pricing` returns the four tiers (after W1-2);
4. `probe_metered.sh`: three real answers;
5. the outbound LLM status counts for the last 24 h in the container log
   (central logs little below WARNING after boot, so count the httpx lines,
   which are INFO and do appear): dominated by 200.

Anything else is the day's first task.

## 8. Targets (targets, not forecasts)

| By | Target |
|---|---|
| Oct 19 | first $9 taken and refunded by us, with the manual completion call |
| Oct 26 | first payment from outside the company through the W2-1 flow, no manual step |
| Nov 2 | 5 paid keys |
| Nov 12 | 10 paid keys; Claude spend under half of paid revenue; the daily line has run 20 days in a row |
