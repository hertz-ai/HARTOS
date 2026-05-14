# Going live with Stripe — from $0 to first real charge

Single-path runbook.  Follow top to bottom.  Every step is verifiable.
Stops at first real Stripe charge through `/api/v1/intelligence/keys/<id>/upgrade`.

## Prerequisites checklist

- [ ] Stripe account exists at https://dashboard.stripe.com
- [ ] You can read the live secret key (starts with `sk_live_`) from
      the dashboard → Developers → API keys
- [ ] SSH access to `etime.hertzai.com:422` as `sathish`
- [ ] HARTOS commits `ffce89f` + `d26141e` are on `origin/main`
- [ ] Latest Nunba.exe build (with bundled HARTOS at `d26141e` or
      later) is installed locally — confirm via
      `curl http://127.0.0.1:5000/api/harthash`

## Step 1 — Buy a domain for the public API surface (one-time)

If `hevolve.ai/api/v1/intelligence/pricing` returns the React SPA
HTML (it does as of 2026-05-14), the public API surface is broken
for buyers.  Three fixes; pick the easiest:

**Option A — subdomain (recommended):**
1. In your DNS provider, add an `A` record:
   `api.hevolve.ai` → `<central-host-public-ip>`
2. On `etime.hertzai.com`, the `langchain` container already exposes
   port 6777.  Add an nginx (or Caddy) front:
   ```
   server {
     listen 443 ssl;
     server_name api.hevolve.ai;
     ssl_certificate     /etc/letsencrypt/live/api.hevolve.ai/fullchain.pem;
     ssl_certificate_key /etc/letsencrypt/live/api.hevolve.ai/privkey.pem;
     location / {
       proxy_pass http://127.0.0.1:6777;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
     }
   }
   ```
3. Run `certbot --nginx -d api.hevolve.ai` once.
4. Verify: `curl https://api.hevolve.ai/api/v1/intelligence/pricing`
   should return JSON, not HTML.

**Option B — path-based routing:**
On the same nginx instance fronting `hevolve.ai`, insert:
```
location /api/v1/intelligence/ {
  proxy_pass http://central-backend:6777;
  proxy_set_header Host $host;
}
```
BEFORE the SPA catch-all `try_files` line.  Order matters.

**Option C — Kong route:**
If you already have Kong fronting the cluster, add a service +
route for `/api/v1/intelligence/*` upstreaming to
`langchain:6777`.  Use the existing key-auth plugin for the
buyer-facing endpoints.

Pick A.  It's the cleanest and the docs in
`Nunba-HART-Companion/docs/reference/commercial-api.md` already
say `hevolve.ai/api/v1/intelligence/` — but `api.hevolve.ai/v1/intelligence/`
is the cleaner shape.  Update the docs once DNS is live.

## Step 2 — Get the Stripe live key

```bash
# In the Stripe dashboard:
# 1. Developers → API keys
# 2. Toggle "View test data" OFF (so you're on live mode)
# 3. Reveal the Secret key (starts with sk_live_)
# 4. Copy it.  Treat it like a password.
```

Optional but recommended: create a separate restricted key with
only the permissions you need (PaymentIntents: write, Refunds: write,
Customers: read).  Lower blast radius if it ever leaks.

## Step 3 — Install Stripe SDK on the central container

The `StripePaymentGateway` in
`integrations/ap2/ap2_protocol.py:247` lazy-imports `stripe`.  If
the SDK isn't installed, `connect()` logs a warning and Stripe
stays disabled (MockPaymentGateway only).

```bash
ssh -p 422 sathish@etime.hertzai.com
sudo docker exec -it langchain pip install stripe
sudo docker exec langchain python -c "import stripe; print('stripe', stripe.VERSION)"
# Expected: stripe 7.x or later
```

If that succeeded, the SDK is in the running container.  But on
the next image rebuild it'll vanish unless added to
`requirements.txt`.  Bake it in:

```bash
# From your laptop:
echo 'stripe>=7.0.0' >> C:/Users/sathi/PycharmProjects/HARTOS/requirements.txt
cd C:/Users/sathi/PycharmProjects/HARTOS
git add requirements.txt
git commit -m "deps: add stripe SDK for live payment rail"
git push origin main
```

CI's docker-deploy will rebuild and the SDK will persist.

## Step 4 — Set `STRIPE_API_KEY` on the central container

```bash
ssh -p 422 sathish@etime.hertzai.com
cd /opt/hzai-LLM-Langchain-Chatbot-Agent/repo/LLM-langchain_Chatbot-Agent

# Append to the host-side .env (which is mounted into the container
# via --env-file .env):
echo 'STRIPE_API_KEY=sk_live_REPLACE_WITH_REAL_KEY' >> .env

# Verify it's readable:
grep STRIPE_API_KEY .env

# Restart so the container re-reads .env:
sudo docker restart langchain

# Wait ~30s for boot, then verify the gateway registered:
sleep 30
sudo docker logs langchain 2>&1 | grep -i "stripe gateway"
# Expected: "PaymentLedger: Stripe gateway auto-registered (real charges enabled)"
```

If you see "STRIPE_API_KEY present but connect failed" instead, the
key is bad or the SDK install didn't stick.  Re-run Step 3.

## Step 5 — Verify Stripe is the selected gateway

