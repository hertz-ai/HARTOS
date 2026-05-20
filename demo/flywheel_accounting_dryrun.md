# Flywheel Accounting Dry-Run — 10k / 10k / $1k path

**Generated**: 2026-05-20T08:50:40.127306Z  
**Mode**: in-memory SQLite sandbox, real schema + real `query_revenue_streams()`, NOT production telemetry  

## What this drives

The master-flywheel goal asks for three numbers — 10,000 Nunba downloads attributed, 10,000 Spark earned in the platform ledger, and $1,000 in trading revenue.  This dry-run inserts rows matching those quantities into the actual HARTOS schema (no shim, no parallel ledger) and runs the production `query_revenue_streams()` against them, applying the canonical 90/9/1 split constants from `revenue_aggregator.py`.

## Inputs (sandboxed)

| Metric | Count / Amount | Where it lands |
|---|---:|---|
| Downloads attributed | **10,000** | `referral_codes.uses` summed across 50 referrers |
| Spark earned (ad ledger) | **10,000** | `ad_units.spent_spark` (`AdUnit` row, status=`completed`) |
| Trading revenue USD | **$1,000.00** | `api_usage_log.cost_credits` (endpoint=`/trading/polymarket/settle`) |

## Production query output

Direct call to `query_revenue_streams(db, period_days=30)`:

```json
{
  "period_days": 30,
  "api_revenue": 1000.0,
  "ad_revenue": 10000.0,
  "hosting_payouts": 0.0,
  "total_gross": 11000.0,
  "user_pool_share": 9900.0,
  "infra_pool_share": 990.0,
  "central_share": 110.0,
  "platform_share": 1100.0
}
```

## 90/9/1 split applied (canonical constants)

| Pool | Share | Amount |
|---|---:|---:|
| User Pool (REVENUE_SPLIT_USERS) | 90% | $9,900.00 |
| Infra Pool (REVENUE_SPLIT_INFRA) | 9% | $990.00 |
| Central (REVENUE_SPLIT_CENTRAL) | 1% | $110.00 |

Spark conversion rate: `SPARK_PER_USD = 100`

## What this proves (architecturally)

- Download attribution → `ReferralCode.uses` is a queryable count, incremented by the `/api/social/marketing/track` endpoint that every ref-tagged URL hits.
- Spark earnings → `AdUnit.spent_spark` flows into `query_revenue_streams` as `ad_revenue`.  Same query used by `finance_tools.get_financial_health()` — single source of truth, no parallel ledger.
- Trading revenue → `APIUsageLog.cost_credits` flows in as `api_revenue`.  The trading-agent settlement path writes to this same row class the commercial-API metered billing does, so trading and API both apply the same split.
- 90/9/1 split → constants live in `integrations/agent_engine/revenue_aggregator.py`; `ad_service.py` + `hosting_reward_service.py` import them via a try/except fallback, so the constitutional split is one edit away from re-pegging in three downstream consumers.

## What this does NOT prove (operator-blocked)

1. **Real download counts** — requires the operator to deploy ref-tagged URLs on real distribution channels (Twitter, LinkedIn, Reddit, ProductHunt).  The intents endpoint (`/api/social/marketing/intents`) is shipped; the posts have to be made by a human or by Nunba Copilot with a logged-in session.
2. **Real trading revenue** — requires `POLYMARKET_PRIVATE_KEY` in AIKeyVault.  Without it, `PolymarketAdapter.place_order()` raises `PolymarketKeyMissingError`.  The 4-strategy backtest in `demo/trading_agent_demo.md` already proved the edge on live public Polymarket data; what is missing is the operator wallet drop + consent ACK on each live trade.
3. **Real Spark payouts to contributor wallets** — requires the payout cron with Stripe/PhonePe live keys.  `deploy/go_live_stripe.md` is the 8-step runbook; today the pipeline records the splits but does not disburse.

## Companion artifacts

- `demo/trading_agent_demo.md` — live backtest against 100 resolved Polymarket markets, regenerated today.  Confirms the trading-agent codepath end-to-end on REAL public market data.
- `demo/flywheel_accounting_dryrun.json` — machine-readable sidecar of this report.
- `_revenue_assets.md` — publishable copy ready for the 5 Twitter / 2 LinkedIn / 3 Show HN / 1 Reddit / 1 Indie Hackers channels.  Operator posts these; this dry-run shows what the accounting will record per channel.
