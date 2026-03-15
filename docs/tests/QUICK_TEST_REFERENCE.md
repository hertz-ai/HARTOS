# Quick Test Reference Card

## TL;DR - Run Tests Now

### ✅ Tests You Can Run Right Now (No LLM Required)

```bash
python run_manual_tests.py
```

**Result:** 8/23 tests passing - validates JSON, recipes, scheduler config, file I/O

---

### 🚀 Full Runtime Tests (Requires LLM Server)

**Prerequisites:**
```bash
# Terminal 1: Start LLM Server
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-VL-2B-Instruct \
  --port 8000

# Terminal 2: Run Tests
./run_runtime_tests.sh
```

---

## Test Files Overview

```
tests/
├── runtime_tests/              # E2E tests (13 tests) - WITH LLM
│   ├── test_e2e_agent_creation.py (6 tests)
│   ├── test_e2e_reuse_mode.py (7 tests)
│   └── conftest_runtime.py    # user_id=10077
│
├── test_agent_creation.py      # Unit tests - agent never fails
├── test_scheduler_creation.py  # Scheduler creation validation
├── test_vlm_agent.py          # VLM agent + interruption
├── test_recipe_generation.py  # Recipe JSON validation
├── test_reuse_mode.py         # Reuse mode functionality
└── conftest.py                # Shared fixtures

run_manual_tests.py            # No dependencies - 8 tests passing
```

---

## What's Validated ✅

### Working Tests (8 Core Tests)
- ✅ JSON processing and validation
- ✅ Recipe structure validation
- ✅ Recipe action ID uniqueness
- ✅ Cron schedule validation
- ✅ Interval schedule validation
- ✅ Date schedule validation
- ✅ Recipe file I/O operations
- ✅ Lifecycle hooks module

### Requires LLM Server (15 Tests)
- ⏳ Multi-agent creation flow
- ⏳ State machine transitions
- ⏳ Dependency resolution (topological sort)
- ⏳ Recipe generation with LLM
- ⏳ Reuse mode execution
- ⏳ Performance comparison (CREATE vs REUSE)

### Intentionally Skipped
- ⏸️ Coding agent tests (omniparser not running)
- ⏸️ execute_windows_command tests (omniparser not running)
- ⏸️ android_command tests (omniparser not running)

---

## Key Commands

### Start LLM Server
```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-VL-2B-Instruct \
  --port 8000
```

### Check LLM Health
```bash
curl http://localhost:8000/health
```

### Run All Runtime Tests
```bash
./run_runtime_tests.sh
```

### Run Specific Test
```bash
./run_runtime_tests.sh tests/runtime_tests/test_e2e_agent_creation.py
```

### Run with Verbose Output
```bash
./run_runtime_tests.sh tests/runtime_tests/ -v
```

### Manual Docker Execution
```bash
# Start services
docker-compose -f docker-compose.test.yml up -d redis mock-crossbar mock-apis

# Run tests
docker-compose -f docker-compose.test.yml run --rm app \
  pytest tests/runtime_tests/ -v

# Cleanup
docker-compose -f docker-compose.test.yml down --volumes
```

---

## Debugging Commands

### View Docker Logs
```bash
docker-compose -f docker-compose.test.yml logs app
docker-compose -f docker-compose.test.yml logs redis
docker-compose -f docker-compose.test.yml logs mock-apis
```

### Check Mock API Stats
```bash
# Messages sent to user
curl http://localhost:9890/autogen_response/messages

# Statistics
curl http://localhost:9890/test/stats

# Reset mock data
curl -X POST http://localhost:9890/test/reset
```

### Interactive Shell in Container
```bash
docker-compose -f docker-compose.test.yml run --rm app /bin/bash
pytest tests/runtime_tests/test_e2e_agent_creation.py -v -s
```

---

## Configuration Quick Reference

