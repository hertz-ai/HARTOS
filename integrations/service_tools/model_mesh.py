"""model_mesh — a model one person installed becomes visible to the fleet.

A user adds a model on the Model Management page. Today that model exists
on exactly one machine: nothing in the beacon payload carries a model
inventory (peer_discovery._build_beacon advertises node identity, the
guardrail hash, the code hash and robot_capabilities -- no models), and no
route publishes one. The other nodes cannot know it exists, so every node
rediscovers the same models independently and the work does not compound.

This is the model half of the capability mesh that `peer_reuse` already
builds for recipes: announce what you have, cache what peers announce,
and let the operator decide what to pull. It reuses those rails rather
than growing a second set --

    transport   gossip.broadcast -> POST /api/social/peers/broadcast
                (bounded fan-out, concurrent, one hard deadline)
    trust       peer_reuse.admitted_peers() -- active, not banned, not
                self, and rows only enter that table through the gossip
                admission gate (guardrail_hash + Ed25519)
    dispatch    discovery.peer_broadcast() branches on message['type'],
                and acks unknown types so older peers never see a 5xx

Central is not special here. It runs the same receiver as every other
node, so a model reaches it the same way it reaches a sibling desktop.
Requiring central would make the fleet depend on a hub it is the whole
point of this system not to need.

WHY AN OFFER CACHE AND NOT CATALOG ROWS
    ModelCatalog.select_best() does not filter on `downloaded` -- it only
    adds +50 to the score of an entry that is present. A row is a live
    candidate the moment it is registered and `enabled`. So writing peer
    adverts into the catalog would let a peer put a candidate in front of
    the local selector, scored with a quality_score and priority the peer
    chose, for weights this node does not have. Offers therefore live in
    their own cache and are never registered. Nothing a peer says can
    move local selection; the operator installs through the ordinary
    admin path, which re-derives every number locally.

WHAT TRAVELS
    Facts the origin MEASURED from the artifact -- read_gguf_facts() on
    the real file gives moe / experts_used / mtp. A node that has a model
    knows things about it that a catalogue entry cannot state, and those
    are exactly the facts that decide whether it is worth pulling. Only
    keys in _FACT_KEYS cross the wire: a peer must not be able to set a
    trust flag such as `install_validated` by naming it in an advert.

Weights are never fetched here. An advert is a pointer.
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('ModelMesh')

ADVERT_TYPE = 'model_available'
MODEL_MESH_ENV = 'HEVOLVE_MODEL_MESH'

# Capability keys an advert may carry. A whitelist, not a filter on known
# bad keys: `install_validated` is the flag the dispatcher's validation
# gate reads, and a peer naming it must not be able to assert it.
_FACT_KEYS = frozenset({
    'chat', 'vision', 'has_vision',
    'moe', 'experts_total', 'experts_used', 'expert_fraction',
    'mtp', 'mtp_layers',
    'architecture', 'weight_bytes', 'quant',
})

# Fields of the offer a peer states about the model itself. Local scoring
# fields (quality_score, speed_score, priority) are deliberately absent:
# they are this node's opinion, re-derived at install time.
_OFFER_FIELDS = ('name', 'model_type', 'repo_id', 'backend', 'files',
                 'vram_gb', 'ram_gb', 'disk_gb', 'languages')

_offers: Dict[str, dict] = {}
_lock = threading.RLock()


def model_mesh_enabled() -> bool:
    """One knob. Default ON, for the same reason peer_reuse is: every
    counterparty passed the guardrail-hash + Ed25519 admission gate, and
    an offer is a pointer to a public model repository -- not user data,
    and not something that runs."""
    return os.environ.get(MODEL_MESH_ENV, '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _offer_ttl_s() -> float:
    """Freshness window for a cached offer. Longer than the recipe advert
    TTL because a model is a durable install, not a per-goal artifact --
    a peer that had a 21 GB model an hour ago almost certainly still
    does."""
    try:
        return float(os.environ.get('HEVOLVE_MODEL_OFFER_TTL_S', '21600'))
    except ValueError as e:
        logger.info('model_mesh: bad HEVOLVE_MODEL_OFFER_TTL_S (%s); '
                    'using 6h', e)
        return 21600.0


def _local_identity() -> tuple:
    """(node_id, advertised_url) from gossip, ('', '') when unavailable."""
    try:
        from integrations.social.peer_discovery import gossip
        return (getattr(gossip, 'node_id', '') or '',
                (getattr(gossip, 'base_url', '') or '').rstrip('/'))
    except Exception as e:
        logger.info('model_mesh: gossip identity unavailable: %s', e)
        return '', ''


# ── outbound ─────────────────────────────────────────────────────────

def announce_model_available(model_id: str) -> bool:
    """Tell admitted peers this node has `model_id` installed.

    Called after a model is downloaded and its facts read -- the admin
    hub-install path and any other installer. Announcing is best-effort
    and must never fail the install that triggered it, so this returns a
    bool and does not raise.

    Refuses to announce a model this node does not actually have. Two
    cases, both of which would put a claim on the wire that the node
    cannot honour:

      not downloaded  nothing to offer yet.
      a peer's offer  re-announcing what we only heard would let a
                      single install echo around the mesh gaining
                      apparent corroboration with every hop, and point
                      peers at a node that never had the weights.
    """
    if not model_mesh_enabled():
        return False
    try:
        from integrations.service_tools.model_catalog import get_catalog
        entry = get_catalog().get(model_id)
    except Exception as e:
        logger.info('model_mesh: catalog unreadable for %r: %s', model_id, e)
        return False
    if entry is None:
        logger.info('model_mesh: %r is not in the catalog; not announced',
                    model_id)
        return False
    if not getattr(entry, 'downloaded', False):
        logger.info('model_mesh: %r is not downloaded here; not announced',
                    model_id)
        return False

    node_id, api_url = _local_identity()
    if not api_url:
        logger.info('model_mesh: no advertised URL; %r not announced',
                    model_id)
        return False

    caps = getattr(entry, 'capabilities', None) or {}
    model = {'id': entry.id}
    for f in _OFFER_FIELDS:
        model[f] = getattr(entry, f, None)
    model['capabilities'] = {k: v for k, v in caps.items() if k in _FACT_KEYS}

    advert = {
        'type': ADVERT_TYPE,
        'model': model,
        'source_node': node_id,
        'source_api_url': api_url,
        'timestamp': time.time(),
    }
    try:
        from integrations.social.peer_discovery import gossip
        sent = gossip.broadcast(advert)
        logger.info('model_mesh: announced %s to %d peer(s)', entry.id, sent)
        return True
    except Exception as e:
        logger.info('model_mesh: broadcast failed for %s: %s', entry.id, e)
        return False


# ── inbound ──────────────────────────────────────────────────────────

def on_model_available_advert(message: dict) -> dict:
    """Receive a peer's `model_available` advert.

    Invoked by the /api/social/peers/broadcast dispatcher. Caches the
    offer; registers nothing and downloads nothing. Rate limiting is
    enforced upstream by the endpoint, as it is for the other branches.
    Returns a structured dict; never raises.
    """
    if not model_mesh_enabled():
        return {'success': False, 'reason': 'model_mesh_disabled'}

    msg = message or {}
    model = msg.get('model') or {}
    model_id = (model.get('id') or '').strip()
    source_node = (msg.get('source_node') or '').strip()
    source_api_url = (msg.get('source_api_url') or '').strip().rstrip('/')
    if not model_id or not source_api_url:
        return {'success': False, 'reason': 'incomplete_advert'}

    local_node, _ = _local_identity()
    if local_node and source_node == local_node:
        return {'success': False, 'reason': 'echo_skip'}

    # Trust gate: the sender must be an admitted peer. Same rail the
    # recipe mesh uses -- presence in the gossip-admitted peer store.
    try:
        from integrations.google_a2a.peer_reuse import admitted_peers
        peers = admitted_peers()
    except Exception as e:
        # Cannot establish trust -> do not cache. Failing closed here
        # costs a re-advert on the next install; failing open would
        # accept offers from anyone who can reach the port.
        logger.info('model_mesh: peer store unavailable (%s); '
                    'refusing offer for %s', e, model_id)
        return {'success': False, 'reason': 'trust_unavailable'}

    trusted = any(
        (source_node and p.get('node_id') == source_node)
        or (p.get('url') or '').rstrip('/') == source_api_url
        for p in peers)
    if not trusted:
        logger.info('model_mesh: rejected offer for %s from non-admitted '
                    'peer (node=%s, url=%s)', model_id,
                    source_node or '?', source_api_url or '?')
        return {'success': False, 'reason': 'peer_not_admitted'}

    caps = model.get('capabilities') or {}
    offer = {
        'model_id': model_id,
        'peer_url': source_api_url,
        'peer_node': source_node,
        'capabilities': {k: v for k, v in caps.items() if k in _FACT_KEYS},
        'ts': time.time(),
    }
    for f in _OFFER_FIELDS:
        offer[f] = model.get(f)

    key = f'{source_api_url}|{model_id}'
    with _lock:
        _offers[key] = offer
    logger.info('model_mesh: cached offer %s from %s', model_id,
                source_api_url)
    return {'success': True, 'cached': key}


# ── read side ────────────────────────────────────────────────────────

def peer_offers(model_type: Optional[str] = None,
                exclude_local: bool = True) -> List[dict]:
    """Fresh offers from peers, newest first.

    This is what the Model Management page shows as "available elsewhere
    in your hive". `exclude_local` drops anything already in the local
    catalog so the page lists what this node could gain, not what it
    already has. Stale entries are evicted on read -- the cache is small
    and read rarely, so a sweep thread would be machinery for nothing.
    """
    if not model_mesh_enabled():
        return []
    now = time.time()
    ttl = _offer_ttl_s()
    with _lock:
        for key, offer in list(_offers.items()):
            if now - offer.get('ts', 0.0) > ttl:
                _offers.pop(key, None)
                logger.info('model_mesh: offer %s stale; evicted', key)
        rows = [dict(o) for o in _offers.values()]

    if model_type:
        rows = [o for o in rows if o.get('model_type') == model_type]

    if exclude_local:
        try:
            from integrations.service_tools.model_catalog import get_catalog
            catalog = get_catalog()
            rows = [o for o in rows if not catalog.get(o['model_id'])]
        except Exception as e:
            # Report the offers rather than none: a page that shows a
            # model the node already has is a cosmetic duplicate, while
            # showing nothing hides the whole feature.
            logger.info('model_mesh: catalog unreadable for offer '
                        'filtering (%s); listing unfiltered', e)

    rows.sort(key=lambda o: o.get('ts', 0.0), reverse=True)
    return rows


def clear_offers() -> int:
    """Drop every cached offer. Returns how many were dropped."""
    with _lock:
        n = len(_offers)
        _offers.clear()
    return n
