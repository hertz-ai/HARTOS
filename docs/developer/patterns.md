# Key Patterns

Patterns used consistently across the HART OS codebase.

## db_session() Context Manager

Always use `db_session()` for database access. Never use manual `get_db()`/`try`/`finally`/`close()`.

```python
from integrations.social.models import db_session

with db_session() as db:
    user = db.query(User).filter_by(id=user_id).first()
    user.name = "New Name"
    db.commit()
```

## @_json_endpoint Decorator

All API endpoints in `langchain_gpt_api.py` that handle errors uniformly use this decorator:

```python
def _json_endpoint(f):
    """Wrap a Flask view so unhandled exceptions return {'error': ...}, 500."""
    @wraps(f)
    def _wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return _wrapped

@app.route('/api/tools/status', methods=['GET'])
@_json_endpoint
def tools_status():
    return jsonify(runtime_tool_manager.get_all_status())
```

## Singleton Pattern

Module-level `_instance = None` with a `get_*()` accessor function. Used in 6+ agent_engine files.

```python
_instance = None

class MyManager:
    def __init__(self):
        self._data = {}

def get_my_manager():
    global _instance
    if _instance is None:
        _instance = MyManager()
    return _instance
```

## Thread-Local Data

Per-request context passed via thread-local storage (Waitress serves concurrent requests):

```python
thread_local_data.set_task_source(task_source)    # 'own' | 'hive' | 'idle'
thread_local_data.set_model_config_override(config)  # per-request model override
```

## NotificationService

Always use `NotificationService.create()` from `integrations/social/services.py`. Never construct `Notification()` directly.

```python
from integrations.social.services import NotificationService
NotificationService.create(db, user_id=target, type='mention',
                           message='You were mentioned', related_id=post_id)
```

## ResonanceService for Currency

Spark, Pulse, and XP are managed through `ResonanceService`:

```python
from integrations.social.services import ResonanceService
ResonanceService.award_spark(db, user_id, amount=100, reason='hosting_reward')
```

## Revenue Constants Import

The 90/9/1 split constants live in `revenue_aggregator.py` and are imported by other modules:

```python
from integrations.agent_engine.revenue_aggregator import (
    REVENUE_SPLIT_USERS,   # 0.90
    REVENUE_SPLIT_INFRA,   # 0.09
    REVENUE_SPLIT_CENTRAL, # 0.01
)
```

Modules like `ad_service.py`, `hosting_reward_service.py`, and `finance_tools.py` import these with `try`/`except` fallback to hardcoded values.

## GPU Detection

Single source of truth for GPU info:

```python
from integrations.service_tools.vram_manager import vram_manager
gpu = vram_manager.detect_gpu()
# Returns: {name, total_gb, free_gb, cuda_available}
```

Never call `torch.cuda.empty_cache()` directly; use `vram_manager.clear_cuda_cache()`.

## Revenue Query

Single source of truth for revenue data:

```python
from integrations.agent_engine.revenue_aggregator import query_revenue_streams
data = query_revenue_streams(db, period_days=30)
```

## ServiceRegistry Pattern

Register services as lazy singletons:

```python
from core.platform.registry import get_registry
registry = get_registry()
registry.register('my_service', lambda: MyService(), singleton=True)
svc = registry.get('my_service')  # Instantiated on first access
```

## EventBus Pattern

Emit events from anywhere:

```python
from core.platform.events import emit_event
emit_event('my.topic', {'key': 'value'})
```

Subscribe:

```python
bus = registry.get('event_bus')
bus.on('my.topic', lambda topic, data: handle(data))
```

## ManifestValidator Pattern

All validators return `(bool, errors)`:

```python
from core.platform.manifest_validator import ManifestValidator
valid, errors = ManifestValidator.validate(manifest)
if not valid:
    raise ValueError(f"Invalid: {'; '.join(errors)}")
```

## AI Capability Declaration

Apps declare AI needs, OS resolves:

```python
from hart_sdk import HartApp
app = HartApp('translator', version='1.0.0')
app.needs_ai('llm', min_accuracy=0.7)
app.needs_ai('tts', required=False)
```

## See Also

- [architecture.md](architecture.md) -- System architecture
- [contributing.md](contributing.md) -- DRY principle and code style
