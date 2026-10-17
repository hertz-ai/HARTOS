# Enable revenue rails on central (`etime.hertzai.com`)

This is the operator checklist that unblocks the autonomous marketing
flywheel after commit `ffce89f` lands on `origin/main`.  Run it ONCE.

Each step is idempotent — safe to re-run if anything mid-procedure goes
wrong.  None of these steps require modifying code on the host; they
only adjust env, mounts, and the run command.

## Prereqs

- `ssh sathish@etime.hertzai.com -p 422` works (password auth).
- Local `git push origin main` succeeded (the email-verification gate
  at github.com/settings/emails is cleared).
- You have the Stripe live key (`sk_live_…`) **and/or** the PhonePe
  merchant credentials available.

## Step 1 — Pull the latest commit on the host

```bash
ssh -p 422 sathish@etime.hertzai.com
cd /opt/hzai-LLM-Langchain-Chatbot-Agent/repo/LLM-langchain_Chatbot-Agent
git pull --ff-only origin main
# Confirm: should show ffce89f among the new commits.
git log --oneline origin/main..main || git log --oneline -5
```

## Step 2 — Add the missing volume mount for `/app/agent_data`

The current container run command does NOT persist `agent_data/`, so
the AP2 payment ledger, outreach prospects, and goal state vanish on
every redeploy.  This is Gap #1 from the Hevolve_Database diagnosis.

```bash
# On the host:
sudo mkdir -p /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data
sudo chown -R 1000:1000 /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data

# If a previous container had data inside, snapshot it first:
sudo docker cp langchain:/app/agent_data \
    /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data_snapshot_$(date +%Y%m%d) || true
sudo cp -a /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data_snapshot_$(date +%Y%m%d)/. \
    /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data/ 2>/dev/null || true
```

The new mount line to add to the `docker run` command:

```
-v /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data:/app/agent_data
```

## Step 3 — Add the payment-rail env vars

`STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `HEVOLVE_API_KEY`,
`HARTOS_PUBLIC_URL` and the PhonePe settings below are owned by the
deploy: set each as a GitHub repo
secret (`gh secret set STRIPE_API_KEY --repo hertz-ai/HARTOS`) and the
deepbox deploy writes it into central's `.env` on every deploy
(`deploy/deepbox_deploy.sh`). Do not hand-edit those; while the secret is
set, the next deploy writes the secret's value back. To remove one: delete
the repo secret FIRST, then the line from `.env`. The webhook secret and
its Stripe-side registration are `deploy/go_live_stripe.md` Step 4b.

`HARTOS_PUBLIC_URL` is the public base of this node's API (the host Kong
fronts it on). PhonePe is sent `HARTOS_PUBLIC_URL` +
`/api/v1/intelligence/phonepe/callback` as its server-to-server callback
and the same base as the buyer's return page, and refund requests name
`/api/v1/intelligence/phonepe/refund-callback`; without it (or with only
a loopback or LAN request host, which no gateway can reach) a PhonePe
payment is refused, never sent with a guessed host: the shopper sees a
plain sentence and the ERROR log names the setting (`core/public_url.py`).

The PhonePe settings are repo secrets too; the deploy forwards them and
writes each one it is given into `.env`. Docker reads `--env-file .env`
only when the container is created, so a `docker restart` changes
nothing: the keys take effect on the next deploy, which recreates the
container. The gateway registers only when the merchant id AND the salt
key are both set; leave the other three unset to keep their defaults.
`gh secret set NAME` with no `--body` asks for the value at a prompt, so
it never lands in shell history:

```bash
# India (UPI / cards / netbanking via PhonePe)
gh secret set PHONEPE_MERCHANT_ID --repo hertz-ai/HARTOS
gh secret set PHONEPE_SALT_KEY --repo hertz-ai/HARTOS
gh secret set PHONEPE_SALT_INDEX --repo hertz-ai/HARTOS    # optional; default 1
gh secret set PHONEPE_ENV --repo hertz-ai/HARTOS           # optional; PROD (default), UAT = sandbox
gh secret set PHONEPE_USD_INR_RATE --repo hertz-ai/HARTOS  # optional; default 84.0
```

## Step 4 — Restart the container with the new mount

```bash
cd /opt/hzai-LLM-Langchain-Chatbot-Agent/repo/LLM-langchain_Chatbot-Agent

sudo docker stop langchain
sudo docker rm langchain
sudo docker run -d --name langchain --restart unless-stopped \
  -p 6777:6777 \
  --env-file .env \
  -e HEVOLVE_MASTER_PRIVATE_KEY="$(sudo cat /etc/hevolve/master_private_key.hex)" \
  -v "$(pwd)/config.json:/app/config.json:ro" \
  -v "$(pwd)/release_manifest.json:/app/release_manifest.json:ro" \
  -v /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data:/app/agent_data \
  -v /opt/hzai-LLM-Langchain-Chatbot-Agent/logs:/app/logs \
  -v /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/images:/app/output_images \
  langchain_gpt:main
