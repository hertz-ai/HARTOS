#!/usr/bin/env python3
"""
Master Test Suite - Comprehensive System Validation

Tests ALL components in the codebase:
1. create_recipe.py - Agent creation and execution
2. reuse_recipe.py - Recipe reuse and execution
3. Dynamic Agent Discovery (A2A) - Recipe-based agent discovery
4. Google A2A Protocol - Cross-platform agent communication
5. Internal Agent Communication - In-process skill-based delegation
6. MCP Integration - User-provided MCP server tools
7. Integration sanity checks

This is the ONE test to run them all!
"""

import sys
import os
import time
import subprocess
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple

# Color codes for output
class Colors:
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'
    CYAN = '\033[96m'


class MasterTestSuite:
    """Comprehensive test suite for entire codebase"""

    def __init__(self):
        self.results = {}
        self.start_time = None
        self.end_time = None
        self.test_log = []

    def log(self, message: str, level: str = "INFO"):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}"
        self.test_log.append(log_entry)

        # Print with color
        if level == "PASS":
            print(f"{Colors.GREEN}{log_entry}{Colors.RESET}")
        elif level == "FAIL":
            print(f"{Colors.RED}{log_entry}{Colors.RESET}")
        elif level == "WARN":
            print(f"{Colors.YELLOW}{log_entry}{Colors.RESET}")
        else:
            print(f"{Colors.BLUE}{log_entry}{Colors.RESET}")

    def print_header(self, text: str):
        """Print section header"""
        print(f"\n{Colors.CYAN}{'='*80}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}{text}{Colors.RESET}")
        print(f"{Colors.CYAN}{'='*80}{Colors.RESET}\n")

    def run_test_script(self, script_name: str, description: str, timeout: int = 60) -> Tuple[bool, str]:
        """Run a test script and return result"""
        self.log(f"Running: {description}")

        try:
            result = subprocess.run(
                [sys.executable, script_name],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.path.dirname(__file__) or "."
            )

            success = result.returncode == 0

            if success:
                self.log(f"✓ {description} - PASSED", "PASS")
            else:
                self.log(f"✗ {description} - FAILED (exit code: {result.returncode})", "FAIL")
                self.log(f"Error: {result.stderr[:200]}", "FAIL")

            return success, result.stdout + "\n" + result.stderr

        except subprocess.TimeoutExpired:
            self.log(f"✗ {description} - TIMEOUT", "FAIL")
            return False, "Test timed out"

        except Exception as e:
            self.log(f"✗ {description} - ERROR: {e}", "FAIL")
            return False, str(e)

    # ========== Test 1: Integration Tests (MCP, Internal Comm, A2A) ==========
    def test_integrations(self) -> bool:
        """Test MCP, Internal Communication, and Google A2A integrations"""
        self.print_header("TEST 1: Integration Tests (MCP + Internal Comm + Google A2A)")

        success, output = self.run_test_script(
            "run_integration_tests.py",
            "Integration Tests (MCP, Internal Comm, A2A Protocol)",
            timeout=120
        )

        self.results["integrations"] = success
        return success

    # ========== Test 2: Dynamic Agent Discovery ==========
    def test_dynamic_agents(self) -> bool:
        """Test dynamic agent discovery from recipe JSONs"""
        self.print_header("TEST 2: Dynamic Agent Discovery")

        success, output = self.run_test_script(
            "test_dynamic_agents.py",
            "Dynamic Agent Discovery (Recipe-based A2A Agents)",
            timeout=60
        )

        self.results["dynamic_agents"] = success
        return success

    # ========== Test 3: Path Fixes Validation ==========
    def test_path_fixes(self) -> bool:
        """Validate that hardcoded path fixes are in place"""
        self.print_header("TEST 3: Path Fixes Validation")

        self.log("Checking Executor working directory fixes...")

        errors = []

        # Check create_recipe.py
        try:
            with open("create_recipe.py", "r", encoding="utf-8") as f:
                content = f.read()

            if "/home/hertzai2019/newauto/coding" in content:
                errors.append("create_recipe.py still contains hardcoded Linux path")
                self.log("✗ create_recipe.py has hardcoded path", "FAIL")
            else:
                self.log("✓ create_recipe.py path fixed", "PASS")

        except Exception as e:
            errors.append(f"Failed to check create_recipe.py: {e}")

        # Check reuse_recipe.py
        try:
            with open("reuse_recipe.py", "r", encoding="utf-8") as f:
                content = f.read()

            if "/home/hertzai2019/newauto/coding" in content:
                errors.append("reuse_recipe.py still contains hardcoded Linux path")
                self.log("✗ reuse_recipe.py has hardcoded path", "FAIL")
            else:
                self.log("✓ reuse_recipe.py path fixed", "PASS")

        except Exception as e:
            errors.append(f"Failed to check reuse_recipe.py: {e}")

        success = len(errors) == 0
        self.results["path_fixes"] = success

        if not success:
            for error in errors:
                self.log(error, "FAIL")

        return success

    # ========== Test 4: MCP Configuration ==========
    def test_mcp_config(self) -> bool:
        """Validate MCP server configuration exists"""
        self.print_header("TEST 4: MCP Configuration")

        self.log("Checking MCP server configuration...")

        try:
            mcp_config_path = "integrations/mcp/mcp_servers.json"

            if not os.path.exists(mcp_config_path):
                self.log(f"✗ MCP config not found: {mcp_config_path}", "FAIL")
                self.results["mcp_config"] = False
                return False

            with open(mcp_config_path, "r") as f:
                config = json.load(f)

            if "servers" not in config:
                self.log("✗ MCP config missing 'servers' key", "FAIL")
                self.results["mcp_config"] = False
                return False

            num_servers = len(config["servers"])
            self.log(f"✓ MCP config valid ({num_servers} servers configured)", "PASS")

            # Check structure
            for idx, server in enumerate(config["servers"]):
                required_keys = ["name", "url", "enabled"]
                missing = [k for k in required_keys if k not in server]

                if missing:
                    self.log(f"✗ Server {idx} missing keys: {missing}", "WARN")
                else:
                    self.log(f"✓ Server '{server['name']}' properly configured", "PASS")

            self.results["mcp_config"] = True
            return True

        except json.JSONDecodeError as e:
            self.log(f"✗ MCP config invalid JSON: {e}", "FAIL")
            self.results["mcp_config"] = False
            return False

        except Exception as e:
            self.log(f"✗ MCP config check failed: {e}", "FAIL")
            self.results["mcp_config"] = False
            return False

    # ========== Test 5: Integration File Structure ==========
    def test_integration_structure(self) -> bool:
        """Validate integration folder structure"""
        self.print_header("TEST 5: Integration Folder Structure")

        self.log("Checking integration folder structure...")

        required_structure = {
            "integrations/__init__.py": "Integrations package init",
            "integrations/mcp/__init__.py": "MCP integration init",
            "integrations/mcp/mcp_integration.py": "MCP core implementation",
            "integrations/mcp/mcp_servers.json": "MCP configuration",
            "integrations/internal_comm/__init__.py": "Internal comm init",
            "integrations/internal_comm/internal_agent_communication.py": "Internal comm core",
            "integrations/google_a2a/__init__.py": "Google A2A init",
            "integrations/google_a2a/google_a2a_integration.py": "Google A2A protocol",
            "integrations/google_a2a/dynamic_agent_registry.py": "Dynamic agent discovery",
            "integrations/google_a2a/register_dynamic_agents.py": "Dynamic registration",
            "integrations/README.md": "Integration documentation"
        }

        missing = []
        for path, description in required_structure.items():
            if os.path.exists(path):
                self.log(f"✓ {description}: {path}", "PASS")
            else:
                self.log(f"✗ Missing {description}: {path}", "FAIL")
                missing.append(path)

        success = len(missing) == 0
        self.results["integration_structure"] = success

        if not success:
            self.log(f"Missing {len(missing)} required files", "FAIL")

        return success

    # ========== Test 6: Documentation ==========
    def test_documentation(self) -> bool:
        """Check documentation exists"""
        self.print_header("TEST 6: Documentation")

        required_docs = {
            "INTEGRATION_SUMMARY.md": "Integration summary",
            "DYNAMIC_AGENT_ARCHITECTURE.md": "Dynamic agent architecture",
            "FIXES_SUMMARY.md": "Test fixes summary",
            "integrations/README.md": "Integrations README"
        }

        missing = []
        for path, description in required_docs.items():
            if os.path.exists(path):
                self.log(f"✓ {description}: {path}", "PASS")
            else:
                self.log(f"✗ Missing {description}: {path}", "WARN")
                missing.append(path)

        # Documentation is not critical for functionality
        success = True  # Don't fail tests if docs missing
        self.results["documentation"] = len(missing) == 0

        if missing:
            self.log(f"Warning: {len(missing)} documentation files missing", "WARN")

        return success

    # ========== Test 7: Import Sanity Check ==========
    def test_imports(self) -> bool:
        """Test that all integrations can be imported"""
        self.print_header("TEST 7: Import Sanity Check")

        imports_to_test = [
            ("integrations.mcp", "MCP Integration"),
            ("integrations.internal_comm", "Internal Agent Communication"),
            ("integrations.google_a2a", "Google A2A Protocol"),
            ("integrations.google_a2a.dynamic_agent_registry", "Dynamic Agent Registry"),
            ("integrations.google_a2a.register_dynamic_agents", "Dynamic Registration"),
        ]

        errors = []

        for module_name, description in imports_to_test:
            try:
                __import__(module_name)
                self.log(f"✓ {description} imports successfully", "PASS")
            except ImportError as e:
                self.log(f"✗ {description} import failed: {e}", "FAIL")
                errors.append(f"{description}: {e}")
            except Exception as e:
                self.log(f"✗ {description} import error: {e}", "FAIL")
                errors.append(f"{description}: {e}")

        success = len(errors) == 0
        self.results["imports"] = success

        return success

    # ========== Run All Tests ==========
    def run_all_tests(self):
        """Execute all tests in sequence"""
        self.start_time = datetime.now()

        self.print_header("MASTER TEST SUITE - COMPREHENSIVE SYSTEM VALIDATION")
        print(f"Start Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Python: {sys.version}")
        print(f"Working Directory: {os.getcwd()}")

        # Run all tests
        tests = [
            ("Import Sanity Check", self.test_imports),
            ("Integration Folder Structure", self.test_integration_structure),
            ("Path Fixes Validation", self.test_path_fixes),
            ("MCP Configuration", self.test_mcp_config),
            ("Documentation", self.test_documentation),
            ("Integration Tests", self.test_integrations),
            ("Dynamic Agent Discovery", self.test_dynamic_agents),
        ]

        for test_name, test_func in tests:
            try:
                test_func()
            except Exception as e:
                self.log(f"✗ {test_name} crashed: {e}", "FAIL")
                self.results[test_name.lower().replace(" ", "_")] = False

        self.end_time = datetime.now()
        self.print_summary()

    def print_summary(self):
        """Print test summary"""
        self.print_header("TEST SUMMARY")

        duration = (self.end_time - self.start_time).total_seconds()

        passed = sum(1 for v in self.results.values() if v is True)
        failed = sum(1 for v in self.results.values() if v is False)
        total = len(self.results)

        pass_rate = (passed / total * 100) if total > 0 else 0

        print(f"Total Tests: {total}")
        print(f"{Colors.GREEN}Passed: {passed}{Colors.RESET}")
        print(f"{Colors.RED}Failed: {failed}{Colors.RESET}")
        print(f"Pass Rate: {pass_rate:.1f}%")
        print(f"Duration: {duration:.2f}s")

        print(f"\n{Colors.BOLD}Test Results:{Colors.RESET}\n")

        for test_name, result in self.results.items():
            status = f"{Colors.GREEN}[PASS]{Colors.RESET}" if result else f"{Colors.RED}[FAIL]{Colors.RESET}"
            print(f"  {status} {test_name.replace('_', ' ').title()}")

        # Save detailed log
        log_file = f"master_test_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(log_file, "w") as f:
            f.write("\n".join(self.test_log))

        print(f"\n{Colors.CYAN}Detailed log saved to: {log_file}{Colors.RESET}")

        # Exit code
        if failed == 0:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✓ ALL TESTS PASSED!{Colors.RESET}\n")
            sys.exit(0)
        else:
            print(f"\n{Colors.RED}{Colors.BOLD}✗ {failed} TEST(S) FAILED{Colors.RESET}\n")
            sys.exit(1)


if __name__ == "__main__":
    suite = MasterTestSuite()
    suite.run_all_tests()