```bash
# Pick any hevolve.ai user; for testing, create a fresh one via the
# normal signup flow.  Get their JWT.

export HEVOLVE_JWT="eyJ..."  # the JWT from step above

# Create a free API key:
curl -X POST https://api.hevolve.ai/api/v1/intelligence/keys \
    -H "Authorization: Bearer $HEVOLVE_JWT" \
    -H "Content-Type: application/json" \
    -d '{"name": "stripe-live-smoke", "tier": "free"}'
# Returns: {"api_key": {...}, "raw_key": "hev_..."}
# Save the api_key.id from the response.

export KEY_ID="..."  # from the response above

# Inspect the gateway pick logic.  An upgrade WITHOUT data.gateway
# specified should pick Stripe (the new auto-registered live gateway,
# preferred over Mock):
curl -X POST https://api.hevolve.ai/api/v1/intelligence/keys/$KEY_ID/upgrade \
    -H "Authorization: Bearer $HEVOLVE_JWT" \
    -H "Content-Type: application/json" \
    -d '{
      "target_tier": "starter",
      "payment_method": "pm_card_visa"
    }'
```

The `pm_card_visa` is a Stripe TEST token that always succeeds on
sk_test_ keys.  On a live key (`sk_live_`), use a real
PaymentMethod created via Stripe.js / Checkout.

Expected on first real card charge: HTTP 200 with
`revenue_split: {users_pool_usd: 8.10, infrastructure_usd: 0.81,
central_usd: 0.09}` for a $9 Starter upgrade.

## Step 6 — Wire the buyer-facing signup flow

The signup → upgrade flow needs ONE of:

**Option A — Stripe Checkout (recommended, lowest engineering effort):**
Replace the existing `/upgrade` POST with a server-side Stripe
Checkout session creator that redirects the buyer to Stripe's
hosted page.  On success, Stripe redirects back to a success
URL where you call our existing `/upgrade` with the Stripe
`payment_method` from the completed session.

**Option B — Stripe Elements in the React SPA:**
More UX control, more code.  Defer until Option A converts enough
revenue to justify the work.

For the first $1K of revenue, Option A is enough.  Use Stripe's
Checkout API to create a one-time payment session per tier:
```python
session = stripe.checkout.Session.create(
    mode='subscription',  # or 'payment' for one-shot
    line_items=[{'price': PRICE_ID_FOR_TIER, 'quantity': 1}],
    success_url='https://hevolve.ai/upgrade-success?session_id={CHECKOUT_SESSION_ID}',
    cancel_url='https://hevolve.ai/pricing',
    customer_email=user.email,
)
return redirect(session.url)
```

You'll need to create Stripe Products + Prices for each tier
($9 Starter, $49 Pro, $499 Enterprise) once via the dashboard.

## Step 7 — Watch the first $9 land

```bash
# Live revenue counter:
curl https://api.hevolve.ai/api/v1/intelligence/usage/admin \
     -H "Authorization: Bearer $ADMIN_JWT"

# Or via the agent tool from inside HARTOS:
# (any /chat call with a goal_tag=revenue agent context)
#   "Run get_api_revenue_stats and summarise the numbers"
```

Look for `revenue.subscription_upgrades_usd` ticking up by $9
(per Starter conversion).  90% of that lands in the users pool
which the nightly settlement loop pays out to compute providers.

## Step 8 — When you hit $111 Starter conversions ($1,000 gross)

That's the goal threshold.  Then:
1. Capture the `get_api_revenue_stats` output as a screenshot.
2. Post the milestone on the channels in `_revenue_assets.md`.
3. Run the nightly settlement to compute provider payouts.
4. Start working on the PhonePe rail (India market, separate
   `enable_revenue_rails.md`).

## Troubleshooting

**`Stripe gateway not connected`**:
- Check `sudo docker exec langchain env | grep STRIPE_API_KEY`
- Verify the key starts with `sk_live_` (production) or `sk_test_`
  (sandbox), not `pk_`.
- Verify `stripe` SDK is installed (Step 3).

**`Payment authorization failed`** with the test card token:
- Make sure you're using `sk_test_` if testing with `pm_card_visa`,
  or a real PaymentMethod ID from a completed Stripe.js flow if
  using `sk_live_`.
- Check Stripe dashboard → Logs → Most recent for the exact
  failure reason from Stripe's side.

**Revenue counter doesn't tick after a successful charge**:
- Confirm the payment_request has `metadata.kind == 'tier_upgrade'`.
- Confirm the request was processed via `payment_ledger.process_payment`
  (not just `create_payment_request`).
- Inspect `/app/agent_data/payment_ledger.json` directly to see if
  the entry has `status: completed`.

**Public URL returns React HTML instead of JSON**:
- Step 1 was not completed.  DNS / nginx routing not yet in place.
- Quick test: `curl http://etime.hertzai.com:6777/api/v1/intelligence/pricing`
  — if that returns JSON, the backend works; the issue is purely
  the public-facing routing layer.

## Reference

- Code home for the Stripe gateway: `integrations/ap2/ap2_protocol.py:247`
- The upgrade endpoint: `integrations/agent_engine/commercial_api.py`
  (function `upgrade_key`)
- The revenue counter source of truth:
  `integrations/agent_engine/revenue_tools.py:get_api_revenue_stats`
- The 90/9/1 split constants:
  `integrations/agent_engine/revenue_aggregator.py:REVENUE_SPLIT_*`
- The PhonePe rail (India, separate runbook): `deploy/enable_revenue_rails.md`
