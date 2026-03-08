"""
Trading Tools — AutoGen tool registration for paper/live trading agents.

Provides market data, technical indicators, sentiment analysis, and
paper trade execution. Follows finance_tools.py pattern.

Live trading is gated by constitutional vote (paper_trading=True default).
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def get_market_data(symbol: str, timeframe: str = '1d',
                    market: str = 'crypto') -> Dict:
    """Fetch price data for a symbol.

    Args:
        symbol: Ticker symbol (e.g. 'BTC-USD', 'AAPL')
        timeframe: '1m', '5m', '1h', '1d', '1w'
        market: 'crypto' or 'stocks'

    Returns: {symbol, timeframe, prices: [...], latest_price, volume}
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        period_map = {'1m': '1d', '5m': '5d', '1h': '5d', '1d': '1mo', '1w': '3mo'}
        period = period_map.get(timeframe, '1mo')
        hist = ticker.history(period=period, interval=timeframe)
        if hist.empty:
            return {'error': f'No data for {symbol}', 'symbol': symbol}

        prices = [
            {'date': str(idx), 'open': round(r['Open'], 4),
             'high': round(r['High'], 4), 'low': round(r['Low'], 4),
             'close': round(r['Close'], 4), 'volume': int(r['Volume'])}
            for idx, r in hist.tail(50).iterrows()
        ]
        latest = prices[-1] if prices else {}
        return {
            'symbol': symbol, 'timeframe': timeframe, 'market': market,
            'prices': prices, 'latest_price': latest.get('close'),
            'volume': latest.get('volume'),
        }
    except ImportError:
        return {'error': 'yfinance not installed', 'symbol': symbol,
                'hint': 'pip install yfinance'}
    except Exception as e:
        return {'error': str(e), 'symbol': symbol}


def get_technical_indicators(symbol: str,
                             indicators: Optional[List[str]] = None) -> Dict:
    """Calculate technical indicators for a symbol.

    Args:
        symbol: Ticker symbol
        indicators: List of ['rsi', 'macd', 'bollinger']. Defaults to all.

    Returns: {symbol, indicators: {rsi: float, macd: {...}, bollinger: {...}}}
    """
    indicators = indicators or ['rsi', 'macd', 'bollinger']
    result = {'symbol': symbol, 'indicators': {}}

    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period='3mo', interval='1d')
        if hist.empty:
            return {'error': f'No data for {symbol}'}
        closes = hist['Close']

        if 'rsi' in indicators:
            delta = closes.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, float('inf'))
            rsi = 100 - (100 / (1 + rs))
            result['indicators']['rsi'] = round(float(rsi.iloc[-1]), 2)

        if 'macd' in indicators:
            ema12 = closes.ewm(span=12).mean()
            ema26 = closes.ewm(span=26).mean()
            macd_line = ema12 - ema26
            signal = macd_line.ewm(span=9).mean()
            result['indicators']['macd'] = {
                'macd': round(float(macd_line.iloc[-1]), 4),
                'signal': round(float(signal.iloc[-1]), 4),
                'histogram': round(float(macd_line.iloc[-1] - signal.iloc[-1]), 4),
            }

        if 'bollinger' in indicators:
            sma20 = closes.rolling(20).mean()
            std20 = closes.rolling(20).std()
            result['indicators']['bollinger'] = {
                'upper': round(float(sma20.iloc[-1] + 2 * std20.iloc[-1]), 4),
                'middle': round(float(sma20.iloc[-1]), 4),
                'lower': round(float(sma20.iloc[-1] - 2 * std20.iloc[-1]), 4),
                'current': round(float(closes.iloc[-1]), 4),
            }

        return result
    except ImportError:
        return {'error': 'yfinance not installed', 'hint': 'pip install yfinance'}
    except Exception as e:
        return {'error': str(e), 'symbol': symbol}


def get_market_sentiment(symbol: str) -> Dict:
    """Analyse news-based sentiment for a symbol.

    Uses existing news tools if available, otherwise returns neutral.
    """
    try:
        from integrations.agent_engine.news_tools import fetch_news_feeds
        articles = fetch_news_feeds(query=symbol, limit=10)
        if not articles or 'error' in articles:
            return {'symbol': symbol, 'sentiment': 'neutral', 'score': 0.0,
                    'articles_analyzed': 0}

        items = articles.get('articles', [])
        positive = sum(1 for a in items if 'up' in (a.get('title', '') + a.get('summary', '')).lower()
                       or 'surge' in (a.get('title', '') + a.get('summary', '')).lower()
                       or 'bull' in (a.get('title', '') + a.get('summary', '')).lower())
        negative = sum(1 for a in items if 'down' in (a.get('title', '') + a.get('summary', '')).lower()
                       or 'crash' in (a.get('title', '') + a.get('summary', '')).lower()
                       or 'bear' in (a.get('title', '') + a.get('summary', '')).lower())

        total = len(items) or 1
        score = (positive - negative) / total
        sentiment = 'bullish' if score > 0.2 else 'bearish' if score < -0.2 else 'neutral'

        return {'symbol': symbol, 'sentiment': sentiment,
                'score': round(score, 3), 'articles_analyzed': len(items)}
    except ImportError:
        return {'symbol': symbol, 'sentiment': 'neutral', 'score': 0.0,
                'articles_analyzed': 0, 'note': 'news tools not available'}
    except Exception as e:
        return {'error': str(e), 'symbol': symbol}


