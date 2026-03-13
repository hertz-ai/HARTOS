"""
Test MCP Integration

This script tests the MCP integration by:
1. Starting a mock MCP server
2. Loading the MCP server configuration
3. Discovering tools from the MCP server
4. Testing tool execution

Prerequisites:
- Mock MCP server running on localhost:9000
- mcp_servers.json configured with the mock server
"""

import sys
import time
import requests
import json
import pytest
pytest.importorskip('mcp_integration', reason='mcp_integration module not available')
from mcp_integration import load_user_mcp_servers, mcp_registry

def test_mcp_server_running():
    """Test if the MCP server is running"""
    print("=" * 80)
    print("TEST 1: Check MCP Server Status")
    print("=" * 80)

    try:
        response = requests.get("http://localhost:9000/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"[OK] MCP Server is running")
            print(f"  Server: {data.get('server')}")
            print(f"  Version: {data.get('version')}")
            print(f"  Status: {data.get('status')}")
            return True
        else:
            print(f"[FAIL] MCP Server returned status code {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[FAIL] Cannot connect to MCP server: {e}")
        print("\nTo start the mock MCP server, run:")
        print("  python test_mcp_server.py")
        return False


def test_load_mcp_servers():
    """Test loading MCP servers from configuration"""
    print("\n" + "=" * 80)
    print("TEST 2: Load MCP Servers from Configuration")
    print("=" * 80)

    # Create a test configuration
    test_config = {
        "servers": [
            {
                "name": "test_user_provider",
                "url": "http://localhost:9000",
                "api_key": None,
                "enabled": True,
                "description": "Test user provider MCP server"
            }
        ],
        "settings": {
            "auto_discover": True,
            "connection_timeout": 5,
            "execution_timeout": 30
        }
    }

    # Save test configuration
    with open('mcp_servers_test.json', 'w') as f:
        json.dump(test_config, f, indent=2)

    print("Created test configuration: mcp_servers_test.json")

    # Load servers
    num_servers = load_user_mcp_servers('mcp_servers_test.json')

    if num_servers > 0:
        print(f"[OK] Successfully loaded {num_servers} MCP server(s)")
        return True
    else:
        print(f"[FAIL] Failed to load MCP servers")
        return False


def test_discover_tools():
    """Test tool discovery from MCP server"""
    print("\n" + "=" * 80)
    print("TEST 3: Discover Tools from MCP Server")
    print("=" * 80)

    tool_defs = mcp_registry.get_tool_definitions()

    if len(tool_defs) > 0:
        print(f"[OK] Discovered {len(tool_defs)} tool(s):")
        for tool_def in tool_defs:
            print(f"  - {tool_def['name']}: {tool_def['description']}")
        return True
    else:
        print(f"[FAIL] No tools discovered")
        return False


def test_execute_tool():
    """Test executing a tool from MCP server"""
    print("\n" + "=" * 80)
    print("TEST 4: Execute MCP Tool")
    print("=" * 80)

    # Get all tool functions
    tools = mcp_registry.get_all_tool_functions()

    if not tools:
        print("[FAIL] No tools available to test")
        return False

    # Test get_user_info tool
    tool_name = "test_user_provider_get_user_info"
    if tool_name in tools:
        print(f"Testing tool: {tool_name}")
        tool_func = tools[tool_name]

        # Execute the tool
        result = tool_func(user_id="10077")
        result_data = json.loads(result)

        if result_data.get('success'):
            print(f"[OK] Tool executed successfully")
            print(f"  Result: {json.dumps(result_data.get('result'), indent=2)}")
            return True
        else:
            print(f"[FAIL] Tool execution failed: {result_data.get('error')}")
            return False
    else:
        print(f"[FAIL] Tool {tool_name} not found")
        available_tools = list(tools.keys())
        print(f"  Available tools: {available_tools}")
        return False


def test_list_users_tool():
    """Test the list_users tool"""
    print("\n" + "=" * 80)
    print("TEST 5: Execute list_users Tool")
    print("=" * 80)

    tools = mcp_registry.get_all_tool_functions()
    tool_name = "test_user_provider_list_users"

    if tool_name in tools:
        print(f"Testing tool: {tool_name}")
        tool_func = tools[tool_name]

        # Execute the tool
        result = tool_func(limit=5)
        result_data = json.loads(result)

        if result_data.get('success'):
            print(f"[OK] Tool executed successfully")
            users = result_data.get('result', {}).get('users', [])
            print(f"  Found {len(users)} users:")
            for user in users:
                print(f"    - {user.get('username')} ({user.get('user_id')})")
            return True
        else:
            print(f"[FAIL] Tool execution failed: {result_data.get('error')}")
            return False
    else:
        print(f"[FAIL] Tool {tool_name} not found")
        return False


def main():
    print("\n" + "=" * 80)
    print("MCP INTEGRATION TEST SUITE")
    print("=" * 80 + "\n")

    results = []

    # Test 1: Check if MCP server is running
    results.append(("MCP Server Running", test_mcp_server_running()))

    if not results[0][1]:
        print("\n[FAIL] Cannot proceed without MCP server running")
        print("\nStart the mock MCP server in another terminal:")
        print("  python test_mcp_server.py")
        return

    # Test 2: Load MCP servers
    results.append(("Load MCP Servers", test_load_mcp_servers()))

    if not results[1][1]:
        print("\n[FAIL] Cannot proceed without loading MCP servers")
        return

    # Test 3: Discover tools
    results.append(("Discover Tools", test_discover_tools()))

    # Test 4: Execute tool
    results.append(("Execute get_user_info", test_execute_tool()))

    # Test 5: Execute list_users tool
    results.append(("Execute list_users", test_list_users_tool()))

    # Print summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f"{status} - {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed ({passed*100//total}%)")

    if passed == total:
        print("\n[OK] All MCP integration tests passed!")
    else:
        print(f"\n[FAIL] {total - passed} test(s) failed")


if __name__ == '__main__':
    main()
