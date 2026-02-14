# Autonomous Agent Test Suite

## Overview

This test suite validates the complete autonomous agent creation and reuse workflow by simulating real user interactions with the local LLM. It tests the system's ability to:

1. **Create agents from scratch** (CREATE mode) - Generate new agents that can execute tasks autonomously
2. **Execute tasks without human intervention** - Agents perform tasks fully autonomously
3. **Reuse saved recipes** (REUSE mode) - Execute tasks faster using previously saved execution patterns
4. **Handle various task types** - Math calculations, file operations, code generation, etc.

## Key Features

✅ **Real LLM Integration** - Uses actual Qwen3-VL-2B-Instruct model at localhost:8000
✅ **Full Autonomy Testing** - Verifies agents complete tasks without human intervention
✅ **Performance Validation** - Measures and compares CREATE vs REUSE mode execution times
✅ **Comprehensive Reporting** - Generates detailed JSON reports with timing and success metrics
✅ **Multiple Scenarios** - Tests different task types to ensure broad compatibility

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    TEST FLOW                                │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Test Suite                                                 │
│      ↓                                                       │
│  Flask App (/chat endpoint)                                │
│      ↓                                                       │
│  Agent Creation System (create_recipe.py / reuse_recipe.py) │
│      ↓                                                       │
│  LLM Server (localhost:8000)                                │
│      ↓                                                       │
│  Recipe Generation → Autonomous Execution                   │
│      ↓                                                       │
│  Recipe Files (prompts/{prompt_id}_*.json)                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

### 1. LLM Server

The LLM server must be running on `localhost:8000`:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-VL-2B-Instruct \
  --port 8000
```

**Verify it's running:**
```bash
curl http://localhost:8000/v1/models
```

### 2. Flask Application

The main Flask application must be running on `localhost:6777`:

```bash
python langchain_gpt_api.py
```

**Verify it's running:**
```bash
curl http://localhost:6777/status
```

### 3. Python Dependencies

```bash
pip install requests
```

## Quick Start

### Run All Tests

```bash
python test_autonomous_agent_suite.py
```

### Expected Output

```
============================================================
Autonomous Agent Test Suite
============================================================

User ID: 10077
LLM Endpoint: http://localhost:8000
Flask App: http://localhost:6777
Model: Qwen3-VL-2B-Instruct

============================================================
Checking LLM Server Availability
============================================================

✓ LLM server is running at http://localhost:8000

============================================================
Checking Flask Application Availability
============================================================

✓ Flask app is running at http://localhost:6777

============================================================
Testing Direct LLM Interaction
============================================================

ℹ Sending test request to LLM...
✓ LLM responded successfully

============================================================
Test Scenario 1: Calculate Taylor Series
============================================================

ℹ Step 1: Testing CREATE mode
ℹ Creating agent for task: 'write a code for calculating taylor series'
✓ Agent creation request accepted
ℹ Waiting for recipe creation (timeout: 180s)...
✓ Recipe created: prompts/9001_0_recipe.json
✓ CREATE mode completed in 45.32s

ℹ Step 2: Verifying autonomous task completion
✓ Task completed autonomously

ℹ Step 3: Testing REUSE mode
ℹ Reusing agent for task: 'write a code for calculating taylor series'
✓ Agent reuse completed in 15.21s
✓ REUSE mode is 66.4% faster than CREATE mode

... [additional scenarios] ...

============================================================
Test Execution Report
============================================================

Total Scenarios: 3
Passed: 3
Failed: 0

✓ Scenario 1: Calculate Taylor Series
  Task: write a code for calculating taylor series
  CREATE mode: ✓ (45.32s)
  REUSE mode: ✓ (15.21s)

✓ Scenario 2: Create Simple File
  Task: create a file named test_output.txt and write 'Hello World' in it
  CREATE mode: ✓ (32.14s)
  REUSE mode: ✓ (12.05s)

✓ Scenario 3: Math Calculation
  Task: calculate the sum of numbers from 1 to 100
  CREATE mode: ✓ (28.76s)
  REUSE mode: ✓ (10.33s)

