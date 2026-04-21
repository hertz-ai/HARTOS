"""
App Marketplace — Google Play + Microsoft Store + Apple App Store for HART OS.

Full marketplace backend for AI apps/agents built on HART OS.
Users publish agents (recipes) as apps, other users install and rate them,
auto-promotion distributes to 30+ channels, and the 90/9/1 revenue split
ensures creators keep 90% of every Spark earned.

JSON-backed listing registry at agent_data/marketplace/ (no schema migration).
Thread-safe, atomic writes, EventBus integration.

Singleton: get_marketplace()
Blueprint: marketplace_bp (register on Flask app)
Goal seed: SEED_APP_MARKETPLACE_PROMOTER
"""

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.marketplace')

# ─── Persistence paths ─────────────────────────────────────────────────────
_MARKETPLACE_DIR = os.path.join('agent_data', 'marketplace')
_LISTINGS_PATH = os.path.join(_MARKETPLACE_DIR, 'listings.json')
_REVIEWS_PATH = os.path.join(_MARKETPLACE_DIR, 'reviews.json')
_INSTALLS_PATH = os.path.join(_MARKETPLACE_DIR, 'installs.json')
_REVENUE_PATH = os.path.join(_MARKETPLACE_DIR, 'revenue.json')

# ─── Categories ─────────────────────────────────────────────────────────────

APP_CATEGORIES = [
    'productivity', 'coding', 'research', 'writing', 'design',
    'marketing', 'finance', 'education', 'health', 'entertainment',
    'social', 'automation', 'data_analysis', 'customer_support',
    'translation', 'legal', 'hr', 'devops', 'security', 'general',
]

# ─── Pricing models ────────────────────────────────────────────────────────

PRICING_MODELS = ['free', 'freemium', 'paid', 'subscription']

# ─── Platform targets ──────────────────────────────────────────────────────

SUPPORTED_PLATFORMS = ['windows', 'linux', 'mac', 'android', 'ios', 'web']

# ─── Revenue split (imports canonical constants) ───────────────────────────

try:
    from integrations.agent_engine.revenue_aggregator import (
        REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
    )
except ImportError:
    REVENUE_SPLIT_USERS = 0.90
    REVENUE_SPLIT_INFRA = 0.09
    REVENUE_SPLIT_CENTRAL = 0.01


# ═══════════════════════════════════════════════════════════════════════════
# Atomic JSON persistence
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_dir():
    os.makedirs(_MARKETPLACE_DIR, exist_ok=True)


