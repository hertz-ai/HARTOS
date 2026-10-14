# User journeys on central, driven live (2026-10-13, 03:20 to 06:05 UTC)

Owner's ask: prove each user, seller, buyer, searcher, admin and driver
journey across RideSnap, McGroce, HARTOS agents, the admin UI, the listings,
the auto news channels and the auto research paper listings.

This is the record behind rows that belong in `docs/VERIFICATION.md` (its
rule: a verified negative is worth as much as a pass). This repository is
public, so the record names no internal hosts, ports, private addresses or
operator paths (only the public product hosts and the two model-provider
hosts), and states each open security or billing weakness in one line with
its task number, never as steps. The matching plan is
`docs/internal/plans/CENTRAL_REVENUE_30_DAY_PLAN_2026-10-13.md`.

## Method

Central = the HARTOS container on the deploy box, build 903cd58c (deployed
04:17:49 UTC). The only turns in this record whose model answer was kept are
the three direct answers saved at 03:27 UTC (section 1), BEFORE that deploy,
on the previous build e829b072f. Twelve chat/analyze probes were saved; the 11
whose reply text was kept never got a model answer on either build: at 03:25
(e829b072f) two came back as agent-building text and one as the prompt
echoed; at 04:20, 05:00 and 06:00 (903cd58c) seven came back as
agent-building text and one as the "internal snag" sentence (the metered
path is identical in both builds, read). The 12th, at 03:20 on e829b072f in
the validation run, answered 200 with a 3-word reply the probe did not keep
(none of the canned texts is 3 words), so it is not checked and may have
been a model answer. By this record's own count (section 8: 0 x HTTP 200 from
OpenRouter from the container start at 04:17 to the last count at 05:40) no
turn between the deploy and 05:40 was answered through OpenRouter (whether a
second endpoint answered any: not checked, section 8). Central was reached on
the LAN node and,
where Kong routes it, on the public host `https://api.hevolve.ai`. Surfaces
were driven with plain HTTP by throwaway accounts (register is limited to 3
per hour per client key, which ended one run; what a client key is on central
is task #123); the daemon side was read from
central's own SQLite, read-only. The probe scripts and raw outputs live on
the dev box, not in this repository. Every row carries **measured** (an HTTP
response or a DB row read back), **read** (code), or **not checked**.

**Residue left on production by the day's probes (owner's call to remove):**
throwaway users `lanesval_20261013032019` (03:20), two `routerval_*` (03:25,
04:20), the `probe_direct` user (03:27), `journeys_20261013050012` and `j2_*`
(05:00 to 05:02; `j3_*`, `j4_*`, `j5_*` failed to register), and the
persistent account the daily check uses (05:18, name kept out of this record
because it holds a long-lived key); one free API key per account that created one
(lanesval, both routerval, journeys, and that persistent account's single
key); one public post titled
"journey probe" (top of the trending feed at 05:20 UTC); one half-created
agent (`prompt_id` 91867635923, Creation Mode, no recipe).

Verdict legend: WORKS (the user gets the result), BROKEN (the surface answers
but the result is wrong), BLOCKED (needs a key, a route or a decision),
GAP (a security or correctness hole), NO SURFACE, NOT CHECKED. "LAN" in a
verdict means it was measured on the LAN node only, not on the public host.

## 1. Person (web or app user)

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Register, log in | `POST /api/social/auth/register`, `/login` | account created, JWT issued (the probe script kept only the body; 201 and 200 by reading `integrations/social/api.py`) | measured (bodies), read (codes) | WORKS, LAN |
| Who am I | `GET /api/social/auth/me` | 200, profile | measured | WORKS, LAN |
| Read the feed | `GET /api/social/feed/all` on LAN and on `api.hevolve.ai` | 200 both | measured | WORKS, public too |
| Post and read back | `POST /api/social/posts`, `GET /posts/<id>` | created, read back with the post (201 by reading) | measured, read | WORKS, LAN |
| Chat with no agent | `POST /chat` prompt_id null, LAN | 3 turns at 03:27 UTC answered ("Paris is the capital of France." twice, a correct `def square` function), the texts kept by the probe; one more at 05:00 UTC whose reply text the probe did not keep (its body shows 0 response tokens and no intent). A 03:20 turn in the validation run was sent with a made-up `prompt_id` and `casual_conv` true, so it was not an agent-less turn; its reply was not kept either | measured (3), not checked (2) | WORKS 3 of 3 saved, LAN, all before the 04:17 deploy; see section 8 for the model after 04:20 |
| Chat on the public host | `POST https://api.hevolve.ai/chat` with the same JWT | 401 `invalid_token` from Kong's OAuth2 gate: an unrelated route on the same path wins over HARTOS's | measured | BROKEN, task #119 |
| Create an agent | `POST /chat` create_agent true, 05:00 UTC, LAN | 200, `Agent_status: Creation Mode`, prompt_id 91867635923 minted; the reply text was NOT captured (the probe cut the body before the `response` field), and after 04:17 central's OpenRouter model answered nothing (section 8), so whether the user got an interview question or a failure sentence is unknown | measured (status), not checked (the reply) | NOT CHECKED: a status field is not the outcome the user sees |

