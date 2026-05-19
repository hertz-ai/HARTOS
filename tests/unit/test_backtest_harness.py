"""Tests for the Polymarket backtest harness (#203 trading half).

The harness must:
  - parse Gamma's outcomePrices JSON string defensively
  - grade a market as YES/NO only when it's truly resolved
  - compute Sharpe / win-rate / drawdown correctly on a hand-rolled tape
  - never produce negative bankroll (bankruptcy guard)
  - return zero metrics for the null strategy on any tape

These are the math-correctness tests. The harness's `fetch_resolved_markets`
hits the live Gamma API and is exercised only in the integration test
that runs the demo end-to-end.
"""
from __future__ import annotations

import json

import pytest

from integrations.trading.backtest_harness import (
    BacktestResult,
    BetSignal,
    _resolved_outcome,
    _yes_price,
    compare_all_strategies,
    contrarian,
    favorite_bias,
    momentum,
    null_strategy,
    run_backtest,
)


# ─── _yes_price helper ─────────────────────────────────────────────


def test_yes_price_parses_gamma_json_string():
    market = {'outcomePrices': '["0.42", "0.58"]'}
    assert _yes_price(market) == 0.42


def test_yes_price_handles_list_form():
    market = {'outcomePrices': [0.7, 0.3]}
    assert _yes_price(market) == 0.7


def test_yes_price_returns_none_on_missing_key():
    assert _yes_price({}) is None
    assert _yes_price({'outcomePrices': None}) is None


def test_yes_price_returns_none_on_bad_json():
    assert _yes_price({'outcomePrices': 'not json'}) is None


def test_yes_price_prefers_last_traded_over_outcome_prices():
    """On resolved markets, outcomePrices is the SETTLED value (1.0/0.0),
    not the entry price.  lastTradePrice carries the pre-close market
    consensus and MUST take priority — otherwise every backtest bet
    has zero PnL (entry == exit).  This is the bug found in the first
    demo run on real Gamma data.
    """
    market = {
        'lastTradePrice': 0.45,
        'outcomePrices': '["1.0", "0.0"]',  # resolved YES
    }
    assert _yes_price(market) == 0.45  # entry, not settlement


def test_yes_price_falls_back_to_outcome_prices_without_last_traded():
    market = {'outcomePrices': '["0.32", "0.68"]'}
    assert _yes_price(market) == 0.32


def test_yes_price_ignores_invalid_last_traded():
    market = {
        'lastTradePrice': 'garbage',
        'outcomePrices': '["0.4", "0.6"]',
    }
    assert _yes_price(market) == 0.4

    market = {
        'lastTradePrice': 1.5,  # out of [0,1] range
        'outcomePrices': '["0.4", "0.6"]',
    }
    assert _yes_price(market) == 0.4


# ─── _resolved_outcome helper ──────────────────────────────────────


def test_resolved_outcome_unclosed_returns_none():
    assert _resolved_outcome({'closed': False}) is None


def test_resolved_outcome_explicit_yes():
    m = {'closed': True, 'resolvedOutcome': 'YES'}
    assert _resolved_outcome(m) == 'YES'


def test_resolved_outcome_falls_back_to_price():
    # YES@1.0 means YES won
    m = {'closed': True, 'outcomePrices': '["1.0", "0.0"]'}
    assert _resolved_outcome(m) == 'YES'
    # NO@1.0 means NO won
    m = {'closed': True, 'outcomePrices': '["0.0", "1.0"]'}
    assert _resolved_outcome(m) == 'NO'


def test_resolved_outcome_returns_none_on_ambiguous_prices():
    m = {'closed': True, 'outcomePrices': '["0.5", "0.5"]'}
    assert _resolved_outcome(m) is None


# ─── Strategies ────────────────────────────────────────────────────


def test_favorite_bias_skips_under_threshold():
    assert favorite_bias({'outcomePrices': '["0.5", "0.5"]'}) is None
    assert favorite_bias({'outcomePrices': '["0.54", "0.46"]'}) is None


def test_favorite_bias_bets_yes_above_threshold():
    sig = favorite_bias({'outcomePrices': '["0.70", "0.30"]'})
    assert sig is not None
    assert sig.side == 'YES'
    assert sig.stake_pct == 0.02


def test_contrarian_only_bets_at_extremes():
    assert contrarian({'outcomePrices': '["0.50", "0.50"]'}) is None
    assert contrarian({'outcomePrices': '["0.80", "0.20"]'}) is None
    sig = contrarian({'outcomePrices': '["0.90", "0.10"]'})
    assert sig is not None
    assert sig.side == 'NO'


