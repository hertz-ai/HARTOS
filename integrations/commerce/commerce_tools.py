"""
McGroce commerce tools: what a shopping or merchant agent can DO.

Each tool is written once, as a plain function whose first parameter is the
``user_id`` it acts for.  Two surfaces consume the same functions:

  * autogen (CREATE/REUSE Tier-2, tag 'commerce'): ``register_commerce_tools``
    binds ``user_id`` from the /chat turn, so the model never chooses whose
    cart it touches.
  * the local MCP bridge: ``COMMERCE_TOOLS`` (ServiceToolRegistry shape),
    where the auth-gated MCP caller names the user.

Money never moves on a tool call.  ``checkout`` creates the McGroce order and
an AP2 payment carrying a mandate, then shows the shopper an approval card.
Only the shopper's answer, arriving at /api/agent/approval with their token,
authorizes it (integrations.ap2.ap2_mandate.decide_payment).  The order is
confirmed with McGroce from the ``mcgroce_order`` payment hook below.
Merchant onboarding is gated the same way (``merchant_onboard:<id>``).

Every tool returns JSON and pushes the matching card (bindings builders).
"""
import functools
import inspect
import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Dict, List, Optional, Tuple

from integrations.commerce import bindings
from integrations.commerce.mcgroce_client import McGroceClient, McGroceError

logger = logging.getLogger('hevolve.commerce')

ORDER_PAYMENT_KIND = 'mcgroce_order'
MERCHANT_ACTION_PREFIX = 'merchant_onboard:'
MAX_PRODUCT_CARDS = 3

_onboarding = bindings.JsonState('merchant_onboarding')


def _client() -> McGroceClient:
    return McGroceClient()


def _ok(**payload) -> str:
    return json.dumps({'success': True, **payload}, default=str)


def _fail(user_id: Optional[str], error: str, title: str = 'Shopping') -> str:
    bindings.push_ui(user_id, bindings.notification_card(title, error, 'error'))
    return json.dumps({'success': False, 'error': error})


# ─── Response normalisation (McGroce JSON -> the shapes the cards take) ─

def _as_list(data, *keys) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [d for d in data[k] if isinstance(d, dict)]
    return []


def _cart(data) -> Dict[str, Any]:
    items = _as_list(data, 'items', 'lines', 'cartItems')
    total = data.get('total') if isinstance(data, dict) else None
    if total is None:
        total = sum(float(i.get('price') or 0) * int(i.get('quantity') or 1)
                    for i in items)
    return {'items': items, 'total': total}


def _order(data) -> Dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    return {
        'order_id': data.get('order_id') or data.get('orderId') or data.get('id'),
        'total': data.get('total') or data.get('amount'),
        'items': _as_list(data, 'items', 'lines'),
        'status': data.get('status') or 'created',
        'store_id': data.get('store_id') or data.get('storeId'),
        'steps': data.get('steps') or [],
        'eta': data.get('eta'),
    }


# ─── Shopper tools ─────────────────────────────────────────────────

def search_products(user_id: str,
                    query: Annotated[str, "What the shopper is looking for"],
                    store_id: Annotated[str, "Optional McGroce store id"] = '') -> str:
    """Search the McGroce catalogue and show the best matches as product cards."""
    try:
        products = _as_list(_client().search_products(query, store_id or None),
                            'products', 'items', 'results')
    except McGroceError as e:
        return _fail(user_id, f'Product search failed: {e}')
    for p in products[:MAX_PRODUCT_CARDS]:
        bindings.push_ui(user_id, bindings.product_card(p))
    return _ok(query=query, count=len(products), products=products[:20])


def find_stores(user_id: str,
                zipcode: Annotated[str, "Postal code to search near"] = '',
                lat: Annotated[str, "Latitude, if no zipcode"] = '',
                lng: Annotated[str, "Longitude, if no zipcode"] = '') -> str:
    """Find McGroce stores near the shopper."""
    try:
        stores = _as_list(_client().find_stores(zipcode, lat or None, lng or None),
                          'stores', 'items')
    except McGroceError as e:
        return _fail(user_id, f'Store search failed: {e}')
    bindings.push_ui(user_id, bindings.list_card('Nearby stores', [{
        'id': s.get('id'), 'title': s.get('name'),
        'subtitle': s.get('address'), 'distance': s.get('distanceFromMe'),
    } for s in stores[:10]]))
    return _ok(count=len(stores), stores=stores[:20])


