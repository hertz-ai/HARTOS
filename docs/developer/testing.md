# Testing Guide

## Running Tests

### All Tests

```bash
pytest tests/ -v
```

### Unit Tests Only

```bash
pytest tests/unit/ -v
```

### Integration Tests Only

```bash
pytest tests/integration/ -v
```

### Specific Test Files

```bash
pytest tests/unit/test_agent_creation.py -v       # Agent creation
pytest tests/unit/test_recipe_generation.py -v     # Recipe generation
pytest tests/unit/test_reuse_mode.py -v            # Reuse execution
pytest tests/unit/test_federation_upgrade.py -v    # Federation upgrade
pytest tests/unit/test_model_lifecycle.py -v       # Model lifecycle
```

### Standalone Suites

```bash
python tests/standalone/test_master_suite.py           # Comprehensive suite
python tests/standalone/test_autonomous_agent_suite.py  # Autonomous agents
```

## Important Flags

### --noconftest

Use `--noconftest` for most test runs. The `TestMediaAgent` fixture in `test_social_models.py` can corrupt pytest's tempfile handle, causing cascading failures across 724+ tests.

```bash
pytest tests/unit/ -v --noconftest
```

### -p no:capture

Required for federation tests to avoid output capture conflicts:

```bash
pytest tests/unit/test_federation_upgrade.py -v -p no:capture
```

## Test Environment Notes

- **Python 3.10 required** for full compatibility (pydantic 1.10.9)
- Python 3.11 works but `autogen` is not installed, causing 9 test files to skip
- Pre-existing: ~70 failures across 27 files (not caused by recent changes)
- All 266 tests from the 6-workstream plan pass (41 new + 225 regression)

## Key Test Files

| File | Coverage |
|------|----------|
| `test_agent_creation.py` | CREATE mode, action decomposition |
| `test_recipe_generation.py` | Recipe save/load, JSON format |
| `test_reuse_mode.py` | REUSE mode, recipe replay |
| `test_federation_upgrade.py` | Federation protocol, peer sync |
| `test_model_lifecycle.py` | Model load/unload/offload |
| `test_social_models.py` | ORM models, db_session() |
| `test_master_suite.py` | Comprehensive end-to-end |

## Writing Tests

### Use db_session() for Database Tests

```python
from integrations.social.models import db_session

def test_create_user():
    with db_session() as db:
        user = User(username='test')
        db.add(user)
        db.commit()
        assert user.id is not None
```

### In-Memory Database

Set `HEVOLVE_DB_PATH=:memory:` for test isolation:

```python
import os
os.environ['HEVOLVE_DB_PATH'] = ':memory:'
```

### Mocking External Services

Mock API calls, not internal functions. Use `unittest.mock.patch` on HTTP endpoints:

```python
from unittest.mock import patch

@patch('requests.post')
def test_external_call(mock_post):
    mock_post.return_value.json.return_value = {'result': 'ok'}
    # Test code here
```

## Functional Tests

The `tests/functional/` suite validates core subsystems end-to-end with real
logic (no mocks on the code under test). Total: **233 tests**, runs in ~30 s.

```bash
pytest tests/functional/ --noconftest -q
```

| File | Tests | What it covers |
|------|------:|----------------|
| `test_message_bus_functional.py` | 8 | Pub/sub delivery, wildcard topics, unsubscribe, thread safety |
| `test_federation_functional.py` | 23 | 3-node convergence, HMAC sign/verify, stale-delta rejection, recipe channel, guardrail hash enforcement |
| `test_revenue_functional.py` | 6 | 90/9/1 split math, real SQLite Spark settlements, dashboard keys, env overrides |
| `test_vlm_loop_functional.py` | 17 | VLM control flow, action parsing, bbox handling, iteration budget, safety-gate stubs |
| `test_pipeline_lifecycle_functional.py` | 22 | ActionState machine transitions, recipe save/load round-trip, path-traversal guards, thread-safe state changes |
| `test_security_modules_functional.py` | 141 | Input sanitization (SQL, HTML, path), audit-log hash chain & tamper detection, action classifier (safe/destructive), DLP scan/redact (PII, credit card, IP), rate limiter, tool allowlist |
| `test_device_control_functional.py` | 16 | Channel-to-device routing via PeerLink, SAME_USER privacy gate, fleet-command fallback, embedded handler GPIO/serial detection |

These tests exercise the actual production code paths. External services (LLM
APIs, network peers) are stubbed at the boundary, but all internal logic runs
unmodified.

## See Also

- [contributing.md](contributing.md) -- PR process
- [architecture.md](architecture.md) -- Understanding what to test
