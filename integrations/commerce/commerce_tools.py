"""
Commerce tools -- McGroce shopping, checkout and merchant onboarding for agents.

Tier-2, goal-gated: create_recipe / reuse_recipe call
``register_commerce_tools`` when the goal carries the ``commerce`` tag
(marketing_tools.detect_goal_tags, or a goal type registered with it).  They
are listed in ``COMMERCE_TOOLS``; the MCP bridge does not expose them (see
there).

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
import os
import re
import time
from typing import Annotated, Any, Dict, List, Optional

from integrations.ap2.ap2_protocol import (
    NO_LIVE_GATEWAY, NO_LIVE_GATEWAY_REASON, mock_would_settle,
    refuse_no_live_gateway)

logger = logging.getLogger('tool_execution')

MERCHANT = 'mcgroce'
CURRENCY = 'INR'
CHECKOUT_KIND = 'commerce_checkout'
_AGENT_ID = 'mcgroce'
_MAX_CARDS = 3
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
    """Show one Liquid UI fragment to ``user_id`` (best effort).

    Through liquid_ui_service.push_agent_ui: the shell's gated agent_ui_update
    when this process serves one, AND the user's own stream (WAMP
    com.hertzai.hevolve.social.<user_id> / per-user SSE) -- the only leg a
    cloud gateway without a shell has.
    """
    try:
        from integrations.agent_engine.liquid_ui_service import push_agent_ui
        component.setdefault('agent_id', _AGENT_ID)
        return bool(push_agent_ui(_AGENT_ID, component, user_id=str(user_id)))
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
    items = []
    for s in stores:
        subtitle = ' · '.join(x for x in (
            s['address'],
            f"{s['distance_km']} km" if s['distance_km'] is not None else '',
            'Delivers' if s['delivery'] else 'Pickup only') if x)
        # `text` is what the list renderers draw (COMPONENT_TYPES['list']:
        # a string or {text|label}); title/subtitle/id stay for hosts keying on them.
        items.append({'text': ' - '.join(x for x in (s['name'], subtitle) if x),
                      'title': s['name'], 'subtitle': subtitle,
                      'id': s['store_id']})
    push_fragment(user_id, {'type': 'list', 'title': 'Stores near you',
                            'items': items})
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


# The shopper's wording for ap2_protocol.NO_LIVE_GATEWAY.  select_gateway
# answers MOCK whenever neither PhonePe nor Stripe is registered, and the mock
# captures anything, so a cart on a node with no gateway keys was marked paid
# and the McGroce order placed with no money taken (read at 903cd58c0,
# 2026-10-14).  The gate is ap2_protocol.mock_would_settle, the one spelling
# every settler reads; the cart path acts for the shopper by id and has no
# operator object, so dev mode is its only exemption.
_NO_LIVE_GATEWAY = f'this store cannot take payments right now: {NO_LIVE_GATEWAY}'


def commerce_prepare_checkout(
    user_id: str,
    cap: Annotated[Optional[float], "The most the shopper said they want to spend, in INR"] = None,
) -> str:
    """Ask the shopper to approve paying for the current cart.

    Shows the checkout summary and an Approve / Decline card.  Nothing is
    charged: the shopper approves on their own device, then call
    commerce_checkout with the returned mandate_id.  Refused outright on a
    node that cannot take money (ap2_protocol.mock_would_settle): a shopper
    is never asked to approve a payment nothing can capture.
    """
    res = _client().cart_get(user_id)
    if not res['success']:
        return _fail(res['error'])
    view = cart_view(res['data'])
    if not view['items']:
        return _fail('the cart is empty')
    from integrations.ap2.ap2_protocol import get_payment_ledger
    if mock_would_settle(get_payment_ledger().select_gateway(CURRENCY)):
        refuse_no_live_gateway('commerce', user_id)
        return _fail(_NO_LIVE_GATEWAY)
    blocked = _why_no_order(_client(), user_id, view)
    if blocked:
        return _fail(f'this cart cannot be paid for by your agent right now: {blocked}')
    from integrations.ap2.ap2_mandate import MandateError
    # One live checkout per shopper and cart: two approval cards for one cart
    # could otherwise both be paid, and only the first could become an order.
    cart_ref = str(view['order_id'])
    try:
        ok, reason = _mandates().supersede(user_id, MERCHANT, CHECKOUT_KIND, cart_ref)
        if not ok:
            return _fail(reason)
        m = _mandates().create_cart_mandate(
            user_id, MERCHANT, mandate_cart(view), cap=cap,
            description=f"McGroce order, {len(view['items'])} item(s)",
            kind=CHECKOUT_KIND, requester_agent_id=_AGENT_ID, cart_ref=cart_ref)
    except MandateError as e:       # a bad cart, or the node's payment pause
        return _fail(str(e))
    count = sum(i['quantity'] for i in view['items'])
    push_fragment(user_id, {'type': 'checkout',
                            'items': _cart_fragment(view)['items'],
                            'total': view['total'], 'currency': CURRENCY,
                            'confirm_action': f'ap2_pay:{m.payment_id}'})
    from integrations.ap2.ap2_mandate import approval_card
    card = approval_card(m, description=(
        f"Pay ₹{view['total']:.2f} to McGroce for {count} "
        f"item{'s' if count != 1 else ''}?"))
    card['agent_id'] = _AGENT_ID
    shown = push_fragment(user_id, card)
    return _out({'success': True, 'status': 'awaiting_approval',
                 'mandate_id': m.mandate_id, 'payment_id': m.payment_id,
                 'amount': m.amount, 'currency': m.currency,
                 'expires_at': m.expires_at, 'approval_card_shown': shown,
                 'next': 'wait for the shopper to approve; approving pays and '
                         'places the order.  If they say it did not go '
                         'through, call commerce_checkout with this mandate_id'})


def commerce_checkout(
    user_id: str,
    mandate_id: Annotated[str, "mandate_id from commerce_prepare_checkout"],
) -> str:
    """Pay for the approved cart and place the McGroce order.

    Approving the card already does this; call it only to retry a checkout
    that did not complete.  Refuses unless the shopper approved.  When the
    payment went through but the order did not, this places the order with
    that payment; it never charges again.
    """
    from integrations.ap2.ap2_mandate import MandateKeyError
    from integrations.ap2.ap2_protocol import PaymentStatus, get_payment_ledger
    try:
        m = _mandates().get(mandate_id)
        if m is None or m.user_id != str(user_id) or m.kind != CHECKOUT_KIND:
            return _fail('checkout refused: mandate not found')
        if m.status == 'consumed':
            return _out(retry_order(m))
        payment = get_payment_ledger().get_payment(m.payment_id)
        if m.status == 'approved' and payment is not None \
                and payment.status == PaymentStatus.COMPLETED:
            # Paid, and its order step never finished (the gateway callback
            # failed midway): it runs now, and never charges again.
            return _out(complete_redirect_checkout(m.payment_id))
        return _out(settle_checkout(m))
    except MandateKeyError as e:
        # The node's payment pause.  The raise site cannot know whether the
        # money moved; this catcher can, from the payment's own record: a
        # paid shopper is told the order is on hold (never "nothing was
        # charged"), an unpaid one hears the pause.  Either way nothing was
        # read or changed and the approval stays as it is.
        from integrations.ap2.ap2_protocol import money_taken
        payment = _payment_for_mandate(mandate_id)
        if payment is not None and (payment.metadata or {}).get('user_id') != str(user_id):
            return _fail('checkout refused: mandate not found')
        if money_taken(payment):
            order_on_hold(payment, f'this node cannot sign its records right now ({e})')
            return _fail(_PAUSED_AFTER_PAID, needs_attention=True,
                         payment_id=payment.payment_id, mandate_id=mandate_id)
        logger.error(f'commerce: checkout {mandate_id} not run: {e}')
        return _fail(str(e))


def _nothing_charged(user_id, message: str) -> None:
    push_fragment(user_id, {'type': 'notification', 'severity': 'warning',
                            'title': 'Payment not taken', 'message': message})


HOLD_TITLE = 'Payment received, order on hold'
#: The shopper's sentence ends with a promise only when the owner card was
#: published (order_on_hold); otherwise it names the next step.
_HOLD_FOLLOW_UP = 'we will sort it out with you.'
_HOLD_CONTACT = 'reply here or contact the shop.'
#: What a paid shopper hears when this node cannot sign its records: not the
#: pause's "nothing was charged", which is false once the money moved.
_PAUSED_AFTER_PAID = ('the payment went through, but this node cannot place the '
                      'order right now (its payment key cannot be read); the order '
                      'is on hold and commerce_checkout places it once the key reads')


def _rupees(amount) -> str:
    return f'₹{_amount(amount):.2f}'


def _tell_owner(payment, user_id, what: str, why: str) -> bool:
    """The owner card half of order_on_hold: this computer's owner (the same
    identity every other ask of this machine goes to: ai_key_vault,
    vlm.safety) hears what a person must do.  True only when the card was
    published; the shopper's sentence promises follow-up only then."""
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    if not owner:
        logger.error(f'held payment {payment.payment_id}: nobody was told, '
                     f'HEVOLVE_OWNER_USER_ID is not set')
        return False
    told = push_fragment(owner, {
        'type': 'notification', 'severity': 'warning',
        'title': 'A held payment needs a person',
        'message': (f'{what} Payment {payment.payment_id} from shopper {user_id}: '
                    f'{why}. Place the order, or refund it from your PhonePe '
                    f'merchant account; nothing is refunded automatically.')})
    if not told:
        logger.error(f'held payment {payment.payment_id}: nobody was told, the '
                     f'owner card was not published')
    return told


