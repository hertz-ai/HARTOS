# Revenue lanes: Claude where it earns, free Qwen everywhere else

Owner rulings, 2026-10-12 (verbatim intent, in order given):

1. Target: $10,000 a month through the platform, apps and agents, from the
   Claude API startup credits ($1,000) plus the free-tier Qwen model.
2. No more than $6 a day on Claude in central.
3. A person's conversation always runs on the free Qwen model unless the
   conversation generates revenue.
4. No friction during payments or while providing a revenue-generating
   service: the funnel and the product never break.
5. Claude is used only where it drives revenue, but it can never become its
   own blocker to running the org that drives the revenue.

## The one rule that satisfies all five

Every LLM call carries a **lane**, and the lane decides three things: which
model may serve it, which cap applies, and what happens at the cap.

| Lane | Who | Model | Cap | At the cap |
|---|---|---|---|---|
| revenue | a paying conversation (McGroce commerce session, paid API tier) or an org goal that earns (marketing, SEO, revenue, hive growth and economics, McGroce merchant and shopper agents, every P2P vertical) | Claude first (expert tier), free Qwen as fallback | the shared Claude day fuse ($6) | falls to Qwen, answer still arrives |
| user | any other person's turn | free Qwen | per-person request caps (Kong) | 429 from Kong, retried by the client |
| daemon | any other background goal | free Qwen | tightest request caps (Kong) | the daemon's own defer and retry |

Claude is therefore never the blocker: a revenue turn past the fuse is served
by Qwen through the autogen fallback ladder that already exists
(`core.autogen_config.with_local_fallback`), and payment steps (AP2 mandates,
Stripe, PhonePe) never touch an LLM gate.

## Topology: central only, by configuration, never by tier branch

The owner wants this routing on central only, and HARTOS stays topology
agnostic. Both hold because nothing in the code reads the node tier for it:

- The expert tier exists only where the expert trio
  (`HEVOLVE_EXPERT_LLM_ENDPOINT_URL / MODEL_NAME / API_KEY`) is configured.
  Central's `.env` carries it; a desktop or regional node does not, and runs
  exactly as before: its person's one configured LLM.
- The day cap only has anything to cap where that expert exists.
- The dollar meter prices through the one meter: a local or free-family model
  costs zero and writes no row, so a desktop on its own llama-server meters
  nothing and a node on a paid API meters what it spends.
- Kong is operator configuration on the box, not HARTOS runtime code.

## Where each piece lives

