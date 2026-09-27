"""
McGroce commerce bindings: configuration, UI components, the push, state.

Everything the commerce package shares lives here, once:

  * Configuration comes from core.config_cache.get_secret (env first, then the
    vault / config.json), never a second loader.
  * UI: commerce emits ONLY the component types the Android client already
    renders (Hevolve_React_Native components/AgentOverlay/AgentOverlayBridge.js),
    with the prop names its getComponentSummary reads, prices in INR.  The
    builders below are the only place those shapes are written; tools call
    them, they never hand-build a card dict.
  * Push: ``push_ui`` goes through liquid_ui_service.push_agent_ui, i.e. the
    LiquidUIService gates and, per user, ``publish_event('chat.social')``.
  * State: small JSON documents under core.platform_paths.get_agent_data_dir().
"""
import hashlib
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.commerce')

CURRENCY = 'INR'
COMMERCE_AGENT_ID = 'mcgroce_commerce'

#: The component types AgentOverlayBridge renders (PLAN §11).  Anything else is
#: refused by ``component`` so a commerce card can never reach the phone as the
#: generic JSON fallback.
COMMERCE_COMPONENT_TYPES = frozenset({
    'product_card', 'cart', 'checkout', 'payment_status', 'order_tracking',
    'approval', 'form', 'list', 'navigate', 'notification', 'progress',
    'agent_action',
})

#: Identity prefix for a McGroce shopper/merchant who has no HARTOS account.
#: Hyphen, not colon or underscore: it lands in file names (Windows) and in the
#: "{user_id}_{prompt_id}" key, whose FIRST underscore separates the user.
MCGROCE_IDENTITY_PREFIX = 'mcg-'
_SAFE_ID = re.compile(r'^[A-Za-z0-9-]{1,64}$')


# ─── Configuration ─────────────────────────────────────────────────

def _secret(name: str, default: str = '') -> str:
    from core.config_cache import get_secret
    return (get_secret(name, default) or default).strip()


def mcgroce_api_url() -> str:
    """Base URL of the McGroce site API (``MCGROCE_API_URL``), no trailing /."""
    return _secret('MCGROCE_API_URL').rstrip('/')


def commerce_session_secret() -> str:
    """Shared secret McGroce's SpaAgentTokenEndpoint sends as X-Commerce-Secret."""
    return _secret('COMMERCE_SESSION_SECRET')


def commerce_session_ttl() -> int:
    from core.config_cache import env_int
    return max(60, env_int('COMMERCE_SESSION_TTL', 3600))


def mcgroce_identity(customer_id) -> Optional[str]:
    """The HARTOS user id for a McGroce customer id: ``mcg-<id>``.

    An id that is not already [A-Za-z0-9-] (an email, a username with dots)
    is hashed, so the result is always safe as a file-name and topic segment
    and the same customer always maps to the same identity.
    """
    cid = str(customer_id or '').strip()
    if not cid:
        return None
    if not _SAFE_ID.match(cid):
        cid = hashlib.sha256(cid.encode('utf-8')).hexdigest()[:32]
    return f'{MCGROCE_IDENTITY_PREFIX}{cid}'


# ─── UI components (AgentOverlayBridge props) ──────────────────────

def component(comp_type: str, **props) -> Dict[str, Any]:
    """A commerce card.  Refuses a type the Android client cannot render."""
    if comp_type not in COMMERCE_COMPONENT_TYPES:
        raise ValueError(f'commerce cannot emit component type {comp_type!r}')
    return {'type': comp_type, **{k: v for k, v in props.items()
                                  if v is not None}}


def _money(value) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def product_card(product: Dict[str, Any]) -> Dict[str, Any]:
    pid = product.get('id') or product.get('product_id')
    return component(
        'product_card',
        name=product.get('name') or 'Product',
        price=_money(product.get('price')),
        currency=CURRENCY,
        image=product.get('image') or product.get('image_url') or product.get('url'),
        rating=product.get('rating'),
        description=product.get('description') or product.get('manu'),
        product_id=pid,
        buy_action={'tool': 'add_to_cart', 'product_id': pid} if pid else None,
    )


def cart_card(cart: Dict[str, Any]) -> Dict[str, Any]:
    items = [{
        'product_id': i.get('product_id') or i.get('id'),
        'name': i.get('name'),
        'quantity': i.get('quantity', 1),
        'price': _money(i.get('price')),
    } for i in (cart.get('items') or [])]
    return component('cart', items=items, total=_money(cart.get('total')),
                     currency=CURRENCY,
                     checkout_action={'tool': 'checkout'})


def checkout_card(order: Dict[str, Any], payment_id: str) -> Dict[str, Any]:
    return component('checkout', items=order.get('items') or [],
                     total=_money(order.get('total')), currency=CURRENCY,
                     order_id=order.get('order_id'), payment_id=payment_id,
                     payment_methods=order.get('payment_methods'))


def order_tracking_card(order: Dict[str, Any]) -> Dict[str, Any]:
    return component('order_tracking', order_id=str(order.get('order_id', '')),
                     status=order.get('status') or '',
                     steps=order.get('steps') or [], eta=order.get('eta'))


def list_card(title: str, items: List[Any]) -> Dict[str, Any]:
    return component('list', title=title, items=items)


def notification_card(title: str, message: str,
                      severity: str = 'info') -> Dict[str, Any]:
    return component('notification', title=title, message=message,
                     severity=severity)


def approval_card(action: str, description: str) -> Dict[str, Any]:
    return component('approval', agent_id=COMMERCE_AGENT_ID, action=action,
                     description=description, options=['Approve', 'Deny'])


def push_ui(user_id: Optional[str], card: Dict[str, Any]) -> bool:
    """Deliver one card to its user (LiquidUIService + per-user stream)."""
    try:
        from integrations.agent_engine.liquid_ui_service import push_agent_ui
        return push_agent_ui(COMMERCE_AGENT_ID, card, user_id=user_id)
    except Exception:
        logger.exception("commerce push_ui failed")
        return False


def event_stream_for(user_id: str) -> Dict[str, str]:
    """Where the Nunba/McGroce embed listens for this user's live cards.

    No new endpoint: push_agent_ui publishes on the per-user ``chat.social``
    topic, which already fans out to WAMP and to the SSE broker per user.
    """
    return {
        'wamp_topic': f'com.hertzai.hevolve.social.{user_id}',
        'sse_event': 'chat.social',
        'message_type': 'agent_ui_update',
    }


# ─── State (JSON under agent_data/commerce) ────────────────────────

class JsonState:
    """A small dict persisted as one JSON file.  Atomic replace on write."""

    def __init__(self, name: str, directory: Optional[str] = None):
        self._name = name
        self._dir = directory
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        directory = self._dir
        if directory is None:
            from core.platform_paths import get_agent_data_dir
            directory = os.path.join(get_agent_data_dir(), 'commerce')
        return os.path.join(directory, f'{self._name}.json')

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("commerce state %s unreadable: %s", self._name, e)
            return {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(key)

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            path = self.path
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f'{path}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