def order_on_hold(payment, why: str, detail: str = '', received=None) -> Dict[str, Any]:
    """The ONE rule for every held payment (money taken, order not placed),
    whatever the cause: the shopper's notice is built from the payment's own
    record (no mandate store, so a node that cannot sign still tells them)
    with the real amounts (``received``, in INR, when the gateway said what it
    took), and this computer's owner gets a card naming the payment and what
    a person must do.  The sentence promises follow-up only when that card
    was published; otherwise it names the next step.  Nothing is refunded
    automatically, and the sentence never says so."""
    from integrations.ap2.ap2_protocol import PaymentStatus
    user_id = (payment.metadata or {}).get('user_id')
    order_amount = _rupees(payment.amount)
    if received is not None:
        what = (f'We received {_rupees(received)} but your order comes to '
                f'{order_amount}, so your order is on hold.')
    elif payment.status == PaymentStatus.FAILED:
        # Money taken on a FAILED payment is a capture of another amount
        # (ap2_protocol.money_taken); this caller did not learn which.
        what = (f"We received a different amount from your order's "
                f'{order_amount}, so your order is on hold.')
    else:
        what = (f'Your payment of {order_amount} arrived, but your order is '
                f'not placed yet, so it is on hold.')
    logger.error(f'MANUAL RECONCILIATION NEEDED: payment {payment.payment_id} '
                 f'held ({why}); {what}')
    owner_told = _tell_owner(payment, user_id, what, why)
    message = ' '.join(filter(None, [
        what, detail, 'Nothing more will be charged;',
        _HOLD_FOLLOW_UP if owner_told else _HOLD_CONTACT]))
    if user_id:
        push_fragment(user_id, {'type': 'notification', 'severity': 'warning',
                                'title': HOLD_TITLE, 'message': message})
    else:
        logger.error(f'held payment {payment.payment_id}: the shopper could not '
                     f'be told, its record names no user')
    return {'held': True, 'owner_told': owner_told, 'message': message}


