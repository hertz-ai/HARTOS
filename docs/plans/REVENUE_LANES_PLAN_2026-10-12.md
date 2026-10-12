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
| Lane decided for a /chat turn | `core/request_lane.py` (pure rule), called from `/chat` beside `set_request_id` | landed |
| Lane decided for a daemon goal | `dispatch.is_revenue_goal_type`, registered in `dispatch_goal` | landed |
| Lane crosses the worker thread, reaches the wire | `core.llm_outbound_logger` (`register_request_lane`, `current_lane`, `X-HARTOS-Lane`, `X-HARTOS-Model`) | landed |
| Paid tier marks its turn | `commercial_api._metered_backend_chat` body `lane` (trusted only with the node's own key) | landed |
| Kong fuse per lane and per Claude model | `integrations/gateway/kong_onboard.py --llm` (Kong 2.8.5 community: request caps only) | landed, apply on deepbox |
| Provider fan-out, verbatim relay, real statuses | gpt3 `llm_gateway.py` (hertz-ai/GPT3 main 36ebae1) | landed, redeploy with start.sh |
| Claude as the expert tier, offered to the revenue lane only | `model_registry` from `HEVOLVE_EXPERT_LLM_*` | next |
| Dollar meter: usage from every response, $6 Claude day cap before dispatch | `budget_gate` + metered-usage ledger | next |

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
2. Set on central's `.env`: the McGroce trio, `HEVOLVE_API_KEY` (the paid
   tier's lane is trusted only through it), `HEVOLVE_AUTONOMOUS_MARKETING=1`,
   the expert trio for Claude, and point the configured LLM at Kong
   (`http://172.17.0.1:8100/llm/v1`) in all three keys the deploy check names.
3. Redeploy gpt3 (`bash start.sh` with `/opt/hzai-GPT3/gpt3.env` holding the
   provider keys) and apply the Kong routes
   (`python -m integrations.gateway.kong_onboard --llm --kong-url http://127.0.0.1:8101`).
4. Rotate the provider keys tracked in the GPT3 repo's config.json.
5. Mint the Stripe payment links and point DNS at the API.
6. Decide the merchant fee, whether Spark becomes purchasable, and the first
   delivery leg.
