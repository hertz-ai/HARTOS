# Dynamic Agent Architecture

**Date:** 2025-10-23
**Status:** ✅ IMPLEMENTED
**Architecture:** Dynamic, No Hardcoded Agents

---

## Executive Summary

**Revolutionary Change:** The system now has **ZERO hardcoded agents**. All agents are dynamically discovered from recipe JSON files in the `prompts/` directory.

### Key Principles

1. **🚫 NO HARDCODED AGENTS** - All agents loaded from recipe JSONs
2. **📁 File-Based Discovery** - Agents discovered from `prompts/` directory
3. **🔄 Fully Dynamic** - New agents automatically available via A2A protocol
4. **👤 User-Configured MCP** - Users provide their own MCP servers
5. **🎯 Specialized Agents** - Each recipe JSON = trained specialist

---

## Architecture Overview

###  File Structure

```
prompts/
├── {prompt_id}.json                      # Prompt definition (flows, personas, goals)
├── {prompt_id}_{flow_id}_recipe.json    # Flow-level trained agent (CONSOLIDATED)
└── {prompt_id}_{flow_id}_{action_id}.json  # Individual action recipes

Examples:
├── 71.json                    # "Generic Story" prompt definition
├── 71_0_recipe.json           # Flow 0 trained agent (story creation)
├── 71_0_1.json                # Action 1 recipe (create 100-word story)
├── 71_0_2.json                # Action 2 recipe (retrieve avatar IDs)
└── 8888.json                  # "Complex Multi-Task" prompt definition
```

### Agent Lifecycle

```
1. User creates prompt → prompts/{prompt_id}.json
                                ↓
2. System executes actions → prompts/{prompt_id}_{flow_id}_{action_id}.json
                                ↓
3. Flow completes → prompts/{prompt_id}_{flow_id}_recipe.json (TRAINED AGENT!)
                                ↓
4. Dynamic Discovery → Agent registered with A2A Protocol
                                ↓
5. Agent available via → GET /a2a/{prompt_id}_{flow_id}/.well-known/agent.json
```

---

## Key Components

### 1. DynamicAgentDiscovery

**Location:** `integrations/google_a2a/dynamic_agent_registry.py`

**Purpose:** Scans `prompts/` directory and discovers trained agents

**Methods:**
```python
class DynamicAgentDiscovery:
    def discover_all_agents() -> int
        # Scans prompts/ for *_*_recipe.json files
        # Returns number of agents discovered

    def get_agent_skills(agent: TrainedAgent) -> List[Dict]
        # Extracts A2A-compatible skills from recipe

    def get_agent_description(agent: TrainedAgent) -> str
        # Generates comprehensive agent description
```

**Discovered Data:**
- Agent ID (e.g., "71_0")
- Prompt ID (71)
- Flow ID (0)
- Persona (e.g., "creator")
- Action (what the agent does)
- Recipe (step-by-step instructions)
- Status (done/pending)
- Autonomy (can_perform_without_user_input)
- Fallback strategy

### 2. DynamicAgentExecutor

**Purpose:** Executes tasks for dynamically discovered agents

**Execution Logic:**
```python
async def execute_agent_task(agent_id, message, context_id):
    if agent.status == "done":
        # Use REUSE mode (trained recipe exists)
        result = chat_agent(message, prompt_id)
    else:
        # Use CREATE mode (still learning)
        result = recipe(user_id, message, prompt_id)
```

### 3. Dynamic A2A Registration

**Location:** `integrations/google_a2a/register_dynamic_agents.py`

**Purpose:** Auto-registers all discovered agents with Google A2A Protocol

**Process:**
1. Discovery scans `prompts/` directory
2. For each agent:
   - Extract skills from recipe
   - Generate Agent Card
   - Create executor function
   - Register with A2A server
3. Agent available at `/a2a/{agent_id}/...`

**Function:**
```python
def register_all_dynamic_agents() -> int:
    # Discovers and registers ALL agents
    # Returns number successfully registered
```

---

## Agent Information Structure

### TrainedAgent Dataclass

```python
@dataclass
class TrainedAgent:
    agent_id: str              # "71_0" (prompt_id_flow_id)
    prompt_id: int             # 71
    flow_id: int               # 0
    persona: str               # "creator"
    action: str                # What the agent does
    recipe: List[Dict]         # Step-by-step instructions
    status: str                # "done" | "pending"
    can_perform_without_user_input: str  # "yes" | "no"
    fallback_action: str       # Fallback strategy
    metadata: Dict             # Additional info
    recipe_file: str           # Path to recipe JSON
    flow_name: str             # "story creation"
    sub_goal: str              # Flow's sub-goal
```

