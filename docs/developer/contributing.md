# Contributing Guide

## Development Setup

### Prerequisites

- Python 3.10 (pydantic 1.10.9 is incompatible with 3.12+)
- Git

### Setup

```bash
git clone <repo-url> && cd HARTOS
python3.10 -m venv venv310
source venv310/Scripts/activate   # Windows: venv310\Scripts\activate.bat
pip install -r requirements.txt
```

### Configuration

Create `.env` in the project root:

```
OPENAI_API_KEY=your-key
GROQ_API_KEY=your-key
LANGCHAIN_API_KEY=your-key
```

Create `config.json` with API keys for: OPENAI, GROQ, GOOGLE_CSE_ID, GOOGLE_API_KEY, NEWS_API_KEY, SERPAPI_API_KEY.

### Running

```bash
python langchain_gpt_api.py    # Flask server on port 6777
```

## Code Style

### DRY Principle

Do not reinvent the wheel. Before writing new code, check if the functionality already exists:

- Revenue queries: use `query_revenue_streams()` from `revenue_aggregator.py`
- GPU detection: use `vram_manager.detect_gpu()`
- CUDA cache: use `vram_manager.clear_cuda_cache()`
- Notifications: use `NotificationService.create()`
- DB sessions: use `db_session()` context manager
- Error handling: use `@_json_endpoint` decorator
- Currency operations: use `ResonanceService`

### Singleton Pattern

For manager classes, use module-level `_instance = None` + `get_*()` function:

```python
_instance = None

def get_my_manager():
    global _instance
    if _instance is None:
        _instance = MyManager()
    return _instance
```

### Imports

Use `try`/`except ImportError` for optional dependencies. Never crash on missing imports for non-critical features.

## PR Process

1. Create a feature branch from `main`
2. Write tests for new functionality
3. Run `pytest tests/unit/ -v --noconftest` and verify no new failures
4. Ensure code follows existing patterns (see [patterns.md](patterns.md))
5. Submit PR with clear description of changes

## Security Guidelines

### Absolute Rules

1. **NEVER** read, display, or log the master private key
2. **NEVER** call `get_master_private_key()` or `sign_child_certificate()`
3. **NEVER** modify `MASTER_PUBLIC_KEY_HEX` in `security/master_key.py`
4. **NEVER** modify or weaken the `HiveCircuitBreaker`
5. **NEVER** modify `_FrozenValues` or module-level `__setattr__` guards

### Best Practices

- Run `GuardrailEnforcer.before_dispatch()` on all user prompts before execution
- Use `secret_redactor.redact_secrets()` on any user-provided text
- Validate `prompt_id` format: alphanumeric, `_`, `-` only
- Rate limit all public endpoints
- Use `db_session()` to avoid dangling database connections

## Testing

See [testing.md](testing.md) for detailed test instructions.

## See Also

- [architecture.md](architecture.md) -- System architecture
- [patterns.md](patterns.md) -- Code patterns
- [security.md](security.md) -- Security model