| Piece | Place | State |
|---|---|---|
| Lane decided for a /chat turn | `core/request_lane.py` (pure rule; `register_chat_lane` is /chat's one registration decision: the node's own daemon turn is left to the id's prefix rule and to `dispatch_goal`'s registration, everyone else is registered by what the request proves, replacing what the id held) | landed |
| Lane decided for a daemon goal | `dispatch.is_revenue_goal_type`, registered in `dispatch_goal` | landed |
| Lane crosses the worker thread, reaches the wire | `core.llm_outbound_logger` (`register_request_lane`, `current_lane`, `X-HARTOS-Lane`, `X-HARTOS-Model`, `X-HARTOS-Model-Family`), stamped on the local target AND on every hosted chat/completions call (Kong's `/llm/v1` path is a hosted call) | landed |
| Paid tier marks its turn | `commercial_api._metered_backend_chat` body `lane` + a `request_id` of its own; trusted only from the node itself (its key, or the JWT it mints for its system identity when no key is set); /chat accepts the `X-API-Key` header the middleware accepts | landed |
| Kong fuse per lane; a fuse on user/daemon Claude calls | `integrations/gateway/kong_onboard.py --llm` (Kong 2.8.5 community: request caps only; the Claude route matches `X-HARTOS-Model-Family: claude`, one rule with the meter). Revenue-lane Claude has NO Kong fuse by design: its cap is the HARTOS day budget below | landed, apply on deepbox |
| Provider fan-out, verbatim relay, real statuses | gpt3 `llm_gateway.py` (hertz-ai/GPT3 main 36ebae1) | landed, redeploy with start.sh |
| Claude as the expert tier, offered to the revenue lane only, selectable only by the expert selectors | `model_registry` from `HEVOLVE_EXPERT_LLM_*` | landed |
| Dollar meter: usage from every completed call on any host, $6 Claude day cap read before the expert is offered; an unreadable ledger withholds the expert (fail closed) | `budget_gate` + metered-usage ledger, `core.llm_outbound_logger` | landed |

## Product journeys and where the money enters

| Journey | Lane | Money | What is still missing |
|---|---|---|---|
| Developer buys an API tier (starter $9, pro $49, enterprise $499) | revenue | Stripe or PhonePe through the live upgrade routes | public DNS or Kong route to :6777, live keys, marketing autonomy flag on central |
| McGroce shopper orders through the agent | revenue | McGroce checkout behind an AP2 mandate the person approves | central config (McGroce URL, service account, session secret), the McGroce-side token endpoint, merchants onboarded, a delivery leg |
| Store owner onboarded by the merchant agent | revenue | future merchant fee or subscription (owner decision, default none today) | outreach CRM pointed at merchant sourcing |
| RideSnap rider and driver | revenue | Juspay in the apps; zero commission for drivers means a driver pass or subscription funds it | a HARTOS client module, settlement config at zero commission, the subscription product |
| Marketing, SEO, outreach, compute recruiting | revenue | indirect: feeds every journey above | consent flag on central; funnel links verified live |
| Ordinary chat, tutoring, companions | user | none | nothing; free Qwen |
| Self-heal, coding, robot, research goals | daemon | none | nothing; free Qwen, tightest caps |

Scale check: $10,000 a month is about 204 pro tiers, or 1,111 starter tiers,
or a mix with merchant and driver subscriptions.

## Owner actions (cannot be done from code)

1. OK the push of local main (the deploy-secret chain and the lane commits).
   A push to main deploys central.
2. Set on central's `.env`: the McGroce trio, `HEVOLVE_AUTONOMOUS_MARKETING=1`,
   the expert trio for Claude, and point the configured LLM at Kong
   (`http://172.17.0.1:8100/llm/v1`) in all three keys the deploy check names.
   Then set `HEVOLVE_API_KEY`, AFTER the build carrying the /chat
   `X-API-Key` fix is deployed (before it, a keyed central answered every
   paid metered call with 401 inside /chat and the customer saw 503,
   measured by the review of 7086adff8).  The key is not optional in
   practice: without it the metered self-POST carries the node's system
   JWT, /chat trusts the JWT over the body (correctly), and every paying
   API customer's turn runs as `system_daemon`, one shared identity, session
   and ledger (measured 2026-10-12; the same collapse the channel relays had
   on 2026-08-06).  With the key, the body's customer id is kept and the lane
   claim is trusted.

   Deferred, recorded here so they are not lost: (a) request ids minted per
   second collide (`core.chat_client.mint_request_id`); the metered endpoint
   mints a unique `api_<ts>_<hex>` id of its own for now, and the shared
   minter should become unique in a change that keeps Nunba's id shape in
   step; (b) a stranger with a JWT who names a goal's `daemon_` request id
   demotes that goal's in-flight lane to `user` (accepted: the alternative,
   keeping the registration, let the stranger inherit revenue); (c) a paid
   API call now counts as user activity, so background daemons yield to it
   for the usual window (intended: a paying customer's turn is a real turn).
3. Redeploy gpt3 (`bash start.sh` with `/opt/hzai-GPT3/gpt3.env` holding the
   provider keys) and apply the Kong routes
   (`python -m integrations.gateway.kong_onboard --llm --kong-url http://127.0.0.1:8101`).
4. Rotate the provider keys tracked in the GPT3 repo's config.json.
5. Mint the Stripe payment links and point DNS at the API.
6. Decide the merchant fee, whether Spark becomes purchasable, and the first
   delivery leg.
