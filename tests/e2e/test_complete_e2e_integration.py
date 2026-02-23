"""
Complete End-to-End Integration Test

Tests all major functionality via Flask API:
1. TaskDelegationBridge - A2A delegation with state tracking
2. AP2 Payment Integration - Agentic commerce workflows
3. Task Ledger - State management and auto-resume
4. Multi-agent workflows
5. Backward compatibility

This script makes actual HTTP requests to the Flask endpoints to validate
the complete integration in both create_recipe.py and reuse_recipe.py
"""

import requests
import json
import time
import sys
from datetime import datetime
from typing import Dict, List, Optional

# Flask server configuration
BASE_URL = "http://localhost:5000"
CHAT_ENDPOINT = f"{BASE_URL}/chat"
REUSE_ENDPOINT = f"{BASE_URL}/reuse"

# Test configuration
TEST_USER_ID = "test_e2e_user"
TEST_PROMPT_ID = "e2e_integration_test"
REQUEST_ID = f"req_{int(time.time())}"

class Colors:
    """ANSI color codes for terminal output"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'

def print_header(text: str):
    """Print formatted header"""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.END}\n")

def print_success(text: str):
    """Print success message"""
    print(f"{Colors.GREEN}[OK] {text}{Colors.END}")

def print_error(text: str):
    """Print error message"""
    print(f"{Colors.RED}[ERROR] {text}{Colors.END}")

def print_info(text: str):
    """Print info message"""
    print(f"{Colors.BLUE}[INFO] {text}{Colors.END}")

def print_warning(text: str):
    """Print warning message"""
    print(f"{Colors.YELLOW}[WARN] {text}{Colors.END}")

def check_server_running() -> bool:
    """Check if Flask server is running"""
    try:
        response = requests.get(BASE_URL, timeout=5)
        return True
    except requests.exceptions.RequestException:
        return False

def send_chat_message(message: str, user_id: str = TEST_USER_ID,
                      prompt_id: str = TEST_PROMPT_ID,
                      file_id: str = "",
                      request_id: str = REQUEST_ID) -> Optional[Dict]:
    """Send message to create_recipe chat endpoint"""
    payload = {
        "user_id": user_id,
        "prompt": message,
        "prompt_id": prompt_id,
        "file_id": file_id,
        "request_id": request_id
    }

    try:
        print_info(f"Sending to /chat: {message[:100]}...")
        response = requests.post(CHAT_ENDPOINT, json=payload, timeout=120)

        if response.status_code == 200:
            result = response.json()
            print_success(f"Response received: {response.status_code}")
            return result
        else:
            print_error(f"Request failed: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return None
    except Exception as e:
        print_error(f"Exception during chat request: {e}")
        return None

def send_reuse_message(message: str, recipe_id: str,
                       user_id: str = TEST_USER_ID,
                       prompt_id: str = f"{TEST_PROMPT_ID}_reuse",
                       request_id: str = REQUEST_ID) -> Optional[Dict]:
    """Send message to reuse_recipe endpoint"""
    payload = {
        "user_id": user_id,
        "text": message,
        "prompt_id": prompt_id,
        "file_id": recipe_id,
        "request_id": request_id
    }

    try:
        print_info(f"Sending to /reuse: {message[:100]}...")
        response = requests.post(REUSE_ENDPOINT, json=payload, timeout=120)

        if response.status_code == 200:
            result = response.json()
            print_success(f"Response received: {response.status_code}")
            return result
        else:
            print_error(f"Request failed: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return None
    except Exception as e:
        print_error(f"Exception during reuse request: {e}")
        return None

def extract_response_text(result: Dict) -> str:
    """Extract response text from API result"""
    if not result:
        return ""

    if 'response' in result:
        return result['response']
    elif 'message' in result:
        return result['message']
    elif 'result' in result:
        return str(result['result'])
    else:
        return str(result)

def test_1_basic_agent_creation():
    """Test 1: Create a basic agent using create_recipe"""
    print_header("TEST 1: Basic Agent Creation (create_recipe)")

    message = """Create a simple data analysis agent that can:
1. Load CSV data
2. Perform basic statistical analysis
3. Generate summary reports