def view_cart(user_id: str) -> str:
    """Show the shopper's current McGroce cart."""
    try:
        cart = _cart(_client().get_cart(user_id))
    except McGroceError as e:
        return _fail(user_id, f'Could not load the cart: {e}')
    bindings.push_ui(user_id, bindings.cart_card(cart))
    return _ok(cart=cart, currency=bindings.CURRENCY)


def add_to_cart(user_id: str,
                product_id: Annotated[str, "McGroce product id"],
                quantity: Annotated[int, "How many"] = 1,
                store_id: Annotated[str, "Optional McGroce store id"] = '') -> str:
    """Add a product to the shopper's McGroce cart."""
    try:
        cart = _cart(_client().add_to_cart(user_id, product_id, quantity,
                                           store_id or None))
    except McGroceError as e:
        return _fail(user_id, f'Could not add to cart: {e}')
    bindings.push_ui(user_id, bindings.cart_card(cart))
    return _ok(cart=cart, currency=bindings.CURRENCY)


def remove_from_cart(user_id: str,
                     product_id: Annotated[str, "McGroce product id"]) -> str:
    """Remove a product from the shopper's McGroce cart."""
    try:
        cart = _cart(_client().remove_from_cart(user_id, product_id))
    except McGroceError as e:
        return _fail(user_id, f'Could not remove from cart: {e}')
    bindings.push_ui(user_id, bindings.cart_card(cart))
    return _ok(cart=cart, currency=bindings.CURRENCY)


def _payment_gateway(ledger):
    """The AP2 gateway commerce charges through: COMMERCE_PAYMENT_GATEWAY if
    that gateway is registered on the ledger, else the mock gateway."""
    from integrations.ap2.ap2_protocol import PaymentGateway
    wanted = bindings._secret('COMMERCE_PAYMENT_GATEWAY', 'mock').lower()
    try:
        gw = PaymentGateway(wanted)
    except ValueError:
        gw = PaymentGateway.MOCK
    return gw if gw in ledger.gateways else PaymentGateway.MOCK


def checkout(user_id: str,
             store_id: Annotated[str, "Optional McGroce store id"] = '') -> str:
    """Place the cart as an order and ask the shopper to approve payment.

    The shopper approves; the agent cannot.  Returns approval_required.
    """
    from integrations.ap2.ap2_mandate import (
        MANDATE_KEY, build_mandate, request_human_approval)
    from integrations.ap2.ap2_protocol import payment_ledger
    try:
        order = _order(_client().create_order(user_id, store_id or None))
    except McGroceError as e:
        return _fail(user_id, f'Checkout failed: {e}')
    try:
        total = Decimal(str(order['total']))
    except (InvalidOperation, TypeError):
        total = Decimal('0')
    if not order['order_id'] or total <= 0:
        return _fail(user_id, 'McGroce returned no payable order')
    items = [{'product_id': i.get('product_id') or i.get('id'),
              'name': i.get('name'), 'quantity': i.get('quantity', 1),
              'price': i.get('price')} for i in order['items']]
    description = f"McGroce order {order['order_id']}"
    mandate = build_mandate(user_id, total, bindings.CURRENCY, description,
                            kind=ORDER_PAYMENT_KIND, items=items,
                            merchant=order['store_id'] or store_id or None,
                            refs={'order_id': order['order_id']})
    payment = payment_ledger.create_payment_request(
        amount=total, currency=bindings.CURRENCY, description=description,
        requester_agent_id=bindings.COMMERCE_AGENT_ID,
        gateway=_payment_gateway(payment_ledger),
        metadata={MANDATE_KEY: mandate, 'user_id': user_id,
                  'kind': ORDER_PAYMENT_KIND, 'order_id': order['order_id']})
    bindings.push_ui(user_id, bindings.checkout_card(
        dict(order, items=items), payment.payment_id))
    approval = request_human_approval(payment_ledger, payment.payment_id)
    return _ok(order_id=order['order_id'], payment_id=payment.payment_id,
               total=str(total), currency=bindings.CURRENCY,
               status='approval_required',
               message='The shopper must approve this payment; wait for it.',
               approval_card_shown=approval.get('approval_card_shown'))