| Setting | Value | Location |
|---------|-------|----------|
| Test User ID | 10077 | `conftest_runtime.py` |
| LLM Model | Qwen3-VL-2B-Instruct | System config |
| LLM Port | 8000 | localhost |
| LLM Base URL | http://localhost:8000/v1 | `docker-compose.test.yml` |
| App Port | 6777 | `hart_intelligence_entry.py` |
| Redis Port | 6379 | `docker-compose.test.yml` |
| Mock API Port | 9890 | `docker-compose.test.yml` |
| Crossbar Port | 8088 | `docker-compose.test.yml` |

---

## Test Execution Times

| Test Type | Expected Duration |
|-----------|-------------------|
| Manual Tests | < 5 seconds |
| Agent Creation | 30-120s per test |
| Reuse Mode | 10-30s per test |
| Full Test Suite | 15-20 minutes |

---

## Directory Structure

```
prompts/
├── {prompt_id}.json                    # Agent config
└── {prompt_id}_{flow_id}_{action_id}.json  # Generated recipes

tests/
├── runtime_tests/
│   ├── mock_services/
│   │   ├── mock_api_server.py
│   │   └── mock_crossbar_server.py
│   ├── test_configs/
│   │   └── sample_coding_agent.json
│   ├── test_e2e_agent_creation.py
│   ├── test_e2e_reuse_mode.py
│   └── conftest_runtime.py
├── test_*.py (unit tests)
└── conftest.py

Documentation/
├── TEST_EXECUTION_GUIDE.md        # This guide
├── QUICK_TEST_REFERENCE.md        # Quick reference
├── RUNTIME_TESTS_SUMMARY.md       # Implementation summary
├── TEST_VALIDATION_REPORT.md      # Detailed analysis
└── tests/runtime_tests/README.md  # Runtime tests user guide
```

---

## Common Issues & Quick Fixes

### Issue: LLM Server Not Running
```bash
# Check health
curl http://localhost:8000/health

# If fails, start server
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-VL-2B-Instruct \
  --port 8000
```

### Issue: Docker Services Fail
```bash
# Reset everything
docker-compose -f docker-compose.test.yml down --volumes --remove-orphans

# Rebuild
docker-compose -f docker-compose.test.yml build --no-cache

# Start fresh
docker-compose -f docker-compose.test.yml up -d
```

### Issue: Tests Timeout
```bash
# Increase timeout in conftest_runtime.py
@pytest.mark.timeout(600)  # 10 minutes
```

### Issue: Recipe Not Created
```bash
# Check app logs
docker-compose -f docker-compose.test.yml logs app | grep -i recipe

# Check messages sent
curl http://localhost:9890/autogen_response/messages
```

---

## Architecture Diagram

```
┌────────────────────────────────────────────┐
│          TEST ENVIRONMENT                  │
├────────────────────────────────────────────┤
│                                            │
│  App Container ──▶ Real LLM (localhost)   │
│       ↓                                    │
│  Redis Container                           │
│       ↓                                    │
│  Mock Services (APIs + Pub/Sub)           │
│                                            │
└────────────────────────────────────────────┘

Network Mode: host (to access LLM)
User ID: 10077
```

---

## Next Actions

### ✅ Completed
- Manual test runner (8 tests passing)
- Unit test suite (18 requirements covered)
- Runtime E2E tests (13 comprehensive tests)
- Mock services (APIs, Crossbar)
- Docker orchestration
- Complete documentation

### ⏳ Pending (When LLM Available)
1. Start LLM server on port 8000
2. Run `./run_runtime_tests.sh`
3. Validate all 13 runtime tests
4. Review generated recipes
5. Test with different user scenarios

### 📝 Future Enhancements
- VLM agent with real visual data
- Story narration agent validation
- Performance benchmarking suite
- CI/CD integration

---

## Documentation Links

- **TEST_EXECUTION_GUIDE.md** - Full execution guide
- **RUNTIME_TESTS_SUMMARY.md** - Implementation details
- **tests/runtime_tests/README.md** - Runtime tests guide
- **TEST_VALIDATION_REPORT.md** - Validation analysis

---

**Last Updated:** 2025-10-23
**Test User ID:** 10077
**Ready to Run:** ✅ Manual tests | ⏳ Runtime tests (need LLM)