✓ Detailed report saved to: test_report_20251023_143522.json
```

## Test Scenarios

The suite includes three default test scenarios:

### 1. Calculate Taylor Series

**Task:** "write a code for calculating taylor series"

**Validates:**
- Code generation capability
- Complex mathematical task handling
- Recipe creation for code tasks

**Expected Outputs:** taylor, series, python, def

**Timeout:** 180 seconds

### 2. Create Simple File

**Task:** "create a file named test_output.txt and write 'Hello World' in it"

**Validates:**
- File operation capability
- Simple task execution
- Recipe reusability for file operations

**Expected Outputs:** file, created, test_output.txt

**Timeout:** 120 seconds

### 3. Math Calculation

**Task:** "calculate the sum of numbers from 1 to 100"

**Validates:**
- Mathematical computation
- Quick task execution
- Performance in simple scenarios

**Expected Outputs:** 5050, sum, 100

**Timeout:** 90 seconds

## Adding Custom Scenarios

Edit `test_autonomous_agent_suite.py` and add to `TEST_SCENARIOS`:

```python
TEST_SCENARIOS.append({
    "name": "Your Test Name",
    "task": "task description to send to the agent",
    "expected_outputs": ["keyword1", "keyword2"],
    "timeout": 120  # seconds
})
```

## Configuration

### Test User ID

All tests use **user_id=10077** (configured in the script)

### LLM Configuration

- **Model:** Qwen3-VL-2B-Instruct
- **Base URL:** http://localhost:8000
- **Chat Endpoint:** /v1/chat/completions
- **Temperature:** 0.7
- **Max Tokens:** 512

### Flask App Configuration

- **Base URL:** http://localhost:6777
- **Chat Endpoint:** /chat
- **Status Endpoint:** /status

### Timeouts

- **LLM check:** 5 seconds
- **Flask app check:** 5 seconds
- **Direct LLM test:** 30 seconds
- **Agent creation:** 300 seconds (5 minutes)
- **Recipe wait:** Per-scenario (90-180 seconds)
- **Agent reuse:** 180 seconds (3 minutes)

## Understanding the Results

### Success Criteria

A test scenario passes if:
1. ✅ CREATE mode successfully creates a recipe
2. ✅ REUSE mode successfully executes using the recipe
3. ✅ No errors occurred during execution

### Performance Metrics

The test suite measures:

- **CREATE mode time** - Time to create agent from scratch
- **REUSE mode time** - Time to execute using saved recipe
- **Speedup percentage** - (CREATE - REUSE) / CREATE × 100%

**Expected Results:**
- CREATE mode: 30-180 seconds (depending on task complexity)
- REUSE mode: 10-30 seconds
- Speedup: 60-80% faster in REUSE mode

### Recipe Files

Generated recipes are saved in `prompts/` directory:

- `prompts/{prompt_id}_0_recipe.json` - Full recipe
- `prompts/{prompt_id}_{flow_id}_{action_id}.json` - Individual action recipes
- `prompts/{prompt_id}.json` - Agent configuration

### Test Reports

Detailed JSON reports are saved as:

```
test_report_YYYYMMDD_HHMMSS.json
```

**Report Structure:**
```json
{
  "timestamp": "2025-10-23T14:35:22.123456",
  "user_id": 10077,
  "total_tests": 3,
  "passed": 3,
  "failed": 0,
  "results": [
    {
      "scenario": "Calculate Taylor Series",
      "task": "write a code for calculating taylor series",
      "success": true,
      "create_mode": {
        "success": true,
        "time": 45.32
      },
      "reuse_mode": {
        "success": true,
        "time": 15.21
      },
      "errors": []
    }
  ]
}
```

## Troubleshooting

### LLM Server Not Available

**Error:** `LLM server not reachable`

**Solution:**
```bash
# Start LLM server
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-VL-2B-Instruct \
  --port 8000

# Verify
curl http://localhost:8000/v1/models
```

### Flask App Not Available

**Error:** `Flask app not reachable`

**Solution:**
```bash
# Start Flask app
python langchain_gpt_api.py

