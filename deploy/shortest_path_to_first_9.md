# Shortest path to first $9 — bypasses DNS, central deploy, Nunba rebuild

This runbook gets the first real $9 charge landing in your Stripe
account in **under 10 minutes** of operator work.  It does NOT
require:

- DNS for api.hevolve.ai
- A Nunba.exe rebuild with the d26141e two-phase init fix
- A re-deploy of the React landing page
- Setting STRIPE_API_KEY on the central container
- nginx + Kong routing changes

The longer runbook in `go_live_stripe.md` covers the full
self-service flow once your customer base outgrows manual links.
Start here.

## Step 1 — Mint Stripe Payment Links (3 min)

A Stripe Payment Link is a hosted URL (e.g.
`https://buy.stripe.com/test_5kA29N7B83yu...`) — any buyer who
clicks it can pay; the charge lands in your Stripe account
immediately.  No backend.  No webhook.  No DNS.

```bash
# 1. From the Stripe dashboard:
#    https://dashboard.stripe.com/apikeys
#    Reveal the secret key.  Live = sk_live_..., test = sk_test_...
#    Copy it.

export STRIPE_API_KEY="sk_live_REPLACE_ME"   # or sk_test_ for sandbox

# 2. From the HARTOS repo root:
pip install stripe         # if you don't have it locally yet
python deploy/mint_stripe_payment_links.py
```

The script prints three URLs (Starter $9, Pro $49, Enterprise $499)
and saves them to `deploy/stripe_payment_links.json`.

Example output:
```
Stripe key mode: LIVE
  STARTER      $   9  →  https://buy.stripe.com/eVa3cR...
  PRO          $  49  →  https://buy.stripe.com/14k4gV...
  ENTERPRISE   $ 499  →  https://buy.stripe.com/cN23cR...

Saved to: deploy/stripe_payment_links.json
```

The script is safe to re-run; it mints new links every time.
Archive old links in the dashboard if you want a single canonical
set.

## Step 2 — Host the static buy page (2 min)

`deploy/buy_hevolve_api.html` is a single-file landing page that
reads `stripe_payment_links.json` from the same directory and wires
the "Buy Starter $9" / "Buy Pro $49" / "Buy Enterprise $499" buttons
to the minted URLs.

Three host options (pick the easiest):

### Option A — GitHub Pages (free, ~2 min)

```bash
# In a public repo of yours (or fork hertz-ai/HARTOS if you want):
mkdir -p docs/buy
cp deploy/buy_hevolve_api.html         docs/buy/index.html
cp deploy/stripe_payment_links.json    docs/buy/

git add docs/buy
git commit -m "buy page"
git push

# Enable Pages in the repo settings → Pages → Branch: main → /docs.
# URL: https://<your-user>.github.io/<repo>/buy/
```

### Option B — Netlify Drop (free, ~1 min)

1. Open https://app.netlify.com/drop
2. Drag the `deploy/` folder (or just buy_hevolve_api.html +
   stripe_payment_links.json) onto the page.
3. Netlify hands you a public URL like `https://breezy-cat-12345.netlify.app`.
4. Optionally point a custom domain (e.g., `pay.hevolve.ai`) at it
   via Netlify's DNS settings.

### Option C — paste the buttons directly (0 min, ugly but works)

Just send the three Payment Link URLs to a buyer.  Plain text in
Twitter DM / email / Slack.  The hosted Stripe page does all the
card UX.  No landing page required at all.

## Step 3 — Post the marketing copy with the live links (3 min)

`_revenue_assets.md` has Twitter / LinkedIn / Show HN / Reddit /
cold-email templates.  Replace any mention of a hosted-API URL
that requires DNS with one of the three minted Payment Link URLs.

Specifically, find-and-replace:
```
hevolve.ai/pricing           →  <your buy page URL from Step 2>
$9/mo Starter                →  <Starter Payment Link URL>
$49/mo Pro                   →  <Pro Payment Link URL>
$499/mo Enterprise           →  <Enterprise Payment Link URL>
```

Or paste the three URLs directly in the post body.  The buyer
clicks → pays → done.

Recommended first post: pick ONE of the Twitter variants in
`_revenue_assets.md` (the local-first one or the revenue-split one)
and post it to your personal handle.  Pin it.  See if it gets a buyer.

## Step 4 — Reconcile the first $9 with API key delivery (manual, ~2 min per buyer)

Until the api.hevolve.ai DNS lands and the new Nunba.exe build
deploys, the buyer-side flow is **manual fulfillment**:

1. Stripe sends you an email receipt when someone pays.
2. You open the receipt, note the buyer's email.
3. Manually mint an API key for them (the bundled local Nunba can
   do this AFTER the next build lands, or you can SSH the central
   container and run a Python one-liner).
4. Email them the key + the docs link.

This is fine for the first 5-10 buyers ($45-$90 of revenue,
~5-20% of the way to the $1K goal).  Once it's working, prioritize:
- Landing the Nunba.exe rebuild (CI fix already pushed; just wait
  for green build).
- Wiring api.hevolve.ai → central:6777 (Step 1 of go_live_stripe.md).
- Switching the Payment Link `after_completion` redirect to point
  at `https://hevolve.ai/upgrade-success` (the React page already
  exists and POSTs the session to /upgrade/checkout/complete which
  mints + delivers the key automatically).

## Step 5 — When you cross $1,000

Once Stripe shows $1,000 in cumulative `Payment Link` revenue:

1. Screenshot the dashboard.
2. Post the milestone on the channels in `_revenue_assets.md`.
3. Switch from Payment Links → Stripe Checkout via the React
   `/upgrade-success` flow (it's already built; see
   `go_live_stripe.md` Step 6).  Payment Links remain a viable
   evergreen sales surface for posters / cold emails.
4. Run the nightly settlement to compute provider payouts (the
   90% pool) via the existing AP2 `payment_ledger.process_payment`
   pipeline.

## Why this works without the rest of the infrastructure

Stripe Payment Links are a complete self-serve payment surface:
Stripe hosts the page, accepts the card, sends the receipt,
deposits to your bank.  The "API key delivery" half can be manual
for the first dozen buyers without anyone noticing — buyers expect
a 1-day turnaround on a new account anyway.

Once revenue starts flowing, the rest of the infrastructure I
shipped (commercial API, /upgrade/checkout, /upgrade-success
page) automates the delivery loop so manual fulfillment isn't
necessary at higher volume.  But for proving the conversion
funnel and capturing the first $1,000, this 10-minute path is
sufficient.

## Files this runbook depends on

- `deploy/mint_stripe_payment_links.py` — link minter
- `deploy/buy_hevolve_api.html` — single-file buy page
- `deploy/stripe_payment_links.json` — generated by the script
- `_revenue_assets.md` — marketing copy (replace placeholders)
- `integrations/agent_engine/revenue_aggregator.py` — 90/9/1 split
  constants (for the trust-signal callout)