def hold_paid_order(payment_id: str, why: str, received=None) -> Dict[str, Any]:
    """A gateway took money for a checkout that cannot become an order as it
    stands (PhonePe captured a different amount: ``received`` in INR when the
    callback has it).  The mandate is kept as it is, so its cart is not
    offered for payment again; the shopper and the owner are told through
    order_on_hold, which needs no store."""
    from integrations.ap2.ap2_protocol import get_payment_ledger
    payment = get_payment_ledger().get_payment(payment_id)
    if payment is None:
        logger.error(f'MANUAL RECONCILIATION NEEDED: payment {payment_id} ({why}) '
                     f'is not in the ledger; nobody was told')
        return {'held': False, 'owner_told': False}
    return order_on_hold(payment, why, received=received)


def _payment_for_mandate(mandate_id: str):
    """The ledger payment minted for ``mandate_id``, found by the payment's
    own record (its metadata names the mandate), so a node that cannot read
    its mandate store can still see whether a shopper's money moved."""
    from integrations.ap2.ap2_protocol import get_payment_ledger
    for p in list(get_payment_ledger().payments.values()):
        if (p.metadata or {}).get('mandate_id') == mandate_id:
            return p
    return None


def settle_checkout(m) -> Dict[str, Any]:
    """Settle an APPROVED McGroce checkout mandate: re-check the live cart
    against what the shopper approved, take the money, place the order.

    The ap2 settler for CHECKOUT_KIND -- what an approval runs
    (ap2_mandate.decide_payment) and what commerce_checkout retries.
    """
    user_id = m.user_id
    client = _client()
    res = client.cart_get(user_id)
    if not res['success']:
        return {'success': False, 'error': res['error']}
    view = cart_view(res['data'])
    store = _mandates()
    ok, reason = store.verify_for_checkout(m.mandate_id, user_id, mandate_cart(view))
    if not ok:
        if 'cart changed' in reason:
            # Nothing was charged: the approval is void, so the payment must
            # not stay AUTHORIZED for anything else to take.
            store.withdraw(m.mandate_id, 'cart changed after approval')
            push_fragment(user_id, {
                'type': 'notification', 'severity': 'warning',
                'title': 'Your cart changed',
                'message': 'Your cart is different from the one you approved, '
                           'so nothing was charged. Review it and approve again.'})
        return {'success': False, 'error': f'checkout refused: {reason}'}
    from integrations.ap2.ap2_protocol import PaymentStatus, get_payment_ledger
    ledger = get_payment_ledger()
    payment = ledger.get_payment(m.payment_id)
    if payment is not None and mock_would_settle(payment.gateway):
        # The money gate itself, whatever created the mandate (an older
        # build, keys removed after the approval): nothing can be charged, so
        # the approval is void and the payment must not stay AUTHORIZED.
        store.withdraw(m.mandate_id, NO_LIVE_GATEWAY_REASON)
        refuse_no_live_gateway('commerce', user_id, m.payment_id)
        from integrations.ap2.ap2_mandate import no_live_gateway_card
        push_fragment(user_id, no_live_gateway_card(
            'This store cannot take payments right now, so nothing was '
            'charged and no order was placed.'))
        return {'success': False, 'error': f'checkout refused: {_NO_LIVE_GATEWAY}',
                'payment_id': m.payment_id}
    if m.cart_ref is None:
        # A card an older version showed, which recorded no cart: no order
        # could be signed for it, so it must not take the money.
        store.withdraw(m.mandate_id, 'approved before carts were recorded')
        _nothing_charged(user_id, 'This approval came from an older version of the '
                                  'store, so nothing was charged. Approve the cart again.')
        return {'success': False,
                'error': 'checkout refused: this approval names no cart; nothing was charged',
                'payment_id': m.payment_id}
    if str(view.get('order_id')) != m.cart_ref:
        # Same contents, another McGroce cart: the approval named that cart.
        store.withdraw(m.mandate_id, 'not the cart that was approved')
        _nothing_charged(user_id, 'This is not the cart you approved, so nothing '
                                  'was charged. Review it and approve again.')
        return {'success': False,
                'error': 'checkout refused: not the cart that was approved'}
    # Everything the order step needs is checked BEFORE the money moves: once
    # it is taken, any of these would leave a paid cart with no order.
    blocked = _why_no_order(client, user_id, view)
    if blocked:
        store.withdraw(m.mandate_id, blocked)
        _nothing_charged(user_id, 'Your agent cannot place this order right now, '
                                  'so nothing was charged.')
        return {'success': False,
                'error': f'checkout refused: {blocked}; nothing was charged',
                'payment_id': m.payment_id}
    from integrations.ap2.ap2_mandate import ALREADY_SETTLING
    claimed, why = store.begin_settle(m.mandate_id)
    if not claimed and why == ALREADY_SETTLING:
        # This same mandate is being paid right now (a retry racing its own
        # approval): that settle owns it, so it is left alone.
        return {'success': False, 'error': f'checkout refused: {why}',
                'payment_id': m.payment_id}
    if not claimed:
        # Another approval for this cart is paying, or already paid: this one
        # must never charge a second time.
        store.withdraw(m.mandate_id, why)
        _nothing_charged(user_id, 'This cart already has a payment, so nothing '
                                  'more was charged.')
        return {'success': False,
                'error': f'checkout refused: {why}; nothing was charged',
                'payment_id': m.payment_id}
    try:
        paid = ledger.process_payment(m.payment_id, settler=True)
        payment = ledger.get_payment(m.payment_id)
        method = payment.gateway.value if payment and payment.gateway else None
        if paid.get('status') == 'redirect_required':
            push_fragment(user_id, {'type': 'payment_status', 'status': 'processing',
                                    'amount': float(m.amount), 'currency': CURRENCY,
                                    'method': method,
                                    'transaction_id': paid.get('transaction_id'),
                                    'redirect_url': paid.get('redirect_url')})
            return {'success': True, 'status': 'awaiting_payment',
                    'redirect_url': paid.get('redirect_url'),
                    'payment_id': m.payment_id}
        if not paid.get('success'):
            if payment is not None and payment.status == PaymentStatus.FAILED:
                push_fragment(user_id, {'type': 'payment_status', 'status': 'failed',
                                        'amount': float(m.amount), 'currency': CURRENCY,
                                        'method': method})
            return {'success': False,
                    'error': f"payment failed: {paid.get('error', 'declined')}",
                    'payment_id': m.payment_id}
        return _place_order(client, user_id, m, view, payment)
    finally:
        store.end_settle(m.mandate_id)


