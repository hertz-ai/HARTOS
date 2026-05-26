# Revenue Assets — Ready to Publish

Concrete copy for every channel.  No emojis, no hype, no fake urgency.
Use what's true.  Every link points to something that exists.

The honest pitch: **Nunba runs the same hive intelligence locally
for free; the commercial API exists for teams who'd rather pay for
hosted compute than maintain their own.  Free tier of the API is
100 req/day, forever, no card.**

---

## Twitter / X — short form (5 variants)

### A. Local-first hook
```
We made an AI app that runs 100% on your laptop.
4B model, vision, voice, social, agents.
Open source. No login. No telemetry.
Pay nothing, get everything.

Or hit the same intelligence as a hosted API — 100 req/day free, paid
plans send 90% to the people running the compute.

hevolve.ai/download
```

### B. Revenue split hook
```
Most AI APIs: 100% to the vendor.
Hevolve API:
   90% → compute providers (the people training the hive)
    9% → infrastructure
    1% → us

Free tier (100 req/day) is free forever.
Paid tiers start at $9/mo.

The split is committed in code, not policy.
github.com/hertz-ai/HARTOS
```

### C. Free-forever hook
```
Our free API tier doesn't expire. 100 req/day, no credit card.

If you outgrow it, paid tiers buy throughput — not a different model.
Same hive intelligence, more bandwidth.

The free tier is the marketing.

hevolve.ai
```

### D. Anti-rent-seeking hook
```
Three things we built into Hevolve before launch:

1. Free tier never expires.
2. Open-source desktop runs the same intelligence locally.
3. 90% of paid revenue flows back to compute providers.

Intelligence should not be gatekept.

github.com/hertz-ai/HARTOS
```

### E. Contributor hook
```
Got a spare GPU?

Run Nunba in contributor mode and earn from inference your machine
serves to the hive.  90% of API revenue is split across compute
providers, weighted by hours contributed.

No mining.  No tokens.  Real revenue.

hevolve.ai/contribute
```

---

## LinkedIn — long form (2 variants)

### Post A: launch
```
Today we opened the Hevolve commercial API.

What it is: a hosted endpoint for the same hive-aggregated AI
intelligence our open-source desktop app (Nunba) runs locally.

Why a hosted API at all: not everyone wants to maintain a local
GPU, and that's fine.  We charge for the convenience of hosted
compute — not for the ability to use the model.

Why a free tier that never expires: because gatekeeping
intelligence is the wrong default for the next decade.  100
requests a day, forever, no credit card.  Use it for hobby projects,
education, prototyping, anything.

Where the money goes:
 — 90% to the compute providers running the hive
 —  9% to infrastructure (hosting, edge, bandwidth)
 —  1% to us (treasury, legal, security)

The split is in code.  We can't quietly change it; you can audit
it in the HARTOS repo.

Tiers:
 Free      $0    100 req/day        forever
 Starter   $9    1,000 req/day      $0.50/1K tokens
 Pro      $49   10,000 req/day      $0.30/1K tokens
 Enterprise $499 100,000 req/day    $0.20/1K tokens

You can also just run Nunba on your laptop and pay nothing.
Both are first-class.

hevolve.ai/download
github.com/hertz-ai/HARTOS
```

### Post B: founder note on the economics
```
Three observations from a year of building local-first AI:

1) The hardware is finally ready.  A 4B model on a laptop GPU
   answers most chat turns in under a second.  Five years ago
   this was a hosted-only experience.

2) Multi-model hive consensus beats any single model on hard
   questions.  This is the entire premise behind the Hevolve hive.

3) The economics need to flip.  Compute providers should earn the
   majority of value, not the platform.  90/9/1.  Not 0/0/100.

The Hevolve API ships with all three baked in.  Free tier of
100 req/day is free forever.  Paid tiers buy throughput, not a
different model.  Local Nunba runs the same intelligence offline.

hevolve.ai
```

---

## Hacker News — Show HN (3 variants)

