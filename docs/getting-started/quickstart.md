# Quick Start

Get HART OS running in 5 minutes.

---

## Prerequisites

- **Python 3.10** (required -- pydantic 1.10.9 is incompatible with Python 3.12+)
- An OpenAI API key or Groq API key

---

## 1. Clone and Set Up

```bash
git clone https://github.com/hertz-ai/HARTOS.git
cd HARTOS

# Create virtual environment with Python 3.10
python3.10 -m venv venv310

# Activate (Linux/macOS)
source venv310/bin/activate

# Activate (Windows)
venv310\Scripts\activate.bat

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Configure Environment

Create a `.env` file in the project root:

```
OPENAI_API_KEY=your-openai-key
GROQ_API_KEY=your-groq-key
```

At minimum, one of these API keys is required. See [Configuration](configuration.md) for the full list of environment variables.

---

## 3. Start the Server

**Bare-metal:**
```bash
python hart_intelligence_entry.py
```

**Docker:**
```bash
scripts/start_docker.sh
```

The server starts on `http://localhost:6777` using Waitress as the production WSGI server.

---

## 4. Health Check

Verify the server is running (use `http://localhost:6777` if self-hosted):

```bash
curl https://hevolve.ai/status
```

Expected response:

```json
{"status": "ok"}
```

---

## 5. First API Call

Send a task to an agent:

```bash
curl -X POST https://hevolve.ai/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user1",
    "prompt_id": "demo1",
    "prompt": "Search for the latest news about AI agents"
  }'
```

This runs in **CREATE mode** -- the agent decomposes the task, executes each action, and saves a recipe for future reuse.

To create a dedicated agent for the task, add `"create_agent": true`:

```bash
curl -X POST https://hevolve.ai/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user1",
    "prompt_id": "demo1",
    "prompt": "Search for the latest news about AI agents",
    "create_agent": true
  }'
```

---

## What Happens Next

1. **CREATE mode**: The agent decomposes your prompt into flows and actions, executes them, and saves a recipe to `prompts/`.
2. **REUSE mode**: On subsequent calls with the same `prompt_id`, the saved recipe is replayed without repeated LLM calls -- up to 90% faster.
3. **Ledger**: Task state is persisted to `agent_data/ledger_{user_id}_{prompt_id}.json` for cross-session recovery.

---

## Next Steps

- [Full Installation Guide](installation.md) -- GPU setup, Docker, config.json
- [Deployment Modes](deployment-modes.md) -- flat, regional, central
- [Configuration Reference](configuration.md) -- all environment variables
- [Features Overview](../features/overview.md) -- what HART OS can do
- [Device Discovery & Pairing](../features/device-pairing.md) -- connect phones, IoT devices, and headless nodes to your mesh
