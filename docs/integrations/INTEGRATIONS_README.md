# Integrations Package

This package contains all external protocol integrations for the LLM-langchain Chatbot Agent system.

## Overview

Three major integrations have been implemented:

1. **MCP (Model Context Protocol)** - Connect to external data sources
2. **Internal Agent Communication** - In-process skill-based delegation
3. **Google A2A (Agent2Agent Protocol)** - Cross-platform agent communication

---

## 1. MCP Integration (`integrations/mcp/`)

### What is MCP?

Anthropic's Model Context Protocol enables AI models to connect to external data sources and tools through a standardized interface.

### Files

- `mcp_integration.py` - Core MCP client implementation
- `mcp_servers.json` - User configuration for MCP servers
- `test_mcp_integration.py` - Comprehensive test suite
- `test_mcp_server.py` - Mock MCP server for testing

### Usage

```python
from integrations.mcp import load_user_mcp_servers, get_mcp_tools_for_autogen

# Load user-configured MCP servers
num_servers = load_user_mcp_servers('mcp_servers.json')

# Get tools for Autogen agents
tools = get_mcp_tools_for_autogen()
```

### Configuration

Edit `integrations/mcp/mcp_servers.json`:

```json
{
  "servers": [
    {
      "name": "my_server",
      "url": "http://localhost:9000",
      "api_key": null,
      "enabled": true
    }
  ]
}
```

---

## 2. Internal Agent Communication (`integrations/internal_comm/`)

### What is Internal Communication?

In-process agent coordination system with skill-based task delegation and context sharing.

### Files

- `internal_agent_communication.py` - Core communication system
- `test_a2a_integration.py` - Comprehensive test suite
- `test_a2a_quick.py` - Quick validation test

### Features

- **AgentSkillRegistry**: Track agent capabilities and proficiency
- **A2AContextExchange**: Manage context sharing and task delegation
- **Skill Proficiency Tracking**: Success rates per skill
- **Best Agent Selection**: Auto-routes to highest proficiency agent

### Usage

```python
from integrations.internal_comm import register_agent_with_skills, a2a_context

# Register agent with skills
register_agent_with_skills('helper', [
    {'name': 'tool_execution', 'description': 'Execute tools', 'proficiency': 1.0}
])

# Share context between agents
a2a_context.share_context('helper', 'task_result', {'status': 'completed'})
```

### Predefined Agent Skills

- **Assistant**: task_coordination, decision_making, context_management
- **Helper**: tool_execution, data_processing, external_api
- **Executor**: code_execution, computation, data_analysis
- **Verify**: status_verification, quality_assurance, validation

---

## 3. Google A2A Protocol (`integrations/google_a2a/`)

### What is Google A2A?

Official open standard by Google (donated to Linux Foundation) for cross-platform agent communication using JSON-RPC 2.0 over HTTP(S).

### Files

- `google_a2a_integration.py` - Core A2A protocol implementation
- `a2a_agent_registry.py` - Agent registration and executor functions
- `test_google_a2a_quick.py` - Quick validation test

### Features

- **Agent Cards**: Published at `/.well-known/agent.json` for discovery
- **JSON-RPC 2.0**: Standardized message format
- **Task Lifecycle**: submitted → working → input_required → completed/failed
- **Cross-Platform**: Works with any A2A-compliant agent

### Flask Endpoints

```
GET  /a2a/<agent_id>/.well-known/agent.json  - Agent Card discovery
POST /a2a/<agent_id>/jsonrpc                 - JSON-RPC message handling
```

### JSON-RPC Methods

- `message/send` - Send message to agent
- `message/get` - Get task status
- `task/cancel` - Cancel task

### Usage

```python
from integrations.google_a2a import initialize_a2a_server, register_all_agents

# Initialize A2A server with Flask app
a2a_server = initialize_a2a_server(app, base_url="http://localhost:6777")

# Register all agents
register_all_agents()
```

### Agent Cards

Each agent (Assistant, Helper, Executor, Verify) has an Agent Card accessible at:
- `http://localhost:6777/a2a/assistant/.well-known/agent.json`
- `http://localhost:6777/a2a/helper/.well-known/agent.json`
- `http://localhost:6777/a2a/executor/.well-known/agent.json`
- `http://localhost:6777/a2a/verify/.well-known/agent.json`

---

## Running Tests

### Quick Tests

```bash
# Internal Communication
python integrations/internal_comm/test_a2a_quick.py

# Google A2A
python integrations/google_a2a/test_google_a2a_quick.py
```

### Comprehensive Test Suite

```bash
python run_integration_tests.py
```

**Expected Output:**
```
======================================================================
INTEGRATION TESTS SUITE
======================================================================

[OK] Internal Agent Communication PASSED
[OK] Google A2A Protocol PASSED

----------------------------------------------------------------------
Total: 2 tests | Passed: 2 | Failed: 0 | Skipped: 0
----------------------------------------------------------------------

[OK] All tests passed!
```

---

## Comparison: Internal Comm vs. Google A2A

| Feature | Internal Communication | Google A2A Protocol |
|---------|------------------------|---------------------|
| **Transport** | In-process (Python objects) | HTTP(S) JSON-RPC 2.0 |
| **Discovery** | Internal skill registry | Agent Cards at `/.well-known/agent.json` |
| **Communication** | Direct function calls | HTTP POST to `/jsonrpc` |
| **Scope** | Same process only | Cross-platform, cross-vendor |
| **Authentication** | N/A (internal) | OAuth 2.0, API keys |
| **Performance** | Very fast (no network) | Network overhead |
| **Use Case** | Fast in-process coordination | External agent interoperability |

---

## Integration into Main System

### hart_intelligence_entry.py

```python
from integrations.google_a2a import initialize_a2a_server, register_all_agents

# Initialize A2A server
a2a_server = initialize_a2a_server(app, base_url="http://localhost:6777")
register_all_agents()
```

### create_recipe.py & reuse_recipe.py

```python
from integrations.mcp import load_user_mcp_servers, mcp_registry
from integrations.internal_comm import register_agent_with_skills, a2a_context

# MCP Integration
num_servers = load_user_mcp_servers()
mcp_tools = mcp_registry.get_all_tool_functions()

# Internal Communication
register_agent_with_skills('helper', helper_skills)
```

---

## Documentation

For complete integration details, see:
- `/INTEGRATION_SUMMARY.md` - Comprehensive integration documentation

---

## Protocol Versions

- **MCP**: User-provided servers
- **Internal Comm**: Custom v1.0.0
- **Google A2A**: Official Protocol v0.2.6

---

## License

Same as parent project.

---

**Maintained By:** Claude Code (Integration Specialist)
**Last Updated:** 2025-10-23