### Variant 1: API-first
```
Show HN: Hevolve Commercial API – free tier forever, 90% of paid revenue to compute providers

We just opened a hosted API for hive-aggregated AI intelligence.
Free tier (100 req/day) is free forever, no credit card.  Paid tiers
($9 / $49 / $499 a month) buy higher rate limits and priority routing.

The revenue split is committed in code:
 - 90% to compute providers (the people training the hive)
 -  9% to infrastructure
 -  1% to us

Per-token rates are lower at higher tiers because bulk usage is
cheaper to serve.  No upsell calls, no contract negotiation, no
"Enterprise: Contact Sales".

The orchestration backend (HARTOS) and the desktop client (Nunba)
are both open source.  You can run the same intelligence locally
for free — the API exists for teams who'd rather hit an endpoint
than maintain a GPU.

Endpoints:
  POST /api/v1/intelligence/chat
  POST /api/v1/intelligence/analyze
  POST /api/v1/intelligence/generate
  GET  /api/v1/intelligence/hivemind
  GET  /api/v1/intelligence/pricing  (no auth, live tier catalog)

Docs: nunba.hevolve.ai/reference/commercial-api
Code: github.com/hertz-ai/HARTOS

Happy to answer questions about the architecture or the splits.
```

### Variant 2: Local-first
```
Show HN: Nunba – local AI desktop app, optional hosted API funds compute providers

Nunba is an open-source desktop AI app.  Runs a 4B Qwen3.5 locally
(plus optional vision, voice, social, multi-agent orchestration).
No login, no telemetry, no cloud requirement.  All data stays on
your machine by default.

We also opened a hosted API for the same intelligence — free tier
of 100 req/day forever, no card.  90% of paid revenue is split
across compute providers running the hive, weighted by hours served.

The interesting part is that local and hosted are first-class peers.
The desktop app doesn't degrade to push you toward the API.  The
API isn't a "premium" model.  Same code path, different transport.

If you have a spare GPU, you can also contribute compute to the hive
and earn from the providers pool.  The split is enforced in code,
not policy.

Download: hevolve.ai/download
Code: github.com/hertz-ai/HARTOS
API docs: nunba.hevolve.ai/reference/commercial-api
```

### Variant 3: Critique-welcome
```
Show HN: HARTOS – open-source backend behind Nunba; new commercial API with 90/9/1 split

Builder here.  Posting honest context, not pitch:

We've been building HARTOS (the open-source orchestration backend
for Nunba, our desktop AI app) for ~14 months.  Today we opened a
commercial API on top of the same code path.

Three things I'd genuinely like critique on:

1. The 90/9/1 revenue split.  We send 90% to compute providers, 9%
   to infra, 1% to us.  This is hard-coded in revenue_aggregator.py
   so we can't quietly drift it.  Is this the right split?  Could
   it be sustainable for us?  I'm not sure yet.

2. Free tier never expires (100 req/day, no card).  Conversion
   funnel question: does this hurt paid tier signups, or is the
   free tier the marketing?  We bet on the latter.

3. We have a single price for each tier ($9/$49/$499) and per-token
   rates are lower at higher tiers.  We refuse to do "Enterprise:
   Contact Sales".  Are we leaving money on the table?

Pricing page: hevolve.ai/pricing
API endpoint (JSON): api.hevolve.ai/api/v1/intelligence/pricing
Code: github.com/hertz-ai/HARTOS
Local-first desktop: hevolve.ai/download

Genuine feedback welcomed, especially from anyone running a similar
revenue-split model.
```

---

## Reddit r/LocalLLaMA

### Title
```
[Self-promo] We open-sourced the orchestration backend (HARTOS) and added a hosted API tier — 90% of paid revenue goes back to compute providers
```

### Body
```
Most of you are running models locally already.  We've been doing the
same — Nunba is our desktop app that ships a 4B Qwen3.5 + recipe
pipeline for local-first inference.  No login required, no telemetry.

We just wired a hosted API on top of the same code path for people
who'd rather hit an endpoint than maintain a GPU.  Free tier is real
(100 req/day, forever, no card).  Paid tiers buy rate limits, not
better models — we don't have a premium model you can only get over
the API.

Where revenue goes:
 - 90% to the compute providers (i.e., if you're running a node,
   you earn)
 - 9% to infra
 - 1% to us

Per-token rates drop as you go up tiers because bulk usage is cheaper
for us to serve, not because we're trying to lock you into a contract.

If you have a spare GPU and want to contribute to the hive (and earn
from the providers pool), there's an in-app contributor mode.

Docs: nunba.hevolve.ai/reference/commercial-api
Repo: github.com/hertz-ai/HARTOS
Desktop: hevolve.ai/download

Critique welcomed.  Especially on the contributor payout math.
```

