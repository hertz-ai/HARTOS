# HART SDK Quick Start

The HART SDK provides a Python API for building apps that run on HART OS.
Apps declare their needs declaratively; the OS provides AI, events, config,
and environment management.

## Install

```bash
pip install hart-os[sdk]
```

Or import directly from source:

```python
from hart_sdk import HartApp, ai, events, config, environments
```

## HartApp Builder

Fluent API for declaring an app manifest:

```python
from hart_sdk import HartApp

app = (HartApp('translator', version='1.0.0')
    .needs_ai('llm', min_accuracy=0.7)
    .needs_ai('tts', required=False)
    .permissions(['network', 'audio'])
    .group('productivity')
    .tags(['translation', 'language'])
    .manifest(type='nunba_panel', entry={'route': '/panels/translator'})
    .register())
```

`register()` validates via ManifestValidator and adds to AppRegistry.

## ai Client

Interact with HART OS AI capabilities:

```python
from hart_sdk import ai

# Inference
result = ai.infer('Translate to French: Hello world', model_type='llm')

# List available models
models = ai.list_models()

# Declare a capability need and resolve it
resolved = ai.capability('llm', min_accuracy=0.8)
# -> ResolvedCapability(model_id='qwen3.5-4b-local', backend='local', ...)

# Check if capabilities can be satisfied
feasible = ai.can_satisfy([
    {'type': 'vision', 'local_only': True},
    {'type': 'tts', 'required': False},
])
```

## events Client

Pub/sub via the platform EventBus:

```python
from hart_sdk import events

# Emit an event
events.emit('my_app.task_done', {'result': 'success'})

# Subscribe
def on_theme_change(topic, data):
    print(f"Theme changed: {data}")

events.on('theme.changed', on_theme_change)

# One-shot subscription
events.once('app.registered', lambda t, d: print(d))

# Unsubscribe
events.off('theme.changed', on_theme_change)
```

## config Client

Read/write platform configuration:

```python
from hart_sdk import config

# Read (3-layer: env > override > DB > default)
scale = config.get('display.scale', default=1.0)

# Write (sets override layer)
config.set('display.scale', 1.5)

# React to changes
config.on_change('display.scale', lambda key, old, new: rescale_ui(new))
```

## environments Client

Create scoped execution environments for agent workloads:

```python
from hart_sdk import environments

# Create
env = environments.create('research',
    allowed_tools=['web_search', 'read_file'],
    denied_tools=['shell_exec'],
    max_cost_spark=50.0,
    model_policy='local_preferred')

# Use
if env.check_tool('web_search'):
    result = env.infer('Summarize recent papers on RLHF')
    env.record_cost(result.get('cost', 0))

# List
all_envs = environments.list_all()

# Destroy
environments.destroy(env.env_id)
```

## Platform Detection

Detect the host environment:

```python
from hart_sdk import detect_platform

info = detect_platform()
# {
#   'arch': 'x86_64',
#   'os': 'hart-os',        # or 'linux', 'darwin', 'win32'
#   'gpu': {'name': 'RTX 4090', 'vram_gb': 24},
#   'capabilities': ['llm', 'tts', 'vision'],
# }
```

## Graceful Degradation

The SDK works outside HART OS. When the platform is not available:
- `ai.infer()` returns `{'error': 'platform not available'}`
- `events.emit()` is a no-op
- `config.get()` returns the provided default
- `detect_platform()` returns host OS info with empty capabilities

This allows developing and testing apps on any machine.

## Full Example: Translator App

```python
from hart_sdk import HartApp, ai, events

# 1. Declare the app
app = (HartApp('translator', version='1.0.0')
    .needs_ai('llm', min_accuracy=0.7)
    .needs_ai('tts', required=False)
    .permissions(['network', 'audio'])
    .manifest(type='nunba_panel', entry={'route': '/panels/translator'})
    .register())

# 2. Handle translation requests
def translate(text, target_lang):
    result = ai.infer(
        f'Translate to {target_lang}: {text}',
        model_type='llm')

    if 'error' in result:
        return {'error': result['error']}

    translation = result.get('response', '')
    events.emit('translator.completed', {
        'source': text,
        'target': target_lang,
        'result': translation,
    })
    return {'translation': translation}

# 3. Optional: speak the translation
def speak(text):
    resolved = ai.capability('tts')
    if resolved.available:
        ai.infer(text, model_type='tts')
```

## See Also

- [user-journey.md](user-journey.md) -- End-to-end developer journey (API key → app → deploy)
- [extensions.md](extensions.md) -- Writing HART OS extensions
- [patterns.md](patterns.md) -- Code patterns
- [../features/ai-capabilities.md](../features/ai-capabilities.md) -- AI capability system
- [../features/agent-environments.md](../features/agent-environments.md) -- Agent environments
