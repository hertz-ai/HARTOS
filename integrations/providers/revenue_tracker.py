"""
Revenue Tracker — understand earning spark vs compute spent.

Tracks two sides of every transaction:
  COST:    what we paid (API calls, local GPU time, bandwidth)
  REVENUE: what we earned (affiliate commissions, user credits, subscriptions)

Key metrics:
  earning_spark = revenue / cost  (>1.0 = profitable)
  cost_per_request = total_cost / total_requests
  revenue_per_user = total_revenue / active_users
  net_margin = (revenue - cost) / revenue

Persisted at ~/Documents/Nunba/data/revenue_tracker.json.
Exposed via /api/admin/providers/revenue endpoint.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CostEntry:
    """A single cost event."""
    timestamp: float = 0.0
    provider_id: str = ''
    model_id: str = ''
    cost_usd: float = 0.0
    cost_type: str = 'api'       # api, compute, bandwidth
    tokens_used: int = 0
    request_type: str = 'llm'    # llm, image_gen, video_gen, etc.


@dataclass
class RevenueEntry:
    """A single revenue event."""
    timestamp: float = 0.0
    source: str = ''             # affiliate, credits, subscription
    provider_id: str = ''
    amount_usd: float = 0.0
    user_id: str = ''
    event_type: str = ''         # purchase, commission, referral


@dataclass
class PeriodStats:
    """Aggregated stats for a time period."""
    period: str = ''             # 'hour', 'day', 'week', 'month'
    start_ts: float = 0.0
    end_ts: float = 0.0
    total_cost: float = 0.0
    total_revenue: float = 0.0
    total_requests: int = 0
    earning_spark: float = 0.0   # revenue / cost
    net_margin: float = 0.0      # (revenue - cost) / revenue
    cost_by_provider: Dict[str, float] = field(default_factory=dict)
    cost_by_type: Dict[str, float] = field(default_factory=dict)
    revenue_by_source: Dict[str, float] = field(default_factory=dict)
    top_models: List[Dict] = field(default_factory=list)


class RevenueTracker:
    """Track revenue vs compute cost for the earning spark metric."""

    def __init__(self, tracker_path: Optional[str] = None):
        try:
            from core.platform_paths import get_db_dir
            data_dir = Path(get_db_dir())
        except ImportError:
            data_dir = Path.home() / 'Documents' / 'Nunba' / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)

        self._path = Path(tracker_path) if tracker_path else data_dir / 'revenue_tracker.json'
        self._lock = threading.Lock()

        # In-memory buffers (persisted periodically)
        self._costs: List[CostEntry] = []
        self._revenues: List[RevenueEntry] = []

        # Running totals
        self._total_cost = 0.0
        self._total_revenue = 0.0
        self._total_requests = 0

        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path, 'r') as f:
                    data = json.load(f)
                self._total_cost = data.get('total_cost', 0.0)
                self._total_revenue = data.get('total_revenue', 0.0)
                self._total_requests = data.get('total_requests', 0)
                # Load recent entries (last 24h only — older data is aggregated)
                for c in data.get('recent_costs', []):
                    self._costs.append(CostEntry(**{
                        k: v for k, v in c.items()
                        if k in CostEntry.__dataclass_fields__}))
                for r in data.get('recent_revenues', []):
                    self._revenues.append(RevenueEntry(**{
                        k: v for k, v in r.items()
                        if k in RevenueEntry.__dataclass_fields__}))
            except Exception as e:
                logger.warning("Failed to load revenue tracker: %s", e)

    def save(self):
        # Keep only last 24h of entries
        cutoff = time.time() - 86400
        with self._lock:
            recent_costs = [asdict(c) for c in self._costs if c.timestamp > cutoff]
            recent_revenues = [asdict(r) for r in self._revenues if r.timestamp > cutoff]
            data = {
                'total_cost': self._total_cost,
                'total_revenue': self._total_revenue,
                'total_requests': self._total_requests,
                'recent_costs': recent_costs,
                'recent_revenues': recent_revenues,
                'last_saved': time.time(),
            }
        try:
            with open(self._path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save revenue tracker: %s", e)

    # ── Recording ─────────────────────────────────────────────────────

    def record_cost(self, provider_id: str, model_id: str, cost_usd: float,
                    tokens_used: int = 0, request_type: str = 'llm',
                    cost_type: str = 'api'):
        """Record an API cost event (called by gateway after each request)."""
        entry = CostEntry(
            timestamp=time.time(), provider_id=provider_id,
            model_id=model_id, cost_usd=cost_usd,
            cost_type=cost_type, tokens_used=tokens_used,
            request_type=request_type,
        )
        with self._lock:
            self._costs.append(entry)
            self._total_cost += cost_usd
            self._total_requests += 1

        # Auto-save every 50 requests
        if self._total_requests % 50 == 0:
            self.save()

    def record_revenue(self, source: str, amount_usd: float,
                       provider_id: str = '', user_id: str = '',
                       event_type: str = 'commission'):
        """Record a revenue event (affiliate commission, credit purchase, etc.)."""
        entry = RevenueEntry(
            timestamp=time.time(), source=source,
            provider_id=provider_id, amount_usd=amount_usd,
            user_id=user_id, event_type=event_type,
        )
        with self._lock:
            self._revenues.append(entry)
            self._total_revenue += amount_usd
        self.save()

    # ── Analytics ─────────────────────────────────────────────────────

    def get_earning_spark(self) -> float:
        """Revenue / Cost ratio. >1.0 = profitable."""
        if self._total_cost <= 0:
            return float('inf') if self._total_revenue > 0 else 0.0
        return self._total_revenue / self._total_cost

    def get_summary(self) -> Dict[str, Any]:
        """Full analytics summary for dashboard."""
        spark = self.get_earning_spark()
        net_margin = ((self._total_revenue - self._total_cost) / self._total_revenue
                      if self._total_revenue > 0 else 0.0)

        # Cost breakdown by provider
        cost_by_provider: Dict[str, float] = {}
        cost_by_type: Dict[str, float] = {}
        for c in self._costs:
            cost_by_provider[c.provider_id] = cost_by_provider.get(
                c.provider_id, 0) + c.cost_usd
            cost_by_type[c.request_type] = cost_by_type.get(
                c.request_type, 0) + c.cost_usd

        # Revenue breakdown
        revenue_by_source: Dict[str, float] = {}
        for r in self._revenues:
            revenue_by_source[r.source] = revenue_by_source.get(
                r.source, 0) + r.amount_usd

        # Top cost models
        model_costs: Dict[str, float] = {}
        for c in self._costs:
            key = f"{c.provider_id}/{c.model_id}"
            model_costs[key] = model_costs.get(key, 0) + c.cost_usd
        top_models = sorted(model_costs.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            'total_cost_usd': round(self._total_cost, 6),
            'total_revenue_usd': round(self._total_revenue, 6),
            'net_profit_usd': round(self._total_revenue - self._total_cost, 6),
            'earning_spark': round(spark, 3),
            'net_margin_pct': round(net_margin * 100, 1),
            'total_requests': self._total_requests,
            'cost_per_request': round(self._total_cost / max(1, self._total_requests), 6),
            'cost_by_provider': {k: round(v, 6) for k, v in
                                 sorted(cost_by_provider.items(), key=lambda x: x[1], reverse=True)},
            'cost_by_type': {k: round(v, 6) for k, v in cost_by_type.items()},
            'revenue_by_source': {k: round(v, 6) for k, v in revenue_by_source.items()},
            'top_cost_models': [{'model': m, 'cost': round(c, 6)} for m, c in top_models],
        }

    def get_period_stats(self, hours: int = 24) -> PeriodStats:
        """Get stats for a specific time period (last N hours)."""
        cutoff = time.time() - (hours * 3600)
        costs = [c for c in self._costs if c.timestamp > cutoff]
        revenues = [r for r in self._revenues if r.timestamp > cutoff]

        total_cost = sum(c.cost_usd for c in costs)
        total_revenue = sum(r.amount_usd for r in revenues)
        spark = total_revenue / total_cost if total_cost > 0 else 0

        return PeriodStats(
            period=f'last_{hours}h',
            start_ts=cutoff,
            end_ts=time.time(),
            total_cost=total_cost,
            total_revenue=total_revenue,
            total_requests=len(costs),
            earning_spark=spark,
            net_margin=((total_revenue - total_cost) / total_revenue
                        if total_revenue > 0 else 0),
        )


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_tracker: Optional[RevenueTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> RevenueTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = RevenueTracker()
    return _tracker
