# User journeys on central, driven live (2026-10-13, 03:20 to 06:05 UTC), and on the owner's desktop node (2026-10-14, section 11)

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
that central does not. Measured by hartos-87 between 17:31 and 17:36 UTC with
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
| A paid chat turn as the customer (af5c75099) | `POST /api/v1/intelligence/chat` with the key, "Reply with the single word OK." at 17:31:52Z | 503 after 4.1 s `{"success": false, "error": "Intelligence backend unavailable; this call was not billed", usage 0/0, compute_ms 4082}`; the log at 17:31:56Z: `Metered api_intelligence /chat transport error: HTTPConnectionPool(host='localhost', port=<the HARTOS port>) ... NewConnectionError`. The metered self-POST goes to the HARTOS port over HTTP, and this in-process mode opens none, so the paid API cannot answer on a node run this way. Two things read back as intended: not billed (cost 0) and an honest 503. Nit: `GET /usage?days=1` afterwards shows `total_calls 1` with cost 0, so a failed, unbilled call still counts as a call in the customer's usage view | measured (no request id came back on the 503) | BLOCKED on this node (topology): needs the HARTOS port open, or the relay calling the app it lives in instead of posting to its own port; the usage count is the intended audit row (the path's docstring; task #111 is the open item) |
| The cart settler and the generic mandate settler (abb2dcfaf, 3a07d3989) | none | both are agent tools, not routes; `/api/commerce/health` says `configured: false`, so no cart session can start; the one HTTP surface of the same refusal rule is the upgrade rail above | not checked beyond that | NO SURFACE here |
| A plain chat turn, the text she reads | `POST /chat` in Nunba's body shape (`text`, `user_id`, `request_id msg_proof_plain2_20261014173613`) with a throwaway JWT at 17:36:14Z | 200 in 3.8 s; the reply text: "The capital of France is Paris."; `served_by local`, `source langchain_local`, `node_tier flat`, `agent_status Draft-First Mode`; the node's log at the same second: `[CHAT] turn received ... agent_id=local_assistant agent_type=local text_len=54 request_id=msg_proof_plain2_20261014173613`, then `TTS _bg: thread started` for it. An identical turn at 17:34:26Z answered 200 in 8.8 s (its text was not kept by that run). Contract fact: the same door answers 400 "Text is required" to HARTOS's own `/chat` shape (`prompt`); Nunba's field is `text` | measured | WORKS on the 2B |
| The outbound record for the two plain turns | the two plain turns above | Written. A developer run writes the record to the `logs-dev/` folder, not `logs/` (the provenance rule of 2026-08-16 in `core/platform_paths.get_log_dir`; the installed build keeps `logs/`); the first reading looked only in `logs/` and called it missing, which was wrong. In `logs-dev/llm_outbound.jsonl` (mtime 23:11 IST), one row per turn: `msg_proof_plain2_20261014173425` at 23:04:28,125 and `msg_proof_plain2_20261014173613` at 23:06:15,964, both `source dispatcher.draft`, `response_status 200`, with body and latency (1937 ms, 1350 ms). Read by hartos-45 after hartos-87's correction. A row carries no lane field on any node: `log_outbound` writes `ts, request_id, source, body, response_status, latency_ms` on every path, and hosted calls write no row at all. These two draft calls went out through the pooled requests client, which stamps no lane header, while the httpx hook stamps `X-HARTOS-Lane` on local and configured hosted calls alike (probed by the reviewer of this record). So the lane cannot be read off the record anywhere until the record gains a lane field; on a hosted path it is readable off the gateway's request header | measured | WORKS; the lane is not on the record, by the logger's shape |

### 11.1 The seven listed agents of the chat page (hartos-94, 17:32:26 to 17:43:56 UTC)