def _why_no_order(client, user_id, view) -> Optional[str]:
    """Why an agent payment for this cart could not become a McGroce order
    (None when it can).  Asked before the shopper is asked to approve and
    again right before the money is taken."""
    from core.config_cache import get_secret
    from integrations.commerce.bindings import is_merchant
    if not get_secret('COMMERCE_SESSION_SECRET', ''):
        logger.warning('commerce: agent checkout refused, COMMERCE_SESSION_SECRET '
                       'is not set, so no order could be signed for McGroce')
        return 'this store is not set up to take agent payments yet'
    binding = client.bindings.get(user_id)
    if not binding or is_merchant(binding):
        return 'this account is not linked to a McGroce shopper'
    if view.get('order_id') is None:
        return 'McGroce did not name the cart'
    if any(i['sku_id'] is None for i in view['items']):
        return 'a cart line has no product variant McGroce can check'
    return client.agent_orders_ready(user_id)


def mandate_attestation_for(client, m, payment, view) -> Optional[str]:
    """The signed X-Commerce-Mandate for a CONSUMED mandate, or None.

    McGroce places an AP2 order only on this: HARTOS is the one payer, and
    the attestation is its signed word that it took exactly ``m.amount`` for
    this McGroce customer's cart ``oid`` holding exactly ``lines``, and used
    the mandate up.  McGroce compares all of it with the cart it reprices,
    so neither a swapped item nor another cart of the same total passes.
    The customer comes from the binding McGroce itself asserted at the
    session exchange, never from the model.  None when the node has no
    COMMERCE_SESSION_SECRET, the shopper is no longer linked, the mandate is
    not consumed, or a cart line has no SKU to name.
    """
    from core.config_cache import get_secret
    from integrations.commerce.bindings import is_merchant
    from integrations.commerce.signing import ATTESTATION_TTL_S, mandate_attestation
    secret = get_secret('COMMERCE_SESSION_SECRET', '')
    binding = client.bindings.get(m.user_id)
    if not secret or not binding or is_merchant(binding) or m.status != 'consumed':
        return None
    # Only what the ledger shows was taken, for a record nobody edited.
    if not _mandates().paid_as_signed(m, payment):
        logger.error(f'MANUAL RECONCILIATION NEEDED: mandate {m.mandate_id} does not '
                     f'match its signature or what payment {m.payment_id} took; '
                     f'no order signed')
        return None
    # The cart named when the mandate was created and signed, and no other.
    if m.cart_ref is None or str(view.get('order_id')) != m.cart_ref:
        return None
    if any(i['sku_id'] is None for i in view['items']):
        return None
    now = int(time.time())
    return mandate_attestation(secret, {
        'v': 1, 'mid': m.mandate_id, 'pid': m.payment_id,
        'cid': str(binding['customer_id']), 'oid': m.cart_ref,
        'lines': sorted([str(i['sku_id']), int(i['quantity']), f"{i['price']:.2f}"]
                        for i in view['items']),
        'amt': str(m.amount), 'cur': str(m.currency).upper(), 'st': 'consumed',
        'iat': now, 'exp': now + ATTESTATION_TTL_S,
        'gw': payment.gateway.value if payment and payment.gateway else None,
        'txn': payment.gateway_transaction_id if payment else None})


