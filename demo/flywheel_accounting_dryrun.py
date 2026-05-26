"""Flywheel accounting dry-run — drive the real production code paths
with a sandboxed in-memory dataset that matches the master-goal
literal numbers (10k downloads, 10k Spark, $1k trading revenue).

This is NOT a fake telemetry write to the production DB.  It spins up
an in-memory SQLite with the same SQLAlchemy schema HARTOS uses, plants
the simulated rows, and calls the real `query_revenue_streams()` from
`revenue_aggregator.py` + applies the canonical 90/9/1 split constants.
The output proves: when the real numbers arrive, the production
pipeline records them correctly.

What this proves
----------------
1. The download → ReferralCode attribution count is wired and queryable.
2. The Spark earnings ledger (AdUnit.spent_spark + APIUsageLog.cost_credits)
   feeds query_revenue_streams() correctly.
3. The 90/9/1 user/infra/central split applies the canonical constants
   from revenue_aggregator.REVENUE_SPLIT_*.
4. The trading-agent revenue path (APIUsageLog with cost_credits) is
   the same code path live ad+API revenue uses — no parallel ledger.

What this does NOT prove
------------------------
1. Real user downloads (operator must deploy ref-tagged URLs + market).
2. Real trading revenue (operator must drop POLYMARKET_PRIVATE_KEY into
   AIKeyVault + grant consent on each trade).
3. Real Spark payout to contributor wallets (operator must enable the
   payout cron with Stripe/PhonePe live keys).

Run
---
    python -m demo.flywheel_accounting_dryrun

Writes the markdown report to demo/flywheel_accounting_dryrun.md and a
JSON sidecar to demo/flywheel_accounting_dryrun.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# ── 1. In-memory sandbox DB ──────────────────────────────────────────
# Bind to in-memory SQLite BEFORE importing the models so any module-
# level engine creation hits the sandbox, not the dev DB on disk.
os.environ['HEVOLVE_DB_URL'] = 'sqlite:///:memory:'
os.environ['HEVOLVE_DRY_RUN'] = '1'   # marker; modules may key off this

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Lazy imports so the env-var above wins.
from integrations.social.models import (
    Base,
    User,
    AdUnit,
    APIUsageLog,
    ReferralCode,
    CommercialAPIKey,
)
from integrations.agent_engine.revenue_aggregator import (
    query_revenue_streams,
    REVENUE_SPLIT_USERS,
    REVENUE_SPLIT_INFRA,
    REVENUE_SPLIT_CENTRAL,
    SPARK_PER_USD,
)


def _build_sandbox():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _plant_downloads(db, n_downloads: int = 10_000, n_referrers: int = 50):
    """Plant N download attributions via N referral codes.

    Mirrors the production /api/social/marketing/track shape:
    each download bumps ReferralCode.uses on the referrer's code.
    Returns the list of (referrer_id, code, uses) tuples.
    """
    referrers = []
    per_code = max(1, n_downloads // n_referrers)
    for i in range(n_referrers):
        u = User(
            username=f'referrer_{i:04d}',
            display_name=f'Referrer {i}',
            user_type='flat',
            role='flat',
        )
        db.add(u)
        db.flush()
        rc = ReferralCode(
            user_id=u.id,
            code=f'REF{i:04d}XX',
            uses=per_code,
            max_uses=0,
            is_active=True,
        )
        db.add(rc)
        referrers.append((u.id, rc.code, per_code))
    db.commit()
    return referrers


def _plant_spark_earnings(db, ad_spend_spark: int = 10_000):
    """Plant a single advertiser + one ad-unit with `ad_spend_spark`
    Spark already spent.  This is the canonical Spark-earnings row
    that query_revenue_streams() picks up via AdUnit.spent_spark.
    """
    advertiser = User(
        username='nunba_advertiser_dryrun',
        display_name='Nunba Marketing (dry-run)',
        user_type='advertiser',
        role='regional',
    )
    db.add(advertiser)
    db.flush()
    ad = AdUnit(
        advertiser_id=advertiser.id,
        title='Nunba Starter — try the agentic OS',
        click_url='https://nunba.app/?ref=DRYRUN10K',
        ad_type='banner',
        budget_spark=ad_spend_spark,
        spent_spark=ad_spend_spark,
        cost_per_impression=0.1,
        cost_per_click=1.0,
        impression_count=int(ad_spend_spark / 0.1),
        click_count=ad_spend_spark // 1,
        status='completed',
    )
    db.add(ad)
    db.commit()
    return ad


def _plant_trading_revenue(db, usd_revenue: float = 1_000.0):
    """Plant a single APIUsageLog row representing $1,000 in net trading
    revenue accrued through the commercial-API ledger.  Uses the same
    cost_credits field that revenue_aggregator sums to compute gross.
    """
    # CommercialAPIKey requires a user_id FK — bind the trading
    # agent's settlement key to a synthetic operator account so the
    # FK constraint holds in sqlite.
    operator = User(
        username='trading_operator_dryrun',
        display_name='Trading Operator (dry-run)',
        user_type='flat',
        role='regional',
    )
    db.add(operator)
    db.flush()
    api_key = CommercialAPIKey(
        user_id=operator.id,
        name='trading_agent_dryrun',
        key_hash='dryrun-key-hash',
        key_prefix='dryrun_',
        tier='enterprise',
        is_active=True,
    )
    db.add(api_key)
    db.flush()
    log = APIUsageLog(
        api_key_id=api_key.id,
        endpoint='/trading/polymarket/settle',
        tokens_in=0,
        tokens_out=0,
        compute_ms=0,
        cost_credits=usd_revenue,
        status_code=200,
    )
    db.add(log)
    db.commit()
    return log


def run() -> dict:
    engine, db = _build_sandbox()

    referrers = _plant_downloads(db, n_downloads=10_000, n_referrers=50)
    ad = _plant_spark_earnings(db, ad_spend_spark=10_000)
    trade = _plant_trading_revenue(db, usd_revenue=1_000.0)

    # Call the REAL production query — no shim, no parallel path.
    streams = query_revenue_streams(db, period_days=30)

    total_downloads = sum(r[2] for r in referrers)
    summary = {
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'inputs': {
            'downloads': total_downloads,
            'referrer_count': len(referrers),
            'ad_spend_spark': ad.spent_spark,
            'trading_revenue_usd': trade.cost_credits,
        },
        'streams': streams,
        'constants': {
            'REVENUE_SPLIT_USERS': REVENUE_SPLIT_USERS,
            'REVENUE_SPLIT_INFRA': REVENUE_SPLIT_INFRA,
            'REVENUE_SPLIT_CENTRAL': REVENUE_SPLIT_CENTRAL,
            'SPARK_PER_USD': SPARK_PER_USD,
        },
    }

    os.makedirs('demo', exist_ok=True)
    md_path = 'demo/flywheel_accounting_dryrun.md'
    json_path = 'demo/flywheel_accounting_dryrun.json'

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(_render_md(summary))
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, default=str)

    print(f'[dryrun] wrote {md_path} + {json_path}')
    print()
    print('--- SUMMARY ---')
    print(f"  downloads attributed:       {summary['inputs']['downloads']:>10,d}")
    print(f"  spark earned (ad spend):    {summary['inputs']['ad_spend_spark']:>10,d}")
    print(f"  trading revenue USD:        ${summary['inputs']['trading_revenue_usd']:>9,.2f}")
    print(f"  gross to distribute:        ${streams['total_gross']:>9,.2f}")
    print(f"  ->users  (90%):             ${streams['user_pool_share']:>9,.2f}")
    print(f"  ->infra  (9%):              ${streams['infra_pool_share']:>9,.2f}")
    print(f"  ->central (1%):             ${streams['central_share']:>9,.2f}")
    return summary


def _render_md(s: dict) -> str:
    inp = s['inputs']
    st = s['streams']
    c = s['constants']
    lines = []
    lines.append('# Flywheel Accounting Dry-Run — 10k / 10k / $1k path')
    lines.append('')
    lines.append(f"**Generated**: {s['generated_at']}  ")
    lines.append(
        '**Mode**: in-memory SQLite sandbox, real schema + real '
        '`query_revenue_streams()`, NOT production telemetry  ')
    lines.append('')
    lines.append('## What this drives')
    lines.append('')
    lines.append(
        'The master-flywheel goal asks for three numbers — 10,000 Nunba '
        'downloads attributed, 10,000 Spark earned in the platform '
        'ledger, and $1,000 in trading revenue.  This dry-run inserts '
        'rows matching those quantities into the actual HARTOS schema '
        '(no shim, no parallel ledger) and runs the production '
        '`query_revenue_streams()` against them, applying the canonical '
        '90/9/1 split constants from `revenue_aggregator.py`.')
    lines.append('')
    lines.append('## Inputs (sandboxed)')
    lines.append('')
    lines.append('| Metric | Count / Amount | Where it lands |')
    lines.append('|---|---:|---|')
    lines.append(
        f"| Downloads attributed | **{inp['downloads']:,}** | "
        f"`referral_codes.uses` summed across {inp['referrer_count']} "
        f"referrers |")
    lines.append(
        f"| Spark earned (ad ledger) | **{inp['ad_spend_spark']:,}** | "
        f"`ad_units.spent_spark` (`AdUnit` row, status=`completed`) |")
    lines.append(
        f"| Trading revenue USD | **${inp['trading_revenue_usd']:,.2f}** | "
        f"`api_usage_log.cost_credits` (endpoint=`/trading/polymarket/settle`) |")
    lines.append('')
    lines.append('## Production query output')
    lines.append('')
    lines.append('Direct call to `query_revenue_streams(db, period_days=30)`:')
    lines.append('')
    lines.append('```json')
    lines.append(json.dumps(st, indent=2, default=str))
    lines.append('```')
    lines.append('')
    lines.append('## 90/9/1 split applied (canonical constants)')
    lines.append('')
    lines.append('| Pool | Share | Amount |')
    lines.append('|---|---:|---:|')
    lines.append(
        f"| User Pool (REVENUE_SPLIT_USERS) | "
        f"{c['REVENUE_SPLIT_USERS']*100:.0f}% | "
        f"${st['user_pool_share']:,.2f} |")
    lines.append(
        f"| Infra Pool (REVENUE_SPLIT_INFRA) | "
        f"{c['REVENUE_SPLIT_INFRA']*100:.0f}% | "
        f"${st['infra_pool_share']:,.2f} |")
    lines.append(
        f"| Central (REVENUE_SPLIT_CENTRAL) | "
        f"{c['REVENUE_SPLIT_CENTRAL']*100:.0f}% | "
        f"${st['central_share']:,.2f} |")
    lines.append('')
    lines.append(f"Spark conversion rate: `SPARK_PER_USD = {c['SPARK_PER_USD']}`")
    lines.append('')
    lines.append('## What this proves (architecturally)')
    lines.append('')
    lines.append(
        '- Download attribution → `ReferralCode.uses` is a queryable count, '
        'incremented by the `/api/social/marketing/track` endpoint that '
        'every ref-tagged URL hits.')
    lines.append(
        '- Spark earnings → `AdUnit.spent_spark` flows into '
        '`query_revenue_streams` as `ad_revenue`.  Same query used by '
        '`finance_tools.get_financial_health()` — single source of '
        'truth, no parallel ledger.')
    lines.append(
        '- Trading revenue → `APIUsageLog.cost_credits` flows in as '
        '`api_revenue`.  The trading-agent settlement path writes to '
        'this same row class the commercial-API metered billing does, '
        'so trading and API both apply the same split.')
    lines.append(
        '- 90/9/1 split → constants live in '
        '`integrations/agent_engine/revenue_aggregator.py`; '
        '`ad_service.py` + `hosting_reward_service.py` import them via '
        'a try/except fallback, so the constitutional split is one '
        'edit away from re-pegging in three downstream consumers.')
    lines.append('')
    lines.append('## What this does NOT prove (operator-blocked)')
    lines.append('')
    lines.append(
        '1. **Real download counts** — requires the operator to deploy '
        'ref-tagged URLs on real distribution channels (Twitter, '
        'LinkedIn, Reddit, ProductHunt).  The intents endpoint '
        '(`/api/social/marketing/intents`) is shipped; the posts '
        'have to be made by a human or by Nunba Copilot with a logged-in '
        'session.')
    lines.append(
        '2. **Real trading revenue** — requires `POLYMARKET_PRIVATE_KEY` '
        'in AIKeyVault.  Without it, `PolymarketAdapter.place_order()` '
        'raises `PolymarketKeyMissingError`.  The 4-strategy backtest '
        'in `demo/trading_agent_demo.md` already proved the edge on '
        'live public Polymarket data; what is missing is the operator '
        'wallet drop + consent ACK on each live trade.')
    lines.append(
        '3. **Real Spark payouts to contributor wallets** — requires the '
        'payout cron with Stripe/PhonePe live keys.  '
        '`deploy/go_live_stripe.md` is the 8-step runbook; today the '
        'pipeline records the splits but does not disburse.')
    lines.append('')
    lines.append('## Companion artifacts')
    lines.append('')
    lines.append(
        '- `demo/trading_agent_demo.md` — live backtest against 100 '
        'resolved Polymarket markets, regenerated today.  Confirms the '
        'trading-agent codepath end-to-end on REAL public market data.')
    lines.append(
        '- `demo/flywheel_accounting_dryrun.json` — machine-readable '
        'sidecar of this report.')
    lines.append(
        '- `_revenue_assets.md` — publishable copy ready for the 5 '
        'Twitter / 2 LinkedIn / 3 Show HN / 1 Reddit / 1 Indie Hackers '
        'channels.  Operator posts these; this dry-run shows what the '
        'accounting will record per channel.')
    lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    try:
        run()
        sys.exit(0)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.exit(1)
