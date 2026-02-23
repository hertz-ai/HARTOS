"""
Mock Crossbar Server
Simulates Crossbar.io WAMP router for pub/sub messaging
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)

# In-memory message storage
published_messages = []
subscriptions = {}
rpc_calls = []

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "mock-crossbar"}), 200

@app.route('/publish', methods=['POST'])
def publish():
    """
    Mock publish endpoint
    Simulates Crossbar HTTP bridge publish
    """
    data = request.json
    topic = data.get('topic')
    args = data.get('args', [])
    kwargs = data.get('kwargs', {})

    # Store published message
    message = {
        'topic': topic,
        'args': args,
        'kwargs': kwargs
    }
    published_messages.append(message)

    # Simulate delivery to subscribers
    if topic in subscriptions:
        for subscriber in subscriptions[topic]:
            subscriber['messages'].append(message)

    return jsonify({
        'publication': len(published_messages),
        'topic': topic
    }), 200

@app.route('/call', methods=['POST'])
def call():
    """
    Mock RPC call endpoint
    Simulates Crossbar HTTP bridge call
    """
    data = request.json
    procedure = data.get('procedure')
    args = data.get('args', [])
    kwargs = data.get('kwargs', {})

    # Store RPC call
    call_record = {
        'procedure': procedure,
        'args': args,
        'kwargs': kwargs
    }
    rpc_calls.append(call_record)

    # Return mock response based on procedure
    if 'agent' in procedure.lower():
        return jsonify({
            'args': [],
            'kwargs': {
                'status': 'success',
                'result': 'Agent task completed'
            }
        }), 200
    elif 'visual' in procedure.lower():
        return jsonify({
            'args': [],
            'kwargs': {
                'visual_context': 'User is looking at screen',
                'objects': ['laptop', 'keyboard']
            }
        }), 200
    else:
        return jsonify({
            'args': [],
            'kwargs': {'status': 'ok'}
        }), 200

@app.route('/ws', methods=['GET'])
def websocket_info():
    """Info about WebSocket endpoint"""
    return jsonify({
        'protocol': 'wamp.2.json',
        'endpoint': 'ws://mock-crossbar:8088/ws'
    }), 200

@app.route('/test/messages', methods=['GET'])
def get_messages():
    """Get all published messages (for testing)"""
    return jsonify(published_messages), 200

@app.route('/test/rpc_calls', methods=['GET'])
def get_rpc_calls():
    """Get all RPC calls (for testing)"""
    return jsonify(rpc_calls), 200

@app.route('/test/reset', methods=['POST'])
def reset():
    """Reset all data (for testing)"""
    published_messages.clear()
    subscriptions.clear()
    rpc_calls.clear()
    return jsonify({'status': 'reset complete'}), 200

if __name__ == '__main__':
    print("Starting Mock Crossbar Server on port 8088...")
    app.run(host='0.0.0.0', port=8088, debug=False)
