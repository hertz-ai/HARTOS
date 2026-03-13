# Runtime End-to-End Tests

Comprehensive end-to-end tests that run against actual Docker containers with real LLM integration.

## Overview

These tests validate the complete agent creation and reuse workflow by:
- Running the actual application in Docker
- Using the real LLM server (Qwen3-VL-2B-Instruct)
- Testing against mock external APIs (student API, action API, etc.)
- Verifying recipe creation and execution

## Prerequisites

### Required Services

1. **LLM Server** - Must be running on `localhost:8000`
   ```bash
   python -m vllm.entrypoints.openai.api_server \
     --model Qwen/Qwen3-VL-2B-Instruct \
     --port 8000
   ```

2. **Docker Desktop** - Must be installed and running

3. **Python Dependencies** (in container)
   - pytest
   - pytest-asyncio
   - pytest-mock
   - pytest-timeout
   - requests

## Quick Start

### Run All Tests

```bash
./run_runtime_tests.sh
```

### Run Specific Test File

```bash
./run_runtime_tests.sh tests/runtime_tests/test_e2e_agent_creation.py
```

### Run with Verbose Output

```bash
./run_runtime_tests.sh tests/runtime_tests/ -v
```

## Test Structure

```
tests/runtime_tests/
├── conftest_runtime.py          # Pytest fixtures for runtime tests
├── test_e2e_agent_creation.py   # Creation mode tests
├── test_e2e_reuse_mode.py       # Reuse mode tests
├── test_e2e_vlm_agent.py        # VLM agent tests
├── test_e2e_scheduled_tasks.py  # Scheduler tests
├── test_e2e_coding_agent.py     # Coding agent tests
├── mock_services/               # Mock service implementations
│   ├── mock_api_server.py       # Mock external APIs
│   └── mock_crossbar_server.py  # Mock pub/sub server
└── test_configs/                # Sample test configurations
    └── sample_coding_agent.json
```

## Test Categories

### 1. Agent Creation Tests (`test_e2e_agent_creation.py`)

Tests the complete CREATE mode workflow:

- **test_create_mode_full_workflow** ✅
  - Sends task to `/chat` endpoint
  - Waits for agent execution
  - Verifies recipe JSON generated
  - Validates recipe structure

- **test_agent_never_fails_creation** ✅
  - Tests edge cases (empty task, very long task, invalid characters)
  - Ensures system handles gracefully

- **test_action_state_transitions** ✅
  - Verifies proper state machine flow
  - ASSIGNED → IN_PROGRESS → ... → TERMINATED

- **test_recipe_json_generation** ✅
  - Validates recipe structure
  - Checks for generalized functions
  - Verifies dependencies

- **test_scheduled_task_creation** ✅
  - Tests cron/interval/date task creation
  - Validates scheduler configuration

### 2. Reuse Mode Tests (`test_e2e_reuse_mode.py`)

Tests the complete REUSE mode workflow:

- **test_reuse_mode_full_workflow** ✅
  - Creates recipe in CREATE mode
  - Executes same task in REUSE mode
  - Verifies recipe loaded and used

- **test_reuse_mode_faster_than_create** ✅
  - Measures execution time
  - Validates performance improvement

- **test_reuse_with_different_parameters** ✅
  - Tests recipe reusability
  - Validates generalization

- **test_output_consistency_create_vs_reuse** ✅
  - Compares outputs between modes
  - Ensures consistency

### 3. Recipe Loading Tests

- **test_load_recipe_from_file** ✅
- **test_handle_missing_recipe_file** ✅
- **test_handle_corrupted_recipe_file** ✅

## Docker Services

### Application Container (`app`)
- Runs the main Flask application
- Uses actual Dockerfile
- Port: 6777
- Network: host (to access LLM on localhost:8000)

### Redis
- Stores video frames and cache
- Port: 6379
- Image: redis:7-alpine

### Mock Crossbar (`mock-crossbar`)
- Simulates pub/sub messaging
- Port: 8088
- HTTP endpoints for testing

### Mock APIs (`mock-apis`)
- Simulates external dependencies:
  - Student API (port 9891)
  - Action API (port 9892)
  - autogen_response (port 9890)
  - txt2img (port 5459)

## Configuration

### Test Configuration

Edit `conftest_runtime.py` to change:
- Service URLs
- Timeouts
- Test user IDs

### Docker Configuration

Edit `docker-compose.test.yml` to change:
- Service versions
- Environment variables
- Network settings

## Running Tests Manually

### 1. Start Services

```bash
# Start all services except app
docker-compose -f docker-compose.test.yml up -d redis mock-crossbar mock-apis
```

### 2. Run Tests

```bash
# Run tests in app container
docker-compose -f docker-compose.test.yml run --rm app \
  pytest tests/runtime_tests/ -v
```

