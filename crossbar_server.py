import logging
import os
import asyncio
from autobahn.asyncio.component import Component, run
from autobahn.wamp.exception import ApplicationError
from reuse_recipe import chat_agent, crossbar_multiagent, time_based_execution

print('Inside crossbar_server')

# Crossbar/WAMP component setup
url = os.environ.get('CBURL', "ws://localhost:8088/ws")
realmvalue = os.environ.get('CBREALM', 'realm1')

# Prefer WSS (TLS) for production — warn if using plaintext ws://
if url.startswith('ws://') and os.environ.get('HEVOLVE_NODE_TIER') == 'central':
    print("WARNING: Using plaintext ws:// on central tier — set CBURL to wss:// for production")

# Build transport config with optional ticket auth
_transport_config = {'url': url, 'max_retry_delay': 30}
_wamp_ticket = os.environ.get('HEVOLVE_WAMP_TICKET', '')

if _wamp_ticket:
    component = Component(
        transports=[_transport_config],
        realm=realmvalue,
        authentication={'ticket': {'ticket': _wamp_ticket, 'authid': os.environ.get('HEVOLVE_NODE_ID', 'anon')}},
    )
else:
    component = Component(transports=url, realm=realmvalue)
wamp_session = None  # Global variable to store session
response_event = asyncio.Event()
response_message = None  # To store the response


async def on_event(msg):
    """Handle incoming messages from the WAMP subscription."""
    print("Event received:", msg)
    crossbar_multiagent(msg)
    # Route to MessageBus so local subscribers receive it
    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()
        data = msg if isinstance(msg, dict) else {'raw': str(msg)}
        bus.receive_from_crossbar('com.hertzai.hevolve.agent.multichat', data)
    except Exception:
        pass


async def call_rpc(message_json):
    """Calls the registered RPC function asynchronously using Autobahn Asyncio."""
    global wamp_session
    if not wamp_session:
        return {"error": "WAMP session is not initialized"}

    try:
        response = await wamp_session.call("com.hertzai.hevolve.action", message_json)
        return response
    except ApplicationError as e:
        print(f"RPC Call Error: {e}")
        return {"error": str(e)}


async def subscribe_and_return(message):
    """Calls an RPC method using Autobahn asyncio and returns the response."""
    global response_message, response_event
    response_event.clear()  # Reset event before making a request

    response_message = None  # Clear previous response
    response = await call_rpc(message)

    if response:
        return response
    else:
        return {"error": "No response received"}


async def on_remote_desktop_signal(msg):
    """Handle remote desktop signaling messages (connection requests, transport offers)."""
    print("Remote desktop signal received:", type(msg))
    try:
        from integrations.remote_desktop.session_manager import get_session_manager
        # Signal handling delegated to session manager
        if isinstance(msg, dict):
            sm = get_session_manager()
            # Future: process connect_request, transport_offer, bye
    except Exception as e:
        print(f"Remote desktop signal error: {e}")
    # Route to MessageBus
    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()
        data = msg if isinstance(msg, dict) else {'raw': str(msg)}
        bus.receive_from_crossbar('com.hartos.remote_desktop.signal', data)
    except Exception:
        pass


