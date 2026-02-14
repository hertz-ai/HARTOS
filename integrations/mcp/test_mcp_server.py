"""
Simple Mock MCP Server for Testing

This server implements the MCP protocol endpoints:
- /health - Health check
- /tools/list - List available tools
- /tools/execute - Execute a tool

Run this server on localhost:9000 for testing MCP integration.
"""

from flask import Flask, request, jsonify
import sys

app = Flask(__name__)

# Define sample MCP tools
MOCK_TOOLS = [
    {
        "name": "get_user_info",
        "description": "Retrieve user information from the user provider system",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "The ID of the user to retrieve"
                }
            },
            "required": ["user_id"]
        }
    },
    {
        "name": "list_users",
        "description": "List all users in the system",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of users to return",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "create_user",
        "description": "Create a new user in the system",
        "parameters": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username for the new user"
                },
                "email": {
                    "type": "string",
                    "description": "Email address for the new user"
                }
            },
            "required": ["username", "email"]
        }
    }
]

# Mock user database
MOCK_USERS = {
    "10077": {
        "user_id": "10077",
        "username": "test_user",
        "email": "test@example.com",
        "created_at": "2025-01-01T00:00:00Z"
    },
    "12345": {
        "user_id": "12345",
        "username": "john_doe",
        "email": "john@example.com",
        "created_at": "2025-01-15T10:30:00Z"
    }
}


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'server': 'Mock MCP Server',
        'version': '1.0.0'
    }), 200


@app.route('/tools/list', methods=['GET'])
def list_tools():
    """List all available tools"""
    return jsonify({
        'tools': MOCK_TOOLS,
        'count': len(MOCK_TOOLS)
    }), 200


@app.route('/tools/execute', methods=['POST'])
def execute_tool():
    """Execute a tool"""
    data = request.json
    tool_name = data.get('tool')
    arguments = data.get('arguments', {})

    print(f"Executing tool: {tool_name} with arguments: {arguments}", file=sys.stderr)

    # Execute the appropriate tool
    if tool_name == 'get_user_info':
        user_id = arguments.get('user_id')
        if user_id in MOCK_USERS:
            return jsonify({
                'success': True,
                'result': MOCK_USERS[user_id]
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': f'User {user_id} not found'
            }), 200

    elif tool_name == 'list_users':
        limit = arguments.get('limit', 10)
        users = list(MOCK_USERS.values())[:limit]
        return jsonify({
            'success': True,
            'result': {
                'users': users,
                'count': len(users)
            }
        }), 200

    elif tool_name == 'create_user':
        username = arguments.get('username')
        email = arguments.get('email')

        # Generate a new user ID
        new_id = str(len(MOCK_USERS) + 10000)

        new_user = {
            'user_id': new_id,
            'username': username,
            'email': email,
            'created_at': '2025-10-23T12:00:00Z'
        }

        MOCK_USERS[new_id] = new_user

        return jsonify({
            'success': True,
            'result': new_user
        }), 200

    else:
        return jsonify({
            'success': False,
            'error': f'Unknown tool: {tool_name}'
        }), 400


if __name__ == '__main__':
    print("Starting Mock MCP Server on http://localhost:9000")
    print("Available endpoints:")
    print("  GET  /health - Health check")
    print("  GET  /tools/list - List available tools")
    print("  POST /tools/execute - Execute a tool")
    print("\nAvailable tools:")
    for tool in MOCK_TOOLS:
        print(f"  - {tool['name']}: {tool['description']}")

    app.run(host='localhost', port=9000, debug=True)