def track_order(user_id: str,
                order_id: Annotated[str, "McGroce order id"]) -> str:
    """Show where a McGroce order is."""
    try:
        order = _order(_client().get_order(user_id, order_id))
    except McGroceError as e:
        return _fail(user_id, f'Could not find order {order_id}: {e}')
    order['order_id'] = order['order_id'] or order_id
    bindings.push_ui(user_id, bindings.order_tracking_card(order))
    return _ok(order=order)


def _on_order_payment(payment, outcome: str) -> Dict[str, Any]:
    """AP2 hook: the shopper has decided an order's payment."""
    meta = payment.metadata or {}
    user_id, order_id = meta.get('user_id'), meta.get('order_id')
    client = _client()
    try:
        if outcome == 'completed':
            order = _order(client.confirm_order(
                user_id, order_id,
                payment.gateway_transaction_id or payment.payment_id))
            order['order_id'] = order['order_id'] or order_id
            order['status'] = order['status'] if order['status'] != 'created' \
                else 'confirmed'
            bindings.push_ui(user_id, bindings.order_tracking_card(order))
            return {'order_id': order_id, 'order_status': order['status']}
        client.cancel_order(user_id, order_id,
                            'payment denied' if outcome == 'denied'
                            else 'payment failed')
        bindings.push_ui(user_id, bindings.notification_card(
            'Order cancelled', f'Order {order_id} was not paid, so it was '
            f'cancelled.', 'warning'))
        return {'order_id': order_id, 'order_status': 'cancelled'}
    except McGroceError as e:
        logger.error("mcgroce order %s after payment %s: %s", order_id,
                     outcome, e)
        bindings.push_ui(user_id, bindings.notification_card(
            'Order needs attention', f'Payment {outcome}, but McGroce could '
            f'not update order {order_id}: {e}', 'error'))
        return {'order_id': order_id, 'error': str(e)}


# ─── Merchant tools ────────────────────────────────────────────────

def merchant_action(request_id: str) -> str:
    return f'{MERCHANT_ACTION_PREFIX}{request_id}'


def parse_merchant_action(action: str) -> Optional[str]:
    action = str(action or '').strip()
    if not action.lower().startswith(MERCHANT_ACTION_PREFIX):
        return None
    return action[len(MERCHANT_ACTION_PREFIX):].strip() or None


def request_merchant_onboarding(
        user_id: str,
        store_name: Annotated[str, "The store's trading name"],
        address: Annotated[str, "Street address"],
        phone: Annotated[str, "Store phone number"],
        zipcode: Annotated[str, "Postal code"] = '',
        gstin: Annotated[str, "GSTIN, if registered"] = '') -> str:
    """Prepare a McGroce merchant listing and ask the merchant to confirm it.

    Nothing is submitted to McGroce until the merchant approves.
    """
    if not (store_name and address and phone):
        return _fail(user_id, 'store_name, address and phone are required',
                     'Merchant onboarding')
    request_id = uuid.uuid4().hex[:16]
    details = {'storeName': store_name, 'address': address, 'phone': phone,
               'zipcode': zipcode, 'gstin': gstin}
    _onboarding.put(request_id, {
        'request_id': request_id, 'user_id': user_id, 'details': details,
        'status': 'pending', 'created_at': datetime.now().isoformat()})
    bindings.push_ui(user_id, bindings.approval_card(
        merchant_action(request_id),
        f'List "{store_name}" ({address}) on McGroce?'))
    return _ok(request_id=request_id, status='approval_required',
               message='The merchant must approve; wait for it.')


