"""
McGroce REST client -- I/O only, no tool or UI logic.

Site API (``MCGROCE_API_URL``, e.g. ``https://mcgroce.example/api/v1``):
HTTP Basic with the service account (``MCGROCE_API_USER`` /
``MCGROCE_API_PASSWORD``) plus the acting customer's ``customerId`` header.
The customer id is read from the commerce binding for ``user_id`` and is
NEVER taken from an argument: McGroce trusts that header blindly
(RestApiCustomerStateFilter "DOES NOT provide any security"), so a model-
supplied id would let an agent act as any shopper.

Admin API (``MCGROCE_ADMIN_API_URL``, e.g. ``.../admin/api/v1``): Bearer
``MCGROCE_ADMIN_API_KEY`` (McGroce's AdminApiKeyAuthenticationFilter).

Every call:
- https only, unless ``MCGROCE_ALLOW_HTTP=1`` AND the host is loopback
  (McGroce's /api/** chain is requires-channel="https"; plain http would
  send the Basic credentials in clear);
- ``Content-Type: application/json`` (McGroce endpoints declare
  ``consumes=JSON``; a GET without it can 415 -- gotcha G1);
- goes through core.http_pool.pooled_request (pooled, bounded timeout);
- is guarded by one CircuitBreaker('mcgroce', 5, 60): five transport
  failures or 5xx in a row stop further calls for a minute.

Results are ``{'success': True, 'status': int, 'data': ...}`` or
``{'success': False, 'error': str, 'status'?: int}`` -- never an exception.
"""

import base64
import ipaddress
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit

import requests

from core.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Connect / read.  McGroce is a Solr-backed Tomcat: searches can take
# seconds, a stuck one must not hold an agent turn for long.
MCGROCE_TIMEOUT = (3, 15)

mcgroce_breaker = CircuitBreaker(name='mcgroce', threshold=5, cooldown=60)


def _err(error: str, status: Optional[int] = None) -> Dict[str, Any]:
    out = {'success': False, 'error': error}
    if status is not None:
        out['status'] = status
    return out


def _seg(value) -> str:
    """One URL path segment -- quoted so an argument can never add a path."""
    return quote(str(value).strip(), safe='')


def _is_loopback(host: str) -> bool:
    if (host or '').lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error_text(resp) -> str:
    """Broadleaf ErrorWrapper -> 'KEY: message; ...', else the status."""
    try:
        body = resp.json()
        msgs = body.get('messages') or []
        text = '; '.join(
            ': '.join(x for x in (m.get('messageKey'), m.get('message')) if x)
            for m in msgs if isinstance(m, dict))
        if text:
            return text
    except Exception:
        pass
    return f'McGroce answered HTTP {resp.status_code}'