# Verify
curl http://localhost:6777/status
```

### Recipe Not Created

**Error:** `Recipe not created within timeout`

**Possible Causes:**
1. Task is too complex for the timeout
2. LLM is responding slowly
3. Agent creation failed

**Solutions:**
1. Increase timeout in scenario definition
2. Check Flask app logs: `tail -f langchain.log`
3. Check if recipe files are being created: `ls -la prompts/`
4. Verify LLM is responding: Test direct LLM interaction

### Tests Timeout

**Error:** Tests hang or timeout

**Solutions:**
1. Check LLM server load: `curl http://localhost:8000/v1/models`
2. Increase timeouts in script
3. Check Flask app logs for errors
4. Verify no other processes are blocking ports 6777 or 8000

### Recipe Created but Task Not Completed

**Warning:** `Task completion verification inconclusive`

**This is normal for:**
- Tasks that don't require file outputs
- Tasks where verification keywords aren't found

**This indicates potential issues if:**
- Recipe status is "pending" or "error"
- Recipe structure is incomplete
- Expected files weren't created

## Advanced Usage

### Running Specific Scenarios

Edit the script to comment out unwanted scenarios:

```python
TEST_SCENARIOS = [
    # Only test Taylor Series
    TEST_SCENARIOS[0]
]
```

### Custom Verification

Add custom verification logic in `verify_task_completion()`:

```python
def verify_task_completion(self, prompt_id: int, expected_outputs: List[str]) -> bool:
    # Your custom verification logic here
    # Check for specific files, outputs, etc.
    pass
```

### Debugging Mode

Add verbose logging:

```python
# At the top of the script
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Testing with Different Users

Change the user ID:

```python
USER_ID = 99999  # Your test user ID
```

## Integration with CI/CD

### GitHub Actions Example

```yaml
name: Autonomous Agent Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

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
          sleep 60

      - name: Start Flask App
        run: |
          pip install -r requirements.txt
          python langchain_gpt_api.py &
          sleep 10

      - name: Run Tests
        run: |
          python test_autonomous_agent_suite.py

      - name: Upload Test Reports
        uses: actions/upload-artifact@v2
        with:
          name: test-reports
          path: test_report_*.json
```

## Performance Expectations

### CREATE Mode

| Task Complexity | Expected Time |
|-----------------|---------------|
| Simple (math) | 20-40s |
| Medium (file ops) | 30-60s |
| Complex (code gen) | 40-120s |
| Very Complex | 60-180s |

### REUSE Mode

| Task Complexity | Expected Time |
|-----------------|---------------|
| Simple | 10-15s |
| Medium | 12-20s |
| Complex | 15-30s |
| Very Complex | 20-45s |

### Expected Speedup

- **Minimum:** 40% faster
- **Typical:** 60-70% faster
- **Optimal:** 75-85% faster

## Limitations

### Current Limitations

1. ⏸️ **Coding agent tests skipped** - Requires omniparser server (not running)
2. ⏸️ **execute_windows_command tests skipped** - Requires omniparser server
3. ⏸️ **VLM agent with visual input** - Not tested in this suite
4. ⏸️ **Multi-user concurrent tests** - Not implemented

### Known Issues

1. **Windows-specific paths** - May need adjustment for Linux/Mac
2. **Recipe file detection** - Uses simple polling, may miss some patterns
3. **Task verification** - Basic keyword matching, not deep validation

## Future Enhancements

### Planned Features

1. **Visual task testing** - Test VLM agents with camera frames
2. **Concurrent execution** - Test multiple users simultaneously
3. **Stress testing** - Load testing with many concurrent agents
4. **Error injection** - Test error handling and recovery
5. **Custom recipe validation** - Deep inspection of recipe quality
6. **Performance benchmarking** - Track performance trends over time

## Support

### Documentation

- **TEST_EXECUTION_GUIDE.md** - General test execution guide
- **QUICK_TEST_REFERENCE.md** - Quick reference card
- **RUNTIME_TESTS_SUMMARY.md** - Runtime test infrastructure
- **tests/runtime_tests/README.md** - Runtime tests guide

### Logs

Check these files for debugging:
- `langchain.log` - Flask app logs
- `test_report_*.json` - Test execution reports
- `prompts/*.json` - Recipe and config files

---

**Created:** 2025-10-23
**User ID for Tests:** 10077
**LLM Model:** Qwen3-VL-2B-Instruct
**Author:** Claude Code (Autonomous Test Suite)
