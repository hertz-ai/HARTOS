import os
import asyncio
from autobahn.asyncio.component import Component, run
from autobahn.wamp.exception import ApplicationError
from reuse_recipe import chat_agent, crossbar_multiagent, time_based_execution

print('Inside crossbar_server')

# Crossbar/WAMP component setup        
url = os.environ.get('CBURL', "ws://aws_rasa.hertzai.com:8088/ws")
realmvalue = os.environ.get('CBREALM', 'realm1')

component = Component(transports=url, realm=realmvalue)
wamp_session = None  # Global variable to store session
response_event = asyncio.Event()
response_message = None  # To store the response


async def on_event(msg):
    """Handle incoming messages from the WAMP subscription."""
    print("Event received:", msg)
    crossbar_multiagent(msg)


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

