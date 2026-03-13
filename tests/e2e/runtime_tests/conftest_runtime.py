"""
Pytest configuration for runtime tests
Provides fixtures for testing against running containers
"""

import pytest
import requests
import time
import json
from datetime import datetime

# Base URLs for services
APP_URL = "http://localhost:6777"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
MOCK_LLM_URL = "http://localhost:8000"
MOCK_CROSSBAR_URL = "http://localhost:8088"
MOCK_API_URL = "http://localhost:9890"

@pytest.fixture(scope="session")
def wait_for_services():
    """Wait for all services to be ready"""
    services = {
        "App": f"{APP_URL}/status",
        "Mock LLM": f"{MOCK_LLM_URL}/health",
        "Mock Crossbar": f"{MOCK_CROSSBAR_URL}/health",
        "Mock APIs": f"{MOCK_API_URL}/health"
    }

    max_retries = 30
    for service_name, url in services.items():
        print(f"Waiting for {service_name}...")
        for i in range(max_retries):
            try:
                response = requests.get(url, timeout=2)
                if response.status_code == 200:
                    print(f"✓ {service_name} is ready")
                    break
            except requests.exceptions.RequestException:
                if i == max_retries - 1:
                    raise Exception(f"{service_name} failed to start")
                time.sleep(2)

@pytest.fixture
def test_user_id():
    """Generate test user ID"""
    return 1001

@pytest.fixture
def test_prompt_id():
    """Generate test prompt ID"""
    return int(time.time())

@pytest.fixture
def cleanup_after_test(test_prompt_id):
    """Cleanup after each test"""
    yield
    # Cleanup recipe files
    import os
    recipe_file = f"prompts/{test_prompt_id}_0_recipe.json"
    if os.path.exists(recipe_file):
        os.remove(recipe_file)

@pytest.fixture
def reset_mock_services():
    """Reset all mock services before each test"""
    try:
        requests.post(f"{MOCK_API_URL}/test/reset")
        requests.post(f"{MOCK_CROSSBAR_URL}/test/reset")
    except:
        pass
    yield

@pytest.fixture
def sample_task_data():
    """Sample task data for testing"""
    return {
        "task_description": "Create a test file and write hello world to it",
        "user_id": 1001,
        "prompt_id": None,  # Will be set in test
        "file_id": None
    }

@pytest.fixture
def sample_recipe():
    """Sample recipe structure for validation"""
    return {
        "actions": [
            {
                "action_id": 1,
                "action": "Create test file",
                "recipe": [
                    {
                        "steps": "Create file using open()",
                        "tool_name": "file_operations",
                        "generalized_functions": "open('test.txt', 'w')",
                        "dependencies": []
                    }
                ]
            }
        ],
        "scheduled_tasks": []
    }

@pytest.fixture
def redis_client():
    """Get Redis client for testing"""
    import redis
    return redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=0)
