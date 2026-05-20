# Trading Agent Demo — Polymarket Walk-Forward Backtest

**Generated**: 2026-05-20T08:40:19.830220Z  
**Data source**: live Polymarket Gamma API (public, no wallet key)  
**Tape**: 100 recently-resolved markets, starting bankroll $1000.00  

## Strategy comparison

| Strategy | Bets | Win % | Total Return | Sharpe | Max DD | Final $ |
|---|---:|---:|---:|---:|---:|---:|
| `favorite_bias` | 30 | 100.0% | +0.07% | 49.740 | 0.00% | $1000.68 |
| `null_strategy` | 0 | 0.0% | +0.00% | 0.000 | 0.00% | $1000.00 |
| `momentum` | 15 | 93.3% | -1.41% | -4.633 | 1.50% | $985.85 |
| `contrarian` | 30 | 0.0% | -26.03% | -216.095 | 26.03% | $739.70 |

## What this proves

- The trading-agent codepath end-to-end:  Polymarket Gamma API → `PolymarketAdapter.get_markets()` → strategy callbacks → `run_backtest()` → metrics → markdown.
- All four bundled strategies emit decisions on REAL public market data.  No mocks, no synthetic prices, no replay logs.
- Bankruptcy guard, drawdown tracking, and Sharpe annualization are pinned by 21 unit tests in `tests/unit/test_backtest_harness.py`.

## What this does NOT prove

- Past performance ≠ future returns.  These strategies need live forward-paper-trading before any real-money capital.
- Strategy bias: every strategy here is heuristic, not learned. The hive-consensus voting layer (`hive_consensus.py`) is where multi-model consensus actually selects which strategy to deploy on live capital.
- This run does NOT place any orders.  `PolymarketAdapter.place_order()` gates on `POLYMARKET_PRIVATE_KEY` in AIKeyVault + per-trade ConsentOverlayService approval; neither is wired here.

## Next-step blockers (operator action required)

1. **Wallet key**: drop `POLYMARKET_PRIVATE_KEY` into AIKeyVault via the credential-request UI.  Without it, every signing call raises `PolymarketKeyMissingError` (verified by unit test).
2. **Trader agent recipe**: wire the strategy with the highest Sharpe above (subject to a min-bets floor) into a recipe that dispatches one bet at a time, each gated on consent push.
3. **Demo video**: pair this report with a screen-recording of a forward-paper trade firing through the agent_engine path, then a real $5 trade with consent approval, then the result.
