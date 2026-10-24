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
``{'success': False, 'error': str, 'status'?: int, 'unconfirmed'?: True}``
-- never an exception.  ``unconfirmed`` marks a request McGroce neither did
nor refused, or did without this node reading what it did: a timeout, a
transport error (a refused connection included, since the client cannot
tell one refused before the request left from one dropped after it did), a
5xx, or a 2xx whose body is not JSON (``status`` 2xx tells that one apart:
McGroce answered with a success it could not explain).  The caller cannot
know what happened.  A refusal (4xx) and a request never attempted (bad
URL, breaker open) carry no such mark.  ``key`` is McGroce's own messageKey
when its answer carried one (AP2_REFUSAL_KEYS names the ones its AP2
verifier refuses with); a 4xx without one came from something in front of
McGroce, not from McGroce refusing.
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


def _err(error: str, status: Optional[int] = None,
         unconfirmed: bool = False, key: Optional[str] = None) -> Dict[str, Any]:
    out = {'success': False, 'error': error}
    if status is not None:
        out['status'] = status
    if unconfirmed:
        out['unconfirmed'] = True
    if key:
        # McGroce's own messageKey, as its own field: callers decide on the
        # key, never by parsing the error text, which is rendered for people.
        out['key'] = key
    return out


#: McGroce's AP2 verifier refusing a mandate: the messageKeys its answers
#: carry (Ap2CheckoutServiceImpl / Ap2MandateVerifierImpl on
#: mcgroce-agentic-commerce).  A 4xx with none of them (Kong's "no Route
#: matched" 404, a 401 wall) is not McGroce refusing the mandate.  The ONE
#: set, for agent_orders_ready and the order probe alike.
AP2_MANDATE_REJECTED = 'AP2_MANDATE_REJECTED'
AP2_REFUSAL_KEYS = frozenset({AP2_MANDATE_REJECTED, 'CART_NOT_FOUND', 'AMOUNT_MISMATCH'})


def order_number_of(data) -> Optional[Any]:
    """The order an AP2 checkout answer names (COMMERCE_MCGROCE.md's order
    contract: 200 {"orderNumber": ...}; an older McGroce's {"id": ...}
    too), or None when it names none: no body, not an object, neither key,
    or an empty one.  The ONE reader for the client's success check and
    commerce_tools._order_placed, so the two can never disagree."""
    if not isinstance(data, dict):
        return None
    for key in ('orderNumber', 'id'):
        value = data.get(key)
        # McGroce's orderNumber is yyyyMMddHHmmssS plus the order id
        # (CompleteOrderActivity.determineOrderNumber), a digit string that
        # can never be blank or all zeros; an older McGroce's id is a
        # positive int.  Anything else names no order: 0, '', '   ', '0',
        # a bool, a dict, a negative number.
        if isinstance(value, str):
            value = value.strip()
            if value and value.strip('0'):
                return value
        elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


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


def _error_parts(resp):
    """Broadleaf ErrorWrapper -> ('KEY: message; ...', first messageKey);
    anything else -> ('McGroce answered HTTP <status>', None)."""
    try:
        body = resp.json()
        msgs = [m for m in (body.get('messages') or []) if isinstance(m, dict)]
        text = '; '.join(
            ': '.join(x for x in (m.get('messageKey'), m.get('message')) if x)
            for m in msgs)
        key = next((m.get('messageKey') for m in msgs if m.get('messageKey')), None)
        if text:
            return text, key
    except Exception:
        pass
    return f'McGroce answered HTTP {resp.status_code}', None


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
            return _err('McGroce timed out', unconfirmed=True)
        except requests.RequestException as e:
            self.breaker.record_failure()
            return _err(f'McGroce unreachable: {type(e).__name__}', unconfirmed=True)
        if resp.status_code >= 500:
            self.breaker.record_failure()
            text, key = _error_parts(resp)
            return _err(text, resp.status_code, unconfirmed=True, key=key)
        self.breaker.record_success()
        if resp.status_code >= 400:
            text, key = _error_parts(resp)
            return _err(text, resp.status_code, key=key)
        data = None
        if resp.content:
            try:
                data = resp.json()
            except ValueError:
                # McGroce did it (a 2xx) and this node could not read what
                # it did: for the caller that is neither confirmed nor
                # refused, the same as an answer that never came, with the
                # 2xx status telling it apart from a 5xx.
                return _err('McGroce sent a non-JSON answer', resp.status_code,
                            unconfirmed=True)
        return {'success': True, 'status': resp.status_code, 'data': data}

    def site(self, user_id, method: str, path: str, params=None,
             json_body=None, need_customer: bool = True,
             extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
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
        if extra_headers:
            headers.update(extra_headers)
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
        """The shopper's open cart.  McGroce answers a customer with no cart
        404 CART_NOT_FOUND (CartEndpoint.findCartForCustomer), so a 2xx that
        names no cart id is not "no cart yet": it is an answer this node
        cannot read as a cart, and a failure, never an empty cart."""
        res = self.site(user_id, 'GET', '/cart')
        if res['success'] and not (isinstance(res.get('data'), dict)
                                   and res['data'].get('id') not in (None, '')):
            return _err('McGroce sent an empty answer where a cart was due', res['status'])
        return res

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

    def complete_ap2_checkout(self, user_id, attestation: str):
        """POST /cart/checkout/ap2 -- McGroce places the order for a mandate
        HARTOS has already been paid for.  ``attestation`` is the signed,
        consumed mandate (signing.mandate_attestation); McGroce checks it
        itself -- this customer, the repriced cart total and currency,
        unexpired, never used before -- and records the payment and submits
        the order in one step.  The two raw calls this replaced
        (POST /cart/checkout/payment, then POST /cart/checkout) let any
        caller mark a cart paid, and McGroce could verify neither.
        The answer names the order (order_number_of): a success that
        names none (an empty body, a wrapper without the keys) breaks the
        order contract and is never a legitimate success, so it is
        answered as ``unconfirmed`` with its 2xx status, like a body this
        node cannot read at all."""
        from integrations.commerce.signing import MANDATE_HEADER
        res = self.site(user_id, 'POST', '/cart/checkout/ap2',
                        extra_headers={MANDATE_HEADER: attestation})
        if res['success'] and order_number_of(res.get('data')) is None:
            return _err('McGroce answered without an order number', res['status'],
                        unconfirmed=True)
        return res

    def agent_orders_ready(self, user_id) -> Optional[str]:
        """None when this McGroce places agent orders, else why not.

        Asked BEFORE any money is taken: the same endpoint called without an
        attestation is refused (403 AP2_MANDATE_REJECTED) by a McGroce that
        has it and touches nothing; one without it answers 404, one without
        the shared secret 503.  Asked every time, never remembered: a McGroce
        rolled back since the last yes would take the money and no order.
        """
        res = self.site(user_id, 'POST', '/cart/checkout/ap2')
        if res.get('status') == 403 and res.get('key') in AP2_REFUSAL_KEYS:
            return None
        if res.get('status') == 404:
            return 'this McGroce does not take agent orders yet'
        return f"McGroce cannot take agent orders right now ({res.get('error') or 'unexpected answer'})"

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
