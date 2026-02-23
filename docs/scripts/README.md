# HevolveBot LangChain Agent - Scripts

Startup, testing, and debugging scripts for the HevolveBot multi-agent system.

## Quick Start

```bash
# Start server (recommended)
scripts/run.bat                    # Windows
scripts/run.sh                     # Linux/Mac

# Start server with PyCharm tracing
scripts/run_with_tracing.bat       # Windows
scripts/run_with_tracing.sh        # Linux/Mac
```

## Scripts Overview

### Startup Scripts

| Script | Purpose |
|--------|---------|
| `run.bat` / `run.sh` | **Recommended** - Start Flask API server on port 6777 |
| `run_with_tracing.bat` / `run_with_tracing.sh` | Start server with PyCharm socket tracing on port 5678 |

### Test Scripts

| Script | Purpose |
|--------|---------|
| `run_tests.bat` / `run_tests.sh` | Interactive test runner with suite selection |
| `run_e2e_tests.bat` / `run_e2e_tests.sh` | End-to-end tests, auto-starts server if needed |

## Server Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `http://localhost:6777/status` | GET | Health check |
| `http://localhost:6777/chat` | POST | Chat with agent (create/reuse) |
| `http://localhost:6777/time_agent` | POST | Scheduled task execution |
| `http://localhost:6777/visual_agent` | POST | VLM/Computer use agent |
| `http://localhost:6777/add_history` | POST | Add conversation history |

## Tracing

The `run_with_tracing` scripts enable PyCharm TrueFlow socket tracing:

- **Port**: 5678 (configurable via `PYCHARM_PLUGIN_TRACE_PORT`)
- **Host**: 127.0.0.1
- **Plugin path**: `.pycharm_plugin/runtime_injector`
- **Trace output**: `traces/` directory

To connect from PyCharm:
1. Run `run_with_tracing.bat`
2. In PyCharm, use the TrueFlow plugin "Attach to Server" on port 5678

You can also use the standalone tracing wrapper:
```bash
.pycharm_plugin/runtime_injector/enable_tracing.bat python your_script.py
```

## Test Suites Available

| # | Suite | Description |
|---|-------|-------------|
| 1 | All tests | Comprehensive - tests/ + channels/ |
| 2 | Channel regression | 91 HevolveBot integration tests |
| 3 | Master suite | test_master_suite.py |
| 4 | Autonomous agents | test_autonomous_agent_suite.py |
| 5 | Dynamic agents | test_dynamic_agents.py |
| 6 | Complex agent | test_complex_agent_comprehensive.py |
| 7 | Smoke test | Quick channel module imports only |
| 8 | Custom pattern | Enter your own pytest pattern |

## Environment Variables

### Required (via `.env` or `config.json`)

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API access |
| `GROQ_API_KEY` | Groq API access |
| `LANGCHAIN_API_KEY` | LangChain tracing |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `WAMP_URL` | `ws://azurekong.hertzai.com:8088/ws` | Crossbar WAMP router |
| `WAMP_REALM` | `realm1` | WAMP realm |
| `SIMPLEMEM_ENABLED` | `false` | Enable SimpleMem long-term memory |
| `SIMPLEMEM_API_KEY` | - | SimpleMem API key |
| `AGENT_LIGHTNING_ENABLED` | `false` | Enable Agent Lightning tracing |
| `PYCHARM_PLUGIN_TRACE_PORT` | `5678` | Socket trace port |

## Prerequisites

- Python 3.10 (pydantic 1.10.9 requires it)
- Virtual environment: `venv310/`
- Dependencies: `pip install -r requirements.txt`