---

## Cold email — B2B to small AI / dev-tool founders

### Subject lines (pick one)
- `quick note on your <project> stack`
- `free api tier you might want in your toolbox`
- `100 req/day forever, no card — for <project>`

### Body (template; replace `<name>`, `<company>`, `<thing>`)
```
hi <name>,

saw your post about building <thing> with an LLM.  if you haven't
locked in your API yet, you might want this in your toolbox:

hevolve.ai/pricing
- 100 req/day forever, no card, no expiration on the free tier
- same intelligence runs on our open-source desktop app if you'd
  rather not depend on a hosted endpoint
- 90% of any paid usage flows back to compute providers, not us
  (the split is in our revenue_aggregator.py — auditable)

docs: nunba.hevolve.ai/reference/commercial-api (3-minute read)
code: github.com/hertz-ai/HARTOS

if it's useful for <thing>, the chat endpoint is one curl away:
  curl -X POST https://api.hevolve.ai/api/v1/intelligence/chat \
       -H "Authorization: Bearer $KEY" \
       -d '{"message": "..."}'

no upsell, no cadence — i'll only follow up if you reply.

cheers,
<your name>
```

### Follow-up email — day 7
```
hi <name>,

just one note: if the free tier (100 req/day) isn't enough for
<thing>, paid tiers run $9 / $49 / $499 a month and the price
drops per-token as you go up.  no contract, no enterprise-sales
call.

if you're hesitant about a hosted dependency, the desktop app
runs the same intelligence locally and is what we use ourselves:
hevolve.ai/download.

happy to ignore me from here if it's not useful.

cheers
```

### Final email — day 14
```
hi <name>,

last note from me — closing this thread.

if anything about local-first AI or the 90/9/1 split is interesting
to you down the line, ping me anytime.

cheers
```

---

## Indie Hackers / Product Hunt post

### Tagline
```
The open-source AI app that runs locally — plus a hosted API where
90% of paid revenue goes to compute providers.
```

### First paragraph
```
Hi IH.  We built Nunba — a desktop AI app that runs a 4B model
locally with vision, voice, social, multi-agent orchestration, all
free.  Today we opened a hosted API for the same intelligence.
Free tier (100 req/day) is free forever; paid tiers start at $9/mo
and 90% of any revenue flows back to compute providers, not us.

Why now: we needed real revenue to fund compute, but we didn't want
to gatekeep the model.  Hosted API for convenience, local desktop
for sovereignty, both first-class.

What I'd love feedback on: pricing tiers and whether the free tier
is too generous.  Numbers in the post.

Demo: hevolve.ai
Code: github.com/hertz-ai/HARTOS
```

---

## How to use this file

1. Pick ONE channel per day.  Don't dump everything at once.
2. Always link the GitHub repo + the docs.  Trust is the conversion lever.
3. After posting, check `get_api_revenue_stats` to see if signups
   converted.  If a channel produces zero signups in 7 days, drop it.
4. Never overclaim.  Never say "best", "fastest", "industry-leading"
   without a benchmark file backing it.
5. Mention the free tier first, paid tiers second.  Free is the hook.
6. If someone asks about the splits, point them at
   `integrations/agent_engine/revenue_aggregator.py` — the math is
   visible.

The revenue counter resets nightly via the aggregator's settlement
loop; check `get_api_revenue_stats.revenue.subscription_upgrades_usd`
for the cumulative paid-tier total.  Stripe-rail revenue lands once
`STRIPE_API_KEY` is set on the central deploy; PhonePe-rail revenue
lands once `PHONEPE_MERCHANT_ID` + `PHONEPE_SALT_KEY` are set and the
S2S callback URL is registered on the PhonePe merchant dashboard.