### A2A Agent Skills

Each agent automatically gets A2A-compatible skills:

```json
{
  "name": "creator_story_creation",
  "description": "Create a 100-word story in dialogue format...",
  "examples": [
    "Create a 100-word story",
    "To generate and present engaging stories"
  ],
  "input_modes": ["text", "text/plain"],
  "output_modes": ["text", "text/plain", "application/json"],
  "metadata": {
    "prompt_id": 71,
    "flow_id": 0,
    "flow_name": "story creation",
    "persona": "creator",
    "autonomous": true,
    "has_fallback": true,
    "recipe_steps": 4
  }
}
```

---

## Integration Points

### 1. Flask App (`hart_intelligence_entry.py`)

**Old (Hardcoded):**
```python
# Register hardcoded agents
from integrations.google_a2a import register_all_agents

register_all_agents()  # Registered: Assistant, Helper, Executor, Verify
```

**New (Dynamic):**
```python
# Register dynamic agents from prompts/
from integrations.google_a2a import register_all_dynamic_agents

num_registered = register_all_dynamic_agents()
# Discovers and registers ALL agents in prompts/ directory
# Number registered depends on how many recipe JSONs exist
```

### 2. A2A Endpoints

**Pattern:** `/a2a/{agent_id}/*`

**Examples:**
```
GET /a2a/71_0/.well-known/agent.json    # Story creator agent card
POST /a2a/71_0/jsonrpc                   # Execute story creation task

GET /a2a/8888_0/.well-known/agent.json  # Data analysis agent card
POST /a2a/8888_0/jsonrpc                 # Execute data analysis task
```

### 3. Agent Discovery API

```python
from integrations.google_a2a import get_registered_agent_info, list_available_agents

# Get statistics
info = get_registered_agent_info()
# Returns:
# {
#   "total_agents": 5,
#   "by_prompt": {"71": ["71_0", "71_1"], "8888": ["8888_0"]},
#   "by_persona": {"creator": ["71_0"], "data_analyst": ["8888_0"]},
#   "autonomous_agents": ["71_0", "8888_0"],
#   ...
# }

# Print formatted list
list_available_agents()
# Shows: Agent ID | Persona | Status | Autonomous | Has Fallback | Steps
```

---

## MCP Integration

### User-Configured MCP Servers

**Location:** `integrations/mcp/mcp_servers.json`

**User provides their own MCP servers:**
```json
{
  "servers": [
    {
      "name": "my_custom_server",
      "url": "http://localhost:9000",
      "api_key": "optional_key",
      "enabled": true
    }
  ]
}
```

**Loaded at startup:**
```python
from integrations.mcp import load_user_mcp_servers

num_servers = load_user_mcp_servers()
# User's MCP servers automatically available to all agents
```

---

## Testing

### Test Dynamic Agent Discovery

```bash
python test_dynamic_agents.py
```

**Output:**
```
DYNAMIC AGENT DISCOVERY TEST
================================================================================

Scanning prompts/ directory for recipe JSONs...

[OK] Discovered 5 trained agents

AGENT DETAILS
--------------------------------------------------------------------------------

Agent ID: 71_0
  Prompt: 71
  Flow: 0 (story creation)
  Persona: creator
  Status: done
  Action: Create a 100-word story...
  Recipe Steps: 4
  Autonomous: yes
  Has Fallback: yes
  Skills: 5
    1. creator_story_creation: Create a 100-word story in dialogue format...
    2. step_1_none: Create a 100-word story in the following format...

[Prompt 71] Generic Story
--------------------------------------------------------------------------------
  ✓ 71_0           | Persona: creator        | ⚡ | 🔄 | Steps: 4

Total: 5 agents
Legend: ✓=Done ○=Pending ⚡=Autonomous 👤=User-Interactive 🔄=Has-Fallback
```

---

## Benefits

### Before (Hardcoded)

```python
# HARDCODED agents
ASSISTANT_SKILLS = [...]
HELPER_SKILLS = [...]
EXECUTOR_SKILLS = [...]
VERIFY_SKILLS = [...]

register_agent("assistant", ..., ASSISTANT_SKILLS, ...)
register_agent("helper", ..., HELPER_SKILLS, ...)
register_agent("executor", ..., EXECUTOR_SKILLS, ...)
register_agent("verify", ..., VERIFY_SKILLS, ...)

# Result: 4 hardcoded agents, no flexibility
```

