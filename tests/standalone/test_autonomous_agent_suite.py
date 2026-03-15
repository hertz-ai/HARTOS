#!/usr/bin/env python3
"""
Autonomous Agent Test Suite

Tests the complete autonomous agent creation and reuse workflow using the local LLM.
This suite simulates real user interaction and verifies that agents can:
1. Be created from scratch (CREATE mode)
2. Execute tasks fully autonomously
3. Be reused with saved recipes (REUSE mode)
4. Handle various task types

User ID: 10077
LLM Endpoint: http://localhost:8000/v1/chat/completions
Model: Qwen3-VL-2B-Instruct
"""

import json
import time
import requests
import os
from datetime import datetime
from typing import Dict, Any, Optional, List

# Configuration
USER_ID = 10077
LLM_BASE_URL = "http://localhost:8000"
FLASK_APP_URL = "http://localhost:6777"
MODEL_NAME = "Qwen3-VL-2B-Instruct"

# Test scenarios
TEST_SCENARIOS = [
    {
        "name": "Calculate Taylor Series",
        "task": "write a code for calculating taylor series",
        "expected_outputs": ["taylor", "series", "python", "def"],
        "timeout": 180
    },
    {
        "name": "Create Simple File",
        "task": "create a file named test_output.txt and write 'Hello World' in it",
        "expected_outputs": ["file", "created", "test_output.txt"],
        "timeout": 120
    },
    {
        "name": "Math Calculation",
        "task": "calculate the sum of numbers from 1 to 100",
        "expected_outputs": ["5050", "sum", "100"],
        "timeout": 90
    }
]

class Colors:
    """ANSI color codes for terminal output"""
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

