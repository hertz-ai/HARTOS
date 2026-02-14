"""
Integration Tests Runner

Runs all integration tests for:
- MCP (Model Context Protocol)
- Internal Agent Communication
- Google A2A Protocol
"""

import subprocess
import sys
import os

def run_test(test_path, test_name):
    """Run a single test and return result"""
    print(f"\n{'='*70}")
    print(f"Running: {test_name}")
    print('='*70)

    try:
        result = subprocess.run(
            [sys.executable, test_path],
            capture_output=True,
            text=True,
            timeout=30
        )

        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)

        if result.returncode == 0:
            print(f"[OK] {test_name} PASSED")
            return True
        else:
            print(f"[FAIL] {test_name} FAILED (exit code: {result.returncode})")
            return False

    except subprocess.TimeoutExpired:
        print(f"[FAIL] {test_name} TIMED OUT")
        return False
    except Exception as e:
        print(f"[FAIL] {test_name} ERROR: {e}")
        return False

def main():
    """Run all integration tests"""
    print("="*70)
    print("INTEGRATION TESTS SUITE")
    print("="*70)

    tests = [
        ("integrations/internal_comm/test_a2a_quick.py", "Internal Agent Communication"),
        ("integrations/google_a2a/test_google_a2a_quick.py", "Google A2A Protocol"),
    ]

    results = {}

    for test_path, test_name in tests:
        full_path = os.path.join(os.path.dirname(__file__), test_path)
        if os.path.exists(full_path):
            results[test_name] = run_test(full_path, test_name)
        else:
            print(f"[SKIP] {test_name} - Test file not found: {full_path}")
            results[test_name] = None

    # Print summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = sum(1 for r in results.values() if r is True)
    failed = sum(1 for r in results.values() if r is False)
    skipped = sum(1 for r in results.values() if r is None)

    for test_name, result in results.items():
        status = "[OK]" if result is True else "[FAIL]" if result is False else "[SKIP]"
        print(f"{status} {test_name}")

    print("\n" + "-"*70)
    print(f"Total: {len(results)} tests | Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
    print("-"*70)

    if failed > 0:
        print("\n[FAIL] Some tests failed!")
        sys.exit(1)
    elif passed == len(results):
        print("\n[OK] All tests passed!")
        sys.exit(0)
    else:
        print("\n[WARN] Some tests were skipped")
        sys.exit(0)

if __name__ == "__main__":
    main()
