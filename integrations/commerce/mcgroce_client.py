"""
McGroce site API client.

One class, one transport: every call goes through core.http_pool (pooled
session, explicit timeout, remote retry policy).  Server-to-server calls carry
the shared ``X-Commerce-Secret`` (the same secret McGroce presents to
/api/commerce/session), and the shopper as ``X-Commerce-Customer``.

The paths are in ONE table, ``PATHS``.  The search and store-discovery paths
are the McGroce v1 endpoints goal_manager's p2p_grocery prompt already
documents; the cart, order and merchant paths are the agentic-commerce
endpoints.  Changing a route on the McGroce side is a one-line change here.
"""
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger('hevolve.commerce')

PATHS = {
    'search': '/search/{query}',
    'stores_by_zip': '/zipcodesearch/stores/{zipcode}',
    'stores_by_geo': '/zipcodesearch/stores/{lat}/{lng}',
    'cart': '/agent/cart',
    'cart_items': '/agent/cart/items',
    'cart_item': '/agent/cart/items/{product_id}',
    'orders': '/agent/orders',
    'order': '/agent/orders/{order_id}',
    'order_confirm': '/agent/orders/{order_id}/confirm',
    'order_cancel': '/agent/orders/{order_id}/cancel',
    'merchant_onboard': '/agent/merchants',
}

#: (connect, read) seconds.  A storefront call, not an LLM completion.
TIMEOUT = (3, 15)


class McGroceError(Exception):
    """McGroce is unconfigured, unreachable, or answered with an error."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _seg(value) -> str:
    return quote(str(value), safe='')


class McGroceClient:
    def __init__(self, base_url: Optional[str] = None,
                 secret: Optional[str] = None):
        from integrations.commerce import bindings
        self.base_url = (base_url if base_url is not None
                         else bindings.mcgroce_api_url()).rstrip('/')
        self.secret = (secret if secret is not None
                       else bindings.commerce_session_secret())

    def _call(self, method: str, path_key: str, customer: Optional[str] = None,
              params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None, **segments) -> Any:
        if not self.base_url:
            raise McGroceError('McGroce is not configured (MCGROCE_API_URL)')
        path = PATHS[path_key].format(**{k: _seg(v) for k, v in segments.items()})
        headers = {'Accept': 'application/json'}
        if self.secret:
            headers['X-Commerce-Secret'] = self.secret
        if customer:
            headers['X-Commerce-Customer'] = str(customer)
        from core.http_pool import pooled_request
        try:
            resp = pooled_request(method, self.base_url + path, timeout=TIMEOUT,
                                  params=params, json=body, headers=headers)
        except Exception as e:
            raise McGroceError(f'McGroce unreachable: {e}') from e
        status = getattr(resp, 'status_code', 0)
        try:
            data = resp.json() if getattr(resp, 'content', b'') else {}
        except ValueError:
            data = {'raw': getattr(resp, 'text', '')[:500]}
        if status >= 400:
            msg = data.get('error') or data.get('message') if isinstance(data, dict) else None
            raise McGroceError(msg or f'McGroce answered HTTP {status}', status)
        return data

    # ── Catalogue ──
    def search_products(self, query: str, store_id: Optional[str] = None):
        params = {'storeId': store_id} if store_id else None
        return self._call('GET', 'search', params=params, query=query)

    def find_stores(self, zipcode: str = '', lat=None, lng=None):
        if lat not in (None, '') and lng not in (None, ''):
            return self._call('GET', 'stores_by_geo', lat=lat, lng=lng)
        if not zipcode:
            raise McGroceError('zipcode or lat/lng required', 400)
        return self._call('GET', 'stores_by_zip', zipcode=zipcode)

    # ── Cart ──
    def get_cart(self, customer: str):
        return self._call('GET', 'cart', customer=customer)

    def add_to_cart(self, customer: str, product_id: str, quantity: int = 1,
                    store_id: Optional[str] = None):
        body = {'productId': product_id, 'quantity': int(quantity)}
        if store_id:
            body['storeId'] = store_id
        return self._call('POST', 'cart_items', customer=customer, body=body)

    def remove_from_cart(self, customer: str, product_id: str):
        return self._call('DELETE', 'cart_item', customer=customer,
                          product_id=product_id)

    # ── Orders ──
    def create_order(self, customer: str, store_id: Optional[str] = None):
        body = {'storeId': store_id} if store_id else {}
        return self._call('POST', 'orders', customer=customer, body=body)

    def get_order(self, customer: str, order_id: str):
        return self._call('GET', 'order', customer=customer, order_id=order_id)

    def confirm_order(self, customer: str, order_id: str, payment_ref: str):
        return self._call('POST', 'order_confirm', customer=customer,
                          body={'paymentRef': payment_ref}, order_id=order_id)

    def cancel_order(self, customer: str, order_id: str, reason: str = ''):
        return self._call('POST', 'order_cancel', customer=customer,
                          body={'reason': reason}, order_id=order_id)

    # ── Merchants ──
    def onboard_merchant(self, owner: str, details: Dict[str, Any]):
        return self._call('POST', 'merchant_onboard', customer=owner,
                          body=details)
