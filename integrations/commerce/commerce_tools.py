"""
Commerce tools -- McGroce shopping, checkout and merchant onboarding for agents.

Tier-2, goal-gated: create_recipe / reuse_recipe call
``register_commerce_tools`` when the goal carries the ``commerce`` tag
(marketing_tools.detect_goal_tags, or a goal type registered with it).  The
same pure functions are listed in ``COMMERCE_TOOLS`` for the MCP bridge --
one implementation, two exposures.

Every function takes ``user_id`` first.  The agent registration binds it from
the registrar (the verified identity of the conversation), so the model never
sees or chooses it; the McGroce customer is then looked up from the commerce
binding inside McGroceClient, never from an argument.

UI: results are pushed as the EXISTING Liquid UI fragment types every client
already renders (Android AgentOverlayBridge, the Nunba web LiquidUI, desktop)
through LiquidUIService.agent_ui_update -- product_card, cart, checkout,
approval, payment_status, order_tracking, form, list.  No new component
type.  Currency is INR.

Payments: prepare -> a person approves -> checkout.  The model cannot
approve: commerce_prepare_checkout only creates an AP2 CartMandate and shows
the approval card; the approval arrives through POST /api/agent/approval
with the person's JWT; commerce_checkout refuses unless that mandate is
approved, unexpired, the caller's own, and the live cart still hashes to
what was approved.

All tools return a JSON string; failures are ``{"success": false, "error"}``.
"""

import functools
import inspect
import json
import logging
import re
from typing import Annotated, Any, Dict, List, Optional

logger = logging.getLogger('tool_execution')

