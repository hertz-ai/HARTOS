# Developer User Journey

End-to-end guide: from zero to shipping an AI-native app on HART OS.

---

## 1. Get a Free API Key

HART OS provides a **free multimodal API** — no credit card, no gatekeeping.

```bash
# Create an account on the social platform
curl -X POST http://localhost:6777/api/social/register \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "email": "alice@example.com", "password": "secure123"}'

# Login to get a JWT token
curl -X POST http://localhost:6777/api/social/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "secure123"}'
# → {"token": "eyJ..."}

# Create a free API key
curl -X POST http://localhost:6777/api/v1/intelligence/keys \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{"name": "my-first-key", "tier": "free"}'
# → {"api_key": {"raw_key": "abc123...", "tier": "free", ...}}
```

Save your `raw_key` — it's shown once and cannot be retrieved later.

### Free Tier Limits

| Tier | Requests/Day | Monthly Quota | Cost/1K Tokens |
|------|-------------|---------------|----------------|
| **free** | 100 | 3,000 | $0 |
| starter | 1,000 | 30,000 | $0.50 |
| pro | 10,000 | 300,000 | $0.30 |
| enterprise | 100,000 | 10,000,000 | $0.20 |

---

## 2. Make Your First API Call

### Chat (Text Intelligence)

```bash
curl -X POST http://localhost:6777/api/v1/intelligence/chat \
  -H "X-API-Key: abc123..." \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain quantum computing in one paragraph"}'
```

### Document Analysis

```bash
curl -X POST http://localhost:6777/api/v1/intelligence/analyze \
  -H "X-API-Key: abc123..." \
  -H "Content-Type: application/json" \
  -d '{"document": "Revenue grew 40% YoY...", "question": "What are the key trends?"}'
```

### Media Generation (Image/Audio/Video)

```bash
curl -X POST http://localhost:6777/api/v1/intelligence/generate \
  -H "X-API-Key: abc123..." \
  -H "Content-Type: application/json" \
  -d '{"modality": "image", "prompt": "A futuristic city at sunset"}'
```

### HiveMind (Collective Knowledge)

```bash
curl http://localhost:6777/api/v1/intelligence/hivemind?query=best+practices+for+RAG \
  -H "X-API-Key: abc123..."
```

### Check Usage

```bash
curl http://localhost:6777/api/v1/intelligence/usage?days=7 \
  -H "X-API-Key: abc123..."
```

---

## 3. OpenAI-Compatible Gateway

If your app already uses the OpenAI SDK, point it at HART OS with zero code changes:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:6777/v1",
    api_key="not-needed",  # uses Kong consumer auth
)