def _atomic_write(path: str, data: Any) -> None:
    """Write JSON atomically (write .tmp, then os.replace)."""
    _ensure_dir()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _load_json(path: str, default: Any = None) -> Any:
    """Load JSON file, returning default if absent or corrupt."""
    if not os.path.isfile(path):
        return default if default is not None else {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.warning(f"Corrupt marketplace file: {path}, returning default")
        return default if default is not None else {}


# ═══════════════════════════════════════════════════════════════════════════
# EventBus helpers
# ═══════════════════════════════════════════════════════════════════════════

def _emit(topic: str, data: Dict) -> None:
    """Emit event on platform EventBus (safe no-op if not bootstrapped)."""
    try:
        from core.platform.events import emit_event
        emit_event(topic, data)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# AppMarketplace
# ═══════════════════════════════════════════════════════════════════════════

class AppMarketplace:
    """Full-featured marketplace for AI apps/agents built on HART OS.

    JSON-backed registry at agent_data/marketplace/.  Thread-safe mutations.
    Every sale follows the 90/9/1 revenue split.
    """

    def __init__(self):
        self._lock = threading.Lock()
        _ensure_dir()

    # ─── Private persistence helpers ───────────────────────────────────

    def _listings(self) -> Dict[str, Dict]:
        return _load_json(_LISTINGS_PATH, {})

    def _save_listings(self, data: Dict[str, Dict]) -> None:
        _atomic_write(_LISTINGS_PATH, data)

    def _reviews(self) -> Dict[str, List[Dict]]:
        return _load_json(_REVIEWS_PATH, {})

    def _save_reviews(self, data: Dict[str, List[Dict]]) -> None:
        _atomic_write(_REVIEWS_PATH, data)

    def _installs(self) -> Dict[str, List[Dict]]:
        return _load_json(_INSTALLS_PATH, {})

    def _save_installs(self, data: Dict[str, List[Dict]]) -> None:
        _atomic_write(_INSTALLS_PATH, data)

    def _revenue(self) -> Dict[str, List[Dict]]:
        return _load_json(_REVENUE_PATH, {})

    def _save_revenue(self, data: Dict[str, List[Dict]]) -> None:
        _atomic_write(_REVENUE_PATH, data)

    # ─── Listing CRUD ──────────────────────────────────────────────────

    def publish_app(self, owner_id: str, name: str, description: str,
                    recipe_id: str = '', agent_type: str = 'general',
                    tagline: str = '', category: str = 'general',
                    screenshots: List[str] = None, demo_url: str = '',
                    pricing_model: str = 'free', price_spark: int = 0,
                    monthly_price_spark: int = 0, feature_list: List[str] = None,
                    competing_with: List[str] = None,
                    platforms: List[str] = None,
                    distribution_channels: List[str] = None,
                    benchmark_scores: Dict = None,
                    product_id: str = '') -> Dict:
        """Create a new marketplace listing. Returns the listing dict."""
        if not owner_id or not name:
            return {'error': 'owner_id and name are required'}
        if category not in APP_CATEGORIES:
            category = 'general'
        if pricing_model not in PRICING_MODELS:
            pricing_model = 'free'

        listing_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        listing = {
            'listing_id': listing_id,
            'product_id': product_id or listing_id,
            'owner_id': owner_id,
            'name': name,
            'description': description,
            'tagline': tagline or '',
            'category': category,
            'screenshots': screenshots or [],
            'demo_url': demo_url,
            'recipe_id': recipe_id,
            'agent_type': agent_type,
            'pricing_model': pricing_model,
            'price_spark': max(0, price_spark),
            'monthly_price_spark': max(0, monthly_price_spark),
            'rating': 0.0,
            'review_count': 0,
            'install_count': 0,
            'feature_list': feature_list or [],
            'competing_with': competing_with or [],
            'platforms': [p for p in (platforms or ['web']) if p in SUPPORTED_PLATFORMS],
            'distribution_channels': distribution_channels or [],
            'benchmark_scores': benchmark_scores or {},
            'created_at': now,
            'updated_at': now,
            'status': 'active',
        }

        with self._lock:
            listings = self._listings()
            listings[listing_id] = listing
            self._save_listings(listings)

        _emit('marketplace.app.published', {
            'listing_id': listing_id,
            'name': name,
            'owner_id': owner_id,
            'category': category,
        })

        logger.info(f"Marketplace: published '{name}' ({listing_id}) by {owner_id}")
        return listing

    def update_app(self, listing_id: str, owner_id: str,
                   updates: Dict) -> Dict:
        """Update an existing listing. Only the owner can update."""
        with self._lock:
            listings = self._listings()
            listing = listings.get(listing_id)
            if not listing:
                return {'error': 'Listing not found'}
            if listing['owner_id'] != owner_id:
                return {'error': 'Only the owner can update this listing'}

            # Whitelist of updatable fields
            allowed = {
                'name', 'description', 'tagline', 'category', 'screenshots',
                'demo_url', 'pricing_model', 'price_spark', 'monthly_price_spark',
                'feature_list', 'competing_with', 'platforms',
                'distribution_channels', 'benchmark_scores', 'status',
                'recipe_id', 'agent_type',
            }
            for key, val in updates.items():
                if key in allowed:
                    if key == 'category' and val not in APP_CATEGORIES:
                        continue
                    if key == 'pricing_model' and val not in PRICING_MODELS:
                        continue
                    listing[key] = val

            listing['updated_at'] = datetime.utcnow().isoformat()
            listings[listing_id] = listing
            self._save_listings(listings)

        return listing

    def get_app(self, listing_id: str) -> Optional[Dict]:
        """Get full listing with reviews."""
        listings = self._listings()
        listing = listings.get(listing_id)
        if not listing or listing.get('status') == 'removed':
            return None

        result = dict(listing)
        reviews = self._reviews()
        result['reviews'] = reviews.get(listing_id, [])

        # Find competing apps in same category
        competitors = []
        for lid, other in listings.items():
            if (lid != listing_id
                    and other.get('category') == listing.get('category')
                    and other.get('status') == 'active'):
                competitors.append({
                    'listing_id': lid,
                    'name': other['name'],
                    'rating': other.get('rating', 0),
                    'install_count': other.get('install_count', 0),
                })
        result['competitors'] = sorted(
            competitors, key=lambda x: x.get('install_count', 0), reverse=True
        )[:10]

        return result

    # ─── Search & Browse ───────────────────────────────────────────────

    def list_apps(self, category: str = None, query: str = None,
                  sort: str = 'popular', page: int = 1,
                  per_page: int = 20) -> Dict:
        """List/search apps with pagination.

        Sort options: popular, newest, rating, name, installs
        """
        listings = self._listings()
        results = []
        for lid, listing in listings.items():
            if listing.get('status') != 'active':
                continue
            if category and listing.get('category') != category:
                continue
            if query:
                q_lower = query.lower()
                searchable = ' '.join([
                    listing.get('name', ''),
                    listing.get('description', ''),
                    listing.get('tagline', ''),
                    ' '.join(listing.get('feature_list', [])),
                ]).lower()
                if q_lower not in searchable:
                    continue
            results.append(listing)

        # Sort
        sort_keys = {
            'popular': lambda x: x.get('install_count', 0),
            'newest': lambda x: x.get('created_at', ''),
            'rating': lambda x: x.get('rating', 0),
            'name': lambda x: x.get('name', '').lower(),
            'installs': lambda x: x.get('install_count', 0),
        }
        sort_fn = sort_keys.get(sort, sort_keys['popular'])
        reverse = sort not in ('name',)
        results.sort(key=sort_fn, reverse=reverse)

        # Paginate
        total = len(results)
        start = (page - 1) * per_page
        end = start + per_page
        page_results = results[start:end]

        return {
            'apps': page_results,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': max(1, math.ceil(total / per_page)),
        }

    def search_apps(self, query: str, filters: Dict = None) -> Dict:
        """Full-text search with optional filters.

        Filters: category, pricing_model, min_rating, platform
        """
        filters = filters or {}
        listings = self._listings()
        results = []
        q_lower = query.lower()

        for lid, listing in listings.items():
            if listing.get('status') != 'active':
                continue

            # Text match (name, description, tagline, features, agent_type)
            searchable = ' '.join([
                listing.get('name', ''),
                listing.get('description', ''),
                listing.get('tagline', ''),
                listing.get('agent_type', ''),
                ' '.join(listing.get('feature_list', [])),
            ]).lower()
            if q_lower not in searchable:
                continue

            # Apply filters
            if filters.get('category') and listing.get('category') != filters['category']:
                continue
            if filters.get('pricing_model') and listing.get('pricing_model') != filters['pricing_model']:
                continue
            if filters.get('min_rating') and listing.get('rating', 0) < filters['min_rating']:
                continue
            if filters.get('platform'):
                if filters['platform'] not in listing.get('platforms', []):
                    continue

            # Score: name match > tagline match > description match
            score = 0
            if q_lower in listing.get('name', '').lower():
                score += 100
            if q_lower in listing.get('tagline', '').lower():
                score += 50
            score += listing.get('install_count', 0) * 0.1
            score += listing.get('rating', 0) * 10

            results.append({**listing, '_search_score': score})

        results.sort(key=lambda x: x.get('_search_score', 0), reverse=True)

        # Strip internal score
        for r in results:
            r.pop('_search_score', None)

        return {
            'query': query,
            'filters': filters,
            'results': results[:50],
            'total': len(results),
        }

    def compare_apps(self, listing_ids: List[str]) -> Dict:
        """Side-by-side comparison of multiple apps."""
        listings = self._listings()
        apps = []
        for lid in listing_ids:
            listing = listings.get(lid)
            if listing:
                apps.append(listing)

        if len(apps) < 2:
            return {'error': 'Need at least 2 valid listings to compare'}

        # Build feature union
        all_features = set()
        for app in apps:
            all_features.update(app.get('feature_list', []))

        comparison = {
            'apps': [],
            'feature_matrix': {},
            'all_features': sorted(all_features),
        }

        for app in apps:
            app_features = set(app.get('feature_list', []))
            comparison['apps'].append({
                'listing_id': app['listing_id'],
                'name': app['name'],
                'rating': app.get('rating', 0),
                'install_count': app.get('install_count', 0),
                'pricing_model': app.get('pricing_model', 'free'),
                'price_spark': app.get('price_spark', 0),
                'platforms': app.get('platforms', []),
                'benchmark_scores': app.get('benchmark_scores', {}),
            })
            comparison['feature_matrix'][app['listing_id']] = {
                feat: (feat in app_features) for feat in all_features
            }

        return comparison

    def get_competing_apps(self, listing_id: str) -> List[Dict]:
        """Get apps competing in the same category."""
        listings = self._listings()
        listing = listings.get(listing_id)
        if not listing:
            return []

        category = listing.get('category', 'general')
        competitors = []
        for lid, other in listings.items():
            if (lid != listing_id
                    and other.get('category') == category
                    and other.get('status') == 'active'):
                competitors.append({
                    'listing_id': lid,
                    'name': other['name'],
                    'tagline': other.get('tagline', ''),
                    'rating': other.get('rating', 0),
                    'install_count': other.get('install_count', 0),
                    'pricing_model': other.get('pricing_model', 'free'),
                    'price_spark': other.get('price_spark', 0),
                })

        competitors.sort(key=lambda x: x.get('install_count', 0), reverse=True)
        return competitors

    def get_trending(self, days: int = 7, limit: int = 20) -> List[Dict]:
        """Most installed apps in the last N days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        installs = self._installs()
        listings = self._listings()

        # Count recent installs per listing
        install_counts = {}
        for lid, records in installs.items():
            count = sum(
                1 for r in records
                if r.get('installed_at', '') >= cutoff
            )
            if count > 0:
                install_counts[lid] = count

        # Build trending list
        trending = []
        for lid, count in sorted(install_counts.items(), key=lambda x: x[1], reverse=True):
            listing = listings.get(lid)
            if listing and listing.get('status') == 'active':
                trending.append({
                    **listing,
                    'recent_installs': count,
                })
            if len(trending) >= limit:
                break

        return trending

    def get_categories(self) -> List[Dict]:
        """All categories with listing counts."""
        listings = self._listings()
        counts = {}
        for lid, listing in listings.items():
            if listing.get('status') != 'active':
                continue
            cat = listing.get('category', 'general')
            counts[cat] = counts.get(cat, 0) + 1

        result = []
        for cat in APP_CATEGORIES:
            result.append({
                'category': cat,
                'count': counts.get(cat, 0),
            })
        return result

    def feature_comparison_matrix(self, category: str) -> Dict:
        """Matrix of features across all apps in a category."""
        listings = self._listings()
        apps_in_cat = []
        all_features = set()

        for lid, listing in listings.items():
            if listing.get('status') != 'active':
                continue
            if listing.get('category') != category:
                continue
            apps_in_cat.append(listing)
            all_features.update(listing.get('feature_list', []))

        sorted_features = sorted(all_features)
        matrix = {}
        for app in apps_in_cat:
            app_features = set(app.get('feature_list', []))
            matrix[app['listing_id']] = {
                'name': app['name'],
                'features': {feat: (feat in app_features) for feat in sorted_features},
                'rating': app.get('rating', 0),
                'install_count': app.get('install_count', 0),
            }

        return {
            'category': category,
            'features': sorted_features,
            'apps': matrix,
            'app_count': len(apps_in_cat),
        }

    # ─── Install & Rate ────────────────────────────────────────────────

    def install_app(self, user_id: str, listing_id: str) -> Dict:
        """Install an app (recipe/agent) for a user.

        For paid apps, deducts Spark and records revenue with 90/9/1 split.
        """
        if not user_id:
            return {'error': 'user_id is required'}

        with self._lock:
            listings = self._listings()
            listing = listings.get(listing_id)
            if not listing:
                return {'error': 'Listing not found'}
            if listing.get('status') != 'active':
                return {'error': 'Listing is not active'}

            # Check if already installed
            installs = self._installs()
            user_installs = installs.get(listing_id, [])
            already = any(r.get('user_id') == user_id for r in user_installs)
            if already:
                return {'error': 'Already installed', 'listing_id': listing_id}

            # Handle payment for paid apps
            price = listing.get('price_spark', 0)
            payment_recorded = False
            if listing.get('pricing_model') in ('paid', 'subscription') and price > 0:
                payment_result = self._process_payment(
                    user_id, listing['owner_id'], listing_id, price)
                if payment_result.get('error'):
                    return payment_result
                payment_recorded = True

            # Record install
            install_record = {
                'user_id': user_id,
                'listing_id': listing_id,
                'installed_at': datetime.utcnow().isoformat(),
                'paid': payment_recorded,
                'price_spark': price if payment_recorded else 0,
            }
            user_installs.append(install_record)
            installs[listing_id] = user_installs

            # Increment install count
            listing['install_count'] = listing.get('install_count', 0) + 1
            listings[listing_id] = listing

            self._save_installs(installs)
            self._save_listings(listings)

        # Wire up the recipe for the user (non-blocking)
        recipe_id = listing.get('recipe_id', '')
        if recipe_id:
            self._wire_recipe_for_user(user_id, recipe_id, listing_id)

        _emit('marketplace.app.installed', {
            'listing_id': listing_id,
            'user_id': user_id,
            'name': listing.get('name', ''),
            'paid': payment_recorded,
        })

        logger.info(f"Marketplace: user {user_id} installed '{listing.get('name')}' ({listing_id})")
        return {
            'success': True,
            'listing_id': listing_id,
            'name': listing.get('name', ''),
            'recipe_id': recipe_id,
            'paid': payment_recorded,
        }

    def rate_app(self, user_id: str, listing_id: str,
                 rating: float, review: str = '') -> Dict:
        """Rate and review an app (1-5 stars). One review per user per app."""
        if not user_id:
            return {'error': 'user_id is required'}
        if rating < 1 or rating > 5:
            return {'error': 'Rating must be between 1 and 5'}

        with self._lock:
            reviews = self._reviews()
            listing_reviews = reviews.get(listing_id, [])

            # Check if user already reviewed
            for existing in listing_reviews:
                if existing.get('user_id') == user_id:
                    # Update existing review
                    existing['rating'] = rating
                    existing['review'] = review
                    existing['updated_at'] = datetime.utcnow().isoformat()
                    reviews[listing_id] = listing_reviews
                    self._save_reviews(reviews)
                    self._update_listing_rating(listing_id, listing_reviews)
                    return {'success': True, 'updated': True}

            # New review
            review_record = {
                'review_id': str(uuid.uuid4()),
                'user_id': user_id,
                'listing_id': listing_id,
                'rating': rating,
                'review': review,
                'created_at': datetime.utcnow().isoformat(),
            }
            listing_reviews.append(review_record)
            reviews[listing_id] = listing_reviews
            self._save_reviews(reviews)
            self._update_listing_rating(listing_id, listing_reviews)

        _emit('marketplace.app.reviewed', {
            'listing_id': listing_id,
            'user_id': user_id,
            'rating': rating,
        })

        return {'success': True, 'review_id': review_record['review_id']}

    def _update_listing_rating(self, listing_id: str, reviews: List[Dict]) -> None:
        """Recalculate average rating for a listing. Must hold self._lock."""
        listings = self._listings()
        listing = listings.get(listing_id)
        if not listing:
            return

        ratings = [r['rating'] for r in reviews if 'rating' in r]
        if ratings:
            listing['rating'] = round(sum(ratings) / len(ratings), 2)
            listing['review_count'] = len(ratings)
        else:
            listing['rating'] = 0.0
            listing['review_count'] = 0

        listings[listing_id] = listing
        self._save_listings(listings)

    # ─── Revenue ───────────────────────────────────────────────────────

    def _process_payment(self, buyer_id: str, seller_id: str,
                         listing_id: str, amount_spark: int) -> Dict:
        """Process Spark payment with 90/9/1 split.

        90% to creator, 9% to infrastructure, 1% to central.
        Uses ResonanceService.award_spark when available.

        Failed payments are persisted to revenue.json with status='pending'
        so the reconciliation job (see ``reconcile_pending_payments``) can
        retry them later. Before this guard a failed ResonanceService call
        was silently dropped and the install was aborted without leaving
        an audit trail.
        """
        creator_share = int(amount_spark * REVENUE_SPLIT_USERS)
        infra_share = int(amount_spark * REVENUE_SPLIT_INFRA)
        central_share = amount_spark - creator_share - infra_share  # remainder

        # Try to deduct from buyer and credit seller via ResonanceService
        try:
            from integrations.social.models import db_session
            from integrations.social.resonance_engine import ResonanceService
            with db_session() as db:
                ResonanceService.award_spark(
                    db, buyer_id, -amount_spark,
                    'marketplace_purchase', listing_id,
                    f'App purchase: {listing_id}')
                ResonanceService.award_spark(
                    db, seller_id, creator_share,
                    'marketplace_sale', listing_id,
                    f'App sale revenue (90%): {listing_id}')
        except ImportError:
            logger.debug("ResonanceService not available, recording payment ledger only")
        except Exception as e:
            logger.warning("Payment processing failed: %s", e)
            # Record the failed payment so reconciliation can retry it.
            self._record_payment(
                seller_id, buyer_id, listing_id, amount_spark,
                creator_share, infra_share, central_share,
                status='pending', error=str(e), retry_count=0)
            return {'error': f'Payment failed: {e}'}

        # Record successful payment in revenue ledger
        self._record_payment(
            seller_id, buyer_id, listing_id, amount_spark,
            creator_share, infra_share, central_share,
            status='settled', error='', retry_count=0)

        return {'success': True, 'creator_share': creator_share}

    def _record_payment(self, seller_id: str, buyer_id: str,
                        listing_id: str, amount_spark: int,
                        creator_share: int, infra_share: int,
                        central_share: int, *, status: str = 'settled',
                        error: str = '', retry_count: int = 0) -> None:
        """Append a payment record to revenue.json (thread-safe).

        ``status`` is 'settled' on success and 'pending' when the Spark
        transfer failed and needs reconciliation. This is the single writer
        for the revenue ledger — all payment state flows through here so
        the reconciliation job can trust the schema.
        """
        with self._lock:
            revenue = self._revenue()
            seller_revenue = revenue.get(seller_id, [])
            seller_revenue.append({
                'listing_id': listing_id,
                'buyer_id': buyer_id,
                'total_spark': amount_spark,
                'creator_share': creator_share,
                'infra_share': infra_share,
                'central_share': central_share,
                'timestamp': datetime.utcnow().isoformat(),
                'status': status,
                'error': error,
                'retry_count': retry_count,
            })
            revenue[seller_id] = seller_revenue
            self._save_revenue(revenue)

    def reconcile_pending_payments(
        self, stale_after_minutes: int = 15,
    ) -> Dict[str, int]:
        """Scan revenue.json for pending payments older than the threshold
        and retry them through the existing payment path.

        Called by the agent_daemon tick on a slow cadence (every few min).
        Respects a retry_count cap so a truly-broken payment can't loop
        forever — after 5 retries the row is marked 'failed' for human
        review.

        Returns a dict of counters: ``scanned``, ``retried``, ``settled``,
        ``failed``, ``skipped``.
        """
        from integrations.social.models import db_session
        from integrations.social.resonance_engine import ResonanceService
        cutoff = datetime.utcnow() - timedelta(minutes=stale_after_minutes)
        counters = {'scanned': 0, 'retried': 0, 'settled': 0,
                    'failed': 0, 'skipped': 0}
        MAX_RETRIES = 5
        with self._lock:
            revenue = self._revenue()
            changed = False
            for seller_id, rows in list(revenue.items()):
                for row in rows:
                    counters['scanned'] += 1
                    if row.get('status') != 'pending':
                        continue
                    ts_str = row.get('timestamp', '')
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except (TypeError, ValueError):
                        counters['skipped'] += 1
                        continue
                    if ts >= cutoff:
                        counters['skipped'] += 1
                        continue
                    retry_count = int(row.get('retry_count', 0))
                    if retry_count >= MAX_RETRIES:
                        row['status'] = 'failed'
                        changed = True
                        counters['failed'] += 1
                        logger.critical(
                            "Marketplace reconcile: payment marked FAILED "
                            "after %d retries — listing=%s seller=%s "
                            "buyer=%s amount=%s last_error=%s",
                            retry_count, row.get('listing_id'), seller_id,
                            row.get('buyer_id'), row.get('total_spark'),
                            row.get('error', ''))
                        continue
                    # Attempt the retry via the canonical gateway
                    counters['retried'] += 1
                    amount = int(row.get('total_spark', 0))
                    creator_share = int(row.get('creator_share', 0))
                    buyer_id = row.get('buyer_id', '')
                    listing_id = row.get('listing_id', '')
                    try:
                        with db_session() as db:
                            ResonanceService.award_spark(
                                db, buyer_id, -amount,
                                'marketplace_purchase_retry', listing_id,
                                f'App purchase retry: {listing_id}')
                            ResonanceService.award_spark(
                                db, seller_id, creator_share,
                                'marketplace_sale_retry', listing_id,
                                f'App sale retry (90%): {listing_id}')
                        row['status'] = 'settled'
                        row['error'] = ''
                        row['retry_count'] = retry_count + 1
                        row['settled_at'] = datetime.utcnow().isoformat()
                        counters['settled'] += 1
                        changed = True
                        logger.info(
                            "Marketplace reconcile: settled pending payment "
                            "for listing=%s seller=%s amount=%s",
                            listing_id, seller_id, amount)
                    except Exception as e:
                        row['retry_count'] = retry_count + 1
                        row['error'] = str(e)
                        row['last_retry_at'] = datetime.utcnow().isoformat()
                        changed = True
                        logger.warning(
                            "Marketplace reconcile: retry failed for "
                            "listing=%s seller=%s (attempt %d): %s",
                            listing_id, seller_id, retry_count + 1, e)
            if changed:
                self._save_revenue(revenue)
        return counters

    def get_revenue_report(self, owner_id: str) -> Dict:
        """Earnings report for an app creator."""
        revenue = self._revenue()
        owner_records = revenue.get(owner_id, [])
        listings = self._listings()

        # Aggregate per app
        per_app = {}
        total_earned = 0
        for record in owner_records:
            lid = record.get('listing_id', '')
            if lid not in per_app:
                listing = listings.get(lid, {})
                per_app[lid] = {
                    'listing_id': lid,
                    'name': listing.get('name', 'Unknown'),
                    'total_sales': 0,
                    'total_spark_earned': 0,
                    'total_gross': 0,
                }
            per_app[lid]['total_sales'] += 1
            per_app[lid]['total_spark_earned'] += record.get('creator_share', 0)
            per_app[lid]['total_gross'] += record.get('total_spark', 0)
            total_earned += record.get('creator_share', 0)

        # Count owned listings
        owned_apps = []
        for lid, listing in listings.items():
            if listing.get('owner_id') == owner_id:
                owned_apps.append({
                    'listing_id': lid,
                    'name': listing.get('name', ''),
                    'install_count': listing.get('install_count', 0),
                    'rating': listing.get('rating', 0),
                    'pricing_model': listing.get('pricing_model', 'free'),
                })

        return {
            'owner_id': owner_id,
            'total_spark_earned': total_earned,
            'total_sales': sum(a['total_sales'] for a in per_app.values()),
            'revenue_split': f'{int(REVENUE_SPLIT_USERS * 100)}% creator / '
                             f'{int(REVENUE_SPLIT_INFRA * 100)}% infra / '
                             f'{int(REVENUE_SPLIT_CENTRAL * 100)}% central',
            'per_app': list(per_app.values()),
            'owned_apps': owned_apps,
        }

    # ─── Recipe wiring ─────────────────────────────────────────────────

    def _wire_recipe_for_user(self, user_id: str, recipe_id: str,
                              listing_id: str) -> None:
        """Wire an installed recipe so the user can invoke it via REUSE mode.

        Copies recipe reference into the user's prompt directory so
        /chat with the matching prompt_id triggers REUSE.
        """
        try:
            import glob, re
            # Sanitize recipe_id to prevent path traversal
            recipe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', recipe_id)
            if not recipe_id:
                return
            pattern = os.path.join('prompts', f'*{recipe_id}*_recipe.json')
            matches = glob.glob(pattern)
            if not matches:
                logger.debug(f"No recipe file found for {recipe_id}")
                return

            # Create user-specific prompt reference
            user_prompt_path = os.path.join(
                'prompts', f'marketplace_{user_id}_{listing_id}.json')
            prompt_data = {
                'prompt_id': f'marketplace_{listing_id}',
                'user_id': user_id,
                'source_recipe': matches[0],
                'listing_id': listing_id,
                'installed_at': datetime.utcnow().isoformat(),
            }
            with open(user_prompt_path, 'w', encoding='utf-8') as f:
                json.dump(prompt_data, f, indent=2)
            logger.debug(f"Wired recipe {recipe_id} for user {user_id}")
        except Exception as e:
            logger.debug(f"Recipe wiring failed: {e}")

    # ─── Channel Distribution ──────────────────────────────────────────

    def distribute_to_channel(self, listing_id: str, channel: str) -> Dict:
        """Deploy app as bot to a distribution channel.

        Dispatches a goal to create the channel bot/integration.
        Supported channels: discord, telegram, slack, matrix, whatsapp, web, etc.
        """
        listings = self._listings()
        listing = listings.get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        prompt = (
            f"Deploy the HART OS app '{listing['name']}' as a bot/integration "
            f"on the {channel} channel. The app description: {listing['description']}. "
            f"Recipe ID: {listing.get('recipe_id', 'none')}. "
            f"Set up the channel adapter, register commands, and make the app "
            f"accessible to users on {channel}."
        )

        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            response = dispatch_goal(
                prompt=prompt,
                user_id=listing['owner_id'],
                goal_id=f'distribute_{listing_id}_{channel}',
                goal_type='distribution',
            )
            # Track distribution
            with self._lock:
                listings = self._listings()
                listing = listings.get(listing_id, {})
                channels = listing.get('distribution_channels', [])
                if channel not in channels:
                    channels.append(channel)
                    listing['distribution_channels'] = channels
                    listings[listing_id] = listing
                    self._save_listings(listings)

            return {
                'success': True,
                'listing_id': listing_id,
                'channel': channel,
                'dispatch_response': response,
            }
        except Exception as e:
            logger.warning(f"Channel distribution failed: {e}")
            return {'error': f'Distribution failed: {e}'}

    # ─── Cross-Platform Distribution (Manifest Generation) ─────────────

    def distribute_to_google_play(self, listing_id: str) -> Dict:
        """Generate Android manifest for Hevolve React Native wrapper."""
        listing = self._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        manifest = {
            'format': 'android_manifest',
            'package': f'com.hevolve.hartos.{_safe_id(listing["name"])}',
            'versionCode': 1,
            'versionName': '1.0.0',
            'minSdkVersion': 24,
            'targetSdkVersion': 34,
            'application': {
                'label': listing['name'],
                'description': listing.get('description', ''),
                'icon': '@mipmap/ic_launcher',
                'theme': '@style/HevolveTheme',
            },
            'permissions': [
                'android.permission.INTERNET',
                'android.permission.ACCESS_NETWORK_STATE',
            ],
            'hartos_config': {
                'listing_id': listing_id,
                'recipe_id': listing.get('recipe_id', ''),
                'api_endpoint': '/chat',
                'agent_type': listing.get('agent_type', 'general'),
            },
            'react_native': {
                'wrapper': 'hevolve-rn-shell',
                'webview_fallback': True,
                'deep_link_scheme': f'hartos-{_safe_id(listing["name"])}',
            },
        }
        return {'success': True, 'platform': 'google_play', 'manifest': manifest}

    def distribute_to_microsoft_store(self, listing_id: str) -> Dict:
        """Generate MSIX/AppX manifest for Windows wrapper."""
        listing = self._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        manifest = {
            'format': 'appx_manifest',
            'Identity': {
                'Name': f'Hevolve.HARTOS.{_safe_id(listing["name"])}',
                'Publisher': 'CN=Hevolve',
                'Version': '1.0.0.0',
            },
            'Properties': {
                'DisplayName': listing['name'],
                'Description': listing.get('description', ''),
                'PublisherDisplayName': 'Hevolve',
                'Logo': 'Assets\\StoreLogo.png',
            },
            'Dependencies': {
                'TargetDeviceFamily': {
                    'Name': 'Windows.Desktop',
                    'MinVersion': '10.0.17763.0',
                },
            },
            'Applications': [{
                'Id': _safe_id(listing['name']),
                'Executable': 'hartos-shell.exe',
                'EntryPoint': 'Windows.FullTrustApplication',
            }],
            'hartos_config': {
                'listing_id': listing_id,
                'recipe_id': listing.get('recipe_id', ''),
                'api_endpoint': '/chat',
            },
        }
        return {'success': True, 'platform': 'microsoft_store', 'manifest': manifest}

    def distribute_to_apple_store(self, listing_id: str) -> Dict:
        """Generate iOS manifest for Apple wrapper."""
        listing = self._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        manifest = {
            'format': 'ios_plist',
            'CFBundleIdentifier': f'com.hevolve.hartos.{_safe_id(listing["name"])}',
            'CFBundleName': listing['name'],
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1',
            'MinimumOSVersion': '15.0',
            'UIDeviceFamily': [1, 2],  # iPhone + iPad
            'UIRequiredDeviceCapabilities': ['arm64'],
            'NSAppTransportSecurity': {
                'NSAllowsArbitraryLoads': False,
                'NSExceptionDomains': {
                    'hartos.local': {'NSTemporaryExceptionAllowsInsecureHTTPLoads': True},
                },
            },
            'hartos_config': {
                'listing_id': listing_id,
                'recipe_id': listing.get('recipe_id', ''),
                'api_endpoint': '/chat',
            },
        }
        return {'success': True, 'platform': 'apple_store', 'manifest': manifest}

    def distribute_to_web(self, listing_id: str) -> Dict:
        """Generate PWA manifest for web distribution."""
        listing = self._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        manifest = {
            'format': 'pwa_manifest',
            'name': listing['name'],
            'short_name': listing['name'][:12],
            'description': listing.get('description', ''),
            'start_url': f'/app/{listing_id}',
            'display': 'standalone',
            'background_color': '#0D1117',
            'theme_color': '#58A6FF',
            'icons': [
                {'src': '/icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                {'src': '/icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
            ],
            'categories': [listing.get('category', 'utilities')],
            'hartos_config': {
                'listing_id': listing_id,
                'recipe_id': listing.get('recipe_id', ''),
                'api_endpoint': '/chat',
            },
        }
        return {'success': True, 'platform': 'web', 'manifest': manifest}

    def distribute_to_flatpak(self, listing_id: str) -> Dict:
        """Generate Flatpak manifest for Linux distribution."""
        listing = self._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        app_id = f'com.hevolve.hartos.{_safe_id(listing["name"])}'
        manifest = {
            'format': 'flatpak_manifest',
            'app-id': app_id,
            'runtime': 'org.freedesktop.Platform',
            'runtime-version': '23.08',
            'sdk': 'org.freedesktop.Sdk',
            'command': 'hartos-shell',
            'finish-args': [
                '--share=network',
                '--share=ipc',
                '--socket=fallback-x11',
                '--socket=wayland',
                '--device=dri',
            ],
            'modules': [{
                'name': 'hartos-agent-wrapper',
                'buildsystem': 'simple',
                'build-commands': ['install -D hartos-shell /app/bin/hartos-shell'],
            }],
            'hartos_config': {
                'listing_id': listing_id,
                'recipe_id': listing.get('recipe_id', ''),
                'api_endpoint': '/chat',
            },
        }
        return {'success': True, 'platform': 'flatpak', 'manifest': manifest}


# ═══════════════════════════════════════════════════════════════════════════
# AppPromotionAgent — Auto-marketing engine
# ═══════════════════════════════════════════════════════════════════════════

class AppPromotionAgent:
    """Auto-marketing engine for marketplace listings.

    When a user publishes an app, this agent automatically:
    - Generates marketing content and SEO keywords
    - Distributes to relevant channels
    - Onboards new users with tutorials
    - Runs benchmark comparisons against competitors
    """

    def __init__(self, marketplace: AppMarketplace):
        self._marketplace = marketplace

    def auto_promote(self, listing_id: str) -> Dict:
        """Create automatic marketing campaign for a listing.

        1. Generate marketing content (description, keywords, comparison hooks)
        2. Post to platform feed
        3. Distribute to channels matching app category
        4. Schedule periodic re-promotion
        """
        listing = self._marketplace._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        results = {
            'listing_id': listing_id,
            'name': listing['name'],
            'actions': [],
        }

        # 1. Generate SEO keywords and marketing copy
        keywords = self._generate_keywords(listing)
        results['generated_keywords'] = keywords

        # 2. Post to platform social feed
        feed_result = self._post_to_feed(listing)
        results['actions'].append({'type': 'feed_post', 'result': feed_result})

        # 3. Distribute to category-matched channels
        channel_map = {
            'coding': ['discord', 'slack', 'matrix'],
            'productivity': ['slack', 'telegram', 'web'],
            'marketing': ['telegram', 'twitter', 'linkedin'],
            'finance': ['telegram', 'discord', 'web'],
            'education': ['telegram', 'discord', 'web'],
            'entertainment': ['discord', 'telegram', 'whatsapp'],
            'research': ['slack', 'matrix', 'discord'],
            'design': ['discord', 'slack', 'web'],
            'social': ['telegram', 'whatsapp', 'discord'],
        }
        category = listing.get('category', 'general')
        channels = channel_map.get(category, ['web', 'telegram'])
        for channel in channels:
            dist_result = self._marketplace.distribute_to_channel(listing_id, channel)
            results['actions'].append({
                'type': 'channel_distribution',
                'channel': channel,
                'result': 'dispatched' if dist_result.get('success') else dist_result.get('error', 'failed'),
            })

        # 4. Create thought experiment for comparison
        thought_result = self._create_thought_experiment(listing)
        results['actions'].append({'type': 'thought_experiment', 'result': thought_result})

        # 5. Schedule re-promotion goal
        repromo_result = self._schedule_repromotion(listing_id)
        results['actions'].append({'type': 'repromotion_scheduled', 'result': repromo_result})

        logger.info(f"Marketplace promotion: '{listing['name']}' — "
                     f"{len(results['actions'])} actions taken")
        return results

    def auto_onboard_users(self, listing_id: str, user_id: str) -> Dict:
        """Onboard a user who just installed an app.

        1. Send welcome message with quick-start tutorial
        2. Verify recipe is wired
        3. Track engagement start
        """
        listing = self._marketplace._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        results = {'listing_id': listing_id, 'user_id': user_id, 'actions': []}

        # 1. Send welcome/tutorial message
        welcome_msg = (
            f"Welcome to {listing['name']}! {listing.get('tagline', '')}\n\n"
            f"Quick start:\n"
            f"- Just type your request naturally\n"
            f"- The agent uses a trained recipe for fast, reliable results\n"
        )
        if listing.get('feature_list'):
            welcome_msg += f"- Features: {', '.join(listing['feature_list'][:5])}\n"

        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            dispatch_goal(
                prompt=f"Send this welcome message to user {user_id}: {welcome_msg}",
                user_id=user_id,
                goal_id=f'onboard_{listing_id}_{user_id}',
                goal_type='onboarding',
            )
            results['actions'].append({'type': 'welcome_sent', 'success': True})
        except Exception as e:
            results['actions'].append({'type': 'welcome_sent', 'success': False, 'error': str(e)})

        return results

    def auto_compete(self, listing_id: str) -> Dict:
        """Find competing apps and generate comparison content."""
        competitors = self._marketplace.get_competing_apps(listing_id)
        listing = self._marketplace._listings().get(listing_id)
        if not listing:
            return {'error': 'Listing not found'}

        if not competitors:
            return {
                'listing_id': listing_id,
                'message': 'No competitors found — first mover in category',
            }

        comparison_ids = [listing_id] + [c['listing_id'] for c in competitors[:4]]
        comparison = self._marketplace.compare_apps(comparison_ids)

        return {
            'listing_id': listing_id,
            'name': listing['name'],
            'competitor_count': len(competitors),
            'comparison': comparison,
        }

    def run_benchmark_comparison(self, listing_ids: List[str]) -> Dict:
        """Run the same benchmark task on competing apps, publish results.

        Dispatches a benchmark goal for each app and collects scores.
        """
        results = {'benchmarks': [], 'timestamp': datetime.utcnow().isoformat()}
        listings = self._marketplace._listings()

        for lid in listing_ids:
            listing = listings.get(lid)
            if not listing:
                continue

            recipe_id = listing.get('recipe_id', '')
            if not recipe_id:
                results['benchmarks'].append({
                    'listing_id': lid,
                    'name': listing['name'],
                    'status': 'skipped',
                    'reason': 'no recipe_id',
                })
                continue

            # Dispatch benchmark via agent engine
            try:
                from integrations.agent_engine.dispatch import dispatch_goal
                response = dispatch_goal(
                    prompt=(
                        f"Benchmark the app '{listing['name']}' (recipe: {recipe_id}). "
                        f"Run a standard task and measure: response time, accuracy, "
                        f"completeness, user satisfaction proxy."
                    ),
                    user_id='benchmark_agent',
                    goal_id=f'benchmark_{lid}',
                    goal_type='benchmark',
                )
                results['benchmarks'].append({
                    'listing_id': lid,
                    'name': listing['name'],
                    'status': 'dispatched',
                    'response': response,
                })
            except Exception as e:
                results['benchmarks'].append({
                    'listing_id': lid,
                    'name': listing['name'],
                    'status': 'failed',
                    'error': str(e),
                })

        return results

    # ─── Private promotion helpers ─────────────────────────────────────

    def _generate_keywords(self, listing: Dict) -> List[str]:
        """Generate SEO keywords from listing metadata."""
        words = set()
        for field in ('name', 'description', 'tagline', 'category', 'agent_type'):
            text = listing.get(field, '')
            if text:
                for word in text.lower().split():
                    cleaned = word.strip('.,!?()[]{}":;')
                    if len(cleaned) > 3:
                        words.add(cleaned)
        # Add category and features
        words.add(listing.get('category', ''))
        for feat in listing.get('feature_list', []):
            words.add(feat.lower())
        return sorted(words)[:30]

    def _post_to_feed(self, listing: Dict) -> str:
        """Post app announcement to platform social feed."""
        try:
            from integrations.social.models import db_session
            from integrations.social.services import PostService
            with db_session() as db:
                PostService.create_post(
                    db,
                    author_id=listing['owner_id'],
                    body=(
                        f"New on HART OS Marketplace: {listing['name']}\n\n"
                        f"{listing.get('tagline', listing.get('description', '')[:200])}\n\n"
                        f"Category: {listing.get('category', 'general')}\n"
                        f"Pricing: {listing.get('pricing_model', 'free')}\n"
                        f"Install it now from the App Marketplace!"
                    ),
                    visibility='public',
                )
            return 'posted'
        except ImportError:
            return 'social_service_unavailable'
        except Exception as e:
            return f'feed_error: {e}'

    def _create_thought_experiment(self, listing: Dict) -> str:
        """Create a thought experiment comparing approaches to the app's problem."""
        try:
            from integrations.social.thought_experiment_service import ThoughtExperimentService
            from integrations.social.models import db_session
            with db_session() as db:
                ThoughtExperimentService.create_experiment(
                    db,
                    author_id=listing['owner_id'],
                    title=f"Which approach solves {listing.get('category', 'this')} tasks better?",
                    hypothesis=(
                        f"'{listing['name']}' offers a unique approach: "
                        f"{listing.get('description', '')[:300]}. "
                        f"How does this compare to existing solutions?"
                    ),
                    intent_category='technology',
                )
            return 'experiment_created'
        except ImportError:
            return 'thought_experiment_service_unavailable'

    def _schedule_repromotion(self, listing_id: str) -> str:
        """Schedule periodic re-promotion based on performance."""
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            dispatch_goal(
                prompt=(
                    f"Re-promote marketplace app {listing_id}. "
                    f"Check current install count and rating. "
                    f"If growth is stalling, create fresh marketing content "
                    f"and redistribute to new channels."
                ),
                user_id='marketplace_promoter',
                goal_id=f'repromo_{listing_id}_{int(time.time())}',
                goal_type='marketing',
            )
            return 'scheduled'
        except Exception as e:
            return f'schedule_error: {e}'


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _safe_id(name: str) -> str:
    """Convert a name to a safe identifier (lowercase, alphanumeric + underscore)."""
    return ''.join(c if c.isalnum() else '_' for c in name.lower()).strip('_')[:64]


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════

_marketplace: Optional[AppMarketplace] = None
_promotion_agent: Optional[AppPromotionAgent] = None


def get_marketplace() -> AppMarketplace:
    """Get the global AppMarketplace singleton."""
    global _marketplace
    if _marketplace is None:
        _marketplace = AppMarketplace()
    return _marketplace


def get_promotion_agent() -> AppPromotionAgent:
    """Get the global AppPromotionAgent singleton."""
    global _promotion_agent
    if _promotion_agent is None:
        _promotion_agent = AppPromotionAgent(get_marketplace())
    return _promotion_agent


# ═══════════════════════════════════════════════════════════════════════════
# Goal Seed — Auto-Promoter
# ═══════════════════════════════════════════════════════════════════════════

SEED_APP_MARKETPLACE_PROMOTER = {
    'slug': 'bootstrap_app_marketplace_promoter',
    'goal_type': 'marketing',
    'title': 'App Marketplace Auto-Promoter',
    'description': (
        'Continuously monitor new app listings on the HART OS Marketplace. '
        'For every new listing: '
        '1) Auto-generate marketing content — description polish, SEO keywords, comparison hooks, '
        '2) Distribute to channels matching the app category (Discord for coding, Telegram for finance, etc.), '
        '3) Run benchmark comparisons against competing apps in the same category, '
        '4) Onboard new users with welcome messages and quick-start tutorials, '
        '5) Schedule periodic re-promotion for apps with stalling growth, '
        '6) Create thought experiments: "Which app solves X better?" to drive organic discussion. '
        'Make every app discoverable. 90% of revenue flows to creators.'
    ),
    'config': {
        'autonomous': True,
        'continuous': True,
        'bootstrap_slug': 'bootstrap_app_marketplace_promoter',
    },
    'spark_budget': 500,
    'use_product': True,
}


# ═══════════════════════════════════════════════════════════════════════════
# Flask Blueprint
# ═══════════════════════════════════════════════════════════════════════════

from flask import Blueprint, jsonify, request

marketplace_bp = Blueprint('marketplace', __name__)


@marketplace_bp.route('/api/marketplace/apps', methods=['GET'])
def list_apps():
    """List/search marketplace apps.

    Query params:
        category: filter by category
        q: search query
        sort: popular|newest|rating|name|installs (default: popular)
        page: page number (default: 1)
        per_page: items per page (default: 20, max: 50)
    """
    mp = get_marketplace()
    category = request.args.get('category')
    query = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'popular')
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(50, max(1, request.args.get('per_page', 20, type=int)))

    if query:
        # Full-text search with optional filters
        filters = {}
        if category:
            filters['category'] = category
        pricing = request.args.get('pricing_model')
        if pricing:
            filters['pricing_model'] = pricing
        min_rating = request.args.get('min_rating', type=float)
        if min_rating:
            filters['min_rating'] = min_rating
        platform = request.args.get('platform')
        if platform:
            filters['platform'] = platform

        result = mp.search_apps(query, filters)
        return jsonify({'success': True, **result})

    result = mp.list_apps(category=category, sort=sort, page=page, per_page=per_page)
    return jsonify({'success': True, **result})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>', methods=['GET'])
