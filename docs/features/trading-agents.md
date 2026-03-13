# Trading Agents

Paper trading with a path to live trading, gated by constitutional vote.

## Paper Trading

All trading begins in paper mode using simulated portfolios:

| Model | Purpose |
|-------|---------|
| **PaperPortfolio** | Tracks simulated holdings, cash balance, and total portfolio value. |
| **PaperTrade** | Records individual buy/sell transactions with timestamps, prices, and quantities. |

Paper trading runs continuously, building a track record that can be evaluated before any real money is at risk.

## Strategy

The default strategy is **long_term diversified**:

- Allocates across multiple asset classes.
- Rebalances periodically based on drift thresholds.
- Optimizes for risk-adjusted returns over months, not minutes.

## Auto-Funding

When platform revenue exceeds a configurable threshold, a portion is automatically allocated to the paper trading portfolio. This grows the simulated portfolio in proportion to platform success, providing a realistic test of what live trading would look like.

## Live Trading Activation

Transitioning from paper to live trading requires a **constitutional vote** via the thought experiment system (see [thought-experiments.md](thought-experiments.md)):

1. A thought experiment is created proposing live trading activation.
2. Network peers review the paper trading track record.
3. A majority vote is required to approve.
4. Only after approval can real funds be deployed.

This ensures no single operator can unilaterally risk platform funds.

## Source Files

- `integrations/social/models.py` (PaperPortfolio, PaperTrade)
- `integrations/agent_engine/` (trading goal execution)