response = client.chat.completions.create(
    model="hevolve",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

Token usage is automatically metered through the Kong gateway.

---

## 4. Build an App with the HART SDK

The SDK is the native way to build on HART OS — like Android SDK but for an AI-native OS.

```python
from hart_sdk import HartApp, ai, events, config

# 1. Declare your app
app = (HartApp('my-translator', version='1.0.0')
    .needs_ai('llm', min_accuracy=0.7)
    .needs_ai('tts', required=False)
    .permissions(['network', 'audio'])
    .manifest(type='nunba_panel', entry={'route': '/panels/translator'})
    .register())

# 2. Use AI inference
result = ai.infer('Translate to French: Hello world', model_type='llm')
print(result)  # {'response': 'Bonjour le monde', ...}

# 3. Emit events for other apps to react to
events.emit('translator.completed', {
    'source': 'Hello world',
    'target': 'fr',
    'result': 'Bonjour le monde',
})

# 4. Read platform config
theme = config.get('display.theme', default='dark')
```

The SDK works outside HART OS too — `ai.infer()` returns `{'error': 'platform not available'}` gracefully, so you can develop and test on any machine.

---

## 5. Add Multimodal Capabilities

### Text-to-Speech

```python
result = ai.infer('Welcome to HART OS', model_type='tts')
# Output: audio file path
```

### Vision (Image Analysis)

```python
result = ai.infer('Describe this image', model_type='vision',
                   options={'image_path': '/path/to/photo.jpg'})
```

### Speech-to-Text

```python
result = ai.infer('/path/to/audio.wav', model_type='stt')
# Output: transcribed text
```

### Check Available Capabilities

```python
from hart_sdk import ai, detect_platform

# What can this node do?
info = detect_platform()
print(info['capabilities'])  # ['llm', 'tts', 'vision', 'stt']

# Can we satisfy our app's needs?
feasible = ai.can_satisfy([
    ai.capability('llm', min_accuracy=0.7),
    ai.capability('tts', required=False),
])
```

---

## 6. Subscribe to Events

React to platform and app events:

```python
from hart_sdk import events

# Theme changes
events.on('theme.changed', lambda t, d: print(f'New theme: {d}'))

# Inference completed (by any app)
events.on('inference.completed', lambda t, d: log_usage(d))

# One-shot: wait for a specific event
events.once('app.registered', lambda t, d: print(f'App registered: {d}'))
```

---

## 7. Write an Extension

For deeper platform integration, write an extension:

```python
from core.platform.extensions import Extension
from core.platform.app_manifest import AppManifest

class WeatherExtension(Extension):
    @property
    def manifest(self):
        return AppManifest(
            id='weather-ext',
            name='Weather',
            type='extension',
            version='1.0.0',
            entry={'module': 'extensions.weather'},
        )

    def on_load(self, registry, config):
        registry.register('weather', lambda: self)

    def on_enable(self):
        pass  # Start polling weather API

    def on_disable(self):
        pass  # Stop polling
```

Place it in `extensions/weather.py` — it's auto-discovered at boot.

See [extensions.md](extensions.md) for the full extension lifecycle and security sandbox.

---

## 8. Use the CLI

The `hart` CLI gives you access to everything from the terminal:

```bash
# AI chat
hart chat "What is the meaning of life?"

# Headless task execution (great for scripts and CI)
hart -p "Summarize the README in this repo"

# Agent management
hart agent list
hart agent create --name "research-bot"

# Check system status
hart status

# Repository map (aider-powered)
hart repomap
```

---

## 9. Deploy

### As a Compute Provider

Join the network and earn Spark by contributing compute:

```bash
# Register as a provider
curl -X PUT http://localhost:6777/api/settings/provider/join \
  -H "Content-Type: application/json" \
  -d '{"node_name": "my-node", "node_tier": "flat"}'

# Your node now participates in the hive — tasks are dispatched to you
# automatically. Revenue split: 90% to you, 9% infra, 1% central.
```

### As a HART OS Instance

```bash
# Docker
scripts/start_docker.sh

# NixOS (native)
sudo nixos-rebuild switch --flake .#hart-os

# Systemd service (Linux)
sudo systemctl enable hart-os
sudo systemctl start hart-os
```

---

## 10. Monitor Usage and Revenue

```bash
# API usage stats
curl http://localhost:6777/api/v1/intelligence/usage?days=30 \
  -H "X-API-Key: abc123..."

# Gateway metering (if running Kong)
curl http://localhost:6777/api/gateway/metering

# List your API keys
curl http://localhost:6777/api/v1/intelligence/keys \
  -H "Authorization: Bearer eyJ..."
```

---

## API Endpoint Reference

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/v1/intelligence/chat` | POST | API Key | Text chat inference |
| `/api/v1/intelligence/analyze` | POST | API Key | Document analysis |
| `/api/v1/intelligence/generate` | POST | API Key | Media generation (image/audio/video) |
| `/api/v1/intelligence/hivemind` | GET | API Key | Collective knowledge query |
| `/api/v1/intelligence/usage` | GET | API Key | Usage statistics |
| `/api/v1/intelligence/keys` | POST | JWT | Create API key |
| `/api/v1/intelligence/keys` | GET | JWT | List API keys |
| `/api/v1/intelligence/keys/<id>` | DELETE | JWT | Revoke API key |
| `/v1/chat/completions` | POST | Kong | OpenAI-compatible proxy |
| `/chat` | POST | None | Core agent endpoint |

---

## See Also

- [SDK Reference](sdk.md) — Full SDK API documentation
- [Extensions Guide](extensions.md) — Writing platform extensions
- [Architecture](architecture.md) — System architecture overview
- [Security](security.md) — Security model and sandbox
- [Patterns](patterns.md) — Code patterns and conventions