async def on_compute_request(msg):
    """Handle compute relay request from phone behind NAT.

    Phone publishes to com.hertzai.hevolve.compute.request.{owner_id},
    HARTOS processes locally, responds on compute.response.{owner_id}.

    SECURITY (M1 fix, T137): The user_id is ALWAYS the node owner
    (HEVOLVE_OWNER_USER_ID), never taken from the message body. This
    prevents a peer on the same WAMP realm from publishing with a
    spoofed user_id to impersonate another user. The subscription is
    already scoped to the owner's topic at line 163, but the handler
    used to read user_id from data.get('user_id') which let an
    attacker override it. Now the body's user_id field is ignored —
    the owner_id is the single source of truth.

    If the body contains a user_id that doesn't match the owner, the
    request is logged as suspicious and dropped.
    """
    # The owner_id is set at subscription time (line 161) — it's the
    # only user this node serves compute for. Reading it from env is
    # safe because HEVOLVE_OWNER_USER_ID is set at boot, not by the
    # WAMP peer.
    owner_id = os.environ.get('HEVOLVE_OWNER_USER_ID', '')
    if not owner_id:
        return

    try:
        data = msg if isinstance(msg, dict) else {}
        request_id = data.get('request_id', '')
        text = data.get('text', '')

        if not text:
            return

        # SECURITY: reject messages where the body claims a different
        # user_id than the owner. This catches both honest mistakes
        # (phone sending the wrong id) and malicious impersonation.
        body_user_id = data.get('user_id', '')
        if body_user_id and str(body_user_id) != str(owner_id):
            print(f"SECURITY: compute relay rejected — body user_id "
                  f"'{body_user_id}' != owner '{owner_id}'. "
                  f"Possible impersonation attempt.")
            return

        # Process via the same /chat endpoint everything else uses.
        # user_id is ALWAYS owner_id, never from the message body.
        import requests as http_requests
        from core.port_registry import get_port
        resp = http_requests.post(
            f'http://localhost:{get_port("backend")}/chat',
            json={
                'text': [text] if isinstance(text, str) else text,
                'raw_text': text if isinstance(text, str) else str(text),
                'user_id': owner_id,  # ALWAYS the owner, never body
                'source': 'compute_relay',
                'request_id': request_id,
            },
            timeout=60,
        )
        result = resp.json() if resp.ok else {'error': f'HTTP {resp.status_code}'}
        result['request_id'] = request_id

        # Publish response back via WAMP (reaches phone through NAT)
        response_topic = f'com.hertzai.hevolve.compute.response.{owner_id}'
        if wamp_session:
            wamp_session.publish(response_topic, result)

    except Exception as e:
        logging.warning(f"Compute relay error: {e}")
        if wamp_session and owner_id:
            # CISO: sanitize error response — don't leak internal paths
            # or stack traces to the WAMP caller. Generic message only.
            wamp_session.publish(
                f'com.hertzai.hevolve.compute.response.{owner_id}',
                {'error': 'Compute relay encountered an error', 'request_id': request_id}
            )


@component.on_join
async def joined(session, details):
    """Handles session join and subscription setup."""
    global wamp_session
    wamp_session = session  # Store session

    try:
        await session.subscribe(on_event, "com.hertzai.hevolve.agent.multichat")
        print("Subscribed to topic")
    except Exception as e:
        print(f"Could not subscribe to topic: {e}")

    # Compute relay — same-user phone→HARTOS behind NAT
    try:
        from integrations.social.models import get_db, User
        # Subscribe to all active user compute topics
        # For now, use a wildcard-style approach: subscribe to the user who owns this node
        owner_id = os.environ.get('HEVOLVE_OWNER_USER_ID', '')
        if owner_id:
            compute_topic = f"com.hertzai.hevolve.compute.request.{owner_id}"
            await session.subscribe(on_compute_request, compute_topic)
            print(f"Subscribed to compute relay: {compute_topic}")
    except Exception as e:
        print(f"Compute relay subscription skipped: {e}")

    # Remote desktop signaling topics
    try:
        from integrations.remote_desktop.device_id import get_device_id
        device_id = get_device_id()
        signal_topic = f"com.hartos.remote_desktop.signal.{device_id}"
        await session.subscribe(on_remote_desktop_signal, signal_topic)
        print(f"Subscribed to remote desktop signaling: {signal_topic}")
    except Exception as e:
        print(f"Remote desktop signaling subscription skipped: {e}")


def main():
    """
    Main entry point for hart-crossbar CLI command.
    Starts the WAMP/Crossbar client.
    """
    run([component])


if __name__ == '__main__':
    main()