MERCHANT = 'mcgroce'
CURRENCY = 'INR'
_AGENT_ID = 'mcgroce'
_MAX_CARDS = 3
_MERCHANT_ROLES = frozenset({'merchant', 'admin', 'vendor'})
ORDER_STEPS = ('Submitted', 'Accepted', 'Out for delivery', 'Delivered')
_STEP_OF_STATUS = {
    'SUBMITTED': 0, 'NEW': 0, 'IN_PROCESS': 1, 'ACCEPTED': 1,
    'OUT_FOR_DELIVERY': 2, 'SHIPPED': 2,
    'DELIVERED': 3, 'FULFILLED': 3, 'COMPLETED': 3,
}
_ZIP_RE = re.compile(r'^\d{6}$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ── plumbing ─────────────────────────────────────────────────────

def _client():
    from integrations.commerce.mcgroce_client import get_client
    return get_client()


def _mandates():
    from integrations.ap2.ap2_mandate import get_mandate_store
    return get_mandate_store()


def _drafts():
    from integrations.commerce.drafts import get_draft_store
    return get_draft_store()


def _out(payload: Dict[str, Any]) -> str:
    from core.agent_tools import _bounded_observation
    return _bounded_observation(json.dumps(payload, default=str),
                                'narrow the query for fewer results')


def _fail(error: str, **extra) -> str:
    return _out({'success': False, 'error': error, **extra})


def push_fragment(user_id, component: Dict[str, Any]) -> bool:
    """Show one Liquid UI fragment to ``user_id`` (best effort)."""
    try:
        from core.platform.registry import get_registry
        service = get_registry().get('LiquidUIService')
        if service is None:
            return False
        component.setdefault('agent_id', _AGENT_ID)
        return bool(service.agent_ui_update(str(user_id), component))
    except Exception as e:
        logger.debug(f'commerce: fragment {component.get("type")} not shown: {e}')
        return False


def _amount(value) -> float:
    """A McGroce Money bean ``{amount, currency}`` or a bare number."""
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _image(product: Dict[str, Any]) -> Optional[str]:
    media = product.get('primaryMedia') or {}
    if not media and product.get('media'):
        media = (product.get('media') or [{}])[0] or {}
    return media.get('url') or product.get('image') or None


def _product_card(p: Dict[str, Any]) -> Dict[str, Any]:
    sale, retail = _amount(p.get('salePrice')), _amount(p.get('retailPrice'))
    img = _image(p)
    return {
        'type': 'product_card', 'name': p.get('name') or 'Product',
        'price': sale or retail, 'currency': CURRENCY,
        'image': img, 'image_url': img,
        'rating': p.get('rating'),
        'description': (p.get('description') or '')[:200],
        'product_id': p.get('id'),
        'category_id': p.get('defaultCategoryId'),
        'buy_action': 'cart.add',
    }


def cart_view(order: Dict[str, Any]) -> Dict[str, Any]:
    """OrderWrapper -> {order_id, items[], total, currency}."""
    order = order or {}
    items = []
    for it in order.get('orderItems') or []:
        price = _amount(it.get('salePrice')) or _amount(it.get('retailPrice'))
        items.append({
            'item_id': it.get('id'), 'sku_id': it.get('skuId'),
            'product_id': it.get('productId'),
            'category_id': it.get('categoryId'),
            'name': it.get('name') or 'Item',
            'quantity': int(it.get('quantity') or 0), 'price': price,
        })
    return {'order_id': order.get('id'), 'items': items,
            'total': _amount(order.get('total')), 'currency': CURRENCY}


def mandate_cart(view: Dict[str, Any]) -> Dict[str, Any]:
    """The payable content of a cart view, as ap2_mandate hashes it."""
    return {
        'lines': [{'sku_id': i['sku_id'] if i['sku_id'] is not None else i['item_id'],
                   'qty': i['quantity'], 'unit_price': i['price']}
                  for i in view['items']],
        'total': view['total'], 'currency': view['currency'],
    }


def _cart_fragment(view: Dict[str, Any]) -> Dict[str, Any]:
    return {'type': 'cart',
            'items': [{'item_id': i['item_id'], 'name': i['name'],
                       'quantity': i['quantity'], 'price': i['price']}
                      for i in view['items']],
            'total': view['total'], 'currency': CURRENCY,
            'checkout_action': 'checkout.start'}


def _tracking_fragment(order_id, status: str, eta=None) -> Dict[str, Any]:
    at = _STEP_OF_STATUS.get(str(status or '').upper(), 0)
    return {'type': 'order_tracking', 'order_id': order_id,
            'status': status or 'SUBMITTED',
            'steps': [{'label': s, 'done': i <= at}
                      for i, s in enumerate(ORDER_STEPS)],
            'eta': eta}


def _cart_result(user_id, res) -> str:
    if not res['success']:
        return _fail(res['error'])
    view = cart_view(res['data'])
    push_fragment(user_id, _cart_fragment(view))
    return _out({'success': True, 'cart': view})


# ── shopper tools ────────────────────────────────────────────────

def commerce_find_stores(
    user_id: str,
    zipcode: Annotated[Optional[str], "6-digit Indian pincode"] = None,
    lat: Annotated[Optional[float], "Latitude, when there is no pincode"] = None,
    lng: Annotated[Optional[float], "Longitude, when there is no pincode"] = None,
) -> str:
    """Find McGroce stores near a pincode or a location."""
    if zipcode in (None, '') and (lat is None or lng is None):
        return _fail('give a pincode, or both lat and lng')
    if zipcode not in (None, '') and not _ZIP_RE.match(str(zipcode).strip()):
        return _fail('a pincode is 6 digits')
    res = _client().find_stores(user_id, zipcode=zipcode, lat=lat, lng=lng)
    if not res['success']:
        return _fail(res['error'])
    data = res['data'] or {}
    rows = data.items() if isinstance(data, dict) else enumerate(data)
    stores = []
    for dist, s in rows:
        if not isinstance(s, dict):
            continue
        try:
            km = round(float(s.get('distanceFromMe') or dist) / 1000.0, 1)
        except (TypeError, ValueError):
            km = None
        stores.append({'store_id': s.get('id'), 'name': s.get('name'),
                       'address': ', '.join(x for x in (s.get('address1'),
                                                         s.get('city')) if x),
                       'distance_km': km,
                       'delivery': bool(s.get('deliveryAvailable'))})
    stores.sort(key=lambda s: (s['distance_km'] is None, s['distance_km'] or 0))
    stores = stores[:10]
    push_fragment(user_id, {
        'type': 'list', 'title': 'Stores near you',
        'items': [{'title': s['name'],
                   'subtitle': ' · '.join(x for x in (
                       s['address'],
                       f"{s['distance_km']} km" if s['distance_km'] is not None else '',
                       'Delivers' if s['delivery'] else 'Pickup only') if x),
                   'id': s['store_id']} for s in stores],
    })
    return _out({'success': True, 'stores': stores})


def commerce_search_catalog(
    user_id: str,
    q: Annotated[str, "What to search for, e.g. 'milk'"],
    page: Annotated[int, "Result page, from 1"] = 1,
    page_size: Annotated[int, "Results per page (max 20)"] = 10,
    category_id: Annotated[Optional[int], "Limit to one category"] = None,
) -> str:
    """Search the McGroce catalog (the shopper's selected store)."""
    q = (q or '').strip()
    if not q:
        return _fail('say what to search for')
    page_size = max(1, min(int(page_size or 10), 20))
    res = _client().search_catalog(user_id, q, page=max(1, int(page or 1)),
                                   page_size=page_size, category_id=category_id)
    if not res['success']:
        return _fail(res['error'])
    data = res['data'] or {}
    products = [p for p in (data.get('products') or []) if isinstance(p, dict)]
    for p in products[:_MAX_CARDS]:
        push_fragment(user_id, _product_card(p))
    return _out({
        'success': True, 'total': data.get('totalResults', len(products)),
        'page': data.get('page', page),
        'products': [{'product_id': p.get('id'), 'name': p.get('name'),
                      'price': _amount(p.get('salePrice')) or _amount(p.get('retailPrice')),
                      'category_id': p.get('defaultCategoryId')}
                     for p in products],
    })


def commerce_suggest(
    user_id: str,
    q: Annotated[str, "The first few letters typed"],
) -> str:
    """Autocomplete product names."""
    q = (q or '').strip()
    if not q:
        return _fail('type a few letters first')
    res = _client().suggest(user_id, q)
    if not res['success']:
        return _fail(res['error'])
    rows = res['data'] or []
    return _out({'success': True, 'suggestions': [
        {'product_id': r.get('id'), 'name': r.get('name')}
        for r in rows if isinstance(r, dict)][:10]})


def commerce_product_detail(
    user_id: str,
    product_id: Annotated[int, "McGroce product id"],
) -> str:
    """One product with its SKUs (sizes / variants)."""
    client = _client()
    res = client.product(user_id, product_id)
    if not res['success']:
        return _fail(res['error'])
    p = res['data'] or {}
    skus_res = client.product_skus(user_id, product_id)
    skus = skus_res['data'] if skus_res['success'] and skus_res['data'] else []
    push_fragment(user_id, _product_card(p))
    return _out({'success': True, 'product': {
        'product_id': p.get('id'), 'name': p.get('name'),
        'description': p.get('description'),
        'price': _amount(p.get('salePrice')) or _amount(p.get('retailPrice')),
        'category_id': p.get('defaultCategoryId'),
        'skus': [{'sku_id': s.get('id'), 'name': s.get('name'),
                  'price': _amount(s.get('salePrice')) or _amount(s.get('retailPrice')),
                  'active': s.get('active')} for s in skus if isinstance(s, dict)],
    }})


def commerce_cart_view(user_id: str) -> str:
    """Show the shopper's cart."""
    return _cart_result(user_id, _client().cart_get(user_id))


def commerce_cart_add(
    user_id: str,
    product_id: Annotated[int, "McGroce product id"],
    category_id: Annotated[int, "The product's category id (from search)"],
    quantity: Annotated[int, "How many"] = 1,
) -> str:
    """Add a product to the cart."""
    if int(quantity) < 1:
        return _fail('quantity must be at least 1')
    return _cart_result(user_id, _client().cart_add(
        user_id, product_id, category_id, quantity))


def commerce_cart_update(
    user_id: str,
    item_id: Annotated[int, "Cart line id (from the cart)"],
    quantity: Annotated[int, "New quantity (0 removes the line)"],
) -> str:
    """Change how many of a cart line."""
    if int(quantity) < 0:
        return _fail('quantity cannot be negative')
    if int(quantity) == 0:
        return commerce_cart_remove(user_id, item_id)
    return _cart_result(user_id, _client().cart_update(user_id, item_id, quantity))


def commerce_cart_remove(
    user_id: str,
    item_id: Annotated[int, "Cart line id (from the cart)"],
) -> str:
    """Remove a line from the cart."""
    return _cart_result(user_id, _client().cart_remove(user_id, item_id))


def commerce_apply_promo(
    user_id: str,
    code: Annotated[str, "Promo code"],
) -> str:
    """Apply a promo code to the cart."""
    code = (code or '').strip()
    if not code:
        return _fail('which promo code?')
    return _cart_result(user_id, _client().apply_promo(user_id, code))


def commerce_prepare_checkout(
    user_id: str,
    cap: Annotated[Optional[float], "The most the shopper said they want to spend, in INR"] = None,
) -> str:
    """Ask the shopper to approve paying for the current cart.

    Shows the checkout summary and an Approve / Decline card.  Nothing is
    charged: the shopper approves on their own device, then call
    commerce_checkout with the returned mandate_id.
    """
    res = _client().cart_get(user_id)
    if not res['success']:
        return _fail(res['error'])
    view = cart_view(res['data'])
    if not view['items']:
        return _fail('the cart is empty')
    from integrations.ap2.ap2_mandate import MandateError
    try:
        m = _mandates().create_cart_mandate(
            user_id, MERCHANT, mandate_cart(view), cap=cap,
            description=f"McGroce order, {len(view['items'])} item(s)")
    except MandateError as e:
        return _fail(str(e))
    count = sum(i['quantity'] for i in view['items'])
    push_fragment(user_id, {'type': 'checkout',
                            'items': _cart_fragment(view)['items'],
                            'total': view['total'], 'currency': CURRENCY,
                            'confirm_action': f'ap2_pay:{m.payment_id}'})
    shown = push_fragment(user_id, {
        'type': 'approval', 'action': f'ap2_pay:{m.payment_id}',
        'description': (f"Pay ₹{view['total']:.2f} to McGroce for {count} "
                        f"item{'s' if count != 1 else ''}?"),
        'options': ['Approve', 'Decline'],
    })
    return _out({'success': True, 'status': 'awaiting_approval',
                 'mandate_id': m.mandate_id, 'payment_id': m.payment_id,
                 'amount': m.amount, 'currency': m.currency,
                 'expires_at': m.expires_at, 'approval_card_shown': shown,
                 'next': 'wait for the shopper to approve, then call '
                         'commerce_checkout with this mandate_id'})


def commerce_checkout(
    user_id: str,
    mandate_id: Annotated[str, "mandate_id from commerce_prepare_checkout"],
) -> str:
    """Pay for the approved cart and place the McGroce order."""
    client = _client()
    res = client.cart_get(user_id)
    if not res['success']:
        return _fail(res['error'])
    view = cart_view(res['data'])
    store = _mandates()
    ok, reason = store.verify_for_checkout(mandate_id, user_id, mandate_cart(view))
    if not ok:
        return _fail(f'checkout refused: {reason}')
    m = store.get(mandate_id)
    from integrations.ap2.ap2_protocol import get_payment_ledger
    ledger = get_payment_ledger()
    paid = ledger.process_payment(m.payment_id)
    payment = ledger.get_payment(m.payment_id)
    method = payment.gateway.value if payment and payment.gateway else None
    if paid.get('status') == 'redirect_required':
        push_fragment(user_id, {'type': 'payment_status', 'status': 'processing',
                                'amount': float(m.amount), 'currency': CURRENCY,
                                'method': method,
                                'transaction_id': paid.get('transaction_id'),
                                'redirect_url': paid.get('redirect_url')})
        return _out({'success': True, 'status': 'awaiting_payment',
                     'redirect_url': paid.get('redirect_url'),
                     'payment_id': m.payment_id})
    if not paid.get('success'):
        from integrations.ap2.ap2_protocol import PaymentStatus
        if payment is not None and payment.status == PaymentStatus.FAILED:
            push_fragment(user_id, {'type': 'payment_status', 'status': 'failed',
                                    'amount': float(m.amount), 'currency': CURRENCY,
                                    'method': method})
        return _fail(f"payment failed: {paid.get('error', 'declined')}",
                     payment_id=m.payment_id)
    return _out(_place_order(client, user_id, m, view, payment))


def _place_order(client, user_id, m, view, payment) -> Dict[str, Any]:
    """After the money is taken: consume the mandate, record the payment on
    the McGroce cart (referenceNumber = mandate_id) and submit the order.
    Shared by commerce_checkout and the redirect-gateway callback."""
    _mandates().consume(m.mandate_id)
    method = payment.gateway.value if payment and payment.gateway else None
    txn = payment.gateway_transaction_id if payment else None
    push_fragment(user_id, {'type': 'payment_status', 'status': 'completed',
                            'amount': float(m.amount), 'currency': CURRENCY,
                            'method': method, 'transaction_id': txn})
    recorded = client.add_checkout_payment(user_id, {
        'orderId': view['order_id'], 'type': 'THIRD_PARTY_ACCOUNT',
        'gatewayType': 'Passthrough', 'amount': m.amount,
        'currency': m.currency, 'referenceNumber': m.mandate_id})
    placed = client.submit_checkout(user_id) if recorded['success'] else recorded
    if not placed['success']:
        logger.error(f'commerce: paid {m.payment_id} but McGroce did not '
                     f'place the order: {placed["error"]}')
        return {'success': False,
                'error': ('the payment went through but McGroce did not place '
                          f'the order ({placed["error"]}); it needs a person to '
                          'reconcile'),
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id,
                'needs_attention': True}
    order = placed['data'] or {}
    order_id = order.get('orderNumber') or order.get('id')
    status = order.get('status') or 'SUBMITTED'
    push_fragment(user_id, _tracking_fragment(order_id, status))
    return {'success': True, 'status': 'ordered', 'order_id': order_id,
            'order_status': status, 'payment_id': m.payment_id,
            'amount': m.amount, 'currency': m.currency}


def complete_redirect_checkout(payment_id: str) -> Dict[str, Any]:
    """Place the McGroce order for a redirect-gateway (PhonePe) payment the
    gateway callback has just confirmed COMPLETED.

    The person approved one exact cart; if the live cart no longer hashes to
    it, nothing is placed -- the payment is flagged for a person instead of
    ordering something they did not approve.
    """
    import hmac
    from integrations.ap2.ap2_mandate import MandateError, canonical_cart_hash
    from integrations.ap2.ap2_protocol import PaymentStatus, get_payment_ledger
    m = _mandates().find_by_payment_id(payment_id)
    if m is None or m.status != 'approved':
        return {'success': False, 'error': 'no approved mandate for this payment'}
    payment = get_payment_ledger().get_payment(payment_id)
    if payment is None or payment.status != PaymentStatus.COMPLETED:
        return {'success': False, 'error': 'payment is not completed'}
    client = _client()
    res = client.cart_get(m.user_id)
    if not res['success']:
        return {'success': False, 'error': res['error'], 'needs_attention': True,
                'payment_id': payment_id}
    view = cart_view(res['data'])
    try:
        same = hmac.compare_digest(canonical_cart_hash(mandate_cart(view)),
                                   m.cart_hash)
    except MandateError:
        same = False
    if not same:
        logger.error(f'commerce: PhonePe payment {payment_id} completed but the '
                     f'cart changed since approval; order NOT placed')
        push_fragment(m.user_id, {
            'type': 'notification', 'severity': 'warning',
            'title': 'Payment received, order on hold',
            'message': 'Your cart changed after you approved it, so we have not '
                       'placed the order. We will sort it out with you.'})
        return {'success': False, 'error': 'cart changed since approval',
                'needs_attention': True, 'payment_id': payment_id}
    return _place_order(client, m.user_id, m, view, payment)


def commerce_order_status(
    user_id: str,
    order_status: Annotated[str, "SUBMITTED, IN_PROCESS, FULFILLED, ..."] = 'SUBMITTED',
) -> str:
    """The shopper's orders in a status, newest first."""
    res = _client().orders(user_id, (order_status or 'SUBMITTED').upper())
    if not res['success']:
        if res.get('status') == 404:
            return _out({'success': True, 'orders': []})
        return _fail(res['error'])
    rows = [o for o in (res['data'] or []) if isinstance(o, dict)]
    rows.sort(key=lambda o: o.get('id') or 0, reverse=True)
    orders = [{'order_id': o.get('orderNumber') or o.get('id'),
               'status': o.get('status'), 'total': _amount(o.get('total'))}
              for o in rows[:10]]
    if orders:
        push_fragment(user_id, _tracking_fragment(orders[0]['order_id'],
                                                  orders[0]['status']))
    return _out({'success': True, 'orders': orders})


# ── merchant tools (drafted, then a person approves) ─────────────

def commerce_onboard_merchant(
    user_id: str,
    display_name: Annotated[str, "Store name shoppers will see"],
    address: Annotated[str, "Street address"],
    zip: Annotated[str, "6-digit pincode"],
    phone: Annotated[str, "Store phone number"],
    email: Annotated[str, "Owner email (a password-reset link goes here)"],
    lat: Annotated[Optional[float], "Store latitude"] = None,
    lng: Annotated[Optional[float], "Store longitude"] = None,
    delivery_radius_km: Annotated[float, "Delivery radius in km"] = 5,
    min_order_inr: Annotated[float, "Minimum order value in INR"] = 0,
) -> str:
    """Draft a new McGroce store for the owner to review and approve."""
    problems = []
    if not (display_name or '').strip():
        problems.append('store name')
    if not (address or '').strip():
        problems.append('address')
    if not _ZIP_RE.match(str(zip or '').strip()):
        problems.append('6-digit pincode')
    if len(re.sub(r'\D', '', str(phone or ''))) < 10:
        problems.append('phone number')
    if not _EMAIL_RE.match(str(email or '').strip()):
        problems.append('email')
    if problems:
        return _fail('missing or invalid: ' + ', '.join(problems))
    dto = {'displayName': display_name.strip(), 'address': address.strip(),
           'zip': str(zip).strip(), 'phone': str(phone).strip(),
           'email': str(email).strip(), 'latitude': lat, 'longitude': lng,
           'deliveryRadiusKm': float(delivery_radius_km),
           'minOrderInr': float(min_order_inr)}
    draft = _drafts().create(user_id, 'merchant', dto)
    action = f"merchant_onboard:{draft['draft_id']}"
    labels = (('displayName', 'Store name'), ('address', 'Address'),
              ('zip', 'Pincode'), ('phone', 'Phone'), ('email', 'Email'),
              ('deliveryRadiusKm', 'Delivery radius (km)'),
              ('minOrderInr', 'Minimum order (₹)'))
    push_fragment(user_id, {
        'type': 'form', 'title': 'Review your store details',
        'fields': [{'name': k, 'label': lbl, 'value': dto[k], 'readonly': True}
                   for k, lbl in labels],
        'submit_label': 'Looks good', 'action': action})
    shown = push_fragment(user_id, {
        'type': 'approval', 'action': action,
        'description': f"Create the McGroce store “{dto['displayName']}”?",
        'options': ['Create store', 'Not now']})
    return _out({'success': True, 'status': 'awaiting_approval',
                 'draft_id': draft['draft_id'], 'approval_card_shown': shown})


def commerce_create_sku(
    user_id: str,
    name: Annotated[str, "Product name"],
    price_inr: Annotated[float, "Price in INR"],
    category: Annotated[str, "Category, e.g. 'Dairy'"],
    description: Annotated[str, "Short description"] = '',
    image_url: Annotated[Optional[str], "https image URL"] = None,
    options: Annotated[Optional[str], "Variants, e.g. '500 ml, 1 L'"] = None,
) -> str:
    """Draft a new product for the merchant to review and approve."""
    from integrations.commerce.bindings import get_bindings
    binding = get_bindings().get(user_id)
    if not binding or binding.get('role') not in _MERCHANT_ROLES:
        return _fail('only a signed-in McGroce merchant can add products')
    if not (name or '').strip():
        return _fail('the product needs a name')
    try:
        price = round(float(price_inr), 2)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return _fail('the price must be more than ₹0')
    if image_url and not str(image_url).startswith('https://'):
        return _fail('the image must be an https URL')
    dto = {'name': name.strip(), 'price': price, 'currency': CURRENCY,
           'category': (category or '').strip(),
           'description': (description or '').strip()[:1000],
           'imageUrl': image_url or None,
           'options': [o.strip() for o in (options or '').split(',') if o.strip()],
           'storeId': binding.get('store_id')}
    draft = _drafts().create(user_id, 'sku', dto)
    action = f"merchant_sku:{draft['draft_id']}"
    push_fragment(user_id, {
        'type': 'product_card', 'name': dto['name'], 'price': price,
        'currency': CURRENCY, 'image': dto['imageUrl'],
        'image_url': dto['imageUrl'], 'description': dto['description'][:200]})
    shown = push_fragment(user_id, {
        'type': 'approval', 'action': action,
        'description': f"Add “{dto['name']}” at ₹{price:.2f} to your store?",
        'options': ['Add product', 'Not now']})
    return _out({'success': True, 'status': 'awaiting_approval',
                 'draft_id': draft['draft_id'], 'approval_card_shown': shown})


# ── registration ─────────────────────────────────────────────────

_TOOL_SPECS = (
    ('commerce_find_stores', commerce_find_stores,
     'Find McGroce grocery stores near a pincode or location'),
    ('commerce_search_catalog', commerce_search_catalog,
     'Search McGroce products in the shopper\'s store'),
    ('commerce_suggest', commerce_suggest,
     'Autocomplete McGroce product names'),
    ('commerce_product_detail', commerce_product_detail,
     'Get one McGroce product with its variants'),
    ('commerce_cart_view', commerce_cart_view,
     'Show the shopper\'s McGroce cart'),
    ('commerce_cart_add', commerce_cart_add,
     'Add a product to the McGroce cart'),
    ('commerce_cart_update', commerce_cart_update,
     'Change the quantity of a McGroce cart line'),
    ('commerce_cart_remove', commerce_cart_remove,
     'Remove a line from the McGroce cart'),
    ('commerce_apply_promo', commerce_apply_promo,
     'Apply a promo code to the McGroce cart'),
    ('commerce_prepare_checkout', commerce_prepare_checkout,
     'Ask the shopper to approve paying for the cart (shows an Approve card; charges nothing)'),
    ('commerce_checkout', commerce_checkout,
     'Pay for a cart the shopper approved and place the order (needs mandate_id)'),
    ('commerce_order_status', commerce_order_status,
     'Show the shopper\'s McGroce orders and tracking'),
    ('commerce_onboard_merchant', commerce_onboard_merchant,
     'Draft a new McGroce store for the owner to approve'),
    ('commerce_create_sku', commerce_create_sku,
     'Draft a new McGroce product for the merchant to approve'),
)

# The MCP bridge (integrations/mcp/mcp_http_bridge.py _load_tools) consumes
# this list.  Its functions take user_id explicitly: an MCP caller is the
# node's own token holder, and checkout still needs the shopper's approval.
COMMERCE_TOOLS: List[Dict[str, Any]] = [
    {'name': name, 'func': fn, 'description': desc, 'tags': ['commerce']}
    for name, fn, desc in _TOOL_SPECS
]


def bind_user(fn, user_id):
    """``fn`` with user_id fixed, its signature WITHOUT user_id -- so the
    model's tool schema never offers the identity as a parameter."""
    @functools.wraps(fn)
    def bound(*args, **kwargs):
        return fn(user_id, *args, **kwargs)
    sig = inspect.signature(fn)
    bound.__signature__ = sig.replace(
        parameters=[p for n, p in sig.parameters.items() if n != 'user_id'])
    bound.__annotations__ = {k: v for k, v in fn.__annotations__.items()
                             if k != 'user_id'}
    del bound.__wrapped__
    return bound


def register_commerce_tools(helper, assistant, user_id: str, executor=None):
    """Register the commerce tools for ``user_id`` (Tier-2, tag 'commerce').

    Same shape as news_tools.register_news_tools: schema on the helper AND
    the assistant, execution on the assistant, and on ``executor`` when
    given so an Assistant-proposed call is not stranded by autogen's
    repeat-speaker rule (see that function's docstring).
    """
    for name, fn, desc in _TOOL_SPECS:
        bound = bind_user(fn, str(user_id))
        helper.register_for_llm(name=name, description=desc)(bound)
        assistant.register_for_llm(name=name, description=desc)(bound)
        assistant.register_for_execution(name=name)(bound)
        if executor is not None:
            executor.register_for_execution(name=name)(bound)
    logger.info(f'Registered {len(_TOOL_SPECS)} commerce tools for user {user_id}')
    return len(_TOOL_SPECS)