```

Confirm:

```bash
sleep 20
sudo docker inspect langchain | grep -E '(agent_data|STRIPE_API_KEY|PHONEPE_MERCHANT)' | head
curl -s http://localhost:6777/status | head -c 200
curl -s http://localhost:6777/api/v1/intelligence/pricing | python3 -m json.tool | head -30
```

## Step 5 — Register PhonePe S2S callback on the merchant dashboard

Log into the PhonePe merchant portal and set the **server-to-server
callback URL** to the one HARTOS sends with every pay request:

```
<HARTOS_PUBLIC_URL>/api/v1/intelligence/phonepe/callback
```

(`HARTOS_PUBLIC_URL` is whatever public URL fronts your central instance
— Cloudflare Tunnel, ALB DNS, Kong upstream, etc.  The URL must reach the
container's port 6777.)

PhonePe rejects mixed-case URLs in some configs — keep it all
lowercase.

## Step 6 — Unpause the marketing goal

```bash
# Find the marketing goal slug + id:
curl -s http://localhost:6777/api/goals?goal_type=marketing | python3 -m json.tool

# Unpause the bootstrap_marketing_awareness goal (replace <id> with the
# id from above):
curl -X POST http://localhost:6777/api/goals/<id>/unpause \
    -H "Authorization: Bearer $HEVOLVE_API_KEY"
```

If the goal doesn't exist, restart the container — the bootstrap
seeding will create it.  See `integrations/agent_engine/goal_seeding.py`
`SEED_BOOTSTRAP_GOALS` for the canonical list.

## Step 7 — Smoke-test the revenue path

```bash
# Sign up a test user via the social API, get a JWT.
# (Skipped here — use whatever signup flow you already have for
# hevolve.ai accounts.)

# Create an API key for that user:
curl -X POST http://localhost:6777/api/v1/intelligence/keys \
    -H "Authorization: Bearer $TEST_USER_JWT" \
    -H "Content-Type: application/json" \
    -d '{"name": "smoke-test", "tier": "free"}'

# Try an upgrade via Stripe (USD):
curl -X POST http://localhost:6777/api/v1/intelligence/keys/<key_id>/upgrade \
    -H "Authorization: Bearer $TEST_USER_JWT" \
    -H "Content-Type: application/json" \
    -d '{"target_tier": "starter", "payment_method": "pm_card_visa"}'
# Expected: 200 OK with payment.status=completed, revenue_split present.

# Try an upgrade via PhonePe (INR):
curl -X POST http://localhost:6777/api/v1/intelligence/keys/<key_id>/upgrade/phonepe \
    -H "Authorization: Bearer $TEST_USER_JWT" \
    -H "Content-Type: application/json" \
    -d '{"target_tier": "starter", "mobile_number": "9876543210"}'
# Expected: 202 Accepted with redirect_url.  Open the redirect_url in a
# browser, complete the payment, watch the container logs for the
# callback POST and the tier-bump line.
```

## Step 8 — Verify revenue is flowing

Once any real or test payment completes:

```bash
# Total revenue (per-token + tier upgrades, both rails):
curl -s http://localhost:6777/api/v1/intelligence/usage/admin \
    -H "Authorization: Bearer $ADMIN_JWT"
```

Or via the revenue tool from inside HARTOS:

```bash
curl -X POST http://localhost:6777/chat \
    -H "Content-Type: application/json" \
    -d '{"user_id": "admin", "prompt_id": "rev-check", "prompt": "Run get_api_revenue_stats and summarize.", "create_agent": false}'
```

## What success looks like

After Step 7 completes successfully, the AP2 payment ledger at
`/app/agent_data/payment_ledger.json` contains COMPLETED entries with
`metadata.kind == "tier_upgrade"`.  `get_api_revenue_stats` reports
the cumulative dollars under
`revenue.subscription_upgrades_usd`.  90/9/1 split lands in the
canonical aggregator on the next nightly settle.

## If anything fails

- Container won't start: `sudo docker logs langchain | tail -50`
- Auth 401 from daemon: confirm `HEVOLVE_API_KEY` is in `.env` OR
  the JWT secret env vars used by `integrations.social.auth.generate_jwt`
  are present.
- PhonePe 503 on `/upgrade/phonepe`: gateway not registered —
  check `PHONEPE_MERCHANT_ID` and `PHONEPE_SALT_KEY` made it into the
  container with `sudo docker exec langchain env | grep PHONEPE`.
- Marketing goal stays paused: check `agent_data/goals.json` for the
  `paused_reason` field — likely `auth_failed_repeatedly` if step 3
  missed.

The verification script `deploy/verify_revenue_rails.sh` re-runs the
read-only probes so you can sanity-check after each step.
