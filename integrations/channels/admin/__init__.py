"""
Admin API Module

Provides REST API endpoints for managing all channel integration components.
Exposes 100+ endpoints for configuration, monitoring, and control.

Also includes:
- AdminDashboard: Server-side dashboard data provider
- MetricsCollector: Metrics collection with Prometheus export
"""

from .api import admin_bp, AdminAPI
from .schemas import (
    ChannelConfigSchema,
    QueueConfigSchema,
    CommandConfigSchema,
    AutomationConfigSchema,
    IdentityConfigSchema,
    PluginConfigSchema,
    SessionConfigSchema,
    MetricsSchema,
)
from .dashboard import (
    AdminDashboard,
    DashboardConfig,
    DashboardStats,
    SessionInfo,
    ChannelStatusInfo,
    QueueStats,
    ErrorEntry,
    ErrorSeverity,
    get_dashboard,
)
from .metrics import (
    MetricsCollector,
    MetricsConfig,
    Metrics,
    Histogram,
    HistogramBucket,
    MetricValue,
    get_metrics_collector,
)

__all__ = [
    # API
    "admin_bp",
    "AdminAPI",
    # Schemas
    "ChannelConfigSchema",
    "QueueConfigSchema",
    "CommandConfigSchema",
    "AutomationConfigSchema",
    "IdentityConfigSchema",
    "PluginConfigSchema",
    "SessionConfigSchema",
    "MetricsSchema",
    # Dashboard
    "AdminDashboard",
    "DashboardConfig",
    "DashboardStats",
    "SessionInfo",
    "ChannelStatusInfo",
    "QueueStats",
    "ErrorEntry",
    "ErrorSeverity",
    "get_dashboard",
    # Metrics
    "MetricsCollector",
    "MetricsConfig",
    "Metrics",
    "Histogram",
    "HistogramBucket",
    "MetricValue",
    "get_metrics_collector",
]