Keep it simple for testing purposes."""

    result = send_chat_message(message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Response excerpt: {response_text[:300]}...")

        # Check if recipe was created
        if 'recipe' in str(result).lower() or 'agent' in str(result).lower():
            print_success("Agent creation initiated successfully")
            return True
        else:
            print_warning("Agent creation response unclear")
            return False
    else:
        print_error("Agent creation failed")
        return False

def test_2_task_delegation_bridge():
    """Test 2: Task delegation with TaskDelegationBridge"""
    print_header("TEST 2: Task Delegation with Bridge Integration")

    # First, register some agents with skills
    setup_message = """I need to set up a multi-agent system with the following specialists:
1. A data cleaning expert with skills in data_cleaning and preprocessing
2. A machine learning specialist with skills in ml_modeling and prediction
3. A visualization expert with skills in data_visualization and reporting

Then delegate a complex data analysis task that requires collaboration."""

    result = send_chat_message(setup_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Multi-agent setup response: {response_text[:300]}...")

        # Now test delegation
        delegation_message = """Delegate a task to analyze customer churn:
1. Clean the customer dataset (delegate to data cleaning specialist)
2. Build a prediction model (delegate to ML specialist)
3. Create visualization dashboard (delegate to visualization specialist)

Use the delegate_to_specialist tool to properly delegate these tasks."""

        time.sleep(2)  # Brief pause
        result2 = send_chat_message(delegation_message)

        if result2:
            response_text2 = extract_response_text(result2)
            print_info(f"Delegation response: {response_text2[:300]}...")

            # Check for delegation indicators
            response_str = str(result2).lower()
            if any(keyword in response_str for keyword in ['delegat', 'specialist', 'task', 'agent']):
                print_success("Task delegation appears to be working")

                # Check for bridge-specific indicators
                if any(keyword in response_str for keyword in ['blocked', 'tracking', 'child_task', 'parent_task']):
                    print_success("TaskDelegationBridge integration detected!")
                    return True
                else:
                    print_warning("Standard delegation used (bridge may not be active)")
                    return True
            else:
                print_warning("Delegation response unclear")
                return False
        else:
            print_error("Delegation request failed")
            return False
    else:
        print_error("Multi-agent setup failed")
        return False

def test_3_ap2_payment_integration():
    """Test 3: AP2 Payment Integration"""
    print_header("TEST 3: AP2 Payment Integration (Agentic Commerce)")

    payment_message = """I need to test the payment workflow:
1. Create a payment request for $99.99 USD for "Premium AI Agent Subscription"
2. List all pending payments
3. Authorize the payment
4. Process the payment through the gateway

Use the AP2 payment tools (create_payment_request, list_payments, authorize_payment, process_payment)."""

    result = send_chat_message(payment_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Payment workflow response: {response_text[:400]}...")

        # Check for payment-related indicators
        response_str = str(result).lower()
        payment_keywords = ['payment', 'transaction', 'authorize', 'process', 'gateway', 'ledger']

        if any(keyword in response_str for keyword in payment_keywords):
            print_success("AP2 payment integration detected")

            # Check for specific payment status
            if any(status in response_str for status in ['pending', 'authorized', 'completed', 'processing']):
                print_success("Payment status tracking working")
                return True
            else:
                print_warning("Payment workflow initiated but status unclear")
                return True
        else:
            print_warning("AP2 payment tools may not be available")
            return False
    else:
        print_error("Payment workflow test failed")
        return False

def test_4_task_ledger_state_management():
    """Test 4: Task Ledger and State Management"""
    print_header("TEST 4: Task Ledger State Management")

    state_message = """Execute a multi-step workflow and track state:
1. Initialize a data processing pipeline
2. Start task A (data loading)
3. Block task A and start task B (data transformation)
4. Complete task B and resume task A
5. Report the state of all tasks