class McGroceClient:

    def __init__(self, base_url: Optional[str] = None,
                 user: Optional[str] = None, password: Optional[str] = None,
                 admin_url: Optional[str] = None,
                 admin_key: Optional[str] = None,
                 bindings=None, breaker: Optional[CircuitBreaker] = None):
        from core.config_cache import get_secret
        self.base_url = (base_url if base_url is not None
                         else get_secret('MCGROCE_API_URL', '')).rstrip('/')
        self.user = user if user is not None else get_secret('MCGROCE_API_USER', '')
        self.password = (password if password is not None
                         else get_secret('MCGROCE_API_PASSWORD', ''))
        self.admin_url = (admin_url if admin_url is not None
                          else get_secret('MCGROCE_ADMIN_API_URL', '')).rstrip('/')
        self.admin_key = (admin_key if admin_key is not None
                          else get_secret('MCGROCE_ADMIN_API_KEY', ''))
        self._bindings = bindings
        self.breaker = breaker or mcgroce_breaker

    @property
    def bindings(self):
        if self._bindings is None:
            from integrations.commerce.bindings import get_bindings
            self._bindings = get_bindings()
        return self._bindings

    # ── transport ────────────────────────────────────────────────
    @staticmethod
    def _url_error(url: str) -> Optional[str]:
        parts = urlsplit(url)
        if parts.scheme == 'https' and parts.hostname:
            return None
        if parts.scheme == 'http' and parts.hostname:
            from core.config_cache import env_flag
            if env_flag('MCGROCE_ALLOW_HTTP', False) and _is_loopback(parts.hostname):
                return None
            return ('McGroce must be reached over https (plain http is allowed '
                    'only for a loopback host with MCGROCE_ALLOW_HTTP=1)')
        return 'McGroce is not configured (MCGROCE_API_URL)'

    def _call(self, method: str, url: str, headers: Dict[str, str],
              params=None, json_body=None) -> Dict[str, Any]:
        bad = self._url_error(url)
        if bad:
            return _err(bad)
        if self.breaker.is_open():
            return _err('McGroce is not answering right now; try again in a minute')
        from core.http_pool import pooled_request
        try:
            resp = pooled_request(method, url, headers=headers, params=params,
                                  json=json_body, timeout=MCGROCE_TIMEOUT)
        except requests.Timeout:
            self.breaker.record_failure()
            return _err('McGroce timed out')
        except requests.RequestException as e:
            self.breaker.record_failure()
            return _err(f'McGroce unreachable: {type(e).__name__}')
        if resp.status_code >= 500:
            self.breaker.record_failure()
            return _err(_error_text(resp), resp.status_code)
        self.breaker.record_success()
        if resp.status_code >= 400:
            return _err(_error_text(resp), resp.status_code)
        data = None
        if resp.content:
            try:
                data = resp.json()
            except ValueError:
                return _err('McGroce sent a non-JSON answer', resp.status_code)
        return {'success': True, 'status': resp.status_code, 'data': data}

    def site(self, user_id, method: str, path: str, params=None,
             json_body=None, need_customer: bool = True) -> Dict[str, Any]:
        """A site-API call acting as ``user_id``'s bound McGroce customer."""
        binding = self.bindings.get(user_id) if user_id else None
        if need_customer and not binding:
            return _err('This account is not linked to McGroce yet; '
                        'open McGroce and sign in first')
        from integrations.commerce.bindings import is_merchant
        if is_merchant(binding):
            # A merchant's id comes from McGroce's admin table: sent as
            # customerId it names whichever SHOPPER has the same number.
            if need_customer:
                return _err('A merchant account has no shopping cart; sign in '
                            'to McGroce as a customer to shop')
            binding = None            # catalog reads: no customer at all
        if not (self.user and self.password):
            return _err('McGroce service account is not configured')
        token = base64.b64encode(
            f'{self.user}:{self.password}'.encode('utf-8')).decode('ascii')
        headers = {'Authorization': f'Basic {token}',
                   'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        if binding:
            headers['customerId'] = str(binding['customer_id'])
        return self._call(method, self.base_url + path, headers,
                          params=params, json_body=json_body)

    def admin(self, method: str, path: str, json_body=None) -> Dict[str, Any]:
        if not self.admin_key:
            return _err('McGroce admin API is not configured')
        headers = {'Authorization': f'Bearer {self.admin_key}',
                   'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        return self._call(method, self.admin_url + path, headers,
                          json_body=json_body)

    # ── site endpoints (paths relative to /api/v1) ───────────────
    def store_id_for(self, user_id) -> Optional[str]:
        binding = self.bindings.get(user_id) if user_id else None
        return (binding or {}).get('store_id')

    def find_stores(self, user_id, zipcode=None, lat=None, lng=None):
        if zipcode not in (None, ''):
            path = f'/zipcodesearch/stores/{_seg(zipcode)}'
        else:
            path = f'/zipcodesearch/stores/{_seg(lat)}/{_seg(lng)}'
        return self.site(user_id, 'GET', path, need_customer=False)

    def search_catalog(self, user_id, q, page=1, page_size=10,
                       category_id=None):
        params = {'q': q, 'page': int(page), 'pageSize': int(page_size)}
        store_id = self.store_id_for(user_id)
        if store_id:
            params['storeId'] = store_id
        path = ('/catalog/search' if category_id in (None, '')
                else f'/catalog/search/category/{_seg(category_id)}')
        return self.site(user_id, 'GET', path, params=params,
                         need_customer=False)

    def suggest(self, user_id, q):
        return self.site(user_id, 'GET', f'/search/suggest/{_seg(q)}',
                         need_customer=False)

    def product(self, user_id, product_id):
        return self.site(user_id, 'GET', f'/catalog/product/{_seg(product_id)}',
                         need_customer=False)

    def product_skus(self, user_id, product_id):
        return self.site(user_id, 'GET',
                         f'/catalog/product/{_seg(product_id)}/skus',
                         need_customer=False)

    def cart_get(self, user_id):
        return self.site(user_id, 'GET', '/cart')

    def cart_add(self, user_id, product_id, category_id, quantity=1):
        return self.site(user_id, 'POST', f'/cart/{_seg(product_id)}',
                         params={'categoryId': category_id,
                                 'quantity': int(quantity)})

    def cart_update(self, user_id, item_id, quantity):
        return self.site(user_id, 'PUT', f'/cart/items/{_seg(item_id)}',
                         params={'quantity': int(quantity)})

    def cart_remove(self, user_id, item_id):
        return self.site(user_id, 'DELETE', f'/cart/items/{_seg(item_id)}')

    def apply_promo(self, user_id, code):
        return self.site(user_id, 'POST', '/cart/offer',
                         params={'promoCode': code})

    def add_checkout_payment(self, user_id, payment: Dict[str, Any]):
        """POST /cart/checkout/payment -- OrderPaymentWrapper fields as
        params (McGroce binds this wrapper as @ModelAttribute, not a JSON
        body; rest_api.md 'no-@RequestBody gotcha')."""
        return self.site(user_id, 'POST', '/cart/checkout/payment',
                         params=payment)

    def submit_checkout(self, user_id):
        return self.site(user_id, 'POST', '/cart/checkout')

    def orders(self, user_id, order_status='SUBMITTED'):
        return self.site(user_id, 'GET', '/orders',
                         params={'orderStatus': order_status})

    # ── admin endpoints (paths relative to /admin/api/v1) ────────
    def onboard_merchant(self, dto: Dict[str, Any]):
        return self.admin('POST', '/mcg/merchants', json_body=dto)

    def create_product(self, dto: Dict[str, Any]):
        return self.admin('POST', '/entities/product', json_body=dto)


_client: Optional[McGroceClient] = None


def get_client() -> McGroceClient:
    """Process-wide client built from config (secrets read once)."""
    global _client
    if _client is None:
        _client = McGroceClient()
    return _client


# The credentials this module reads from the environment; a vault value is
# delivered for these names (hartos.ai_key_vault.reads_from_env).
ENV_SECRETS = (
    'MCGROCE_API_PASSWORD',
    'MCGROCE_ADMIN_API_KEY',
)
