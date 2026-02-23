"""
Tests for Trading Agents — prompt builder, paper trade records, portfolio P&L,
guardrails, goal type registration, and loss halt.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock


# ─── Prompt Builder Tests ───

class TestTradingPromptBuilder:
    def test_intraday_prompt(self):
        """Intraday strategy prompt includes RSI, MACD, Bollinger."""
        from integrations.agent_engine.goal_manager import _build_trading_prompt
        goal = {
            'title': 'BTC Intraday',
            'description': 'Trade BTC on technical signals',
            'config': {'strategy': 'intraday', 'market': 'crypto',
                       'paper_trading': True, 'max_budget': 5000},
        }
        prompt = _build_trading_prompt(goal)
        assert 'PAPER TRADING' in prompt
        assert 'INTRADAY' in prompt
        assert 'RSI' in prompt
        assert 'MACD' in prompt
        assert 'Bollinger' in prompt
        assert '5000' in prompt
        assert 'stop-loss' in prompt.lower() or 'Stop-loss' in prompt

    def test_longterm_prompt(self):
        """Long-term strategy prompt includes fundamental + sentiment."""
        from integrations.agent_engine.goal_manager import _build_trading_prompt
        goal = {
            'title': 'Diversified Portfolio',
            'description': 'Long-term diversified strategy',
            'config': {'strategy': 'long_term', 'market': 'stocks',
                       'paper_trading': True, 'max_budget': 10000},
        }
        prompt = _build_trading_prompt(goal)
        assert 'LONG-TERM' in prompt
        assert 'sentiment' in prompt.lower()
        assert 'Diversify' in prompt
        assert 'STOCKS' in prompt

    def test_live_trading_label(self):
        """Live trading mode shows LIVE TRADING label."""
        from integrations.agent_engine.goal_manager import _build_trading_prompt
        goal = {
            'title': 'Live Trade',
            'description': 'Go live',
            'config': {'paper_trading': False, 'strategy': 'intraday'},
        }
        prompt = _build_trading_prompt(goal)
        assert 'LIVE TRADING' in prompt

    def test_risk_rules_always_present(self):
        """All prompts include non-negotiable risk rules."""
        from integrations.agent_engine.goal_manager import _build_trading_prompt
        for strategy in ['intraday', 'long_term']:
            goal = {'title': 'T', 'description': 'D',
                    'config': {'strategy': strategy}}
            prompt = _build_trading_prompt(goal)
            assert 'NON-NEGOTIABLE' in prompt
            assert 'constitutional vote' in prompt.lower()
            assert 'HALT' in prompt


# ─── Paper Trade Record Tests ───

class TestPaperTradeRecord:
    def test_paper_trade_simulated(self):
        """place_paper_trade returns trade dict when DB unavailable."""
        from integrations.agent_engine.trading_tools import place_paper_trade
        mock_data = {
            'symbol': 'BTC-USD', 'latest_price': 50000.0,
            'timeframe': '1d', 'market': 'crypto',
            'prices': [], 'volume': 100,
        }
        # Force ImportError on DB models so it hits fallback path
        with patch('integrations.agent_engine.trading_tools.get_market_data',
                   return_value=mock_data), \
             patch.dict('sys.modules', {'integrations.social.models': None}):
            result = place_paper_trade('BTC-USD', 'buy', 1000.0, 48000.0)
        assert result['symbol'] == 'BTC-USD'
        assert result['side'] == 'buy'
        assert result['entry_price'] == 50000.0
        assert result['stop_loss'] == 48000.0
        assert result['status'] == 'open'

    def test_stop_loss_mandatory(self):
        """Trade rejected without stop-loss."""
        from integrations.agent_engine.trading_tools import place_paper_trade
        result = place_paper_trade('BTC-USD', 'buy', 1000.0, stop_loss=None)
        assert 'error' in result
        assert 'stop-loss' in result['error'].lower() or 'Stop-loss' in result['error']

    def test_invalid_side_rejected(self):
        """Trade rejected with invalid side."""
        from integrations.agent_engine.trading_tools import place_paper_trade
        result = place_paper_trade('BTC-USD', 'short', 1000.0, 48000.0)
        assert 'error' in result


# ─── Portfolio P&L Tests ───

class TestPortfolioPnL:
    def test_portfolio_model_to_dict(self):
        """PaperPortfolio.to_dict() includes win_rate."""
        from integrations.social.models import PaperPortfolio
        p = PaperPortfolio(
            user_id='u1', strategy='long_term',
            initial_balance=10000, current_balance=10500,
            total_pnl=500, total_trades=10, winning_trades=7,
        )
        d = p.to_dict()
        assert d['win_rate'] == pytest.approx(0.7)
        assert d['total_pnl'] == 500

    def test_portfolio_zero_trades_win_rate(self):
        """Win rate is 0.0 when no trades executed."""
        from integrations.social.models import PaperPortfolio
        p = PaperPortfolio(user_id='u1', total_trades=0, winning_trades=0)
        assert p.to_dict()['win_rate'] == 0.0


# ─── Guardrail Tests ───

class TestTradingGuardrails:
    def test_guardrail_blocks_live_trading_goal(self):
        """GoalManager.create_goal checks guardrails on trading goals."""
        from integrations.agent_engine.goal_manager import GoalManager
        db = MagicMock()
        # Mock guardrails at the import location inside create_goal
        mock_cf = MagicMock()
        mock_cf.check_goal.return_value = (False, 'live trading not approved')
        mock_he = MagicMock()
        mock_he.check_goal_ethos.return_value = (True, '')
        mock_guardrails = MagicMock()
        mock_guardrails.ConstitutionalFilter = mock_cf
        mock_guardrails.HiveEthos = mock_he
        with patch.dict('sys.modules', {'security.hive_guardrails': mock_guardrails}):
            result = GoalManager.create_goal(
                db, 'trading', 'Go Live',
                config={'paper_trading': False})
        assert not result.get('success', True)
        assert 'Guardrail' in result.get('error', '')


# ─── Goal Type Registration Tests ───

class TestTradingGoalType:
    def test_trading_type_registered(self):
        """'trading' goal type is in the prompt builder registry."""
        from integrations.agent_engine.goal_manager import _prompt_builders
        assert 'trading' in _prompt_builders


# ─── Loss Halt Tests ───

class TestLossHalt:
    def test_halt_on_max_loss(self):
        """Paper trade rejected when portfolio cumulative loss exceeds 10%."""
        from integrations.agent_engine.trading_tools import place_paper_trade
        mock_portfolio = MagicMock()
        mock_portfolio.current_balance = 8000
        mock_portfolio.initial_balance = 10000
        mock_portfolio.total_pnl = -1100  # 11% loss
        mock_portfolio.status = 'active'

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_portfolio

        mock_data = {
            'symbol': 'BTC-USD', 'latest_price': 50000.0,
            'prices': [], 'volume': 100,
        }
        with patch('integrations.agent_engine.trading_tools.get_market_data',
                   return_value=mock_data), \
             patch('integrations.social.models.get_db', return_value=mock_db), \
             patch('integrations.social.models.PaperTrade'), \
             patch('integrations.social.models.PaperPortfolio'):
            result = place_paper_trade('BTC-USD', 'buy', 500.0, 48000.0,
                                       portfolio_id='port-1')
        assert 'error' in result
        assert 'halted' in result['error'].lower() or 'loss' in result['error'].lower()