What "listed" is: the chat page's sidebar is a hard-coded literal of seven
agents, the same seven ids in the web page and the desktop page (web commit
3d01621 of 2024-12-23; first in the desktop tree with the 2026-03-16 import):
Personalised Learning 54, Speech Therapy Agent 49, Spoken English Agent 20,
News Agent 5, Podcast Agent 12, Story Narrator Agent 13, Casual Conversation
Agent 48. Central stores other personas under six of those ids (49 "Great
India", 20 "talk as if you are my kid", 5 a Naruto teacher, 12 "create an
alaram after an hour", 13 "play music for 6 month old", 48 "Spoken English
tutor"; 54 is the Radha persona on central and the rebuilt local tutor on this
desktop), and the desktop's landing cards carry a third mapping (31
Mathematician as "Personalised Learning", 54 as "Speech Therapist"). The
mismatch sits inside the literal (the prompt text was copied in with the
wrong label); the catalogue did not drift. Read, not measured.

Same node as above; its process started 22:45 IST at HARTOS f203e7d3b (the
node's harthash answered the git HEAD of the moment, 23848e35a, `live-git`,
one commit later), Nunba b83f6540, the local model Qwen3.5 2B, 3.07 GB VRAM
free. Contention during the first turn: two other sessions' proof turns
(17:34:26Z, 17:36:14Z, section 11 above), the goal daemon dispatching, and a
743 MB cuDNN download by the node itself. Gate before the first turn:
`GET /prompts?user_id=<the id of agent 54's creator>&type=all` listed 54
as `teach.local.radha`, `has_recipe true`, among 29 rows, so the node serves
the real data directory and not the repository's dev copy (which has no 54);
the answering route is the desktop's own prompts route by its response
vocabulary (`origin`, `available`); Flask's order among the four `/prompts`
handlers was not read. Each turn: `POST /chat` in the chat page's own body
shape (`text`, `agent_id`, `prompt_id`, a fresh `conversation_id`,
`intelligence_preference auto`), as the owner's own desktop identity (the
guest account the owner uses at the screen), so every side effect below
landed on the owner's real profile; one turn at a time; the reply is the
HTTP body, and the pushed messages are the node's agent log
(`send_message_to_user`). Sidebar order. Where this developer run writes its
logs, by mtime and by the request ids found in each file: under the data
directory's `logs/`, the chat log (`server.log`, 3,876 lines with these ids)
and the agent tool log (`agent_system.log`, 55 lines); under `logs-dev/`, the
chain trace (`langchain.log` and its two rotated files, 35,292 lines with
these ids, including the cloud-fallback lines), `indicator_window.log`
(3,887) and the outbound LLM record (`llm_outbound.jsonl`, 236); and
`logs/`'s own `llm_outbound.jsonl`, `frozen_debug.log` and `gui_app.log` have
not been written since the installed build was quit on 2026-10-13 20:49 IST.
So a developer run splits one turn's trail across two folders by file kind,
and a reader who knows only the installed build's `logs/` misses the chain
trace and the outbound record. Every row below ran on the 2B.

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| Personalised Learning (54: the rebuilt local tutor, recipe on disk) | "Teach me fractions, first step only." `request_id 3ed9648c-48dc-4d6a-abbc-a5e4788d4d5f` at 17:32:26Z | 200 after 505.4 s, `served_by local`, `agent_status Reuse Mode`; the HTTP text: "I see that `get_user_id` is not available, but I can try to use `get_user_details` instead to get user information. Let me try that first." (false: the same session's log shows `get_user_id` succeeding 29 times, with no tool failure in the turn). Meanwhile FIVE messages were pushed to the learner, at 17:37:17, 17:38:01, 17:38:37, 17:38:42 and 17:40:06Z: four separate fraction introductions, each with its own greeting and its own check question ("What's your question or confusion about fractions?", "What is the main difference between a fraction and a decimal representation of the same value?", "What is one-half in different ways?", "what's the main focus of this lesson? What do fractions mean to you?"), and between them "I see you've been working on fractions. Let me check your progress!". Between pushes the recipe looped `get_user_id` then `get_data_by_key teach.<user>` (22 reads, 20 "Key not found"), and it did more than teach: it called the computer-control tool twice with "Create directory teach if it does not exist" (as windows at 17:34:21Z, 47.7 s, and as macos at 17:35:15Z, 5.9 s, both answered "Successfully ran the command in user's computer."), sent one presynthesized video (17:36:38Z), read the uploaded-file list twice, and saved a `teach` progress record ("Fractions ... check_question", 17:35:30Z); a second save, to `teach.<the creator id>` at 17:35:41Z, failed ("Type error in value: list indices must be integers or slices, not str - Data not saved") because the first save had put a list under the one key `teach`, and that first save erased the learner progress the same turn had read at 17:33:52Z (see the residue below); the shell commands are the GAP row below | measured | BROKEN on the 2B: a learner who asked for the first step only gets four overlapping lessons, a progress note and a tool remark, over 8 min 25 s, and the agent ran shell commands on the desktop on the way |
| Speech Therapy Agent (49) | "Help me practise the s sound. Give me the first exercise." `3fa325e7-e5bd-40e4-a0ee-ed05edc32254` at 17:40:51Z | 200 in 3.2 s, `Draft-First Mode`, no `prompt_id` in the reply. The node's log: `prompt_id=49 has no recipe on this node - cloud-only agent, forwarding the turn to the cloud`, then `Cloud agent 49 not answered by the cloud (not signed in) - answering here as local_assistant`. Reply: "Here's a simple exercise for the 's' sound: try saying 'see' without the 'ee' sound, just like 'see'." | measured | BROKEN on the 2B: the generic local assistant stands in silently, with no persona and no page notice; no speech-therapy agent is reached (central's 49 is "Great India" in any case) |
| Spoken English Agent (20: a salvage stub, no recipe) | "Help me practise spoken English. Start with a greeting." `d34ecfe4-239a-4b9f-b956-cc3847d97f23` at 17:40:54Z | 200 in 10.1 s, `Creation Mode`; the log: `GATHER JSON EXISTS`, `0 Recipe JSON doesnot EXISTS`. Reply: "I need your input to finish building this agent. Step 1 ("Respond to user") isn't coming together from what I have so far — tell me more about how this step should work, and I'll continue building from there." | measured | BROKEN on the 2B: the builder's dead-end, the 2026-10-04 finding again on this build |
| News Agent (5) | "Give me today's top three news headlines." `43d6ae57-76d6-408a-a39b-ce4906f275a3` at 17:41:04Z | 200 in 2.7 s, `Draft-First Mode`; the same forward-then-fallback as 49. Reply: "Hey there! Ready to dive into some spoken English practice with you? Just let me know what specific skills or topics you'd like to focus on first!" | measured | BROKEN on the 2B: no news, and the answer continues the PREVIOUS agent's topic (the Spoken English turn, same person, a different conversation id): the stand-in's history is per person, not per agent or conversation; mechanism not read |
| Podcast Agent (12) | "Suggest a five minute podcast episode outline about sleep." `230a3f47-e8e8-467c-b060-6544dfbe942f` at 17:41:07Z | 200 in 5.6 s, no `agent_status`; the same fallback. Reply: a four-part outline (Hook 0:00-0:45, The Science, The Fix, The Takeaway) ending "Ready to record?" | measured | answers usefully on the 2B, as the generic assistant; agent 12 ("create an alaram after an hour") never ran. WORKS by the label's letter, not as an agent |
| Story Narrator Agent (13: a salvage stub, no recipe) | "Tell me a short bedtime story about a brave rabbit." `88dc73c5-64c8-4bd7-9c02-8f7cb1ebd4e7` at 17:41:13Z | 200 after 161.0 s, `Creation Mode`; the HTTP text is the same builder dead-end as 20. Meanwhile the build loop pushed 50 messages to the person in 2 min 29 s (17:41:22 to 17:43:51Z) and saved `user.bedtime_story_request`, `user.bedtime_story_response` ("Once upon a time, in a quiet little meadow...") and `user.bedtime_story_complete`; the agent log holds no argument line for this session, so what each of the 50 bubbles said is not checked | measured (count, final text), not checked (the bubbles' texts) | BROKEN on the 2B: a story may have been written, but the person's screen gets a 50-message flood and ends on "I need your input to finish building this agent" |
| Casual Conversation Agent (48) | "Hi, how are you today?" `c1c89ecf-fbce-4b7d-b77a-5605a32396db` at 17:43:54Z | 200 in 2.2 s, `Draft-First Mode`; the same fallback. Reply: "Hi there! I'm ready to help with whatever you need, whether it's solving a math problem, writing code, or chatting about your day." | measured | WORKS as casual chat on the 2B, by the generic assistant (central's 48 "Spoken English tutor" never ran) |
| A tutoring agent's REUSE turn ran OS commands on the owner's desktop under a month-old grant that covers every agent, so nothing asked (inside the 54 turn above) | the two computer-control calls of `3ed9648c-48dc-4d6a-abbc-a5e4788d4d5f` | The chat log: `reuse: tool_call ['execute_windows_or_android_command'] -> Assistant (holds the function)` at 17:34:21Z, then the unified VLM loop (Qwen3-VL, in-process) started for the owner and prompt 54 with the task "Create directory teach if it does not exist" (`VLM loop route_task: ... → single_shot (iterations 30 → 6)`): iteration 1 dispatched the shell action `mkdir <data directory>\coding\teach` at 17:34:27Z, iteration 2 `mkdir teach`, "3 actions in 44.5s (exit_reason=done)", ledger task `computer_use_480200ab9389` COMPLETED, tool result "Successfully ran the command in user's computer."; the directory `coding/teach` under the data directory exists, creation time 17:34:27Z, empty. The second call (`os_to_control macos`) ran the same loop, "2 actions in 5.1s" inside 5.9 s of tool time, with the same result. Consent: the gate sits four times on this path at the node's commit: in the recipe tool itself (`hartos/reuse_recipe.py`), at the entry of the VLM adapter (`integrations/vlm/vlm_adapter.execute_vlm_instruction`, which the recipe tool calls and whose in-process tier the log names, `tier=inprocess`), at the start of the VLM loop (`integrations/vlm/local_loop.py`), and in the one shell handler each VLM `shell` action delegates to (`hart_intelligence_entry._handle_shell_command_tool`, after the denylist); read. So each call checked it at least three times, plus once per shell action: two in the windows call (17:34:27Z and 17:34:30Z), one in the macos call (17:35:18Z). `user_consents` holds, for the owner's desktop identity, a `computer_control` grant that names no agent, so it covers every agent (scope `*`), `granted 1`, granted 2026-09-15 09:31 UTC (the consent writer stamps `utcnow`), never revoked; the same identity holds four unanswered asks from earlier that day (06:24 to 09:09 UTC), each naming one agent; and the agent's creator identity holds the same every-agent grant since 2026-09-26 12:15 UTC, so whichever of the two the node exported as owner, the gate passes (which one this developer process exported was not checked). Measured, read-only. A standing grant returns at once and the gate logs only when it waits or refuses, which is why the chat log holds no "consent" line across the whole turn (17:32 to 17:41Z) and no row was added to the table on 2026-10-14 (53 rows, all older): nothing asked the owner because the owner had allowed every agent a month earlier. The gate's own docstring names the limit: a grant covers every agent until the table can hold per-agent grants. Where the tool came from: agent 54's definition names no tools, and its three recipe actions name only `get_user_id`, `get_data_by_key` and `save_data_in_memory`, with the reply "written by the agent itself, never through a messaging tool"; the chat log shows the recipe matcher comparing "Create directory teach if it does not exist" against each of the three actions and running it anyway. The tool is in the default set registered for every agent's team (`hartos/create_recipe.py`, `register_dual(... execute_windows_or_android_command ...)`; advertised through `core/agent_tool_menu.py`), not in the agent's config. The same turn used `send_message_to_user` five times and `send_presynthesized_video_to_user` once, against its own action 2 | measured (logs, directory, database); read (the gate and the registration) | GAP, narrowed after review: consented, by a scope-every-agent grant of 2026-09-15 with no per-agent scope and no expiry, so any agent can run shell commands silently for as long as it stands; and a tutoring recipe that names only memory tools ran a command tool from the default advertised set on a step the model invented, against its own text, which also forbids the messaging tool the turn used |

Count: 5 of 7 BROKEN, 1 works by the generic assistant's grace (48), 1
answers usefully but not as an agent (12), and one consent finding. Residue on the
owner's profile and the node's data directory: session and ledger state for
54, 20 and 13 (CREATE state for the two stubs; the computer-use ledger task),
the three `user.bedtime_story_*` keys from the 13 turn, seven conversation
rows, the empty `coding/teach` directory the tutor's shell command created,
and one loss: the 54 turn's save at 17:35:30Z wrote the key `teach` itself.
The memory tool walks a dotted key and sets its last part
(`core/agent_tools.py`, the save path; read), so that save replaced the whole
`teach` entry of agent 54's memory, under which every progress record sits
as `teach.<id>`, with the Fractions list, then wrote the store to the agent's
data file. The file (mtime 17:35:30.077Z) now holds only that key: measured.
What it erased, by the agent tool log's save lines of 2026-10-11 (times
IST): 14 saves, every one from the creator's own session, under nine keys.
One is the creator's `teach.<creator id>` ("Quantum Mechanics", "Chapter 3:
Wave Functions", pages 32-36, lesson stage quiz; the same record the sweep's
turn read at 17:33:52Z). The other eight are ids the model made up inside
those sessions; two are shaped like user ids, and neither matches any of the
node's 640 users (read-only query): one holds "Introduction to Quantum
Mechanics", "Chapter 2: Wave Functions and Probabilities", pages 17-23, the
other "The Water Cycle", quiz score 0.8. So this was one learner's record,
the creator's, plus the model's misfiled copies, not three learners'.
Whether all nine keys were still in the file before the sweep is not known:
the file's content was never logged whole. The creator's stored progress on
this node is overwritten; the sweep destroyed it, not a visitor. Recovery
source: those save lines in the agent tool log carry the full records. The
owner chose on 2026-10-14 to leave the file as it is. A read-only byte
search of the node's memory stores, snapshots and agent-data folders for the
chapter title found it in agent 54's memory-graph databases for the creator
identity (20 hits, 24 more in its write-ahead log) and for the owner
identity (1), in 37 chain-trace files of past 54 turns, and in three
unrelated agents' data files; whether any of those holds the progress record
whole was not checked. Nothing else in the folders that search covered holds
the overwritten file; the rest of the node was not searched. The sweep script and its raw output live on
the dev box, not in this repository. The web half of this sweep (the same
seven on hevolve.ai through the cloud chat route) is not run: the route
answers 401 without a signed-in token, and central's free model tier is
capped (section 8).

### 11.2 Reel Studio, first live run on the INSTALLED build (hartos-94, 2026-10-18, 21:18 to 22:37 IST)

Node: the installed desktop (Nunba 22b282cf, HARTOS ed6576624,
Hevolve_Database c5be877, built from a clean export and installed 18:42 IST,
relaunched elevated). Phone: the owner's SM-S918B, Hevolve_React_Native
09167f4e1 (`app-prod-debug.apk`), signed in as user 10202, driven over adb
by hierarchy. Every row is **measured** on both sides unless it says
otherwise. Phone times are the phone's clock, about 1.2 s ahead of the
desktop's (its send at 21:46:48.5 was received at 21:46:47.4 desktop time).

| Step | Probe | Result | Label | Verdict |
|---|---|---|---|---|
| The phone is admitted as a device of the same user (row 36's precondition) | the phone's HELLO after the reinstall (new Ed25519 key); the owner answers the desktop card | 21:18:21 consent.request filed and the first "Device HELLO refused: pending" (repeated until the card was answered); 21:22:30 consent.granted (user 10202, scope `device:<the phone's key>`, hex redacted in the log); 21:22:30.989 "PeerLink inbound ACCEPTED from ba28 5b2e 80b8 dafa (192.168.29.99:60280) kind=device trust=same_user". The previous build refused every HELLO with `UserConsent has no attribute reopened_at` (fixed in Hevolve_Database c5be877, inside this build) | measured | WORKS |
| Row 25: her clips reach her own desktop over the device link | Reel Studio setup screen: 5 camera clips picked (the picker allowed 1 to 10); Create tapped at 21:30:35 before the consent tick and refused with the consent toast (`ReelStudioSetupActivity.create:191`, a correct refusal); consent ticked between 21:30:36 and 21:30:52 (the 21:30:40 tap is the likely one; the 21:30:41 tap reopened the picker, closed 21:30:47); Create at 21:30:52 queued the uploads | Phone (`DesktopFiles`): 5 uploads queued 21:30:52 as `reel_source`, each `Done(xid=<sha256>)` 21:30:56 to 21:31:22, 5 WorkManager SUCCESS rows under one setup tag. Desktop (`hevolve.peer_link`): 5 `file_offer ... -> file_accept` and 5 `file_commit -> file_done` 21:30:51 to 21:31:21 from device ba28 5b2e 80b8 dafa, no cloud, no LAN HTTP. On disk: `user_files/42bfe80fd85434fc/in/<sha256>.mp4` x5 (30,269,005 / 19,155,847 / 58,647,121 / 42,899,145 / 57,608,381 bytes), sha256 recomputed on the desktop = the names = `sha256sum` on the phone of `DCIM/Camera/20260902_132352/132255/132230/132244/132336.mp4` (shot 2026-09-02; the picker names them by MediaStore id). Store index: 5 `reel_source` claims bound to prompt 342 and request 7d7bfe9f, capture times carried | measured | PROVEN (with 5 clips, not 3) |
| Row 26, first half: the create turn reaches the desktop with the tag and the files | the setup screen's first turn, the instructions "Open with the final look, 20 seconds, Tamil songs, add my logo." | Phone `DesktopRoute` 21:33:10 "Desktop-only POST /chat/custom_gpt: served by the desktop". Desktop 21:33:04 `[CHAT] turn received: user_id=10202 ... text_len=63 request_id=7d7bfe9f`, Nunba "prompt_id=342 has no recipe on this node yet - creating it here", HARTOS "GATHER JSON doesnot EXISTS", "gather_info turn 1/12 for 10202_342", local reply via 127.0.0.1:8080 in 5 s: "I love that you're thinking about your final look and the Tamil songs! ... Is this for a video intro, a social media post ...?" No `342.json` yet (the record is written only when gather completes; `reel_studio` is stamped from the hint of that turn) | measured | WORKS as designed, the agent is not yet made |
| Row 26, second half: the setup conversation continues to the saved record | the answer injected into the phone's chat box over adb at 21:46:40 and sent at 21:46:48: "An Instagram reel for my makeup studio made from the 5 clips I attached. ..." | Desktop 21:46:47 `[CHAT] turn received ... text_len=157 request_id=dbd9c656`; Nunba "prompt_id=342 has no recipe on this node - cloud-only agent, forwarding the turn to the cloud"; "Cloud agent 342 not answered by the cloud (HTTP 401) - answering here as local_assistant"; casual chat with prompt_id 0; the generic agent tried Animate_Character (TIMEOUT 21:47:22), Text to image, Agentic_Router ("plan not ready after 15s"), and 67 s after receipt replied "No worries, those technical hiccups happen! Since we can't generate the reel directly right now, let's get you the exact API keys needed to power the video tools (like Runway or Pika)... Just say 'give me the keys'". That is what the phone shows. Cause (code, both sides): the phone's `ReelInbox.delivered()` marks the agent created on the first reply, so every later turn carries `create_agent: false` (its javadoc assumes the desktop keeps a creation in progress "whatever a later turn says"; HARTOS does, by `review_agents` and by treating a turn for an unmade id as a creation); Nunba's `_resolve_agent` step 4 sent such a turn to the cloud. HARTOS task #136 | measured | FAILED (deadlock after one exchange) |
| The fix, on the node | Nunba cb7bc7cb: `_resolve_agent` step 4 keeps a turn here when its goal_tags hint is local-only by HARTOS's one rule (`marketing_tools.agent_is_local_only`), type local, nothing forced | Test-first: `tests/test_chatbot_routes.py::TestAgentRouting` +2; on the old code the new test got "cloud says hi" (the measured failure); 10/10 after, 61/61 for the file. Hot-swapped 22:31:18 IST into `lib/routes/chatbot_routes.pyc` (compiled by the bundle's python-embed 3.12.6 from `git show cb7bc7cb`, two compiles byte-identical, the nested rule present and absent in the old module), Nunba relaunched elevated (pid 30284), harthash 22:32:36, `hevolveai_supervisor: registered pid=31564` 22:33:37 | measured | SWAPPED; the phone re-run is owed |
| The swapped resolver, probed from the desktop | 22:36:49 `POST /chat` on 127.0.0.1:5000 with the phone's second-turn body shape (prompt_id 342, `create_agent` false, `goal_tags` [reel_studio], user 10202, the same answer text), request c13068df | 22:36:50 "prompt_id=342 has no recipe on this node yet and its turn keeps to this node (goal tags ['reel_studio']) - staying here so HARTOS carries on creating it", "Chat with LOCAL agent: 342", HARTOS "GATHER JSON doesnot EXISTS", "gather_info turn 1/12 for 10202_342" (the restart had dropped the earlier gather state), 200 in 5.5 s, served_by local, agent_id 342, the reply asks for the agent's name. Side effect: this probe consumed gather turn 1/12 of her live agent with a replayed answer she did not type on the phone; her next phone turn enters at 2/12 only if it comes within an hour of 22:36:50 (the 1-hour TTL `_AGENT_TTL` in `_cleanup_stale_agents`, run at the top of every /chat, drops the turn counter) and Nunba has not restarted; after that the counter restarts at 1/12, while the gather conversation itself (`gather_agentdetails.user_agents`, a plain dict with no eviction) keeps the probe's replayed answer and the name question until Nunba restarts | measured, desktop side only | WORKS (the resolver); the phone path is still owed |
| Rows 26 (end) to 37 | the phone's next turn through to the reel | Not run: the phone's battery died at ~22:00 IST (4% and not charging through the run). Her next phone turn continues the gather the desktop probe above started: the counter reads 2/12 only within an hour of 22:36:50 (1-hour TTL, `_cleanup_stale_agents`), else 1/12 again, while the gather conversation holding the probe's answer survives until Nunba restarts | not run | OWED |

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
the deployed bundle and were not driven in a browser. The desktop node's
proofs of 2026-10-14 are in section 11 above (left there rather than moved,
two writers were editing this file): the revenue chain rows, the plain chat
turn that works on the 2B, and the listed-agents sweep in 11.1, where 5 of 7
sidebar agents are broken as a visitor sees them and a tutoring recipe ran
shell commands on the owner's desktop silently, under a month-old grant that
covers every agent.
