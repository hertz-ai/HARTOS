# Comprehensive Test Suite for LLM-Langchain Chatbot Agent

This test suite provides comprehensive coverage for all functionalities in the create_recipe.py and reuse_recipe.py modules.

## Table of Contents
1. [Overview](#overview)
2. [Test Structure](#test-structure)
3. [Installation](#installation)
4. [Running Tests](#running-tests)
5. [Test Coverage](#test-coverage)
6. [Test Files Description](#test-files-description)
7. [Writing New Tests](#writing-new-tests)
8. [Troubleshooting](#troubleshooting)

## Overview

This test suite validates all critical functionalities of the agent creation process:
- ✅ Agent creation never fails
- ✅ Scheduler creation in both review and reuse modes
- ✅ VLM agent interruption capability
- ✅ VLM agent command execution
- ✅ Coding agent autonomous repository setup
- ✅ Story narration agent creation
- ✅ Visual context-based question answering
- ✅ Action execution validation in creation mode
- ✅ JSON generation for each action
- ✅ Flow execution status tracking
- ✅ Recipe JSON creation for each flow
- ✅ Flow recipes validation
- ✅ Completion verification before mode switching
- ✅ Actions execution in reuse mode
- ✅ Output validation between creation and reuse modes
- ✅ Shell command execution generalization
- ✅ Final execution completion

## Test Structure

```
tests/
├── __init__.py
├── conftest.py                    # Pytest fixtures and configuration
├── test_agent_creation.py         # Agent creation tests
├── test_scheduler_creation.py     # Scheduler tests
├── test_vlm_agent.py              # VLM agent tests
├── test_action_execution.py       # Action execution tests
├── test_recipe_generation.py      # Recipe generation tests
├── test_reuse_mode.py             # Reuse mode tests
├── test_coding_agent.py           # Coding agent tests
├── test_shell_execution.py        # Shell execution tests
├── test_integration.py            # End-to-end integration tests
└── README.md                      # This file
```

## Installation

### Prerequisites
- Python 3.8+
- pip

### Install Dependencies

```bash
pip install -r requirements.txt
pip install pytest pytest-cov pytest-mock pytest-asyncio
```

## Running Tests

### Run All Tests
```bash
pytest tests/
```

### Run Specific Test File
```bash
pytest tests/test_agent_creation.py
```

### Run Specific Test Class
```bash
pytest tests/test_agent_creation.py::TestAgentCreation
```

### Run Specific Test Function
```bash
pytest tests/test_agent_creation.py::TestAgentCreation::test_create_agents_basic_success
```

### Run with Coverage Report
```bash
pytest tests/ --cov=. --cov-report=html
```

### Run with Verbose Output
```bash
pytest tests/ -v
```

### Run in Parallel (faster)
```bash
pip install pytest-xdist
pytest tests/ -n auto
```

### Run Only Failed Tests from Last Run
```bash
pytest tests/ --lf
```

## Test Coverage

### test_agent_creation.py
**Coverage:** Agent creation robustness

**Key Tests:**
- ✅ `test_create_agents_basic_success` - Basic agent creation
- ✅ `test_create_agents_with_empty_task` - Empty task handling
- ✅ `test_create_agents_with_invalid_user_id` - Invalid ID handling
- ✅ `test_create_time_agents_success` - Time-based agent creation
- ✅ `test_agent_creation_never_fails_guarantee` - Never-fail guarantee
- ✅ `test_multiple_concurrent_agent_creations` - Concurrent creation

**Validates:**
- Agent creation never fails under any circumstances
- Proper error handling and recovery
- Concurrent agent creation without conflicts

### test_scheduler_creation.py
**Coverage:** Scheduler creation in both modes

**Key Tests:**
- ✅ `test_time_based_job_scheduling_creation_mode` - Time-based scheduling
- ✅ `test_cron_trigger_scheduling` - Cron triggers
- ✅ `test_interval_trigger_scheduling` - Interval triggers
- ✅ `test_create_schedule_function` - Reuse mode scheduling
- ✅ `test_scheduler_handles_past_run_dates` - Edge case handling
- ✅ `test_scheduler_timezone_handling` - Timezone support

**Validates:**
- Schedulers work correctly in both creation and reuse modes
- All trigger types (date, cron, interval) function properly
- Error recovery and edge case handling

### test_vlm_agent.py
**Coverage:** Vision-Language Model agent functionality

**Key Tests:**
- ✅ `test_vlm_agent_user_interrupt_during_execution` - User interruption
- ✅ `test_vlm_agent_execute_shell_command` - Command execution
- ✅ `test_vlm_agent_file_operations` - File operations
- ✅ `test_vlm_get_visual_context` - Visual context retrieval
- ✅ `test_vlm_visual_question_answering` - Visual Q&A
- ✅ `test_vlm_object_detection` - Object detection

**Validates:**
- User can interrupt VLM agent actions
- VLM agent executes commands on user's computer
- Visual context-based question answering works correctly

### test_action_execution.py
**Coverage:** Action execution in creation mode

**Key Tests:**
- ✅ `test_action_assignment_tracking` - Action tracking
- ✅ `test_action_status_verification_request` - Status verification
- ✅ `test_action_completion_verification` - Completion validation
- ✅ `test_json_generation_for_action` - JSON generation
- ✅ `test_flow_execution_tracking` - Flow tracking
- ✅ `test_validate_action_output_structure` - Output validation

**Validates:**
- Actions execute correctly in creation mode
- JSON is generated for each action
- Flow execution status is tracked properly
- Output validation ensures correctness

### test_recipe_generation.py
**Coverage:** Recipe generation and validation

**Key Tests:**
- ✅ `test_create_basic_recipe_json` - Basic recipe creation
- ✅ `test_create_recipe_with_dependencies` - Dependency handling
- ✅ `test_topological_sort_dependencies` - Dependency sorting
- ✅ `test_validate_recipe_structure` - Structure validation
- ✅ `test_save_recipe_to_file` - File persistence
- ✅ `test_verify_all_actions_completed_before_switch` - Mode switch validation

**Validates:**
- Recipe JSON created correctly for each flow
- Flow recipes are validated
- Completion verified before switching modes

### test_reuse_mode.py
**Coverage:** Reuse mode execution

**Key Tests:**
- ✅ `test_load_recipe_from_file` - Recipe loading
- ✅ `test_execute_action_from_recipe` - Action execution
- ✅ `test_compare_creation_and_reuse_outputs` - Output consistency
- ✅ `test_reuse_mode_handles_message2user` - Message formatting
- ✅ `test_output_consistency_across_runs` - Consistency validation

**Validates:**
- Actions execute correctly in reuse mode
- Outputs are validated between creation and reuse modes
- Recipe loading and execution works properly

### test_coding_agent.py
**Coverage:** Coding agent functionality

**Key Tests:**
- ✅ `test_clone_repository` - Repository cloning
- ✅ `test_detect_project_type` - Project type detection
- ✅ `test_install_python_dependencies` - Dependency installation
- ✅ `test_generate_python_function` - Code generation
- ✅ `test_execute_python_code` - Code execution
- ✅ `test_full_project_setup_workflow` - Complete workflow

**Validates:**
- Coding agent can setup open source repositories autonomously
- Code generation and execution works correctly
- Dependency management functions properly

### test_shell_execution.py
**Coverage:** Shell command execution

**Key Tests:**
- ✅ `test_execute_simple_command` - Basic execution
- ✅ `test_generalize_file_path_commands` - Path generalization
- ✅ `test_cross_platform_command_execution` - Cross-platform support
- ✅ `test_validate_command_safety` - Security validation
- ✅ `test_prevent_command_injection` - Injection prevention

**Validates:**
- Shell commands execute correctly
- Command generalization works properly
- Security and safety measures are in place

### test_integration.py
**Coverage:** End-to-end workflows

**Key Tests:**
- ✅ `test_complete_creation_flow` - Full creation flow
- ✅ `test_complete_reuse_flow` - Full reuse flow
- ✅ `test_mode_transition_validation` - Mode switching
- ✅ `test_coding_and_vlm_agent_collaboration` - Multi-agent collaboration
- ✅ `test_recovery_from_action_failure` - Error recovery
- ✅ `test_handle_multiple_concurrent_users` - Scalability

**Validates:**
- Complete workflows from creation to reuse
- Mode transitions work correctly
- Error recovery and scalability

## Test Files Description

### conftest.py
Contains shared pytest fixtures:
- `test_user_prompt` - Standard test user prompt
- `test_user_id` - Standard test user ID
- `test_prompt_id` - Standard test prompt ID
- `sample_actions` - Sample action data
- `mock_user_tasks` - Mock Action object
- `mock_group_chat` - Mock group chat
- `mock_agents` - Mock agent objects
- `mock_flask_app` - Mock Flask application
- `temp_prompts_dir` - Temporary directory for prompts
- `sample_config_json` - Sample configuration
- `sample_recipe_json` - Sample recipe data

## Writing New Tests

### Template for New Test

```python
import pytest
from unittest.mock import Mock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestNewFeature:
    """Test description"""

    def test_feature_basic_functionality(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test basic functionality"""
        # Arrange
        expected_result = "success"

        # Act
        with patch('module.function') as mock_func:
            mock_func.return_value = expected_result
            result = mock_func()

        # Assert
        assert result == expected_result
```

### Best Practices

1. **Use Descriptive Names**: Test names should clearly describe what is being tested
2. **Follow AAA Pattern**: Arrange, Act, Assert
3. **Use Fixtures**: Leverage conftest.py fixtures for common setup
4. **Mock External Dependencies**: Use `patch` to mock external calls
5. **Test Edge Cases**: Include tests for error conditions and edge cases
6. **Keep Tests Independent**: Each test should run independently
7. **Document Complex Tests**: Add comments for complex test logic

## Troubleshooting

### Common Issues

#### Issue: `ModuleNotFoundError`
**Solution:** Ensure the parent directory is in the Python path:
```python
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
```

#### Issue: `fixture 'mock_flask_app' not found`
**Solution:** Make sure conftest.py is in the tests directory

#### Issue: Tests pass individually but fail when run together
**Solution:** Check for shared state. Use `autouse=True` fixtures to reset state:
```python
@pytest.fixture(autouse=True)
def reset_state():
    # Reset global state
    yield
    # Cleanup
```

#### Issue: Slow test execution
**Solution:** Use pytest-xdist for parallel execution:
```bash
pytest tests/ -n auto
```

### Debug Mode

Run tests in debug mode:
```bash
pytest tests/ -v -s --pdb
```

### Show Print Statements
```bash
pytest tests/ -s
```

### Generate HTML Report
```bash
pytest tests/ --html=report.html --self-contained-html
```

## Continuous Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov
      - name: Run tests
        run: pytest tests/ --cov=. --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v2
```

## Coverage Goals

Target coverage: **80%+**

Check current coverage:
```bash
pytest tests/ --cov=. --cov-report=term-missing
```

## Contributing

When adding new functionality:
1. Write tests first (TDD)
2. Ensure tests pass
3. Maintain coverage above 80%
4. Update this README if needed

## Support

For issues or questions:
1. Check this README
2. Review existing tests for examples
3. Check pytest documentation: https://docs.pytest.org/

## License

Same as the main project.