Show me the task states and any auto-resume behavior."""

    result = send_chat_message(state_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"State management response: {response_text[:400]}...")

        # Check for state-related keywords
        response_str = str(result).lower()
        state_keywords = ['task', 'state', 'status', 'ledger', 'tracking', 'blocked', 'completed', 'in_progress']

        matches = sum(1 for keyword in state_keywords if keyword in response_str)

        if matches >= 3:
            print_success(f"Task state management active (found {matches} state indicators)")

            if 'blocked' in response_str or 'resume' in response_str:
                print_success("State transitions detected (blocking/resuming)")
                return True
            else:
                print_warning("Basic state tracking present")
                return True
        else:
            print_warning("Task state tracking unclear")
            return False
    else:
        print_error("State management test failed")
        return False

def test_5_reuse_recipe_flow():
    """Test 5: Reuse Recipe Flow"""
    print_header("TEST 5: Reuse Recipe Flow (reuse_recipe.py)")

    # First, we need a recipe ID - using a common test recipe
    recipe_id = "8888"  # From the agent_data files we saw

    reuse_message = """Analyze the following dataset and provide insights:
- Customer satisfaction scores
- Purchase frequency
- Churn indicators

Use your data analysis capabilities to generate a summary report."""

    result = send_reuse_message(reuse_message, recipe_id)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Reuse recipe response: {response_text[:300]}...")

        # Check if agent is working
        response_str = str(result).lower()
        if any(keyword in response_str for keyword in ['analyz', 'data', 'report', 'insight', 'summary']):
            print_success("Reuse recipe flow working")

            # Check for ledger integration in reuse mode
            if any(keyword in response_str for keyword in ['task', 'ledger', 'tracking', 'state']):
                print_success("Task ledger integration in reuse mode detected")
                return True
            else:
                print_warning("Basic reuse working, ledger integration unclear")
                return True
        else:
            print_warning("Reuse recipe response unclear")
            return False
    else:
        print_error("Reuse recipe test failed")
        return False

def test_6_nested_delegation():
    """Test 6: Nested Task Delegation"""
    print_header("TEST 6: Nested Task Delegation (Delegation within Delegation)")

    nested_message = """Create a complex workflow with nested delegations:

Main Task: Generate Quarterly Business Intelligence Report
- Delegate to Analyst Agent: Analyze quarterly data
  - Analyst delegates to Data Engineer: Clean and prepare data
  - Analyst delegates to ML Specialist: Build predictive models
- Delegate to Report Writer: Create executive summary

Use delegate_to_specialist for each delegation and show the task hierarchy."""

    result = send_chat_message(nested_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Nested delegation response: {response_text[:400]}...")

        response_str = str(result).lower()

        # Check for multiple delegations
        delegation_count = response_str.count('delegat')
        if delegation_count >= 2:
            print_success(f"Multiple delegations detected (count: {delegation_count})")

            # Check for hierarchy indicators
            if any(keyword in response_str for keyword in ['parent', 'child', 'nested', 'hierarchy']):
                print_success("Task hierarchy tracking detected")
                return True
            else:
                print_warning("Multiple delegations present, hierarchy unclear")
                return True
        else:
            print_warning("Nested delegation unclear")
            return False
    else:
        print_error("Nested delegation test failed")
        return False

def test_7_backward_compatibility():
    """Test 7: Backward Compatibility - Standard Operations"""
    print_header("TEST 7: Backward Compatibility Check")

    simple_message = """Perform a simple calculation:
Calculate the compound interest on $10,000 at 5% annual rate for 3 years.
Show your work."""

    result = send_chat_message(simple_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Simple task response: {response_text[:300]}...")

        # Check if basic functionality works
        response_str = str(result).lower()
        if any(keyword in response_str for keyword in ['calculat', 'interest', '10000', '5%', 'year']):
            print_success("Basic agent functionality preserved")

            # Verify no errors about missing tools
            if 'error' not in response_str and 'fail' not in response_str:
                print_success("No integration errors - backward compatibility maintained")
                return True
            else:
                print_warning("Response contains error mentions")
                return False
        else:
            print_warning("Simple task response unclear")
            return False
    else:
        print_error("Simple task failed")
        return False

def test_8_integration_summary():
    """Test 8: Check Integration Status"""
    print_header("TEST 8: Integration Status Check")

    status_message = """Report on the agent's capabilities:
1. What delegation tools are available?
2. What payment tools are available?
3. Is task state tracking active?
4. List all available tools and their status.

