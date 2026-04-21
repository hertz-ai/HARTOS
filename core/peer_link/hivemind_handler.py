"""
HiveMind channel (0x05 PRIVATE) receiver.

The channel registry in `core.peer_link.channels` declares 0x05 = hivemind
(PRIVATE).  `world_model_bridge.query_hivemind` broadcasts on this channel via
`PeerLinkManager.collect('hivemind', ...)` expecting each peer to reply with a
thought.  Before this module existed, no handler was registered on the
receiving side — hive-scoped private messages were silently dropped on the
floor and `collect()` returned an empty list regardless of peer count.

This module fills that gap in two modes:

1.  ``type == 'query'`` (the shape emitted by ``link_manager.collect``) —
    forward to the in-process HiveMind via ``world_model_bridge`` and return
    the peer's local thought.  Returning a dict from a channel handler makes
    the peer's reply travel back on the same link (see ChannelDispatcher
    docstring).

2.  ``type == 'deliver'`` (private agent-to-agent message) — deliver to the
    target agent's mailbox via the agent_ledger `receive_message` primitive
    so the receiving agent picks it up on its next tick.

The handler is registered via ``bootstrap_hivemind_handler()`` from the same
boot sequence that registers the `dispatch` handler in
``embedded_main._register_device_control_handler`` (and its Nunba
equivalent).  Calls are safe no-ops when PeerLink is disabled or when the
HiveMind bridge isn't loaded.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve.peer_link.hivemind')

# Sentinel returned when the local peer has nothing to contribute.  Making
# this a module-level constant keeps the shape stable for test assertions
# and avoids accidental None → "no handler registered" ambiguity on the
# caller side (world_model_bridge treats None/empty as "no peer responded").
_EMPTY_REPLY: Dict[str, Any] = {'type': 'reply', 'thought': ''}


def handle_hivemind_message(data: Any, sender_peer_id: str) -> Optional[dict]:
    """Route an incoming 0x05 hivemind frame.

    Handler signature matches ``ChannelDispatcher.register`` expectations:
    ``handler(data, sender_peer_id) -> Optional[dict]``.  Returning a dict
    sends a response back on the same link.

    Failure modes:
    - malformed payload (non-dict)            → log at debug, return None
    - unknown ``type``                         → log at debug, return None
    - HiveMind bridge missing (HTTP-only tier) → log at debug, return
      ``_EMPTY_REPLY`` so the requesting peer's ``collect()`` still sees a
      structured response rather than hanging on the timeout.
    """
    if not isinstance(data, dict):
        logger.debug(
            f"hivemind frame from {sender_peer_id[:8] if sender_peer_id else '?'} "
            f"not a dict: {type(data).__name__}"
        )
        return None

    msg_type = data.get('type', 'query')

    if msg_type == 'query':
        return _handle_query(data, sender_peer_id)
    if msg_type == 'deliver':
        return _handle_deliver(data, sender_peer_id)

    logger.debug(
        f"hivemind frame from {sender_peer_id[:8] if sender_peer_id else '?'} "
        f"has unknown type={msg_type!r}"
    )
    return None


def _handle_query(data: dict, sender_peer_id: str) -> Optional[dict]:
    """Respond to a HiveMind thought query.

    world_model_bridge.query_hivemind emits ``{'type': 'query'}`` by default
    (see link_manager.collect).  Callers may optionally include:
      * 'query'      — textual prompt (when a peer wants targeted fusion)
      * 'user_id'    — owner identity, used for consent checks if present
      * 'timeout_ms' — the caller's own budget (informational; we don't
        enforce it here because ChannelDispatcher dispatches synchronously)
    """
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
    except ImportError:
        # HTTP-only central tier — hevolveai not loaded, nothing to think.
        return _EMPTY_REPLY

    try:
        bridge = get_world_model_bridge()
    except Exception as e:
        logger.debug(f"hivemind query: bridge unavailable ({e})")
        return _EMPTY_REPLY

    query_text = data.get('query', '') or ''
    user_id = data.get('user_id') or None

    try:
        # query_hivemind already guards on in-process HiveMind availability
        # and falls back to PeerLink broadcast on its own.  We call it with
        # a conservative timeout — the far-side caller is already waiting
        # on their own timeout, so ours must be shorter.
        result = bridge.query_hivemind(
            query_text=query_text,
            user_id=user_id,
            timeout_ms=min(int(data.get('timeout_ms', 500)), 500),
        )
    except Exception as e:
        logger.debug(f"hivemind query failed locally: {e}")
        return _EMPTY_REPLY

    if not result:
        return _EMPTY_REPLY

    # Shape matches what world_model_bridge.query_hivemind reads back
    # (line 954–958 of world_model_bridge.py):
    #   peer_resp.get('thought') or peer_resp.get('response')
    thought = (
        (result.get('thought') if isinstance(result, dict) else None)
        or (result.get('response') if isinstance(result, dict) else None)
        or ''
    )
    return {
        'type': 'reply',
        'thought': thought,
        'peer_id': sender_peer_id,
    }


def _handle_deliver(data: dict, sender_peer_id: str) -> Optional[dict]:
    """Deliver a private message to the target agent's mailbox.

    Uses the agent_ledger primitive so the receiving agent picks up the
    message on its next dispatcher tick.  Required fields:
      * 'target_agent_id' — agent (task) to deliver to
      * 'message'         — arbitrary dict payload, forwarded as-is

    The agent_ledger singleton is imported lazily so this handler works
    when the ledger module isn't loaded (e.g. pure peer-relay topologies).
    """
    target = data.get('target_agent_id')
    message = data.get('message')
    if not target or not isinstance(message, dict):
        logger.debug(
            f"hivemind deliver from {sender_peer_id[:8] if sender_peer_id else '?'}: "
            f"missing target_agent_id or message (target={target!r})"
        )
        return {'type': 'ack', 'delivered': False, 'reason': 'invalid_payload'}

    try:
        from integrations.distributed_agent.api import _get_coordinator
    except ImportError:
        logger.debug("hivemind deliver: distributed_agent not importable")
        return {'type': 'ack', 'delivered': False, 'reason': 'no_coordinator'}

    try:
        coordinator = _get_coordinator()
        if coordinator is None:
            return {'type': 'ack', 'delivered': False, 'reason': 'no_coordinator'}
        ledger = getattr(coordinator, '_ledger', None)
        if ledger is None:
            return {'type': 'ack', 'delivered': False, 'reason': 'no_ledger'}
        task = ledger.get_task(target)
        if task is None:
            return {'type': 'ack', 'delivered': False, 'reason': 'unknown_agent'}
        # Stamp sender so the receiving agent can filter by origin.
        stamped = dict(message)
        stamped.setdefault('from_peer_id', sender_peer_id)
        task.receive_message(stamped)
        try:
            ledger.save()
        except Exception:
            # Best-effort persist; the in-memory receive is still valid.
            pass
        return {'type': 'ack', 'delivered': True, 'target_agent_id': target}
    except Exception as e:
        logger.debug(f"hivemind deliver failed for {target}: {e}")
        return {'type': 'ack', 'delivered': False, 'reason': str(e)}


def bootstrap_hivemind_handler() -> bool:
    """Register the HiveMind handler on the channel dispatcher and on the
    PeerLinkManager (so future links pick it up).

    Idempotent — returns ``True`` on first successful registration,
    ``False`` on any subsequent call (or when PeerLink is absent).
    """
    try:
        from core.peer_link.channels import get_channel_dispatcher
        from core.peer_link.link_manager import get_link_manager
    except ImportError:
        logger.debug("hivemind bootstrap: peer_link not importable")
        return False

    dispatcher = get_channel_dispatcher()
    if dispatcher.has_handlers('hivemind'):
        return False

    dispatcher.register('hivemind', handle_hivemind_message)

    try:
        mgr = get_link_manager()
        mgr.register_channel_handler('hivemind', handle_hivemind_message)
    except Exception as e:
        # dispatcher registration is the source of truth; manager is a
        # convenience for per-link delivery.  Failure here is non-fatal.
        logger.debug(f"hivemind bootstrap: link_manager registration skipped: {e}")

    logger.info("HiveMind (0x05 PRIVATE) channel handler registered")
    return True