def test_momentum_handles_missing_field():
    assert momentum({'outcomePrices': '["0.5", "0.5"]'}) is None
    assert momentum({'oneDayPriceChange': None}) is None
    assert momentum({'oneDayPriceChange': 'garbage'}) is None


def test_momentum_picks_side_by_sign():
    up = momentum({'oneDayPriceChange': 0.08})
    assert up.side == 'YES'
    down = momentum({'oneDayPriceChange': -0.08})
    assert down.side == 'NO'


def test_null_strategy_always_skips():
    assert null_strategy({'outcomePrices': '["0.99", "0.01"]'}) is None


# ─── run_backtest — math correctness ───────────────────────────────


def _make_market(yes_price: float, outcome: str) -> dict:
    """Build a closed market for the engine."""
    return {
        'closed': True,
        'outcomePrices': json.dumps([str(yes_price), str(1.0 - yes_price)]),
        'resolvedOutcome': outcome,
    }


def test_backtest_zero_bets_for_null_strategy():
    markets = [_make_market(0.6, 'YES') for _ in range(10)]
    result = run_backtest(markets, null_strategy, 'null',
                          starting_bankroll=1000.0)
    assert result.n_bets == 0
    assert result.win_rate == 0.0
    assert result.total_return_pct == 0.0
    assert result.final_bankroll == 1000.0
    assert result.sharpe_annualized == 0.0


def test_backtest_winning_yes_bet_pnl():
    """Bet $20 on YES at price 0.5 (because 0.5 < 0.55 → favorite_bias
    won't fire; use a 0.60 favorite, bet 2% of 1000 = $20, win at 0.6)
    Payoff = stake / price = 20/0.6 = 33.33.  PnL = 33.33 - 20 = 13.33
    """
    market = _make_market(0.60, 'YES')
    result = run_backtest([market], favorite_bias, 'fb',
                          starting_bankroll=1000.0)
    assert result.n_bets == 1
    assert result.win_rate == 1.0
    expected_payoff = 20.0 / 0.60
    expected_bankroll = 1000.0 - 20.0 + expected_payoff
    assert abs(result.final_bankroll - expected_bankroll) < 0.01


def test_backtest_losing_yes_bet_pnl():
    """Same market but resolves NO — lose the $20 stake."""
    market = _make_market(0.60, 'NO')
    result = run_backtest([market], favorite_bias, 'fb',
                          starting_bankroll=1000.0)
    assert result.n_bets == 1
    assert result.win_rate == 0.0
    assert abs(result.final_bankroll - 980.0) < 0.01


def test_backtest_bankruptcy_guard_clips_at_zero():
    """Twenty consecutive losing bets at 50% stake each should bankrupt
    the strategy but never produce a negative bankroll.
    """
    def all_in(m):
        return BetSignal(side='YES', stake_pct=0.50)
    markets = [_make_market(0.50, 'NO') for _ in range(20)]
    result = run_backtest(markets, all_in, 'all_in',
                          starting_bankroll=100.0)
    assert result.final_bankroll >= 0.0


def test_backtest_drawdown_tracking():
    """Two losses followed by a win — drawdown peaks after the second loss."""
    markets = [
        _make_market(0.60, 'NO'),  # lose 20
        _make_market(0.60, 'NO'),  # lose 19.6 (2% of 980)
        _make_market(0.60, 'YES'),  # win
    ]
    result = run_backtest(markets, favorite_bias, 'fb',
                          starting_bankroll=1000.0)
    # Drawdown should reflect the 2-loss low point.
    assert result.max_drawdown_pct > 0.0
    assert result.n_bets == 3


# ─── compare_all_strategies ────────────────────────────────────────


def test_compare_all_returns_every_strategy():
    markets = [_make_market(0.6, 'YES') for _ in range(5)]
    out = compare_all_strategies(markets, starting_bankroll=500.0)
    assert set(out.keys()) == {
        'favorite_bias', 'contrarian', 'momentum', 'null_strategy',
    }
    for name, row in out.items():
        assert 'sharpe_annualized' in row
        assert 'final_bankroll' in row
        assert 'win_rate' in row


# ─── BacktestResult.to_dict shape ──────────────────────────────────


def test_result_to_dict_rounds_values():
    # Use values that round unambiguously in IEEE 754 — 0.55555 isn't
    # exact in float so its rounding is platform-dependent.  These are.
    r = BacktestResult(
        strategy='x', n_bets=5, win_rate=0.625,
        total_return_pct=12.34567, sharpe_annualized=1.234567,
        max_drawdown_pct=8.7654, final_bankroll=1123.456)
    d = r.to_dict()
    assert d['strategy'] == 'x'
    assert d['n_bets'] == 5
    assert d['win_rate'] == 0.625
    assert d['sharpe_annualized'] == 1.2346
    assert d['final_bankroll'] == 1123.456
