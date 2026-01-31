"""
Mock API Server
Simulates all external API dependencies:
- autogen_response endpoint
- student API
- action API
- txt2img endpoint
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import json
import threading

app = Flask(__name__)
CORS(app)

# In-memory storage for testing
conversations = []
user_data = {}
action_data = {}
messages_sent = []

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "mock-apis"}), 200

# ============================================================================
# AUTOGEN RESPONSE ENDPOINT (Port 9890)
# ============================================================================

@app.route('/autogen_response', methods=['POST'])
def autogen_response():
    """
    Mock endpoint that receives agent responses
    Simulates the endpoint that receives messages to send to users
    """
    data = request.json
    user_id = data.get('user_id')
    message = data.get('message')
    inp = data.get('inp')
    request_id = data.get('request_id')
    agent_status = data.get('Agent_status', 'Unknown')

    # Store the message
    messages_sent.append({
        'user_id': user_id,
        'message': message,
        'input': inp,
        'request_id': request_id,
        'agent_status': agent_status,
        'timestamp': datetime.now().isoformat()
    })

    return jsonify({
        'status': 'success',
        'message': 'Response received',
        'request_id': request_id
    }), 200

@app.route('/autogen_response/messages', methods=['GET'])
def get_messages():
    """Get all messages sent (for testing)"""
    return jsonify(messages_sent), 200

@app.route('/autogen_response/clear', methods=['POST'])
def clear_messages():
    """Clear message history (for testing)"""
    messages_sent.clear()
    return jsonify({'status': 'cleared'}), 200

# ============================================================================
# CONVERSATION ENDPOINT (Database simulation)
# ============================================================================

@app.route('/conversation', methods=['POST'])
def save_conversation():
    """Mock conversation save endpoint"""
    data = request.json

    conv_id = len(conversations) + 1
    conversation = {
        'conv_id': conv_id,
        **data,
        'created_at': datetime.now().isoformat()
    }

    conversations.append(conversation)

    return jsonify({'conv_id': conv_id}), 200

# ============================================================================
# STUDENT API (Port 9891)
# ============================================================================

@app.route('/student', methods=['POST'])
def get_student():
    """Mock student API endpoint"""
    data = request.json
    user_id = data.get('user_id')

    # Return mock user data
    return jsonify({
        'user_id': user_id,
        'name': f'Test User {user_id}',
        'gender': 'Other',
        'preferred_language': 'English',
        'dob': '2000-01-01'
    }), 200

# ============================================================================
# ACTION API (Port 9892)
# ============================================================================

@app.route('/actions', methods=['GET'])
def get_actions():
    """Mock action API endpoint"""
    user_id = request.args.get('user_id')

    # Return mock action history
    if user_id not in action_data:
        action_data[user_id] = [
            {
                'action_id': 1,
                'action': 'Login',
                'created_date': datetime.now().isoformat(),
                'zeroshot_label': 'Authentication'
            },
            {
                'action_id': 2,
                'action': 'Query',
                'created_date': datetime.now().isoformat(),
                'zeroshot_label': 'Information Retrieval'
            }
        ]

    return jsonify(action_data[user_id]), 200

@app.route('/actions', methods=['POST'])
def add_action():
    """Add action to history"""
    data = request.json
    user_id = data.get('user_id')

    if user_id not in action_data:
        action_data[user_id] = []

    action = {
        'action_id': len(action_data[user_id]) + 1,
        'action': data.get('action'),
        'created_date': datetime.now().isoformat(),
        'zeroshot_label': data.get('zeroshot_label', 'General')
    }

    action_data[user_id].append(action)

    return jsonify(action), 201

# ============================================================================
# TEXT-TO-IMAGE API (Port 5459)
# ============================================================================

@app.route('/txt2img', methods=['POST'])
def text_to_image():
    """Mock text-to-image endpoint"""
    prompt = request.args.get('prompt', 'default')

    # Return mock image URL
    return jsonify({
        'img_url': f'http://mock-apis:5459/images/{hash(prompt)}.png',
        'prompt': prompt,
        'status': 'generated'
    }), 200

# ============================================================================
# AGENT CREATION DATABASE ENDPOINTS
# ============================================================================

@app.route('/agent_update/<int:prompt_id>', methods=['PATCH'])
def update_agent(prompt_id):
    """Mock endpoint for updating agent creation status"""
    data = request.json

    return jsonify({
        'status': 'success',
        'prompt_id': prompt_id,
        'updated': data
    }), 200

# ============================================================================
# TESTING HELPERS
# ============================================================================

@app.route('/test/reset', methods=['POST'])
def reset_all():
    """Reset all mock data (for testing)"""
    conversations.clear()
    user_data.clear()
    action_data.clear()
    messages_sent.clear()

    return jsonify({'status': 'reset complete'}), 200

@app.route('/test/stats', methods=['GET'])
def get_stats():
    """Get statistics about mock API usage"""
    return jsonify({
        'conversations': len(conversations),
        'users': len(user_data),
        'action_logs': sum(len(actions) for actions in action_data.values()),
        'messages_sent': len(messages_sent)
    }), 200

if __name__ == '__main__':
    # Run on multiple ports using threading
    def run_on_port(port):
        from werkzeug.serving import run_simple
        run_simple('0.0.0.0', port, app, use_reloader=False, use_debugger=False)

    # Primary port
    print("Starting Mock API Server...")
    print("- Port 9890: autogen_response")
    print("- Port 9891: student API")
    print("- Port 9892: action API")
    print("- Port 5459: txt2img")

    app.run(host='0.0.0.0', port=9890, debug=False)