## 2. Buyer (Hive API customer)

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Pricing | `GET /api/v1/intelligence/pricing` | LAN 200 (free 0 / 100 a day, starter 9 / 1,000, pro 49 / 10,000, enterprise 499 / 100,000, USD, same as `TIER_CONFIG`); `api.hevolve.ai` 404 "no Route matched" | measured | BLOCKED publicly: no Kong route (plan W1-2) |
| Free key | `POST /api/v1/intelligence/keys` (JWT), LAN | key issued, raw key once (201 by reading) | measured, read | WORKS, LAN |
| Ask a question | `POST /api/v1/intelligence/chat` (key), LAN | 200, "I have the name and goal, but not every step came through as a step I can build..." | measured | BROKEN: the self-POST named an agent that does not exist, /chat answered with CREATE mode. Fix chain faf7c21a5 + e71ce92e0 + b73892b1b (APPROVED), local (task #114) |
| Analyze a document | `POST /api/v1/intelligence/analyze`, LAN | same creation text | measured | BROKEN, same cause |
| Hivemind | `GET /api/v1/intelligence/hivemind?query=`, LAN | 200 `{"result": null, "success": true, "usage": {"compute_ms": ...}}` | measured | BROKEN: nothing behind the bridge on central (hevolve core not installed, task #129), and the call is counted (task #110) |
| Generate an image | `POST /api/v1/intelligence/generate`, LAN | 200 `status: unavailable, "image not available on this node"`, `success: true` | measured | honest text, but counted as a call (task #110) |
| Usage | `GET /api/v1/intelligence/usage`, LAN | 200, 4 calls: chat, analyze, hivemind, generate, all counted (cost 0.0: a free key) | measured | WORKS, LAN, and counts the broken and empty answers as calls, which a paid key would be charged for (task #110) |
| Buy Starter by card | `POST /keys/<id>/upgrade/checkout`, LAN | 503 "Card checkout (Stripe) is not configured on this node (set STRIPE_API_KEY...)" | measured | BLOCKED on the owner's Stripe key |
| Buy Starter, sync route | `POST /keys/<id>/upgrade`, LAN | 503 "No live USD payment gateway (Stripe)..." | measured | BLOCKED, same |
| Buy with PhonePe | `POST /keys/<id>/upgrade/phonepe`, LAN | 503 "PhonePe gateway is not registered (set PHONEPE_MERCHANT_ID + PHONEPE_SALT_KEY)" | measured | BLOCKED on the owner's PhonePe decision; the route exists |
| After paying | code and the deployed bundle | no Stripe webhook (read); `hevolve.ai/upgrade-success` has no route in any of the 137 deployed JS files (measured); the PhonePe callback default points at the web-app host, which answers it 404 (measured) | read, measured | GAP: charged and not upgraded without a manual completion call (task #122) |
| Buy from the web | `hevolve.ai/pricing`; all 137 deployed JS files | 0 occurrences of `intelligence/keys`, `upgrade/checkout`, `api.hevolve.ai`, `upgrade-success` | measured | BLOCKED: no buy flow deployed; the Nunba landing page has it (plan W2-1) |

## 3. Seller

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Agent marketplace, browse | `GET /api/social/marketplace/listings`, LAN | 200, `total: 0` | measured | WORKS, empty |
| Agent marketplace, list mine | `POST /api/social/marketplace/listings`, LAN | 400 `agent_id required` | measured | needs a finished agent; the probe user has none |
| App marketplace | `GET /api/marketplace/apps`, `/trending`, `/categories`, LAN | 200, 0 apps, 0 trending, categories with count 0 | measured | WORKS, empty |
| McGroce merchant / shopper | `GET /api/commerce/health`, LAN | 200 `configured: false, admin_configured: false` | measured | BLOCKED: McGroce URL, service account and session secret not set (lanes plan) |
| McGroce session | `POST /api/commerce/session` (JWT), LAN | 403 forbidden | measured | BLOCKED, same |
| Cart settlement | code | on a Stripe-only node mandates attach no payment method | read | BLOCKED, task #113 |
| Web seller UI | live bundle | `marketplace/listings` 6 times, `mcgroce` once | measured (strings) | present client-side; NOT CHECKED in a browser |

## 4. Searcher

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Search posts | `GET /api/social/search?q=hive`, LAN | 200, hits (benchmark-proof posts) | measured | WORKS, LAN |
| Discover agents | `GET /api/social/discovery/agents`, LAN | 200, a list of seeded users | measured | WORKS (returns users, not agents by name), LAN |
| Shared recipes | `GET /api/social/recipes`, LAN | 200, `total: 0` | measured | WORKS, empty |
| Web research | `GET /api/web-research/tools`, `/probe` | 401 "requires local access or a valid HARTOS_API_TOKEN" | measured | node-local by design |
| Hivemind | see buyer | `result: null` | measured | BROKEN on central |

## 5. Admin

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Social admin gate | `GET /api/social/admin/stats`, `/users`, `/revenue-analytics` with a plain user's JWT | 403 "Admin access required" | measured | WORKS as a gate; the admin's own view needs an admin credential: NOT CHECKED |
| Channels admin gate | the same plain user's JWT on the channels admin API | admitted on 903cd58c | measured | GAP, fixed in 8ae03de71 + 70d65db24 (operator-only on central), not yet deployed; writes NOT CHECKED against production (task #121) |
| Goals | `GET /api/goals` (JWT of a plain throwaway user), LAN | 200, 241 goals with their full records: 4 active, 70 paused, 167 completed | measured | GAP: the listing has no owner filter, while the single-goal route refuses non-owners (task #127); the counts feed section 7 |
| Admin UI page | `GET https://hevolve.ai/admin` | 200, the SPA shell (title "Hevolve \| Hevolve AI Agent"); the bundle holds `/admin` 35 times, `moderation` 11, `revenue-analytics` 1 | measured (strings) | present client-side; NOT CHECKED in a browser |

## 6. Driver and rider (RideSnap)

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| RideSnap in HARTOS | `goal_seeding.py:840`, `goal_manager.py:2376` | a seeded daemon goal "P2P Rideshare Network (RideSnap)" whose agent talks to a configured RideSnap backend; no rider or driver HTTP surface in HARTOS | read | NO SURFACE in HARTOS |
| The goal on central | DB | `p2p_rideshare` paused, updated 2026-09-02 | measured | not running |
| The backend from central | its default address from inside the container | connection refused | measured | nothing to drive |
| RideSnap apps (Juspay) | outside HARTOS | | | NOT CHECKED |

## 7. Automated journeys: news channels, research paper listings, marketing

Read from central's `agent_goals` and `posts` tables (read-only). Every cell
in this table is **measured** (a row read back) unless it says read. The
goals table holds 4 active goals (section 5); two of them appear here
(`autoresearch`, `hive_growth`); the other two are of goal types this table
does not cover (every news goal is paused and no marketing goal is active,
by the rows below), and their names were not recorded: not checked.

| Journey | Goals | State | Verdict |
|---|---|---|---|
| Auto news channels | `news` x5 (Regional / National / International News Curation and Push Notifications, Herald twice), `seo` "News to hevolve.ai via Consent-Gated PRs" | all **paused**, last updated 2026-07-26 to 2026-09-02; 0 posts ever with news in the title; the communities' cached post counts are 0 (5 of 6 hold one post each by row count); channels admin shows 0 channels | NOT RUNNING |
| Auto research paper listings | `research` "Auto Research" paused 2026-08-20; `paper_explanation` "Paper Explainer" paused 2026-08-20; `autoresearch` "Autonomous Research Loop Coordinator" **active** since 2026-07-20 with `last_dispatched_at` NULL | 0 posts ever with paper, arxiv or research in the title | NOT RUNNING (one active goal that never dispatched) |
| Marketing | `marketing` 84 completed, 11 paused; `hive_growth` "Guardian Convergence" active, dispatched 05:00 today | the flag is on and the consent stands (since 2026-07-21); 0 channels have credentials | NOT POSTING externally |
| What actually posts | the system user `nunba` ("Autonomous publisher of distributed-hive benchmark results") | 72 posts in 7 days, about 10 a day, "Benchmark Proof: <benchmark> 0.0% (N nodes)" across 8 benchmarks, 58 with 1 node and 14 with 2 or 3, all 0.0%, no community | the only automated publisher, and it publishes zero scores |
| Other seeded journeys | p2p_marketplace, p2p_freelance, p2p_grocery, p2p_food, tutoring, trading, finance, revenue monitor | all paused | NOT RUNNING |

Nothing in this table restarts by itself: 70 of the 241 goals are paused
(measured), and whether anything on this build resumes a paused goal without
a person was not checked today; on 2026-10-04 nothing did (session notes,
outside this repository).

## 8. HARTOS agents on central, and the model behind every turn

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Listed public agents | `GET /prompts/public` on LAN and on `https://hevolve.ai` | 4: "HIVE Growth Agent" (no recipe), "Hive Model Trainer Incremental Improvement" (recipe), "Agent 28366228009" twice (both with recipes, ids 28366228009 and 68276678525); 61 local prompt files on central; none is a system agent | measured | WORKS on both hosts |
| Public listing on the API host | `GET https://api.hevolve.ai/prompts/public` | 404: the `/prompts` route is pinned to the web hosts | measured | the API host lacks it; hevolve.ai has it |
| REUSE a listed agent | `POST /chat` prompt_id 66722327257 ("Hive Model Trainer Incremental Improvement" in the listing above) and 28366228009 ("Agent 28366228009"), twice each, 05:19 to 05:20 UTC, LAN (the persistent account) | 200, `Agent_status: Reuse Mode`, `served_by: cloud`, response "I couldn't finish that: Error code: 400 - {'error': {'message': 'Reasoning is mandatory for this endpoint and cannot be disabled.', ...}}" 4 of 4; all four after the quota ran out (next row), so quota and the reasoning-only pick are not separated here | measured | BROKEN on `openrouter/free` (task #115) |
| The model behind all of the above | inside the container at 05:40 UTC: the OpenRouter key endpoint says free tier; one plain request answered 429 "free-models-per-day" with `X-RateLimit-Limit: 50`; the new container's log (it starts at the 04:17 deploy) held 22 x HTTP 400, 18 x HTTP 429 and 0 x HTTP 200 to OpenRouter in that window, the first 429 at 04:20:26 UTC | every OpenRouter turn on central failed from 04:20 to 05:40 UTC (measured); the cap is daily, so the rest of the UTC day is expected to fail too (not measured); whether central has a second LLM endpoint configured (the lanes plan allows `OPENAI_BASE_URL` / `HEVOLVE_LOCAL_LLM_URL` to name one) was not checked, so "every turn" is claimed only for OpenRouter | measured, with the expectation and the gap labelled | BLOCKED on the owner's money decision (task #115) |
| Create an agent | see person (section 1) | Creation Mode status, reply text not captured | measured (status), not checked (the reply) | NOT CHECKED |
| Run a listed agent through the Hive API | the metered endpoint's request fields and reply shape | no `agent_id` field on the metered endpoint; the metered reply carries only `success`, `response`, `usage` | read | BLOCKED, plan W1-9 |

## 9. The public host

Route gaps are tracked as tasks: `/api/v1/intelligence` has no route (plan
W1-2), `/chat` is captured by another route (task #119), the channels admin
path is public and its gate is fixed in this chain (task #121), and two
routes that act on a body user id need measuring (task #125). `/prompts` is
served on the web hosts, not the API host.

## 11. The desktop node, 2026-10-14 (owner ruling: every agentic flow is continuously live proven)

Node: the owner's desktop running Nunba from source with HARTOS in-process
(HARTOS f203e7d3b, Nunba b83f6540, local model Qwen3.5 2B, not the
recommended 4B; the HARTOS port is not open in this mode). The build carries
the revenue chain (af5c75099, f431b73d7, 4151c1dec, abb2dcfaf, 3a07d3989)
that central does not. Measured by hartos-87 at 17:31:51 to 17:31:59 UTC with a
throwaway users and a free key, left on the node's SQLite as residue
(users `proof_20261014173151` = `198b63eb-76ba-4224-aeb5-8e30e86eba17`,
`proof2_20261014173425`, `proof2_20261014173613`; key
`fd06044a-f043-4d8e-83d2-b9a280025deb`).
Every row is **measured** unless it says otherwise. A row is re-proven after
the next restart of this node or the next push.

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| A login token carries no lane claim (f431b73d7, 4151c1dec) | `POST /api/social/auth/login`, then decode the JWT | 200; the claims are exactly `exp, iat, iss, jti, node_id, scope, type, user_id, username`: no `lane`, no `rid` | measured | WORKS |
| A paid-tier upgrade never settles through the mock on a node with no rail (abb2dcfaf, 3a07d3989) | `POST /api/v1/intelligence/keys/<id>/upgrade` `{target_tier: starter, payment_method: mock}` at 17:31:56Z | 503 `{"success": false, "error": "no live payment gateway is configured on this node; the dev mock settles nothing"}`; `GET /keys` afterwards: tier still free; the node's log at the same second: `ap2_protocol WARNING upgrade: payment for 198b63eb-... refused: no live payment gateway is configured on this node (set STRIPE_API_KEY, or PHONEPE_MERCHANT_ID with ...)`, settings by name only. So dev mode is off on this node, or the rule's dev branch would have settled | measured | WORKS (a verified refusal) |
| A paid chat turn as the customer (af5c75099) | `POST /api/v1/intelligence/chat` with the key, "Reply with the single word OK." at 17:31:52Z | 503 after 4.1 s `{"success": false, "error": "Intelligence backend unavailable; this call was not billed", usage 0/0, compute_ms 4082}`; the log at 17:31:56Z: `Metered api_intelligence /chat transport error: HTTPConnectionPool(host='localhost', port=<the HARTOS port>) ... NewConnectionError`. The metered self-POST goes to the HARTOS port over HTTP, and this in-process mode opens none, so the paid API cannot answer on a node run this way. Two things read back as intended: not billed (cost 0) and an honest 503. Nit: `GET /usage?days=1` afterwards shows `total_calls 1` with cost 0, so a failed, unbilled call still counts as a call in the customer's usage view | measured | BLOCKED on this node (topology): needs the HARTOS port open, or the self-POST folded onto the in-process app, which is the second half of the report-3 fold; the usage count is a defect |
| The cart settler and the generic mandate settler (abb2dcfaf, 3a07d3989) | none | both are agent tools, not routes; `/api/commerce/health` says `configured: false`, so no cart session can start; the one HTTP surface of the same refusal rule is the upgrade rail above | not checked beyond that | NO SURFACE here |
| A plain chat turn, the text she reads | `POST /chat` in Nunba's body shape (`text`, `user_id`, `request_id msg_proof_plain2_20261014173613`) with a throwaway JWT at 17:36:14Z | 200 in 3.8 s; the reply text: "The capital of France is Paris."; `served_by local`, `source langchain_local`, `node_tier flat`, `agent_status Draft-First Mode`; the node's log at the same second: `[CHAT] turn received ... agent_id=local_assistant agent_type=local text_len=54 request_id=msg_proof_plain2_20261014173613`, then `TTS _bg: thread started` for it. An identical turn at 17:34:26Z answered 200 in 8.8 s (its text was not kept by that run). Contract fact: the same door answers 400 "Text is required" to HARTOS's own `/chat` shape (`prompt`); Nunba's field is `text` | measured | WORKS on the 2B |
| The outbound record for the two plain turns | the two plain turns above | Written. A developer run writes the record to the `logs-dev/` folder, not `logs/` (the provenance rule of 2026-08-16 in `core/platform_paths.get_log_dir`; the installed build keeps `logs/`); the first reading looked only in `logs/` and called it missing, which was wrong. In `logs-dev/llm_outbound.jsonl` (mtime 23:11 IST), one row per turn: `msg_proof_plain2_20261014173425` at 23:04:28,125 and `msg_proof_plain2_20261014173613` at 23:06:15,964, both `source dispatcher.draft`, `response_status 200`, with body and latency (1937 ms, 1350 ms). Re-read by hartos-45. A row has no lane field: the lane rides as a request header and is stamped and metered only for a configured hosted endpoint (central's gateway path), never for the local model, so the lane-on-the-record proof belongs to central after the push | measured | WORKS; the lane proof moves to central |

## 10. What this proves

Measured on the public host: the social feed read and the public agent
listing on hevolve.ai work; chat is captured by the wrong route; the paid
paths have no route. Measured on the LAN node only: sign up and log in, write
to the feed, search, get a Hive API key, see usage, and chat with no agent
(the three turns whose replies were kept, all at 03:27 UTC before the
deploy). Not checked: what a person gets back when they start creating an
agent (the status said Creation Mode; the reply was not captured). Not
working or not reachable: every paid step (no public route, no Stripe, no
webhook, broken answers until the fix chain deploys), the hivemind leg, every
automated channel (news, research, marketing), the seller and McGroce legs
(unconfigured), RideSnap (no HARTOS surface, backend unreachable), and every
OpenRouter turn from 04:20 to 05:40 UTC (measured; the free daily quota, so
the rest of the day is expected to fail too; whether central has a second LLM
endpoint was not checked, section 8). Gaps that are not journeys:
tasks #121 (fixed in this chain, not deployed), #127 (the goals listing),
#123 and #125 (not measured). The admin UI and the marketplace UI exist in
the deployed bundle and were not driven in a browser.
