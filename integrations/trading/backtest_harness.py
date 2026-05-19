"""Walk-forward backtest harness for Polymarket prediction-market strategies.

Pulls live + historical market data from the public Gamma + CLOB APIs
(no wallet key required) and computes a strategy's Sharpe ratio against
the actual resolved outcomes.

Strategies bundled here:
    favorite_bias       — bet YES whenever the YES price > 0.55
    contrarian          — bet against the favorite when YES > 0.85
    momentum            — bet the side that gained >5% in last 24h
    null_strategy       — never bet (Sharpe = 0 baseline)

Each strategy is a callable(market_row) -> Optional[BetSignal] where
BetSignal is ('YES'|'NO', stake_pct).  The harness handles position
sizing (% of bankroll), bankruptcy guard, and P&L computation.

This module is intentionally independent of the adapter's wallet
state — backtest only reads public history.  Used by the trader
agent to prove edge BEFORE asking the user for the wallet key.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger('hevolve.trading.backtest')

GAMMA_HOST = 'https://gamma-api.polymarket.com'


# ─── Data shapes ───────────────────────────────────────────────────


@dataclass
class BetSignal:
    """A strategy's decision on one market.

    side: 'YES' or 'NO'
    stake_pct: fraction of current bankroll (0.0 .. 1.0)
    """
    side: str
    stake_pct: float


@dataclass
class BacktestResult:
    """One backtest run's aggregate metrics.

    Sharpe is computed on daily returns, annualized by sqrt(365).
    """
    strategy: str
    n_bets: int
    win_rate: float
    total_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    final_bankroll: float

    def to_dict(self) -> dict:
        return {
            'strategy': self.strategy,
            'n_bets': self.n_bets,
            'win_rate': round(self.win_rate, 4),
            'total_return_pct': round(self.total_return_pct, 4),
            'sharpe_annualized': round(self.sharpe_annualized, 4),
            'max_drawdown_pct': round(self.max_drawdown_pct, 4),
            'final_bankroll': round(self.final_bankroll, 4),
        }


# ─── Bundled strategies ────────────────────────────────────────────


def favorite_bias(market: dict) -> Optional[BetSignal]:
    """Bet YES when the implied probability exceeds 0.55.

    This is the simplest bias-test strategy.  It WILL underperform
    over time on liquid markets (the market price is the consensus),
    but it's a useful baseline — a strategy claiming to "beat the
    market" should beat this.
    """
    yes_price = _yes_price(market)
    if yes_price is None or yes_price <= 0.55:
        return None
    return BetSignal(side='YES', stake_pct=0.02)


def contrarian(market: dict) -> Optional[BetSignal]:
    """Bet AGAINST the favorite when YES > 0.85.

    Heuristic for finding mispriced longshots.  Pays out only when
    the consensus is overconfident.
    """
    yes_price = _yes_price(market)
    if yes_price is None or yes_price <= 0.85:
        return None
    return BetSignal(side='NO', stake_pct=0.01)


def momentum(market: dict) -> Optional[BetSignal]:
    """Bet the side that gained >5% in the trailing 24h.

    Requires market['oneDayPriceChange'] from Gamma's enriched market
    payload.  If absent, returns None.
    """
    change = market.get('oneDayPriceChange')
    if change is None:
        return None
    try:
        c = float(change)
    except (TypeError, ValueError):
        return None
    if abs(c) <= 0.05:
        return None
    side = 'YES' if c > 0 else 'NO'
    return BetSignal(side=side, stake_pct=0.015)


def null_strategy(market: dict) -> Optional[BetSignal]:
    """Never bet — Sharpe-0 baseline.

    Used to verify the harness math: a never-bets strategy must
    have n_bets=0, win_rate undefined (we report 0), and total
    return 0%.
    """
    return None


STRATEGIES: dict[str, Callable[[dict], Optional[BetSignal]]] = {
    'favorite_bias': favorite_bias,
    'contrarian': contrarian,
    'momentum': momentum,
    'null_strategy': null_strategy,
}


# ─── Helpers ───────────────────────────────────────────────────────


def _yes_price(market: dict) -> Optional[float]:
    """Extract the YES entry price as a float in [0, 1].

    PRIORITY:
        1. `lastTradePrice` — the most recent traded price BEFORE close.
           This is the pre-resolution market consensus the strategy
           bets against.  Always use this when available.
        2. `outcomePrices` first element — only as fallback.  On
           RESOLVED markets this is the settled value (1.0 or 0.0),
           not the entry price; using it for entry-pricing gives
           zero PnL on every bet (the bug found in the first demo run).

    Returns None if no usable price exists.
    """
    last_traded = market.get('lastTradePrice')
    if last_traded is not None:
        try:
            p = float(last_traded)
            if 0.0 <= p <= 1.0:
                return p
        except (TypeError, ValueError):
            pass

    raw = market.get('outcomePrices')
    if not raw:
        return None
    try:
        import json
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if not prices or len(prices) < 1:
            return None
        return float(prices[0])
    except (ValueError, TypeError, IndexError):
        return None


def _resolved_outcome(market: dict) -> Optional[str]:
    """Return 'YES' or 'NO' if the market resolved, else None.

    Gamma marks resolved markets with `closed=true` + the winning
    outcome in either `resolvedOutcome` (string) or by reading
    outcomePrices (winner is the 1.0 side).
    """
    if not market.get('closed'):
        return None
    explicit = market.get('resolvedOutcome')
    if isinstance(explicit, str) and explicit.upper() in ('YES', 'NO'):
        return explicit.upper()
    # Fall back to outcomePrices: winner is 1.0 (or near), loser 0.0
    raw = market.get('outcomePrices')
    if not raw:
        return None
    try:
        import json
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if len(prices) < 2:
            return None
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except (ValueError, TypeError, IndexError):
        return None
    if yes_p >= 0.95:
        return 'YES'
    if no_p >= 0.95:
        return 'NO'
    return None


# ─── Backtest engine ───────────────────────────────────────────────


def fetch_resolved_markets(limit: int = 500, timeout: float = 15.0) -> list:
    """Pull recently-resolved markets from Gamma.

    Returns markets where `closed=true` so the backtest can grade
    each bet against the actual outcome.
    """
    import requests
    params = {'closed': 'true', 'limit': limit, 'order': 'endDate',
              'ascending': 'false'}
    resp = requests.get(
        f'{GAMMA_HOST}/markets', params=params, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()
    # Defensive: only keep rows we can grade
    return [m for m in rows if _resolved_outcome(m) is not None]


def run_backtest(
    markets: list,
    strategy: Callable[[dict], Optional[BetSignal]],
    strategy_name: str,
    starting_bankroll: float = 1000.0,
) -> BacktestResult:
    """Walk through `markets` in chronological order, applying
    `strategy` to each.  When the strategy emits a BetSignal, size
    the bet against current bankroll, settle against the resolved
    outcome, update bankroll, and record the daily-return for Sharpe.

    Bankruptcy guard: bankroll cannot go negative.  If a bet's
    stake exceeds bankroll, it's clipped.
    """
    bankroll = starting_bankroll
    high_water = bankroll
    max_dd = 0.0
    wins = 0
    bets = 0
    daily_returns: list[float] = []

    for market in markets:
        signal = strategy(market)
        if signal is None:
            continue
        yes_price = _yes_price(market)
        outcome = _resolved_outcome(market)
        if yes_price is None or outcome is None:
            continue

        stake = min(bankroll * signal.stake_pct, bankroll)
        if stake <= 0:
            continue

        bets += 1
        # P&L: if we bet YES at price p and YES wins, payout = stake / p
        # (we bought shares at p that pay $1 each).  If NO wins, lose stake.
        if signal.side == outcome:
            if signal.side == 'YES':
                payoff = stake / yes_price
            else:
                no_price = 1.0 - yes_price
                payoff = stake / no_price
            pnl = payoff - stake
            wins += 1
        else:
            pnl = -stake

        bankroll += pnl
        if bankroll < 0:
            bankroll = 0  # bankruptcy clip
        daily_returns.append(pnl / max(starting_bankroll, 1e-9))

        high_water = max(high_water, bankroll)
        dd = (high_water - bankroll) / high_water if high_water > 0 else 0.0
        max_dd = max(max_dd, dd)

        if bankroll <= 0:
            break  # bankrupt, stop

    win_rate = wins / bets if bets else 0.0
    total_return_pct = (
        (bankroll - starting_bankroll) / starting_bankroll * 100.0
        if starting_bankroll else 0.0)

    # Annualized Sharpe: assume one return per market resolution,
    # markets resolve ~daily on average for an active book.
    if len(daily_returns) >= 2:
        mean_r = statistics.mean(daily_returns)
        std_r = statistics.stdev(daily_returns)
        sharpe = (mean_r / std_r * (365 ** 0.5)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    return BacktestResult(
        strategy=strategy_name, n_bets=bets, win_rate=win_rate,
        total_return_pct=total_return_pct,
        sharpe_annualized=sharpe,
        max_drawdown_pct=max_dd * 100.0,
        final_bankroll=bankroll)


def compare_all_strategies(
    markets: list,
    starting_bankroll: float = 1000.0,
) -> dict:
    """Run every bundled strategy on the same market set.

    Returns {name: BacktestResult.to_dict()}.  The trader agent
    uses this to pick which strategy to deploy on live capital.
    """
    out = {}
    for name, fn in STRATEGIES.items():
        result = run_backtest(
            markets, fn, name, starting_bankroll=starting_bankroll)
        out[name] = result.to_dict()
    return out