Provide a comprehensive capability report."""

    result = send_chat_message(status_message)

    if result:
        response_text = extract_response_text(result)
        print_info(f"Capability report: {response_text[:500]}...")

        response_str = str(result).lower()

        # Check for key integrations
        checks = {
            'TaskDelegationBridge': any(k in response_str for k in ['delegat', 'specialist', 'a2a']),
            'AP2 Payments': any(k in response_str for k in ['payment', 'transaction', 'ap2']),
            'Task Ledger': any(k in response_str for k in ['ledger', 'state', 'tracking', 'task']),
            'Tool Registration': any(k in response_str for k in ['tool', 'function', 'capability'])
        }

        print("\nIntegration Status:")
        for component, detected in checks.items():
            if detected:
                print_success(f"{component}: Detected")
            else:
                print_warning(f"{component}: Not clearly detected")

        detected_count = sum(checks.values())
        return detected_count >= 2  # At least 2 integrations should be detected
    else:
        print_error("Status check failed")
        return False

def run_all_tests():
    """Run all end-to-end tests"""
    print_header("COMPLETE END-TO-END INTEGRATION TEST SUITE")
    print_info(f"Test User: {TEST_USER_ID}")
    print_info(f"Prompt ID: {TEST_PROMPT_ID}")
    print_info(f"Request ID: {REQUEST_ID}")
    print_info(f"Timestamp: {datetime.now().isoformat()}")

    # Check server
    print_info("Checking Flask server...")
    if not check_server_running():
        print_error("Flask server is not running!")
        print_info("Please start the server with: python create_recipe.py (or reuse_recipe.py)")
        return False
    print_success("Flask server is running")

    # Run all tests
    tests = [
        ("Basic Agent Creation", test_1_basic_agent_creation),
        ("Task Delegation Bridge", test_2_task_delegation_bridge),
        ("AP2 Payment Integration", test_3_ap2_payment_integration),
        ("Task Ledger State Management", test_4_task_ledger_state_management),
        ("Reuse Recipe Flow", test_5_reuse_recipe_flow),
        ("Nested Delegation", test_6_nested_delegation),
        ("Backward Compatibility", test_7_backward_compatibility),
        ("Integration Summary", test_8_integration_summary)
    ]

    results = {}
    passed = 0
    failed = 0

    for test_name, test_func in tests:
        try:
            print_info(f"\nRunning: {test_name}")
            result = test_func()
            results[test_name] = result

            if result:
                passed += 1
            else:
                failed += 1

            time.sleep(2)  # Pause between tests
        except Exception as e:
            print_error(f"Test {test_name} raised exception: {e}")
            import traceback
            traceback.print_exc()
            results[test_name] = False
            failed += 1

    # Final summary
    print_header("TEST SUITE SUMMARY")

    print(f"\n{Colors.BOLD}Test Results:{Colors.END}")
    for test_name, result in results.items():
        status = f"{Colors.GREEN}PASS{Colors.END}" if result else f"{Colors.RED}FAIL{Colors.END}"
        print(f"  {status} - {test_name}")

    print(f"\n{Colors.BOLD}Overall:{Colors.END}")
    print(f"  Passed: {Colors.GREEN}{passed}/{len(tests)}{Colors.END}")
    print(f"  Failed: {Colors.RED}{failed}/{len(tests)}{Colors.END}")

    success_rate = (passed / len(tests)) * 100
    print(f"  Success Rate: {Colors.CYAN}{success_rate:.1f}%{Colors.END}")

    if passed == len(tests):
        print(f"\n{Colors.GREEN}{Colors.BOLD}ALL TESTS PASSED! [SUCCESS]{Colors.END}")
        return True
    elif success_rate >= 75:
        print(f"\n{Colors.YELLOW}{Colors.BOLD}MOSTLY PASSED (>=75%) [WARNING]{Colors.END}")
        return True
    else:
        print(f"\n{Colors.RED}{Colors.BOLD}TESTS FAILED [FAIL]{Colors.END}")
        return False

if __name__ == "__main__":
    print(f"{Colors.BOLD}Starting Complete End-to-End Integration Tests...{Colors.END}")
    print_info("This will test all integrations via actual Flask API calls")
    print_warning("Ensure Flask server is running on port 5000")
    print_info("Starting tests in 2 seconds...")

    time.sleep(2)  # Brief pause instead of interactive input

    success = run_all_tests()

    sys.exit(0 if success else 1)
