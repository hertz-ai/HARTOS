# AI Capability Intents

**File:** `core/platform/ai_capabilities.py`

## Overview

Apps declare what AI they need declaratively -- like Android Intents but for AI.
The OS resolves to available backends. No app bundles llama.cpp or ships its own
model loader. This is the abstraction that makes HART OS fundamentally different
from Windows/Linux/macOS.

## Core Types

### AICapabilityType (enum)

Seven capability types the OS can provide:

| Value | Description |
|-------|-------------|
| `llm` | Language model inference |
| `vision` | Image/video understanding |
| `tts` | Text-to-speech synthesis |
| `stt` | Speech-to-text transcription |
| `image_gen` | Image generation |
| `embedding` | Text/code embedding vectors |
| `code` | Code generation/completion |

### AICapability (dataclass)

A single AI capability an app needs. Declarative -- says WHAT, not HOW.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | str | (required) | AICapabilityType value |
| `required` | bool | True | False = nice-to-have, app works without it |
| `local_only` | bool | False | Never route to cloud |
| `min_accuracy` | float | 0.0 | 0.0-1.0 quality threshold |
| `max_latency_ms` | float | 0.0 | 0 = no constraint |
| `max_cost_spark` | float | 0.0 | 0 = no constraint (free models only) |
| `options` | dict | {} | Backend-specific hints |

Serializable via `to_dict()` / `from_dict()` for AppManifest storage.

### ResolvedCapability (dataclass)

Result of resolving an AICapability to a concrete backend. Contains
`model_id`, `backend` (local/cloud/mesh), `is_local`, estimated latency/cost,
and `available` flag with `reason` if unavailable.

## CapabilityRouter

Maps AICapability constraints to `ModelRegistry.get_model_by_policy()`:

- `local_only` or `max_cost_spark == 0` --> policy=`local_only`
- `min_accuracy >= 0.7` --> policy=`any` (allow cloud for quality)
- Otherwise --> policy=`local_preferred`

Additional checks:
- Latency constraint: rejects models exceeding `max_latency_ms`
- Cost constraint: rejects models exceeding `max_cost_spark`
- VRAM check: logs warning if GPU memory is low for local models

Methods:
- `resolve(capability)` -- resolve one capability
- `resolve_all(capabilities)` -- resolve a list
- `can_satisfy(capabilities)` -- True if all required capabilities are satisfiable

Emits `capability.resolved` and `capability.unavailable` events via EventBus.

## Example: Translator App

```python
from core.platform.app_manifest import AppManifest
from core.platform.ai_capabilities import AICapability

manifest = AppManifest(
    id='translator',
    name='Translator',
    type='nunba_panel',
    version='1.0.0',
    ai_capabilities=[
        AICapability(type='llm', min_accuracy=0.7).to_dict(),
        AICapability(type='tts', required=False).to_dict(),
    ],
    entry={'route': '/panels/translator'},
)
```

The OS resolves `llm` to the best available model (local Qwen, cloud GPT, mesh
peer) and `tts` to Pocket TTS or LuxTTS. If TTS is unavailable, the app still
works -- it declared `required=False`.

## SDK Usage

```python
from hart_sdk import ai

# Check what's available
models = ai.list_models()

# Declare a capability need
resolved = ai.capability('llm', min_accuracy=0.8)
if resolved.available:
    result = ai.infer('Translate to French: Hello world')

# Check feasibility without resolving
can_do = ai.can_satisfy([
    AICapability(type='vision', local_only=True),
])
```

## See Also

- [platform-layer.md](platform-layer.md) -- Core platform services
- [../developer/sdk.md](../developer/sdk.md) -- Full SDK reference