def _place_order(client, user_id, m, view, payment) -> Dict[str, Any]:
    """After the money is taken: consume the mandate, then have McGroce
    place the order (_submit_order)."""
    failed = _consume_paid(user_id, m, payment)
    if failed:
        return failed
    m = _mandates().get(m.mandate_id) or m
    return _submit_order(client, user_id, m, view, payment)


def _consume_paid(user_id, m, payment) -> Optional[Dict[str, Any]]:
    """The money for ``m`` is taken: use the mandate up and tell the
    shopper.  None when done, else the answer to return.  From here on the
    order step is retry_order's, which never charges again."""
    if not _mandates().consume(m.mandate_id):
        current = _mandates().get(m.mandate_id)
        if current is not None and current.status == 'consumed':
            # Another settle of this mandate consumed it and places its order;
            # this one must not place a second.
            return {'success': False, 'error': 'this checkout is already being completed',
                    'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
        if current is not None and current.status == 'approved':
            # consume refuses an approved mandate only when its record fails
            # its signature: edited outside the store, or signed with a key
            # this node no longer has.  No order can be signed for it.
            cause = "this checkout's record does not match its signature"
        elif current is None:
            cause = "this checkout's record is gone"
        else:
            cause = f'this checkout was {current.status} meanwhile'
        order_on_hold(payment, f'mandate {m.mandate_id} cannot be used: {cause}')
        return {'success': False, 'needs_attention': True,
                'error': (f'the payment went through, but {cause}, so no order can '
                          f'be placed for it; a person has to place it or refund it'),
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
    method = payment.gateway.value if payment and payment.gateway else None
    txn = payment.gateway_transaction_id if payment else None
    push_fragment(user_id, {'type': 'payment_status', 'status': 'completed',
                            'amount': float(m.amount), 'currency': CURRENCY,
                            'method': method, 'transaction_id': txn})
    return None


def retry_order(m, claimed: bool = False) -> Dict[str, Any]:
    """The order step again, for a CONSUMED mandate: its payment went
    through, its order did not (or the answer was lost).  Never charges.

    McGroce answers a mandate it already turned into an order with that
    order, so a retry after a lost answer finds it instead of placing a
    second one.  The live cart must still be the one the shopper approved.
    One order step per mandate at a time (MandateStore.begin_settle): a
    retry racing the gateway callback is told it is in progress.
    ``claimed``: the caller already holds that claim.
    """
    import hmac
    from integrations.ap2.ap2_mandate import MandateError, canonical_cart_hash
    from integrations.ap2.ap2_protocol import PaymentStatus, get_payment_ledger
    if m.order_ref:
        return _ordered(m)
    if m.cart_ref is None:
        # Consumed by an older build, which kept no order_ref and no cart:
        # its order may well exist, and nothing says which cart it paid for.
        return {'success': False, 'needs_attention': True,
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id,
                'error': ('this payment was handled by an older version; check the '
                          "shopper's orders, or a person reconciles it")}
    payment = get_payment_ledger().get_payment(m.payment_id)
    if payment is None or payment.status != PaymentStatus.COMPLETED:
        return {'success': False, 'error': 'checkout refused: mandate is consumed'}
    store = _mandates()
    if not claimed:
        ok, why = store.begin_settle(m.mandate_id)
        if not ok:
            return {'success': False, 'in_progress': True,
                    'error': f'this checkout is already being completed ({why})',
                    'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
    try:
        m = store.get(m.mandate_id) or m
        if m.order_ref:                 # placed while this one waited
            return _ordered(m)
        client = _client()
        res = client.cart_get(m.user_id)
        if not res['success']:
            return {'success': False, 'error': res['error'], 'needs_attention': True,
                    'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
        view = cart_view(res['data'])
        try:
            same = (str(view.get('order_id')) == m.cart_ref and hmac.compare_digest(
                canonical_cart_hash(mandate_cart(view)), m.cart_hash))
        except MandateError:
            same = False
        if not same:
            return {'success': False, 'needs_attention': True, 'cart_changed': True,
                    'payment_id': m.payment_id, 'mandate_id': m.mandate_id,
                    'error': ('the payment went through, but the cart no longer holds '
                              'what was paid for; put it back as it was approved and '
                              'call commerce_checkout with this mandate_id, which '
                              'places the order with that payment and never charges '
                              'again, or a person sorts it out')}
        return _submit_order(client, m.user_id, m, view, payment)
    finally:
        if not claimed:
            store.end_settle(m.mandate_id)


def _ordered(m) -> Dict[str, Any]:
    return {'success': True, 'status': 'ordered', 'order_id': m.order_ref,
            'payment_id': m.payment_id, 'amount': m.amount, 'currency': m.currency}


def _submit_order(client, user_id, m, view, payment) -> Dict[str, Any]:
    """Ask McGroce to place the order for consumed ``m`` with the signed
    attestation (mandate_attestation_for).  McGroce checks it itself and
    turns each mandate into one order at most.  One immediate retry when
    the answer never came (transport error or 5xx): McGroce's single-use
    check makes that safe."""
    attestation = mandate_attestation_for(client, m, payment, view)
    if not attestation:
        placed = {'success': False,
                  'error': 'the order could not be signed for McGroce (no '
                           'COMMERCE_SESSION_SECRET, the shopper is no longer '
                           'linked, the cart is not the one paid for, a cart '
                           'line has no SKU, or the record does not match what '
                           'was paid)'}
    else:
        placed = client.complete_ap2_checkout(user_id, attestation)
        if not placed['success'] and (placed.get('status') or 500) >= 500:
            placed = client.complete_ap2_checkout(user_id, attestation)
    if not placed['success']:
        logger.error(f'commerce: paid {m.payment_id} but McGroce did not '
                     f'place the order: {placed["error"]}')
        return {'success': False,
                'error': ('the payment went through but McGroce did not place '
                          f'the order ({placed["error"]}); retry with '
                          'commerce_checkout and this mandate_id, which never '
                          'charges again, or a person reconciles it'),
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id,
                'needs_attention': True}
    order = placed['data'] or {}
    order_id = order.get('orderNumber') or order.get('id')
    status = order.get('status') or 'SUBMITTED'
    if order_id:
        _mandates().record_order(m.mandate_id, order_id)
    push_fragment(user_id, _tracking_fragment(order_id, status))
    return {'success': True, 'status': 'ordered', 'order_id': order_id,
            'order_status': status, 'payment_id': m.payment_id,
            'amount': m.amount, 'currency': m.currency}


def complete_redirect_checkout(payment_id: str) -> Dict[str, Any]:
    """_complete_redirect_checkout, with the node's payment pause answered
    as a HOLD: the money is taken, so a MandateKeyError here never leaves
    the shopper untold or the callback raising.  The shopper and the owner
    are told from the payment's own record, and commerce_checkout places the
    order once the key reads."""
    from integrations.ap2.ap2_mandate import MandateKeyError
    from integrations.ap2.ap2_protocol import get_payment_ledger
    try:
        return _complete_redirect_checkout(payment_id)
    except MandateKeyError as e:
        payment = get_payment_ledger().get_payment(payment_id)
        if payment is None:
            raise
        order_on_hold(payment, f'this node cannot sign its records right now ({e})')
        return {'success': False, 'needs_attention': True, 'held': True,
                'payment_id': payment_id, 'error': _PAUSED_AFTER_PAID}


def _complete_redirect_checkout(payment_id: str) -> Dict[str, Any]:
    """Place the McGroce order for a redirect-gateway (PhonePe) payment the
    gateway callback has just confirmed COMPLETED.

    The money is taken, so the mandate is used up first, whatever becomes of
    the order: the order step is then retry_order's, which places it only
    for the cart the approval named, still holding what was approved.  When
    it cannot (McGroce down, the cart changed) the order is held, the
    shopper is told, and commerce_checkout retries it later without a new
    charge.  That cart is not offered for payment again meanwhile, and since
    McGroce keeps one open cart per shopper until an order is placed, that
    is the shopper's live cart.  commerce_checkout also runs this for an
    approved mandate whose payment completed but whose order step never ran.
    """
    from integrations.ap2.ap2_protocol import PaymentStatus, get_payment_ledger
    m = _mandates().find_by_payment_id(payment_id)
    if m is None or m.status != 'approved':
        return {'success': False, 'error': 'no approved mandate for this payment'}
    payment = get_payment_ledger().get_payment(payment_id)
    if payment is None or payment.status != PaymentStatus.COMPLETED:
        return {'success': False, 'error': 'payment is not completed'}
    from integrations.ap2.ap2_mandate import ALREADY_SETTLING
    store = _mandates()
    # Claimed before the mandate is used up: the callback and a retry of it
    # never both consume it and then both leave the order to the other.
    claimed, why = store.begin_settle(m.mandate_id)
    if not claimed and why == ALREADY_SETTLING:
        return {'success': False, 'in_progress': True,
                'error': 'this checkout is already being completed',
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
    if not claimed:
        order_on_hold(payment, f'mandate {m.mandate_id} cannot be ordered: {why}')
        return {'success': False, 'needs_attention': True,
                'error': f'the payment went through, but {why}; a person has to sort it out',
                'payment_id': m.payment_id, 'mandate_id': m.mandate_id}
    try:
        failed = _consume_paid(m.user_id, m, payment)
        if failed:
            return failed
        if m.cart_ref is None:
            # Approved under an older version, which recorded no cart: no
            # order can be signed for it, and none was placed (this is its
            # order step).
            order_on_hold(payment, f'mandate {m.mandate_id} was approved under an '
                                   f'older version, which named no cart')
            return {'success': False, 'needs_attention': True,
                    'payment_id': m.payment_id, 'mandate_id': m.mandate_id,
                    'error': ('the payment went through for a checkout approved under '
                              'an older version, which named no cart; a person has to '
                              'place the order or refund it')}
        placed = retry_order(store.get(m.mandate_id) or m, claimed=True)
    finally:
        store.end_settle(m.mandate_id)
    if not placed['success']:
        order_on_hold(payment, placed['error'], detail=(
            'Your cart changed while you were paying: put the cart back the way '
            'you approved it and ask your agent to check out, and it is placed '
            'with this payment, with no new charge.')
            if placed.get('cart_changed') else '')
    return placed


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
    from integrations.commerce.bindings import get_bindings, is_merchant
    binding = get_bindings().get(user_id)
    if not is_merchant(binding):
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

# Every commerce tool, by name.  Its functions take user_id explicitly, so it
# is never handed to a caller who is not that shopper: the MCP bridge
# (integrations/mcp/mcp_http_bridge.py _load_tools) leaves it out, because
# an MCP caller is the node's one token and could name any shopper.
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


# An approval answered anywhere in the process settles a McGroce checkout
# through this module (ap2_mandate.decide_payment -> settle).
from integrations.ap2.ap2_mandate import register_settler  # noqa: E402
register_settler(CHECKOUT_KIND, settle_checkout)
