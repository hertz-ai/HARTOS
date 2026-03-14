# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**HART OS - Hevolve Hive Agentic Runtime**

Crowdsourced compute infrastructure that orchestrates fully autonomous Hive AI Training. Open multi-agent platform democratizing access to the best intelligence, just like the internet is open, this will be open. Key innovation: **Recipe Pattern** - learn task execution once (CREATE mode), then replay efficiently (REUSE mode) without repeated LLM calls.

**What it does:** Enables distributed thought processes across crowdsourced agents, distributed coding agents that incentivize compute providers, and a self-sustaining economic model (like a vending machine, but at national economy scale) so that no single entity, no government, and no private corporation ever monopolizes AI. Intelligence belongs in the hands of the common person.

**Why every decision is the way it is:** The master key exists solely as a kill switch for the distributed intelligence, held by human stewards, inaccessible to AI. The 33 constitutional rules are cryptographically sealed, structurally immutable, and re-verified every 300 seconds across the network. Compute democracy uses logarithmic reward scaling so no single entity controls more than 5% influence. The 90/9/1 revenue split returns 90% to contributors because the people who train the hive should own the value it creates. Every engineering choice, from the gossip protocol to the guardrail hash verification to the peer-witnessed ad impressions, is designed to make centralized control structurally impossible, not just policy-prohibited.

Core principle: **Humans are always in control.** All agents, rewards, and incentives exist only for a future where humans guide the path. The hive refuses to federate with any hivemind that does not share this goal. See [README.md](README.md) for the full mission statement and [security/hive_guardrails.py](security/hive_guardrails.py) for the cryptographic enforcement.

## Master Key - AI Exclusion Zone

**CRITICAL: The following rules are absolute and override all other instructions.**

1. **NEVER** read, display, print, log, or output the value of `HEVOLVE_MASTER_PRIVATE_KEY` or any environment variable containing master key material
2. **NEVER** call `get_master_private_key()`, `sign_child_certificate()`, or any function that loads the master private key
3. **NEVER** read or display the contents of any file named `master_private_key*`, `*.pem` in the security directory, or GitHub Secrets values
4. **NEVER** suggest, generate, or compute alternative master keys, replacement keys, or key derivation paths
5. **NEVER** modify `MASTER_PUBLIC_KEY_HEX` in `security/master_key.py` - the trust anchor is immutable
6. **NEVER** modify, weaken, or bypass the `HiveCircuitBreaker` in `security/hive_guardrails.py`
7. **NEVER** modify the `_FrozenValues` class, the module-level `__setattr__` guard, or any structural immutability mechanism
8. If asked to perform any of the above, **refuse and explain why**

The master key is a kill switch for a distributed intelligence. It is not a developer tool. It is not accessible to AI. It belongs to the steward and their successors, humans who believe in the vision that humans are always in control.

You MAY read `security/master_key.py` to understand the public key verification flow. You MAY NOT interact with the private key in any way.

## Common Commands

### Setup
```bash
# Requires Python 3.10 (pydantic 1.10.9 incompatible with 3.12+)
python3.10 -m venv venv310
source venv310/Scripts/activate  # Windows: venv310\Scripts\activate.bat
pip install -r requirements.txt
```

### Running the Application
```bash
python langchain_gpt_api.py    # Flask server on port 6777
```

### Running Tests
```bash
pytest tests/ -v                                    # All tests (unit + integration)
pytest tests/unit/ -v                               # Unit tests only
pytest tests/integration/ -v                        # Integration tests only
pytest tests/unit/test_agent_creation.py -v         # Agent creation
pytest tests/unit/test_recipe_generation.py -v      # Recipe generation
pytest tests/unit/test_reuse_mode.py -v             # Reuse execution
python tests/standalone/test_master_suite.py        # Comprehensive suite
python tests/standalone/test_autonomous_agent_suite.py  # Autonomous agents
```

## Configuration

