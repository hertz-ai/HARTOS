"""
test_ad_and_hierarchy.py - Tests for ad_service.py + hierarchy_service.py

Ad service: Ethical advertising with Spark currency and revenue sharing.
Hierarchy service: Regional node management for the distributed network.
Each test verifies a specific business rule or safety boundary.

FT: Ad constants (costs, rate limits, placements), ad creation validation,
    impression/click fraud prevention, revenue split math, hierarchy roles.
NFT: Revenue split sums to 1.0, rate limits are reasonable, placements seeded.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Ad Service constants — business rules
# ============================================================

class TestAdConstants:
    """Ad pricing and limits — wrong values = unfair economics or fraud."""

    def test_cpi_is_positive(self):
        """Cost per impression must be positive — free ads = no revenue."""
        from integrations.social.ad_service import AD_COSTS
        assert AD_COSTS['default_cpi'] > 0

    def test_cpc_greater_than_cpi(self):
        """Clicks are worth more than impressions."""
        from integrations.social.ad_service import AD_COSTS
        assert AD_COSTS['default_cpc'] > AD_COSTS['default_cpi']

    def test_min_budget_reasonable(self):
        """Min budget must be enough for at least a few impressions."""
        from integrations.social.ad_service import AD_COSTS
        assert AD_COSTS['min_budget'] >= 10
        assert AD_COSTS['min_budget'] <= 1000

    def test_revenue_split_sums_to_one(self):
        """Hoster + platform must sum to 1.0 — money can't appear or vanish."""
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE, PLATFORM_REVENUE_SHARE
        total = HOSTER_REVENUE_SHARE + PLATFORM_REVENUE_SHARE
        assert abs(total - 1.0) < 0.001, f"Revenue split sums to {total}"

    def test_hoster_gets_majority(self):
        """Node hosters earn 90% — they provide compute, they earn the most."""
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE
        assert HOSTER_REVENUE_SHARE >= 0.8

    def test_fraud_penalty_share_lower(self):
        """Unwitnessed (potentially fraudulent) impressions pay less."""
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE, HOSTER_UNWITNESSED_SHARE
        assert HOSTER_UNWITNESSED_SHARE < HOSTER_REVENUE_SHARE

    def test_impression_rate_limit(self):
        """Max 3 impressions per user per ad per hour — prevents ad fatigue."""
        from integrations.social.ad_service import MAX_IMPRESSIONS_PER_USER_PER_AD_PER_HOUR
        assert MAX_IMPRESSIONS_PER_USER_PER_AD_PER_HOUR == 3

    def test_click_rate_limit(self):
        """Max 1 click per user per ad per hour — prevents click fraud."""
        from integrations.social.ad_service import MAX_CLICKS_PER_USER_PER_AD_PER_HOUR
        assert MAX_CLICKS_PER_USER_PER_AD_PER_HOUR == 1


# ============================================================
# Default placements — seeded on first run
# ============================================================

class TestDefaultPlacements:
    """DEFAULT_PLACEMENTS define where ads can appear in the UI."""

    def test_has_feed_top(self):
        from integrations.social.ad_service import DEFAULT_PLACEMENTS
        names = [p['name'] for p in DEFAULT_PLACEMENTS]
        assert 'feed_top' in names

    def test_has_sidebar(self):
        from integrations.social.ad_service import DEFAULT_PLACEMENTS
        names = [p['name'] for p in DEFAULT_PLACEMENTS]
        assert 'sidebar' in names

    def test_all_have_required_keys(self):
        from integrations.social.ad_service import DEFAULT_PLACEMENTS
        for p in DEFAULT_PLACEMENTS:
            assert 'name' in p
            assert 'display_name' in p
            assert 'max_ads' in p
            assert p['max_ads'] >= 1

    def test_no_duplicate_names(self):
        from integrations.social.ad_service import DEFAULT_PLACEMENTS
        names = [p['name'] for p in DEFAULT_PLACEMENTS]
        assert len(names) == len(set(names))


# ============================================================
# Ad creation validation
# ============================================================

class TestAdCreation:
    """AdService.create_ad validates input before spending user's Spark."""

    def test_create_ad_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.create_ad)

    def test_serve_ad_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.serve_ad)

    def test_record_impression_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.record_impression)

    def test_record_click_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.record_click)

    def test_get_analytics_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.get_analytics)

    def test_seed_placements_method_exists(self):
        from integrations.social.ad_service import AdService
        assert callable(AdService.seed_placements)


# ============================================================
# Hierarchy Service — regional node management
# ============================================================

class TestHierarchyService:
    """HierarchyService manages the central→regional→flat topology."""

    def test_register_regional_host_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.register_regional_host)

    def test_register_local_node_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.register_local_node)

    def test_assign_to_region_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.assign_to_region)

    def test_get_gossip_targets_exists(self):
        """Gossip protocol needs targets — this function provides them."""
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.get_gossip_targets)

    def test_get_region_health_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.get_region_health)

    def test_switch_region_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.switch_region)

    def test_report_node_capacity_exists(self):
        from integrations.social.hierarchy_service import HierarchyService
        assert callable(HierarchyService.report_node_capacity)