### 3. Cleanup

```bash
# Stop all services
docker-compose -f docker-compose.test.yml down --volumes
```

## Debugging

### View Service Logs

```bash
# All services
docker-compose -f docker-compose.test.yml logs

# Specific service
docker-compose -f docker-compose.test.yml logs app
docker-compose -f docker-compose.test.yml logs redis
```

### Interactive Shell

```bash
# Get shell in app container
docker-compose -f docker-compose.test.yml run --rm app /bin/bash

# Run tests manually
pytest tests/runtime_tests/test_e2e_agent_creation.py -v -s
```

### Check Mock API Calls

```bash
# Get messages sent to user
curl http://localhost:9890/autogen_response/messages

# Get statistics
curl http://localhost:9890/test/stats

# Reset mock data
curl -X POST http://localhost:9890/test/reset
```

## Test Data

### Sample Config JSON (Agent Definition)

```json
{
  "status": "pending",
  "name": "Test Agent",
  "personas": [
    {
      "name": "assistant",
      "description": "Helpful assistant"
    }
  ],
  "tools": ["execute_windows_command"],
  "flows": [
    {
      "flow_name": "Task flow",
      "persona": "assistant",
      "actions": [
        "Action 1",
        "Action 2"
      ],
      "sub_goal": "Complete task"
    }
  ],
  "goal": "Test goal",
  "prompt_id": 1001,
  "creator_user_id": 1001
}
```

### Sample Recipe JSON (Generated Output)

```json
{
  "status": "done",
  "action": "Create file",
  "persona": "Assistant",
  "action_id": 1,
  "recipe": [
    {
      "steps": "Create file using open()",
      "tool_name": "file_operations",
      "agent_to_perform_this_action": "Assistant"
    }
  ],
  "can_perform_without_user_input": "yes",
  "scheduled_tasks": []
}
```

## Troubleshooting

### Tests Timeout

**Symptom:** Tests hang or timeout

**Solutions:**
1. Check if LLM server is responding:
   ```bash
   curl http://localhost:8000/v1/models
   ```

2. Increase timeout in tests:
   ```python
   @pytest.mark.timeout(600)  # 10 minutes
   ```

3. Check Docker logs:
   ```bash
   docker-compose -f docker-compose.test.yml logs app
   ```

### Services Fail to Start

**Symptom:** "Services failed to start" error

**Solutions:**
1. Check if ports are available:
   ```bash
   netstat -an | grep -E '6777|6379|8088|9890'
   ```

2. Remove old containers:
   ```bash
   docker-compose -f docker-compose.test.yml down --volumes --remove-orphans
   ```

3. Rebuild containers:
   ```bash
   docker-compose -f docker-compose.test.yml build --no-cache
   ```

### LLM Server Not Found

**Symptom:** "LLM server not running" error

**Solutions:**
1. Start LLM server:
   ```bash
   python -m vllm.entrypoints.openai.api_server \
     --model Qwen/Qwen3-VL-2B-Instruct \
     --port 8000
   ```

2. Or use existing server endpoint:
   ```bash
   export LLM_BASE_URL=http://your-llm-server:8000/v1
   ```

### Recipe File Not Created

**Symptom:** Test fails with "Recipe file not created"

**Solutions:**
1. Check prompts directory permissions:
   ```bash
   ls -la prompts/
   ```

2. Check application logs:
   ```bash
   docker-compose -f docker-compose.test.yml logs app | grep -i recipe
   ```

3. Verify agent completed all actions:
   ```bash
   curl http://localhost:9890/autogen_response/messages
   ```

## Performance Benchmarks

Expected execution times (on typical hardware):

| Test Category | Duration | Notes |
|--------------|----------|-------|
| Agent Creation | 30-120s | Depends on LLM response time |
| Reuse Mode | 10-30s | Faster due to recipe usage |
| Recipe Loading | <5s | File I/O only |
| State Transitions | 20-60s | Validates complete flow |

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Runtime Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v2

      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.10

      - name: Start LLM Server
        run: |
          pip install vllm
          python -m vllm.entrypoints.openai.api_server \
            --model Qwen/Qwen3-VL-2B-Instruct \
            --port 8000 &
          sleep 30

      - name: Run Tests
        run: |
          chmod +x run_runtime_tests.sh
          ./run_runtime_tests.sh
```

## Contributing

When adding new tests:

1. Follow the naming convention: `test_e2e_*.py`
2. Use runtime fixtures from `conftest_runtime.py`
3. Clean up test data in fixture teardown
4. Add test description in docstring
5. Update this README with test description

## License

Same as main project

---

**For questions or issues, check:**
- Main README.md
- tests/README.md
- TEST_VALIDATION_REPORT.md
