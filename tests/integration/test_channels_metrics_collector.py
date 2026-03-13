"""
Tests for Metrics Collector

Tests metrics recording, aggregation, Prometheus export,
and Docker persistence features.
"""

import asyncio
import json
import os
import pytest
import tempfile
import time
from datetime import datetime, timedelta

# Import the metrics module
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.admin.metrics import (
    MetricsCollector,
    MetricsConfig,
    Metrics,
    Histogram,
    HistogramBucket,
    MetricValue,
    get_metrics_collector,
)


class TestMetricValue:
    """Tests for MetricValue dataclass."""

    def test_create_metric(self):
        """Test creating a metric value."""
        metric = MetricValue(
            name="test_metric",
            value=42.0,
            labels={"channel": "telegram"},
        )
        assert metric.name == "test_metric"
        assert metric.value == 42.0
        assert metric.labels["channel"] == "telegram"

    def test_prometheus_format_no_labels(self):
        """Test Prometheus format without labels."""
        metric = MetricValue(name="simple_metric", value=100.0)
        output = metric.prometheus_format()
        assert output == "simple_metric 100.0"

    def test_prometheus_format_with_labels(self):
        """Test Prometheus format with labels."""
        metric = MetricValue(
            name="labeled_metric",
            value=50.0,
            labels={"channel": "discord", "direction": "inbound"},
        )
        output = metric.prometheus_format()
        assert "labeled_metric{" in output
        assert 'channel="discord"' in output
        assert 'direction="inbound"' in output
        assert "50.0" in output


class TestHistogram:
    """Tests for Histogram class."""

    def test_default_buckets(self):
        """Test default histogram buckets."""
        histogram = Histogram(name="test_histogram")
        assert len(histogram.buckets) == 11
        assert histogram.buckets[0].le == 5
        assert histogram.buckets[-1].le == float('inf')

    def test_observe(self):
        """Test observing values."""
        histogram = Histogram(name="test_histogram")

        histogram.observe(10.0)
        histogram.observe(100.0)
        histogram.observe(1000.0)

        assert histogram.count == 3
        assert histogram.sum_value == 1110.0

    def test_bucket_counts(self):
        """Test that values go into correct buckets."""
        histogram = Histogram(name="test_histogram")

        # Observe values that fall into different buckets
        histogram.observe(3.0)    # <= 5
        histogram.observe(7.0)    # <= 10
        histogram.observe(50.0)   # <= 50
        histogram.observe(5000.0) # <= +Inf

        # Check cumulative bucket counts
        assert histogram.buckets[0].count == 1   # <= 5: 3
        assert histogram.buckets[1].count == 2   # <= 10: 3, 7
        assert histogram.buckets[3].count == 3   # <= 50: 3, 7, 50
        assert histogram.buckets[-1].count == 4  # <= +Inf: all

    def test_prometheus_format(self):
        """Test Prometheus format output."""
        histogram = Histogram(
            name="request_latency",
            labels={"channel": "telegram"},
        )
        histogram.observe(10.0)
        histogram.observe(20.0)

        lines = histogram.prometheus_format()
        assert any("_bucket" in line for line in lines)
        assert any("_sum" in line for line in lines)
        assert any("_count" in line for line in lines)


class TestMetrics:
    """Tests for Metrics dataclass."""

    def test_create_metrics(self):
        """Test creating metrics container."""
        metrics = Metrics(
            timestamp="2025-01-01T00:00:00",
            period="1h",
            messages_total=1000,
            latency_avg_ms=50.0,
        )
        assert metrics.messages_total == 1000
        assert metrics.latency_avg_ms == 50.0

    def test_to_dict(self):
        """Test serialization."""
        metrics = Metrics(
            messages_total=500,
            errors_total=10,
            messages_per_second=5.0,
        )
        data = metrics.to_dict()
        assert data["messages_total"] == 500
        assert data["errors_total"] == 10
        assert data["messages_per_second"] == 5.0

    def test_default_values(self):
        """Test default values."""
        metrics = Metrics()
        assert metrics.messages_total == 0
        assert metrics.latency_avg_ms == 0.0
        assert metrics.errors_total == 0