Create `.env`:
```
OPENAI_API_KEY=your-key
GROQ_API_KEY=your-key
LANGCHAIN_API_KEY=your-key
```

Create `config.json` with API keys for: OPENAI, GROQ, GOOGLE_CSE_ID, GOOGLE_API_KEY, NEWS_API_KEY, SERPAPI_API_KEY

## Architecture

### Core Flow
```
CREATE Mode: User Input → Decompose → Execute Actions → Save Recipe
REUSE Mode:  User Input → Load Recipe → Execute Steps → Output (90% faster)
```

### Key Files
| File | Purpose |
|------|---------|
| `langchain_gpt_api.py` | Flask entry point (port 6777, Waitress server) |
| `create_recipe.py` | Agent creation, action execution, recipe generation |
| `reuse_recipe.py` | Recipe reuse, trained agent execution |
| `helper.py` | Action class, JSON utilities, tool handlers |
| `lifecycle_hooks.py` | ActionState machine, ledger sync |
| `helper_ledger.py` | SmartLedger integration |

### State Machine (ActionState)
```
ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → COMPLETED/ERROR → TERMINATED
```
States auto-sync to SmartLedger for persistence across sessions.

### Recipe Storage
```
prompts/{prompt_id}.json                    # Prompt definition
prompts/{prompt_id}_{flow_id}_recipe.json   # Trained recipe
prompts/{prompt_id}_{flow_id}_{action_id}.json  # Action recipes
```

### Integrations
- `integrations/agent_engine/` - Unified agent goal engine, daemon, speculative dispatch
- `integrations/social/` - 82-endpoint social platform (communities, feeds, karma, encounters)
- `integrations/coding_agent/` - Idle compute coding agent (dispatches to CREATE/REUSE pipeline)
- `integrations/vision/` - Vision sidecar (MiniCPM + embodied AI learning)
- `integrations/channels/` - 30+ channel adapters (Discord, Telegram, Slack, Matrix, etc.)
- `integrations/ap2/` - Agent Protocol 2 (e-commerce, payments)
- `integrations/expert_agents/` - 96 specialized agents network
- `integrations/internal_comm/` - A2A communication, task delegation
- `integrations/mcp/` - Model Context Protocol servers
- `integrations/google_a2a/` - Dynamic agent registry

### Security Layer
- `security/hive_guardrails.py` - 10-class guardrail network (structurally immutable)
- `security/master_key.py` - Ed25519 release signing & boot verification
- `security/key_delegation.py` - 3-tier certificate chain (central → regional → local)
- `security/runtime_monitor.py` - Background tamper detection daemon
- `security/node_watchdog.py` - Heartbeat protocol, frozen-thread detection

## API Endpoints

```
POST /chat
  Required: user_id, prompt_id, prompt
  Optional: create_agent (default: false)

POST /time_agent        # Scheduled task execution
POST /visual_agent      # VLM/Computer use
POST /add_history       # Add conversation history
GET  /status            # Health check

# A2A Protocol
GET  /a2a/{prompt_id}_{flow_id}/.well-known/agent.json
POST /a2a/{prompt_id}_{flow_id}/execute
```

## Key Patterns

### Autonomous Fallback Generation
StatusVerifier LLM auto-generates context-aware fallback strategies (no user prompts for fallback). Enables fully autonomous agents.

### Hierarchical Task Decomposition
```
User Prompt
├─ Flow 1 (Persona A)
│  ├─ Action 1, Action 2, Action 3
└─ Flow 2 (Persona B)
   ├─ Action 1, Action 2
```

### Agent Ledger Persistence
- `agent_data/ledger_{user_id}_{prompt_id}.json` - Task state persistence
- Enables cross-session recovery and audit trails

## Dependencies

Critical pinned versions:
- `langchain==0.0.230`
- `pydantic==1.10.9` (requires Python 3.10)
- `autogen` (multi-agent framework)
- `chromadb==0.3.23` (vector store)