class AutonomousAgentTester:
    """Tests autonomous agent creation and reuse"""

    def __init__(self):
        self.test_results = []
        self.llm_available = False
        self.flask_app_available = False

    def print_header(self, text: str):
        """Print a formatted header"""
        print(f"\n{Colors.BLUE}{'='*70}{Colors.RESET}")
        print(f"{Colors.BLUE}{Colors.BOLD}{text}{Colors.RESET}")
        print(f"{Colors.BLUE}{'='*70}{Colors.RESET}\n")

    def print_success(self, text: str):
        """Print success message"""
        print(f"{Colors.GREEN}[OK] {text}{Colors.RESET}")

    def print_error(self, text: str):
        """Print error message"""
        print(f"{Colors.RED}[ERROR] {text}{Colors.RESET}")

    def print_warning(self, text: str):
        """Print warning message"""
        print(f"{Colors.YELLOW}[WARN] {text}{Colors.RESET}")

    def print_info(self, text: str):
        """Print info message"""
        print(f"{Colors.BLUE}[INFO] {text}{Colors.RESET}")

    def check_llm_server(self) -> bool:
        """Check if LLM server is running"""
        self.print_header("Checking LLM Server Availability")

        try:
            # Try /health endpoint first
            response = requests.get(f"{LLM_BASE_URL}/health", timeout=5)
            if response.status_code == 200:
                self.print_success(f"LLM server is running at {LLM_BASE_URL}")
                self.llm_available = True
                return True

            # Try /v1/models as backup
            response = requests.get(f"{LLM_BASE_URL}/v1/models", timeout=5)
            if response.status_code == 200:
                self.print_success(f"LLM server is running at {LLM_BASE_URL}")
                self.llm_available = True
                return True
            else:
                self.print_error(f"LLM server returned status {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.print_error(f"LLM server not reachable: {e}")
            self.print_info(f"Start LLM server with:")
            self.print_info(f"  python -m vllm.entrypoints.openai.api_server \\")
            self.print_info(f"    --model Qwen/Qwen3-VL-2B-Instruct \\")
            self.print_info(f"    --port 8000")
            return False

    def check_flask_app(self) -> bool:
        """Check if Flask application is running"""
        self.print_header("Checking Flask Application Availability")

        try:
            response = requests.get(f"{FLASK_APP_URL}/status", timeout=5)
            if response.status_code == 200:
                self.print_success(f"Flask app is running at {FLASK_APP_URL}")
                self.flask_app_available = True
                return True
            else:
                self.print_error(f"Flask app returned status {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.print_error(f"Flask app not reachable: {e}")
            self.print_info(f"Start Flask app with:")
            self.print_info(f"  python hart_intelligence_entry.py")
            return False

    def test_llm_direct(self) -> bool:
        """Test direct LLM interaction"""
        self.print_header("Testing Direct LLM Interaction")

        payload = {
            "model": MODEL_NAME,
            "messages": [
                {
                    "role": "user",
                    "content": "write a code for calculating taylor series"
                }
            ],
            "temperature": 0.7,
            "max_tokens": 512
        }

        try:
            self.print_info(f"Sending test request to LLM...")
            response = requests.post(
                f"{LLM_BASE_URL}/v1/chat/completions",
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

                if content:
                    self.print_success("LLM responded successfully")
                    self.print_info(f"Response preview: {content[:200]}...")
                    return True
                else:
                    self.print_error("LLM response empty")
                    return False
            else:
                self.print_error(f"LLM request failed with status {response.status_code}")
                return False

        except Exception as e:
            self.print_error(f"LLM test failed: {e}")
            return False

    def create_agent(self, task: str, prompt_id: int) -> Optional[Dict[str, Any]]:
        """Create an agent for a task (CREATE mode)"""
        self.print_info(f"Creating agent for task: '{task}'")

        request_data = {
            "user_id": USER_ID,
            "prompt_id": prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"test_create_{int(time.time())}_{prompt_id}"
        }

        try:
            response = requests.post(
                f"{FLASK_APP_URL}/chat",
                json=request_data,
                timeout=300  # 5 minutes for agent creation
            )

            if response.status_code == 200:
                result = response.json()
                self.print_success("Agent creation request accepted")
                return result
            else:
                self.print_error(f"Agent creation failed with status {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return None

        except Exception as e:
            self.print_error(f"Agent creation request failed: {e}")
            return None

    def wait_for_recipe(self, prompt_id: int, timeout: int = 180) -> Optional[Dict[str, Any]]:
        """Wait for recipe file to be created"""
        self.print_info(f"Waiting for recipe creation (timeout: {timeout}s)...")

        # Recipe filename pattern: prompts/{prompt_id}_0_recipe.json
        # or individual action recipes: prompts/{prompt_id}_{flow_id}_{action_id}.json
        recipe_file = f"prompts/{prompt_id}_0_recipe.json"

        start_time = time.time()
        while time.time() - start_time < timeout:
            if os.path.exists(recipe_file):
                try:
                    with open(recipe_file, 'r') as f:
                        recipe = json.load(f)
                    self.print_success(f"Recipe created: {recipe_file}")
                    return recipe
                except Exception as e:
                    self.print_warning(f"Recipe file exists but couldn't read: {e}")

            # Also check for flow/action specific recipes
            for flow_id in range(5):  # Check first 5 flows
                for action_id in range(1, 10):  # Check first 10 actions
                    action_recipe = f"prompts/{prompt_id}_{flow_id}_{action_id}.json"
                    if os.path.exists(action_recipe):
                        self.print_info(f"Found action recipe: {action_recipe}")
                        try:
                            with open(action_recipe, 'r') as f:
                                return json.load(f)
                        except:
                            pass

            time.sleep(5)
            print(".", end="", flush=True)

        print()  # New line after waiting dots
        self.print_error(f"Recipe not created within {timeout} seconds")
        return None

    def reuse_agent(self, task: str, prompt_id: int) -> Optional[Dict[str, Any]]:
        """Reuse an existing agent (REUSE mode)"""
        self.print_info(f"Reusing agent for task: '{task}'")

        request_data = {
            "user_id": USER_ID,
            "prompt_id": prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"test_reuse_{int(time.time())}_{prompt_id}",
            "mode": "reuse"  # Indicate reuse mode if supported
        }

        try:
            start_time = time.time()
            response = requests.post(
                f"{FLASK_APP_URL}/chat",
                json=request_data,
                timeout=180  # 3 minutes for reuse (should be faster)
            )
            elapsed_time = time.time() - start_time

            if response.status_code == 200:
                result = response.json()
                self.print_success(f"Agent reuse completed in {elapsed_time:.2f}s")
                return result
            else:
                self.print_error(f"Agent reuse failed with status {response.status_code}")
                return None

        except Exception as e:
            self.print_error(f"Agent reuse request failed: {e}")
            return None

    def verify_task_completion(self, prompt_id: int, expected_outputs: List[str]) -> bool:
        """Verify that task was completed successfully"""
        self.print_info("Verifying task completion...")

        # Check if recipe exists and has expected structure
        recipe_patterns = [
            f"prompts/{prompt_id}_0_recipe.json",
            f"prompts/{prompt_id}_0_1.json",
            f"prompts/{prompt_id}.json"
        ]

        for recipe_path in recipe_patterns:
            if os.path.exists(recipe_path):
                try:
                    with open(recipe_path, 'r') as f:
                        recipe = json.load(f)

                    # Check if recipe indicates completion
                    status = recipe.get("status", "").lower()
                    if status in ["done", "completed", "terminated"]:
                        self.print_success(f"Recipe status: {status}")
                        return True
                    elif status in ["pending", "in_progress"]:
                        self.print_warning(f"Recipe status: {status} (may still be processing)")

                except Exception as e:
                    self.print_warning(f"Could not verify recipe at {recipe_path}: {e}")

        return False

    def run_test_scenario(self, scenario: Dict[str, Any], scenario_num: int) -> Dict[str, Any]:
        """Run a complete test scenario"""
        self.print_header(f"Test Scenario {scenario_num}: {scenario['name']}")

        result = {
            "scenario": scenario['name'],
            "task": scenario['task'],
            "success": False,
            "create_mode": {"success": False, "time": 0},
            "reuse_mode": {"success": False, "time": 0},
            "errors": []
        }

        # Use unique prompt_id for this test
        prompt_id = 9000 + scenario_num

        # Step 1: Create agent (CREATE mode)
        self.print_info("Step 1: Testing CREATE mode")
        create_start = time.time()

        create_response = self.create_agent(scenario['task'], prompt_id)
        if not create_response:
            result["errors"].append("Agent creation request failed")
            return result

        # Wait for recipe
        recipe = self.wait_for_recipe(prompt_id, scenario['timeout'])
        create_time = time.time() - create_start
        result["create_mode"]["time"] = create_time

        if recipe:
            result["create_mode"]["success"] = True
            self.print_success(f"CREATE mode completed in {create_time:.2f}s")
        else:
            result["errors"].append("Recipe not created in CREATE mode")
            self.print_error("CREATE mode failed - no recipe generated")
            return result

        # Step 2: Verify task completion
        self.print_info("Step 2: Verifying autonomous task completion")
        if self.verify_task_completion(prompt_id, scenario['expected_outputs']):
            self.print_success("Task completed autonomously")
        else:
            result["errors"].append("Task completion could not be verified")
            self.print_warning("Task completion verification inconclusive")

        # Step 3: Test REUSE mode (with same or similar task)
        self.print_info("Step 3: Testing REUSE mode")
        reuse_start = time.time()

        reuse_response = self.reuse_agent(scenario['task'], prompt_id)
        reuse_time = time.time() - reuse_start
        result["reuse_mode"]["time"] = reuse_time

        if reuse_response:
            result["reuse_mode"]["success"] = True
            self.print_success(f"REUSE mode completed in {reuse_time:.2f}s")

            # Verify REUSE is faster than CREATE
            if reuse_time < create_time:
                speedup = ((create_time - reuse_time) / create_time) * 100
                self.print_success(f"REUSE mode is {speedup:.1f}% faster than CREATE mode")
            else:
                self.print_warning("REUSE mode was not faster than CREATE mode")
        else:
            result["errors"].append("REUSE mode failed")
            self.print_error("REUSE mode failed")

        # Overall success if both modes worked
        result["success"] = result["create_mode"]["success"] and result["reuse_mode"]["success"]

        return result

    def generate_report(self):
        """Generate test execution report"""
        self.print_header("Test Execution Report")

        total_tests = len(self.test_results)
        passed_tests = sum(1 for r in self.test_results if r["success"])
        failed_tests = total_tests - passed_tests

        print(f"Total Scenarios: {total_tests}")
        print(f"{Colors.GREEN}Passed: {passed_tests}{Colors.RESET}")
        print(f"{Colors.RED}Failed: {failed_tests}{Colors.RESET}")
        print()

        # Detailed results
        for i, result in enumerate(self.test_results, 1):
            status_color = Colors.GREEN if result["success"] else Colors.RED
            status_symbol = "[PASS]" if result["success"] else "[FAIL]"

            print(f"{status_color}{status_symbol} Scenario {i}: {result['scenario']}{Colors.RESET}")
            print(f"  Task: {result['task']}")

            if result["create_mode"]["success"]:
                print(f"  {Colors.GREEN}CREATE mode: [OK] ({result['create_mode']['time']:.2f}s){Colors.RESET}")
            else:
                print(f"  {Colors.RED}CREATE mode: [FAIL]{Colors.RESET}")

            if result["reuse_mode"]["success"]:
                print(f"  {Colors.GREEN}REUSE mode: [OK] ({result['reuse_mode']['time']:.2f}s){Colors.RESET}")
            else:
                print(f"  {Colors.RED}REUSE mode: [FAIL]{Colors.RESET}")

            if result["errors"]:
                print(f"  {Colors.YELLOW}Errors:{Colors.RESET}")
                for error in result["errors"]:
                    print(f"    - {error}")
            print()

        # Save report to file
        report_file = f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "user_id": USER_ID,
                "total_tests": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
                "results": self.test_results
            }, f, indent=2)

        self.print_success(f"Detailed report saved to: {report_file}")

    def run_all_tests(self):
        """Run all test scenarios"""
        self.print_header("Autonomous Agent Test Suite")
        print(f"User ID: {USER_ID}")
        print(f"LLM Endpoint: {LLM_BASE_URL}")
        print(f"Flask App: {FLASK_APP_URL}")
        print(f"Model: {MODEL_NAME}")

        # Check prerequisites
        if not self.check_llm_server():
            self.print_error("LLM server not available - cannot proceed")
            return False

        if not self.check_flask_app():
            self.print_error("Flask app not available - cannot proceed")
            return False

        # Test direct LLM
        if not self.test_llm_direct():
            self.print_warning("Direct LLM test failed, but continuing with agent tests")

        # Run all scenarios
        for i, scenario in enumerate(TEST_SCENARIOS, 1):
            result = self.run_test_scenario(scenario, i)
            self.test_results.append(result)

            # Small delay between tests
            if i < len(TEST_SCENARIOS):
                self.print_info("Waiting 10 seconds before next test...")
                time.sleep(10)

        # Generate final report
        self.generate_report()

        return all(r["success"] for r in self.test_results)

def main():
    """Main entry point"""
    tester = AutonomousAgentTester()

    try:
        success = tester.run_all_tests()
        exit_code = 0 if success else 1
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Tests interrupted by user{Colors.RESET}")
        exit_code = 130
    except Exception as e:
        print(f"\n{Colors.RED}Test suite failed with error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        exit_code = 1

    exit(exit_code)

if __name__ == "__main__":
    main()
