"""
Metrics Collector - System Metrics Collection and Export

Provides metrics collection for monitoring and observability.
Supports Prometheus export format for container monitoring.

Features:
- Message metrics (count, direction, channel)
- Latency tracking with histograms
- Error tracking by type
- Prometheus text format export
- Container-network compatible endpoints
- Volume-mounted persistence for metrics history
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class MetricValue:
    """A single metric value with metadata."""
    name: str
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def prometheus_format(self) -> str:
        """Format as Prometheus text format."""
        if self.labels:
            labels_str = ",".join(f'{k}="{v}"' for k, v in self.labels.items())
            return f'{self.name}{{{labels_str}}} {self.value}'
        return f'{self.name} {self.value}'


@dataclass
class HistogramBucket:
    """Histogram bucket for latency tracking."""
    le: float  # Less than or equal to
    count: int = 0


@dataclass
class Histogram:
    """Histogram for tracking distributions."""
    name: str
    buckets: List[HistogramBucket] = field(default_factory=list)
    sum_value: float = 0.0
    count: int = 0
    labels: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.buckets:
            # Default latency buckets (in ms)
            self.buckets = [
                HistogramBucket(le=5),
                HistogramBucket(le=10),
                HistogramBucket(le=25),
                HistogramBucket(le=50),
                HistogramBucket(le=100),
                HistogramBucket(le=250),
                HistogramBucket(le=500),
                HistogramBucket(le=1000),
                HistogramBucket(le=2500),
                HistogramBucket(le=5000),
                HistogramBucket(le=float('inf')),
            ]

    def observe(self, value: float) -> None:
        """Record an observation."""
        self.sum_value += value
        self.count += 1
        for bucket in self.buckets:
            if value <= bucket.le:
                bucket.count += 1

    def prometheus_format(self) -> List[str]:
        """Format as Prometheus text format."""
        lines = []
        labels_base = ",".join(f'{k}="{v}"' for k, v in self.labels.items())

        for bucket in self.buckets:
            le_str = "+Inf" if bucket.le == float('inf') else str(bucket.le)
            if labels_base:
                lines.append(f'{self.name}_bucket{{{labels_base},le="{le_str}"}} {bucket.count}')
            else:
                lines.append(f'{self.name}_bucket{{le="{le_str}"}} {bucket.count}')

        if labels_base:
            lines.append(f'{self.name}_sum{{{labels_base}}} {self.sum_value}')
            lines.append(f'{self.name}_count{{{labels_base}}} {self.count}')
        else:
            lines.append(f'{self.name}_sum {self.sum_value}')
            lines.append(f'{self.name}_count {self.count}')

        return lines


@dataclass
class Metrics:
    """Aggregated metrics container."""
    timestamp: str = ""
    period: str = "1h"
    # Message metrics
    messages_total: int = 0
    messages_by_channel: Dict[str, int] = field(default_factory=dict)
    messages_by_direction: Dict[str, int] = field(default_factory=dict)
    # Latency metrics
    latency_avg_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p90_ms: float = 0.0
    latency_p99_ms: float = 0.0
    latency_max_ms: float = 0.0
    latency_by_channel: Dict[str, float] = field(default_factory=dict)
    # Error metrics
    errors_total: int = 0
    errors_by_channel: Dict[str, int] = field(default_factory=dict)
    errors_by_type: Dict[str, int] = field(default_factory=dict)
    # Rate metrics
    messages_per_second: float = 0.0
    errors_per_second: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MetricsConfig:
    """Configuration for metrics collector."""
    # Persistence path (should be volume-mounted in Docker)
    persistence_path: Optional[str] = None
    # Retention settings
    retention_hours: int = 24
    aggregation_interval_seconds: int = 60
    # Prometheus settings
    prometheus_prefix: str = "hevolvebot"
    include_hostname_label: bool = True

    def get_persistence_path(self) -> str:
        """Get persistence path, defaulting to Docker-friendly location."""
        if self.persistence_path:
            return self.persistence_path
        return os.environ.get(
            "METRICS_DATA_PATH",
            "/app/data/metrics" if os.path.exists("/app") else "./agent_data/metrics"
        )


class MetricsCollector:
    """
    Collects and exports system metrics.

    Designed for Docker/container environments with:
    - Prometheus export format
    - Container-network compatible addressing
    - Volume-mounted persistence

    Usage:
        collector = MetricsCollector()

        # Record metrics
        collector.record_message("telegram", "inbound")
        collector.record_latency("telegram", 45.2)
        collector.record_error("telegram", "timeout")

        # Get aggregated metrics
        metrics = collector.get_metrics(period="1h")

        # Export for Prometheus
        prometheus_text = collector.export_prometheus()
    """

    def __init__(self, config: Optional[MetricsConfig] = None):
        self.config = config or MetricsConfig()
        self._start_time = time.time()
        self._lock = Lock()

        # Counters
        self._message_counter: Dict[Tuple[str, str], int] = defaultdict(int)  # (channel, direction)
        self._error_counter: Dict[Tuple[str, str], int] = defaultdict(int)  # (channel, error_type)

        # Histograms
        self._latency_histograms: Dict[str, Histogram] = {}

        # Time series data for aggregation
        self._message_times: List[Tuple[float, str, str]] = []  # (timestamp, channel, direction)
        self._latency_values: List[Tuple[float, str, float]] = []  # (timestamp, channel, latency_ms)
        self._error_times: List[Tuple[float, str, str]] = []  # (timestamp, channel, error_type)

        # Hostname for labels
        self._hostname = os.environ.get("HOSTNAME", os.environ.get("COMPUTERNAME", "unknown"))

        # Ensure persistence directory exists
        self._ensure_persistence_dir()

        # Load persisted state
        self._load_state()

    def _ensure_persistence_dir(self) -> None:
        """Ensure persistence directory exists."""
        path = self.config.get_persistence_path()
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create persistence directory {path}: {e}")

    def _get_state_file(self) -> str:
        """Get path to state file."""
        return os.path.join(self.config.get_persistence_path(), "metrics_state.json")

    def _load_state(self) -> None:
        """Load persisted state."""
        state_file = self._get_state_file()
        try:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    data = json.load(f)
                    # Restore counters
                    for key_str, value in data.get("message_counter", {}).items():
                        parts = key_str.split("|")
                        if len(parts) == 2:
                            self._message_counter[(parts[0], parts[1])] = value
                    for key_str, value in data.get("error_counter", {}).items():
                        parts = key_str.split("|")
                        if len(parts) == 2:
                            self._error_counter[(parts[0], parts[1])] = value
                    logger.info(f"Loaded metrics state from {state_file}")
        except Exception as e:
            logger.warning(f"Could not load metrics state: {e}")

    def _save_state(self) -> None:
        """Persist state to disk."""
        state_file = self._get_state_file()
        try:
            data = {
                "message_counter": {
                    f"{k[0]}|{k[1]}": v for k, v in self._message_counter.items()
                },
                "error_counter": {
                    f"{k[0]}|{k[1]}": v for k, v in self._error_counter.items()
                },
                "saved_at": datetime.now().isoformat(),
            }
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save metrics state: {e}")

    def _cleanup_old_data(self) -> None:
        """Clean up data older than retention period."""
        cutoff = time.time() - (self.config.retention_hours * 3600)

        with self._lock:
            self._message_times = [(t, c, d) for t, c, d in self._message_times if t > cutoff]
            self._latency_values = [(t, c, l) for t, c, l in self._latency_values if t > cutoff]
            self._error_times = [(t, c, e) for t, c, e in self._error_times if t > cutoff]

    def _get_or_create_histogram(self, channel: str) -> Histogram:
        """Get or create a latency histogram for a channel."""
        if channel not in self._latency_histograms:
            self._latency_histograms[channel] = Histogram(
                name=f"{self.config.prometheus_prefix}_request_latency_ms",
                labels={"channel": channel},
            )
        return self._latency_histograms[channel]

    def record_message(self, channel: str, direction: str) -> None:
        """
        Record a message being processed.

        Args:
            channel: Channel name (e.g., "telegram", "discord")
            direction: Message direction ("inbound" or "outbound")
        """
        now = time.time()
        with self._lock:
            self._message_counter[(channel, direction)] += 1
            self._message_times.append((now, channel, direction))

        self._save_state()

    def record_latency(self, channel: str, latency_ms: float) -> None:
        """
        Record a latency measurement.

        Args:
            channel: Channel name
            latency_ms: Latency in milliseconds
        """
        now = time.time()
        with self._lock:
            histogram = self._get_or_create_histogram(channel)
            histogram.observe(latency_ms)
            self._latency_values.append((now, channel, latency_ms))

    def record_error(self, channel: str, error_type: str) -> None:
        """
        Record an error.

        Args:
            channel: Channel name
            error_type: Type of error (e.g., "timeout", "rate_limit", "auth")
        """
        now = time.time()
        with self._lock:
            self._error_counter[(channel, error_type)] += 1
            self._error_times.append((now, channel, error_type))

        self._save_state()

    def _parse_period(self, period: str) -> float:
        """Parse period string to seconds."""
        unit = period[-1].lower()
        try:
            value = int(period[:-1])
        except ValueError:
            return 3600  # Default to 1 hour

        if unit == 's':
            return value
        elif unit == 'm':
            return value * 60
        elif unit == 'h':
            return value * 3600
        elif unit == 'd':
            return value * 86400
        return 3600

    def get_metrics(self, period: str = "1h") -> Metrics:
        """
        Get aggregated metrics for a time period.

        Args:
            period: Time period (e.g., "1h", "24h", "7d")

        Returns:
            Metrics object with aggregated data
        """
        self._cleanup_old_data()
        period_seconds = self._parse_period(period)
        cutoff = time.time() - period_seconds

        with self._lock:
            # Filter data by period
            messages_in_period = [(t, c, d) for t, c, d in self._message_times if t > cutoff]
            latencies_in_period = [(t, c, l) for t, c, l in self._latency_values if t > cutoff]
            errors_in_period = [(t, c, e) for t, c, e in self._error_times if t > cutoff]

            # Aggregate message counts
            messages_by_channel: Dict[str, int] = defaultdict(int)
            messages_by_direction: Dict[str, int] = defaultdict(int)
            for _, channel, direction in messages_in_period:
                messages_by_channel[channel] += 1
                messages_by_direction[direction] += 1

            # Aggregate latencies
            latency_by_channel: Dict[str, List[float]] = defaultdict(list)
            for _, channel, latency in latencies_in_period:
                latency_by_channel[channel].append(latency)

            all_latencies = [l for _, _, l in latencies_in_period]

            # Calculate latency percentiles
            latency_avg = 0.0
            latency_p50 = 0.0
            latency_p90 = 0.0
            latency_p99 = 0.0
            latency_max = 0.0

            if all_latencies:
                sorted_latencies = sorted(all_latencies)
                latency_avg = sum(all_latencies) / len(all_latencies)
                latency_p50 = sorted_latencies[int(len(sorted_latencies) * 0.5)]
                latency_p90 = sorted_latencies[int(len(sorted_latencies) * 0.9)]
                latency_p99 = sorted_latencies[min(int(len(sorted_latencies) * 0.99), len(sorted_latencies) - 1)]
                latency_max = sorted_latencies[-1]

            # Average latency by channel
            avg_latency_by_channel = {
                channel: sum(latencies) / len(latencies) if latencies else 0.0
                for channel, latencies in latency_by_channel.items()
            }

            # Aggregate errors
            errors_by_channel: Dict[str, int] = defaultdict(int)
            errors_by_type: Dict[str, int] = defaultdict(int)
            for _, channel, error_type in errors_in_period:
                errors_by_channel[channel] += 1
                errors_by_type[error_type] += 1

            # Calculate rates
            messages_per_second = len(messages_in_period) / period_seconds if period_seconds > 0 else 0.0
            errors_per_second = len(errors_in_period) / period_seconds if period_seconds > 0 else 0.0

            return Metrics(
                timestamp=datetime.now().isoformat(),
                period=period,
                messages_total=len(messages_in_period),
                messages_by_channel=dict(messages_by_channel),
                messages_by_direction=dict(messages_by_direction),
                latency_avg_ms=latency_avg,
                latency_p50_ms=latency_p50,
                latency_p90_ms=latency_p90,
                latency_p99_ms=latency_p99,
                latency_max_ms=latency_max,
                latency_by_channel=avg_latency_by_channel,
                errors_total=len(errors_in_period),
                errors_by_channel=dict(errors_by_channel),
                errors_by_type=dict(errors_by_type),
                messages_per_second=messages_per_second,
                errors_per_second=errors_per_second,
            )

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus text format.

        Returns:
            Prometheus-compatible metrics text
        """
        lines = []
        prefix = self.config.prometheus_prefix

        # Add hostname label if configured
        base_labels = {}
        if self.config.include_hostname_label:
            base_labels["hostname"] = self._hostname

        # Helper to format labels
        def format_labels(extra_labels: Dict[str, str]) -> str:
            all_labels = {**base_labels, **extra_labels}
            if not all_labels:
                return ""
            return "{" + ",".join(f'{k}="{v}"' for k, v in all_labels.items()) + "}"

        # Uptime
        uptime = time.time() - self._start_time
        lines.append(f"# HELP {prefix}_uptime_seconds Time since metrics collector started")
        lines.append(f"# TYPE {prefix}_uptime_seconds gauge")
        lines.append(f"{prefix}_uptime_seconds{format_labels({})} {uptime}")
        lines.append("")

        # Message counters
        lines.append(f"# HELP {prefix}_messages_total Total messages processed")
        lines.append(f"# TYPE {prefix}_messages_total counter")
        with self._lock:
            for (channel, direction), count in self._message_counter.items():
                labels = format_labels({"channel": channel, "direction": direction})
                lines.append(f"{prefix}_messages_total{labels} {count}")
        lines.append("")

        # Error counters
        lines.append(f"# HELP {prefix}_errors_total Total errors")
        lines.append(f"# TYPE {prefix}_errors_total counter")
        with self._lock:
            for (channel, error_type), count in self._error_counter.items():
                labels = format_labels({"channel": channel, "error_type": error_type})
                lines.append(f"{prefix}_errors_total{labels} {count}")
        lines.append("")

        # Latency histograms
        lines.append(f"# HELP {prefix}_request_latency_ms Request latency in milliseconds")
        lines.append(f"# TYPE {prefix}_request_latency_ms histogram")
        with self._lock:
            for channel, histogram in self._latency_histograms.items():
                for bucket in histogram.buckets:
                    le_str = "+Inf" if bucket.le == float('inf') else str(bucket.le)
                    labels = format_labels({"channel": channel, "le": le_str})
                    lines.append(f"{prefix}_request_latency_ms_bucket{labels} {bucket.count}")
                sum_labels = format_labels({"channel": channel})
                lines.append(f"{prefix}_request_latency_ms_sum{sum_labels} {histogram.sum_value}")
                lines.append(f"{prefix}_request_latency_ms_count{sum_labels} {histogram.count}")
        lines.append("")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._lock:
            self._message_counter.clear()
            self._error_counter.clear()
            self._latency_histograms.clear()
            self._message_times.clear()
            self._latency_values.clear()
            self._error_times.clear()

    def get_summary(self) -> Dict[str, Any]:
        """Get a quick summary of current metrics."""
        total_messages = sum(self._message_counter.values())
        total_errors = sum(self._error_counter.values())
        channels = set(k[0] for k in self._message_counter.keys())

        return {
            "total_messages": total_messages,
            "total_errors": total_errors,
            "channels_active": len(channels),
            "uptime_seconds": time.time() - self._start_time,
        }


# Singleton instance
_collector: Optional[MetricsCollector] = None


def get_metrics_collector(config: Optional[MetricsConfig] = None) -> MetricsCollector:
    """Get or create the global metrics collector instance."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector(config)
    return _collector