class TestMetricsCollector:
    """Tests for the main MetricsCollector class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def collector(self, temp_dir):
        """Create a collector with temp storage."""
        config = MetricsConfig(persistence_path=temp_dir)
        return MetricsCollector(config)

    def test_init(self, collector):
        """Test collector initialization."""
        assert collector is not None
        assert len(collector._message_counter) == 0
        assert len(collector._error_counter) == 0

    def test_record_message(self, collector):
        """Test recording messages."""
        collector.record_message("telegram", "inbound")
        collector.record_message("telegram", "inbound")
        collector.record_message("telegram", "outbound")
        collector.record_message("discord", "inbound")

        assert collector._message_counter[("telegram", "inbound")] == 2
        assert collector._message_counter[("telegram", "outbound")] == 1
        assert collector._message_counter[("discord", "inbound")] == 1

    def test_record_latency(self, collector):
        """Test recording latency."""
        collector.record_latency("telegram", 10.0)
        collector.record_latency("telegram", 20.0)
        collector.record_latency("telegram", 30.0)

        assert "telegram" in collector._latency_histograms
        histogram = collector._latency_histograms["telegram"]
        assert histogram.count == 3
        assert histogram.sum_value == 60.0

    def test_record_error(self, collector):
        """Test recording errors."""
        collector.record_error("telegram", "timeout")
        collector.record_error("telegram", "timeout")
        collector.record_error("telegram", "rate_limit")
        collector.record_error("discord", "auth")

        assert collector._error_counter[("telegram", "timeout")] == 2
        assert collector._error_counter[("telegram", "rate_limit")] == 1
        assert collector._error_counter[("discord", "auth")] == 1

    def test_get_metrics_empty(self, collector):
        """Test getting metrics with no data."""
        metrics = collector.get_metrics(period="1h")
        assert isinstance(metrics, Metrics)
        assert metrics.messages_total == 0
        assert metrics.errors_total == 0

    def test_get_metrics_with_data(self, collector):
        """Test getting metrics with recorded data."""
        # Record some messages
        for _ in range(10):
            collector.record_message("telegram", "inbound")
        for _ in range(5):
            collector.record_message("discord", "outbound")

        # Record latencies
        for i in range(10):
            collector.record_latency("telegram", float(i * 10))

        # Record errors
        collector.record_error("telegram", "timeout")
        collector.record_error("telegram", "timeout")

        metrics = collector.get_metrics(period="1h")

        assert metrics.messages_total == 15
        assert metrics.messages_by_channel["telegram"] == 10
        assert metrics.messages_by_channel["discord"] == 5
        assert metrics.errors_total == 2
        assert metrics.errors_by_type["timeout"] == 2

    def test_get_metrics_latency_percentiles(self, collector):
        """Test latency percentile calculations."""
        # Record known latencies
        latencies = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        for lat in latencies:
            collector.record_latency("telegram", float(lat))

        metrics = collector.get_metrics(period="1h")

        assert metrics.latency_avg_ms == 55.0  # Average of 10-100
        # p50 should be around 50-60 depending on rounding
        assert 50.0 <= metrics.latency_p50_ms <= 60.0
        assert metrics.latency_max_ms == 100.0

    def test_export_prometheus(self, collector):
        """Test Prometheus export format."""
        collector.record_message("telegram", "inbound")
        collector.record_message("telegram", "inbound")
        collector.record_error("telegram", "timeout")
        collector.record_latency("telegram", 50.0)

        output = collector.export_prometheus()

        # Check for expected metric types
        assert "# HELP" in output
        assert "# TYPE" in output
        assert "_uptime_seconds" in output
        assert "_messages_total" in output
        assert "_errors_total" in output
        assert "_request_latency_ms" in output

    def test_export_prometheus_labels(self, collector):
        """Test Prometheus export includes correct labels."""
        collector.record_message("telegram", "inbound")
        collector.record_error("discord", "auth")

        output = collector.export_prometheus()

        assert 'channel="telegram"' in output
        assert 'direction="inbound"' in output
        assert 'channel="discord"' in output
        assert 'error_type="auth"' in output

    def test_reset(self, collector):
        """Test resetting metrics."""
        collector.record_message("telegram", "inbound")
        collector.record_error("telegram", "timeout")
        collector.record_latency("telegram", 50.0)

        collector.reset()

        assert len(collector._message_counter) == 0
        assert len(collector._error_counter) == 0
        assert len(collector._latency_histograms) == 0

    def test_get_summary(self, collector):
        """Test getting quick summary."""
        collector.record_message("telegram", "inbound")
        collector.record_message("discord", "inbound")
        collector.record_error("telegram", "timeout")

        summary = collector.get_summary()

        assert summary["total_messages"] == 2
        assert summary["total_errors"] == 1
        assert summary["channels_active"] == 2
        assert summary["uptime_seconds"] >= 0

    def test_period_parsing(self, collector):
        """Test period string parsing."""
        assert collector._parse_period("30s") == 30
        assert collector._parse_period("5m") == 300
        assert collector._parse_period("1h") == 3600
        assert collector._parse_period("1d") == 86400
        assert collector._parse_period("invalid") == 3600  # Default


class TestMetricsPersistence:
    """Tests for metrics state persistence."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_counters_persist(self, temp_dir):
        """Test that counters persist across restarts."""
        config = MetricsConfig(persistence_path=temp_dir)

        # Create collector and record data
        collector1 = MetricsCollector(config)
        collector1.record_message("telegram", "inbound")
        collector1.record_message("telegram", "inbound")
        collector1.record_error("telegram", "timeout")

        # Create new collector instance (simulating restart)
        collector2 = MetricsCollector(config)

        assert collector2._message_counter[("telegram", "inbound")] == 2
        assert collector2._error_counter[("telegram", "timeout")] == 1


class TestMetricsConfig:
    """Tests for MetricsConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = MetricsConfig()
        assert config.retention_hours == 24
        assert config.prometheus_prefix == "hevolvebot"
        assert config.include_hostname_label is True

    def test_custom_prefix(self):
        """Test custom Prometheus prefix."""
        config = MetricsConfig(prometheus_prefix="myapp")
        collector = MetricsCollector(config)

        collector.record_message("telegram", "inbound")
        output = collector.export_prometheus()

        assert "myapp_messages_total" in output
        assert "myapp_uptime_seconds" in output

    def test_persistence_path(self):
        """Test persistence path configuration."""
        config = MetricsConfig(persistence_path="/custom/path")
        assert config.get_persistence_path() == "/custom/path"


class TestDockerIntegration:
    """Tests for Docker-specific features."""

    def test_hostname_in_labels(self, monkeypatch):
        """Test hostname is included in Prometheus labels."""
        monkeypatch.setenv("HOSTNAME", "container-123")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = MetricsConfig(
                persistence_path=tmpdir,
                include_hostname_label=True,
            )
            collector = MetricsCollector(config)
            collector.record_message("telegram", "inbound")

            output = collector.export_prometheus()
            assert 'hostname="container-123"' in output

    def test_no_hostname_in_labels(self):
        """Test hostname can be excluded from labels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MetricsConfig(
                persistence_path=tmpdir,
                include_hostname_label=False,
            )
            collector = MetricsCollector(config)
            collector.record_message("telegram", "inbound")

            output = collector.export_prometheus()
            # Should not contain hostname label
            assert "hostname=" not in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