def get_app(listing_id):
    """Get full app details with reviews and competitors."""
    mp = get_marketplace()
    app = mp.get_app(listing_id)
    if not app:
        return jsonify({'success': False, 'error': 'Listing not found'}), 404
    return jsonify({'success': True, 'app': app})


@marketplace_bp.route('/api/marketplace/apps', methods=['POST'])
def publish_app():
    """Publish a new app to the marketplace.

    Body (JSON):
        owner_id: str (required)
        name: str (required)
        description: str (required)
        recipe_id: str
        agent_type: str
        tagline: str
        category: str
        pricing_model: free|freemium|paid|subscription
        price_spark: int
        monthly_price_spark: int
        feature_list: list[str]
        platforms: list[str]
        screenshots: list[str]
        demo_url: str
        distribution_channels: list[str]
        benchmark_scores: dict
    """
    data = request.get_json(force=True)
    owner_id = data.get('owner_id', '')
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()

    if not owner_id or not name or not description:
        return jsonify({
            'success': False,
            'error': 'owner_id, name, and description are required',
        }), 400

    mp = get_marketplace()
    result = mp.publish_app(
        owner_id=owner_id,
        name=name,
        description=description,
        recipe_id=data.get('recipe_id', ''),
        agent_type=data.get('agent_type', 'general'),
        tagline=data.get('tagline', ''),
        category=data.get('category', 'general'),
        screenshots=data.get('screenshots', []),
        demo_url=data.get('demo_url', ''),
        pricing_model=data.get('pricing_model', 'free'),
        price_spark=data.get('price_spark', 0),
        monthly_price_spark=data.get('monthly_price_spark', 0),
        feature_list=data.get('feature_list', []),
        competing_with=data.get('competing_with', []),
        platforms=data.get('platforms', ['web']),
        distribution_channels=data.get('distribution_channels', []),
        benchmark_scores=data.get('benchmark_scores', {}),
        product_id=data.get('product_id', ''),
    )

    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'listing': result}), 201