def decide_merchant_onboarding(request_id: str, approver_id: Optional[str],
                               approved: bool) -> Tuple[Dict[str, Any], int]:
    """Apply the merchant's answer.  ``approver_id`` comes from their token."""
    if not approver_id:
        return {'status': 'error', 'reason': 'authentication required'}, 401
    record = _onboarding.get(request_id)
    if record is None:
        return {'status': 'error', 'reason': 'onboarding request not found'}, 404
    if str(record.get('user_id')) != str(approver_id):
        return {'status': 'error', 'reason': 'not your onboarding request'}, 403
    if record.get('status') != 'pending':
        return {'status': 'error',
                'reason': f"request is {record.get('status')}"}, 409
    record['decided_at'] = datetime.now().isoformat()
    user_id = record['user_id']
    if not approved:
        record['status'] = 'denied'
        _onboarding.put(request_id, record)
        return {'status': 'denied', 'request_id': request_id}, 200
    try:
        merchant = _client().onboard_merchant(user_id, record['details'])
    except McGroceError as e:
        record['status'] = 'failed'
        record['error'] = str(e)
        _onboarding.put(request_id, record)
        bindings.push_ui(user_id, bindings.notification_card(
            'Merchant onboarding', f'McGroce refused the listing: {e}', 'error'))
        return {'status': 'failed', 'request_id': request_id,
                'reason': str(e)}, 502
    record['status'] = 'onboarded'
    record['merchant'] = merchant
    _onboarding.put(request_id, record)
    bindings.push_ui(user_id, bindings.notification_card(
        'Merchant onboarding',
        f"{record['details']['storeName']} is now listed on McGroce.",
        'success'))
    return {'status': 'approved', 'request_id': request_id,
            'merchant': merchant}, 200


# ─── Registration ──────────────────────────────────────────────────

_SHOPPER = [
    ('search_products', 'Search the McGroce grocery catalogue (prices in INR) and show product cards', search_products),
    ('find_stores', 'Find McGroce stores near a zipcode or lat/lng', find_stores),
    ('view_cart', "Show the shopper's McGroce cart", view_cart),
    ('add_to_cart', "Add a product to the shopper's McGroce cart", add_to_cart),
    ('remove_from_cart', "Remove a product from the shopper's McGroce cart", remove_from_cart),
    ('checkout', 'Place the cart as an order and ask the shopper to approve payment; agents cannot approve', checkout),
    ('track_order', 'Show the status of a McGroce order', track_order),
]
_MERCHANT = [
    ('request_merchant_onboarding', 'Prepare a McGroce store listing and ask the merchant to confirm it', request_merchant_onboarding),
]

#: ServiceToolRegistry shape, consumed by the MCP HTTP bridge.
COMMERCE_TOOLS = [
    {'name': n, 'description': d, 'func': f,
     'tags': ['commerce', 'mcgroce_merchant' if (n, d, f) in _MERCHANT
              else 'mcgroce_shopper']}
    for n, d, f in _SHOPPER + _MERCHANT
]


def bind_user(fn, user_id: str):
    """``fn`` with its leading ``user_id`` fixed, and a signature without it
    (autogen builds the tool schema from the signature)."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]

    @functools.wraps(fn)
    def bound(*args, **kwargs):
        return fn(user_id, *args, **kwargs)

    bound.__signature__ = sig.replace(parameters=params)
    bound.__annotations__ = {k: v for k, v in fn.__annotations__.items()
                             if k != 'user_id'}
    del bound.__wrapped__          # inspect must not unwrap back to user_id
    return bound


def register_commerce_tools(helper, assistant, user_id: str, executor=None):
    """Register the commerce tools for ``user_id`` (Tier 2, tag 'commerce').

    Same registration shape as revenue/news tools: schema on the Helper and
    the Assistant, execution on the Assistant and on the executor.
    """
    count = 0
    for name, desc, fn in _SHOPPER + _MERCHANT:
        func = bind_user(fn, str(user_id))
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)
        if executor is not None:
            executor.register_for_execution(name=name)(func)
        count += 1
    logger.info("Registered %d commerce tools for user %s", count, user_id)
    return count


# Order payments settle through this module's hook, registered on import so any
# process that can create a commerce payment can also settle it.
from integrations.ap2.ap2_mandate import register_payment_hook  # noqa: E402
register_payment_hook(ORDER_PAYMENT_KIND, _on_order_payment)
