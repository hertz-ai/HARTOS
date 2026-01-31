"""
Mock LLM API Server
Simulates OpenAI-compatible API for testing
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import time
import json
import random

app = Flask(__name__)
CORS(app)

# Mock responses for different scenarios
MOCK_RESPONSES = {
    "create_file": {
        "status": "completed",
        "message": "File created successfully",
        "result": {"filename": "test.txt", "path": "/tmp/test.txt"}
    },
    "status_verification": {
        "status": "completed",
        "message": "Action completed successfully",
        "verified": True
    },
    "fallback": {
        "can_perform_without_user_input": "yes",
        "assumptions": ["Using default filename", "Creating in current directory"],
        "questions_for_user": []
    },
    "recipe": {
        "steps": "Create file using open() function",
        "tool_name": "file_operations",
        "generalized_functions": "open('{filename}', 'w')",
        "dependencies": []
    }
}

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "mock-llm"}), 200

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """
    Mock OpenAI chat completions endpoint
    Returns appropriate responses based on message content
    """
    data = request.json
    messages = data.get('messages', [])

    # Extract the last message to determine response type
    last_message = messages[-1]['content'] if messages else ""

    # Determine what type of response to give
    response_content = generate_response(last_message)

    # Simulate processing time
    time.sleep(0.1)

    # Return OpenAI-compatible response
    return jsonify({
        "id": f"chatcmpl-{random.randint(1000, 9999)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get('model', 'Qwen3-VL-2B-Instruct'),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_content
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 20,
            "total_tokens": 70
        }
    })

def generate_response(message):
    """
    Generate appropriate response based on message content
    Uses pattern matching to return contextual responses
    """
    message_lower = message.lower()

    # Status verification responses
    if "status" in message_lower and "verify" in message_lower:
        return json.dumps(MOCK_RESPONSES["status_verification"])

    # Fallback request responses
    if "fallback" in message_lower or "without user input" in message_lower:
        return json.dumps(MOCK_RESPONSES["fallback"])

    # Recipe request responses
    if "recipe" in message_lower or "generalized" in message_lower:
        return json.dumps(MOCK_RESPONSES["recipe"])

    # Action execution responses
    if "create" in message_lower and "file" in message_lower:
        return json.dumps(MOCK_RESPONSES["create_file"])

    # Visual task responses
    if "visual" in message_lower or "image" in message_lower:
        return json.dumps({
            "message2user": "I can see a laptop with code editor open",
            "objects_detected": ["laptop", "keyboard", "mouse"],
            "activity": "User is coding"
        })

    # Code generation responses
    if "code" in message_lower or "function" in message_lower:
        return """```python
def hello_world():
    print("Hello, World!")
    return "Hello, World!"
```"""

    # General task execution
    if "execute" in message_lower or "perform" in message_lower:
        return json.dumps({
            "status": "in_progress",
            "message": "Executing task...",
            "progress": 50
        })

    # Default response
    return json.dumps({
        "message2user": "Task understood. Processing...",
        "status": "acknowledged"
    })

@app.route('/v1/models', methods=['GET'])
def list_models():
    """Mock models endpoint"""
    return jsonify({
        "object": "list",
        "data": [
            {
                "id": "Qwen3-VL-2B-Instruct",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "test"
            }
        ]
    })

@app.route('/v1/completions', methods=['POST'])
def completions():
    """Mock completions endpoint (non-chat)"""
    data = request.json
    prompt = data.get('prompt', '')

    response_text = generate_response(prompt)

    return jsonify({
        "id": f"cmpl-{random.randint(1000, 9999)}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": data.get('model', 'Qwen3-VL-2B-Instruct'),
        "choices": [
            {
                "text": response_text,
                "index": 0,
                "finish_reason": "stop"
            }
        ]
    })

if __name__ == '__main__':
    print("Starting Mock LLM Server on port 8000...")
    app.run(host='0.0.0.0', port=8000, debug=False)