@marketplace_bp.route('/api/marketplace/apps/<listing_id>', methods=['PUT'])
def update_app(listing_id):
    """Update an existing listing."""
    data = request.get_json(force=True)
    owner_id = data.pop('owner_id', '')
    if not owner_id:
        return jsonify({'success': False, 'error': 'owner_id required'}), 400

    mp = get_marketplace()
    result = mp.update_app(listing_id, owner_id, data)
    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'listing': result})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/compare', methods=['GET'])
def compare_app(listing_id):
    """Compare an app with its competitors or specific apps.

    Query params:
        with: comma-separated listing IDs to compare against
    """
    mp = get_marketplace()
    compare_with = request.args.get('with', '')
    if compare_with:
        ids = [listing_id] + [x.strip() for x in compare_with.split(',') if x.strip()]
    else:
        # Auto-compare with top competitors
        competitors = mp.get_competing_apps(listing_id)
        ids = [listing_id] + [c['listing_id'] for c in competitors[:4]]

    result = mp.compare_apps(ids)
    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'comparison': result})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/install', methods=['POST'])
def install_app(listing_id):
    """Install an app for a user.

    Body: { user_id: str }
    """
    data = request.get_json(force=True)
    user_id = data.get('user_id', '')
    if not user_id:
        return jsonify({'success': False, 'error': 'user_id required'}), 400

    mp = get_marketplace()
    result = mp.install_app(user_id, listing_id)
    if result.get('error'):
        code = 409 if 'Already installed' in result['error'] else 400
        return jsonify({'success': False, 'error': result['error']}), code
    return jsonify(result)


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/review', methods=['POST'])
def review_app(listing_id):
    """Rate and review an app.

    Body: { user_id: str, rating: float (1-5), review: str }
    """
    data = request.get_json(force=True)
    user_id = data.get('user_id', '')
    rating = data.get('rating', 0)
    review = data.get('review', '')

    if not user_id:
        return jsonify({'success': False, 'error': 'user_id required'}), 400
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'rating must be a number'}), 400

    mp = get_marketplace()
    result = mp.rate_app(user_id, listing_id, rating, review)
    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify(result)


