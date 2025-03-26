import os
from twisted.internet import reactor
from autobahn.twisted.component import Component, run
from twisted.internet.defer import inlineCallbacks
from twisted.internet.defer import Deferred
from reuse_recipe import chat_agent, crossbar_multiagent, time_based_execution
print('Inside crossbar_server')
# Crossbar/WAMP component setup        
url = "ws://aws_rasa.hertzai.com:8088/ws"
url = os.environ.get('CBURL', url)
realmvalue = os.environ.get('CBREALM', 'realm1')

component = Component(transports=url, realm=realmvalue)
wamp_session = None  # Global variable to store session

@component.on_join
@inlineCallbacks
def joined(session, details):
    global wamp_session
    wamp_session = session  # Store session
    print("WAMP session ready")

    def onevent(msg):
        print("Event received:", msg)
        crossbar_multiagent(msg)
    try:
        yield session.subscribe(onevent, "com.hertzai.hevolve.agent.multichat")
        print("Subscribed to topic")
    except Exception as e:
        print(f"Could not subscribe to topic: {e}")
    
@inlineCallbacks
def call_rpc(message_json):
    """Calls the registered RPC function asynchronously and ensures the response is awaited."""
    global wamp_session
    try:
        response = yield wamp_session.call("com.hertzai.hevolve.action", message_json)

        # Ensure the response is resolved properly
        if isinstance(response, Deferred):
            print(f"Deferred: {type(response)}, Value: {response}")
            response = yield response  # Wait until the Deferred is resolved
            print(f"AFTER WAIT Deferred: {type(response)}, Value: {response}")

        # Ensure response is JSON-serializable (WAMP call should return JSON)
        if not isinstance(response, dict):
            print(f"Unexpected Response Type: {type(response)}, Value: {response}")
            response = {"error": "Invalid response format", "data": str(response)}

        print(f"Final RPC Response: {response}")
        return response
    except Exception as e:
        print(f"RPC Call Error: {e}")
        return {"error": str(e)}

