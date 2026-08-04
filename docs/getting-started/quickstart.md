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

**No API key is required.** With none set, chat is served by a local model
through llama.cpp. That is the point: it works with the wifi off, and what you
type stays on the machine because there is nowhere else for it to go.

Local is the floor, not the ceiling. When a turn is beyond what the local model
should take, it can be handed whole to a peer whose model is bigger, rather than
answered badly. That path is opt-in per node (`HEVOLVE_HIVE_ADVERTISE=1` plus a
public endpoint), so on a network where nobody has opted in it simply stays
local. Anything that leaves the device is consent-gated, and the consent prompt
fans out to your own devices for an explicit yes.

Add a key only if you also want a cloud route. Create a `.env` file in the
project root:

```
OPENAI_API_KEY=your-openai-key      # optional
GROQ_API_KEY=your-groq-key          # optional
```

See [Configuration](configuration.md) for the full list of environment variables.

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

The server starts on `http://localhost:6777`. It runs on Hypercorn (ASGI), so
idle keep-alive and SSE clients do not each hold a worker thread; Waitress is
the fallback when the Hypercorn stack is unavailable, such as in a frozen
bundle missing the h2/wsproto chain.

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