@marketplace_bp.route('/api/marketplace/trending', methods=['GET'])
def trending_apps():
    """Get trending apps (most installed in last 7 days)."""
    mp = get_marketplace()
    days = request.args.get('days', 7, type=int)
    limit = min(50, max(1, request.args.get('limit', 20, type=int)))
    trending = mp.get_trending(days=days, limit=limit)
    return jsonify({'success': True, 'trending': trending, 'count': len(trending)})


@marketplace_bp.route('/api/marketplace/categories', methods=['GET'])
def list_categories():
    """Get all marketplace categories with listing counts."""
    mp = get_marketplace()
    categories = mp.get_categories()
    return jsonify({'success': True, 'categories': categories})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/promote', methods=['POST'])
def promote_app(listing_id):
    """Trigger auto-promotion for an app."""
    agent = get_promotion_agent()
    result = agent.auto_promote(listing_id)
    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'promotion': result})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/distribute', methods=['POST'])
def distribute_app(listing_id):
    """Distribute an app to a specific platform or channel.

    Body: { platform: str, channel: str }
    One of platform or channel is required.
    platform: google_play|microsoft_store|apple_store|web|flatpak
    channel: discord|telegram|slack|matrix|whatsapp|etc.
    """
    data = request.get_json(force=True)
    mp = get_marketplace()

    platform = data.get('platform', '')
    channel = data.get('channel', '')

    if platform:
        distributors = {
            'google_play': mp.distribute_to_google_play,
            'microsoft_store': mp.distribute_to_microsoft_store,
            'apple_store': mp.distribute_to_apple_store,
            'web': mp.distribute_to_web,
            'flatpak': mp.distribute_to_flatpak,
        }
        fn = distributors.get(platform)
        if not fn:
            return jsonify({
                'success': False,
                'error': f'Unknown platform: {platform}. '
                         f'Valid: {list(distributors.keys())}',
            }), 400
        result = fn(listing_id)
    elif channel:
        result = mp.distribute_to_channel(listing_id, channel)
    else:
        return jsonify({
            'success': False,
            'error': 'Either platform or channel is required',
        }), 400

    if result.get('error'):
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify(result)


@marketplace_bp.route('/api/marketplace/revenue/<owner_id>', methods=['GET'])
def revenue_report(owner_id):
    """Get revenue report for an app creator."""
    mp = get_marketplace()
    report = mp.get_revenue_report(owner_id)
    return jsonify({'success': True, 'report': report})


@marketplace_bp.route('/api/marketplace/apps/<listing_id>/feature-matrix', methods=['GET'])
def feature_matrix(listing_id):
    """Get feature comparison matrix for the category of this app."""
    mp = get_marketplace()
    listing = mp.get_app(listing_id)
    if not listing:
        return jsonify({'success': False, 'error': 'Listing not found'}), 404
    matrix = mp.feature_comparison_matrix(listing.get('category', 'general'))
    return jsonify({'success': True, 'matrix': matrix})


logger.info("App Marketplace module loaded")