### After (Dynamic)

```python
# ZERO hardcoded agents!
num_registered = register_all_dynamic_agents()

# Result: N agents dynamically discovered
# - User creates prompt → agent automatically available
# - Each flow becomes a trained specialist
# - Full A2A protocol compliance
# - Agents evolve as recipes are created
```

### Advantages

1. **🎯 Specialization** - Each agent is a trained specialist for specific tasks
2. **📈 Scalability** - Unlimited agents (limited only by prompts created)
3. **🔄 Evolution** - Agents improve as recipes are refined
4. **🚀 Zero Config** - No code changes needed to add new agents
5. **🌐 A2A Compatible** - Every agent gets full A2A protocol support
6. **👤 User Control** - Users control MCP servers, agents created from their prompts
7. **📊 Transparency** - Clear mapping from recipe JSON → agent capabilities

---

## API Reference

### Discovery Functions

```python
# Get discovery instance
discovery = get_dynamic_discovery()

# Discover all agents
num_agents = discovery.discover_all_agents()

# Get specific agent
agent = discovery.get_agent_by_id("71_0")

# Get agent skills
skills = discovery.get_agent_skills(agent)

# Get agent description
desc = discovery.get_agent_description(agent)
```

### Registration Functions

```python
# Register all dynamic agents with A2A
num_registered = register_all_dynamic_agents()

# Get registration info
info = get_registered_agent_info()

# List all agents
list_available_agents()
```

### Execution Functions

```python
# Get executor
executor = get_dynamic_executor()

# Execute agent task
result = await executor.execute_agent_task(
    agent_id="71_0",
    message="Create a story about space exploration",
    context_id="context_123"
)
```

---

## Migration Guide

### Old Code (Deprecated)

```python
# DON'T USE - Legacy hardcoded agents
from integrations.google_a2a import register_all_agents_legacy
from integrations.google_a2a import assistant_executor, helper_executor

register_all_agents_legacy()  # Only registers 4 hardcoded agents
```

### New Code (Recommended)

```python
# USE THIS - Dynamic agent discovery
from integrations.google_a2a import register_all_agents  # Alias to register_all_dynamic_agents

num_agents = register_all_agents()  # Discovers and registers ALL agents from prompts/
```

**Backward Compatibility:** Old functions still work but are deprecated.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                  LLM-langchain Chatbot System                   │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  User Creates Prompt → prompts/{prompt_id}.json            ││
│  └────────────┬───────────────────────────────────────────────┘│
│               ↓                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  System Executes Flows → Creates Recipe JSONs              ││
│  │  prompts/{prompt_id}_{flow_id}_recipe.json                 ││
│  └────────────┬───────────────────────────────────────────────┘│
│               ↓                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  Dynamic Agent Discovery                                    ││
│  │  - Scans prompts/ for *_*_recipe.json                      ││
│  │  - Extracts skills, capabilities, metadata                 ││
│  │  - Creates A2A Agent Cards                                 ││
│  └────────────┬───────────────────────────────────────────────┘│
│               ↓                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  Google A2A Protocol Registration                           ││
│  │  - Auto-registers all discovered agents                    ││
│  │  - Creates endpoints: /a2a/{agent_id}/*                    ││
│  │  - JSON-RPC 2.0 message handling                           ││
│  └────────────┬───────────────────────────────────────────────┘│
│               ↓                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  Agents Available via A2A Protocol                          ││
│  │  GET /a2a/{agent_id}/.well-known/agent.json               ││
│  │  POST /a2a/{agent_id}/jsonrpc                             ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  User-Configured MCP Servers                              │ │
│  │  integrations/mcp/mcp_servers.json                        │ │
│  │  - Users provide their own MCP servers                    │ │
│  │  - Loaded at startup, available to all agents             │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## Status

✅ **Fully Implemented**
- ✅ Dynamic agent discovery from recipe JSONs
- ✅ Automatic A2A registration
- ✅ Flow-level agent support
- ✅ User-configured MCP integration
- ✅ Zero hardcoded agents
- ✅ Backward compatibility maintained

⏳ **Future Enhancements**
- Support for action-level agents (`{prompt_id}_{flow_id}_{action_id}.json`)
- Agent versioning and updates
- Multi-tenant agent isolation
- Agent performance metrics

---

**Architecture By:** Claude Code
**Date:** 2025-10-23
**Version:** 2.0.0 (Dynamic Architecture)