def place_paper_trade(symbol: str, side: str, amount: float,
                      stop_loss: float, portfolio_id: str = None) -> Dict:
    """Execute a simulated paper trade.

    Args:
        symbol: Ticker symbol
        side: 'buy' or 'sell'
        amount: Trade amount in portfolio currency
        stop_loss: Stop-loss price (mandatory)
        portfolio_id: Portfolio to trade in (optional)

    Returns: Trade record dict
    """
    if side not in ('buy', 'sell'):
        return {'error': f'Invalid side: {side}. Must be buy or sell.'}
    if not stop_loss:
        return {'error': 'Stop-loss is mandatory for all trades.'}

    # Get current price
    data = get_market_data(symbol, '1d')
    if 'error' in data:
        return data
    price = data.get('latest_price')
    if not price:
        return {'error': f'Cannot determine price for {symbol}'}

    quantity = amount / price

    try:
        from integrations.social.models import db_session, PaperTrade, PaperPortfolio
        with db_session() as db:
            # Find or validate portfolio
            portfolio = None
            if portfolio_id:
                portfolio = db.query(PaperPortfolio).filter_by(
                    id=portfolio_id, status='active').first()

            if portfolio:
                # Check budget
                if amount > portfolio.current_balance:
                    return {'error': 'Insufficient balance',
                            'available': portfolio.current_balance}
                # Check cumulative loss halt
                if portfolio.total_pnl < 0:
                    loss_pct = abs(portfolio.total_pnl) / portfolio.initial_balance * 100
                    if loss_pct >= 10:
                        return {'error': 'Trading halted: cumulative loss exceeds 10%',
                                'loss_pct': round(loss_pct, 2)}
                portfolio.current_balance -= amount

            trade = PaperTrade(
                portfolio_id=portfolio_id or 'unlinked',
                symbol=symbol, side=side, quantity=quantity,
                entry_price=price, stop_loss=stop_loss,
                status='open',
            )
            db.add(trade)
            if portfolio:
                portfolio.total_trades = (portfolio.total_trades or 0) + 1
            return trade.to_dict()
    except ImportError:
        # No DB — return simulated result
        return {
            'id': 'paper_sim', 'symbol': symbol, 'side': side,
            'quantity': round(quantity, 6), 'entry_price': price,
            'stop_loss': stop_loss, 'status': 'open',
            'opened_at': datetime.utcnow().isoformat(),
        }


def get_portfolio_status(portfolio_id: str = None) -> Dict:
    """Get current portfolio positions, P&L, and risk metrics."""
    try:
        from integrations.social.models import db_session, PaperPortfolio, PaperTrade
        with db_session(commit=False) as db:
            if portfolio_id:
                portfolio = db.query(PaperPortfolio).filter_by(id=portfolio_id).first()
                if not portfolio:
                    return {'error': 'Portfolio not found'}
                open_trades = db.query(PaperTrade).filter_by(
                    portfolio_id=portfolio_id, status='open').all()
                return {
                    'portfolio': portfolio.to_dict(),
                    'open_positions': [t.to_dict() for t in open_trades],
                    'position_count': len(open_trades),
                }
            else:
                portfolios = db.query(PaperPortfolio).filter_by(status='active').all()
                return {'portfolios': [p.to_dict() for p in portfolios]}
    except ImportError:
        return {'error': 'Database not available'}


def get_trade_history(portfolio_id: str = None, limit: int = 20) -> Dict:
    """Get trade history for audit trail."""
    try:
        from integrations.social.models import db_session, PaperTrade
        with db_session(commit=False) as db:
            query = db.query(PaperTrade)
            if portfolio_id:
                query = query.filter_by(portfolio_id=portfolio_id)
            trades = query.order_by(PaperTrade.opened_at.desc()).limit(limit).all()
            return {'trades': [t.to_dict() for t in trades], 'count': len(trades)}
    except ImportError:
        return {'error': 'Database not available'}
