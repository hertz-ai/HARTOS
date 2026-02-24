#!/usr/bin/env python
"""
HART OS — Automation Test Suite with Runtime Code Coverage

Usage:
    python tests/run_coverage.py              # Run all unit tests with coverage
    python tests/run_coverage.py --html       # Also generate HTML report
    python tests/run_coverage.py --ws-only    # Only WS1-WS8 new code coverage
    python tests/run_coverage.py --full       # Full suite (unit + integration)

Outputs (all under test-reports/):
    - test-reports/coverage/          HTML coverage browser (with --html)
    - test-reports/coverage/.coverage Raw coverage data
    - test-reports/junit/coverage_results.xml  JUnit XML
    - test-reports/logs/coverage_run.txt       Console log
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

REPORT_DIR = os.path.join(ROOT, 'test-reports')
JUNIT_DIR = os.path.join(REPORT_DIR, 'junit')
COVERAGE_DIR = os.path.join(REPORT_DIR, 'coverage')
LOGS_DIR = os.path.join(REPORT_DIR, 'logs')

# Ensure output dirs exist
for d in [JUNIT_DIR, COVERAGE_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

# Test file groups
WS_TESTS = [
    'tests/unit/test_compute_config.py',
    'tests/unit/test_model_routing.py',
    'tests/unit/test_metered_recovery.py',
    'tests/unit/test_settings_api.py',
]

REGRESSION_TESTS = [
    'tests/unit/test_budget_gate.py',
    'tests/unit/test_boot_hardening.py',
    'tests/unit/test_revenue_pipeline.py',
    'tests/unit/test_trading_agents.py',
    'tests/unit/test_voting_rules.py',
    'tests/unit/test_model_lifecycle.py',
    'tests/unit/test_system_requirements.py',
    'tests/unit/test_ad_hosting_rewards.py',
    'tests/unit/test_integrity_system.py',
    'tests/unit/test_federation_upgrade.py',
]

SECURITY_TESTS = [
    'tests/unit/test_immutable_audit_log.py',
    'tests/unit/test_tool_allowlist.py',
    'tests/unit/test_goal_rate_limit.py',
    'tests/unit/test_action_classifier.py',
    'tests/unit/test_dlp_engine.py',
    'tests/unit/test_build_verification.py',
]

# Source modules for WS1-WS8 coverage focus
WS_SOURCES = [
    'integrations/agent_engine/compute_config.py',
    'integrations/agent_engine/model_registry.py',
    'integrations/agent_engine/budget_gate.py',
    'integrations/agent_engine/revenue_aggregator.py',
    'integrations/agent_engine/tool_allowlist.py',
    'integrations/social/hosting_reward_service.py',
    'integrations/social/models.py',
    'integrations/coding_agent/tool_backends.py',
    'integrations/vlm/qwen3vl_backend.py',
    'security/immutable_audit_log.py',
    'security/action_classifier.py',
    'security/dlp_engine.py',
    'security/rate_limiter_redis.py',
    'threadlocal.py',
]


def run_tests(test_files, html=False, ws_only=False):
    """Run pytest with coverage and report results."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    cmd = [sys.executable, '-m', 'pytest']
    cmd.extend(test_files)
    cmd.extend(['-v', '--noconftest', '--tb=short'])

    # JUnit XML output
    junit_path = os.path.join(JUNIT_DIR, f'coverage_{timestamp}.xml')
    cmd.append(f'--junitxml={junit_path}')

    # Coverage options
    if ws_only:
        for src in WS_SOURCES:
            cmd.extend(['--cov', src])
    else:
        cmd.extend(['--cov=integrations', '--cov=security', '--cov=threadlocal'])

    cmd.append('--cov-report=term-missing')

    if html:
        cmd.append(f'--cov-report=html:{COVERAGE_DIR}')

    cmd.append('--cov-config=.coveragerc')

    header = (
        f"\n{'='*70}\n"
        f"HART OS Automation Test Suite\n"
        f"Tests: {len(test_files)} files\n"
        f"Coverage: {'WS focus' if ws_only else 'Full project'}\n"
        f"Outputs: test-reports/\n"
        f"{'='*70}\n"
    )
    print(header)

    # Capture output to log file AND console
    log_path = os.path.join(LOGS_DIR, f'coverage_{timestamp}.log')
    with open(log_path, 'w') as log_file:
        log_file.write(header)
        log_file.write(f"Command: {' '.join(cmd)}\n\n")

    result = subprocess.run(cmd, cwd=ROOT)

    summary = f"\n{'='*70}\n"
    if result.returncode == 0:
        summary += "RESULT: ALL TESTS PASSED\n"
    else:
        summary += f"RESULT: FAILURES (exit code {result.returncode})\n"
    summary += f"JUnit XML: {junit_path}\n"
    if html:
        summary += f"Coverage HTML: {os.path.join(COVERAGE_DIR, 'index.html')}\n"
    summary += f"Log: {log_path}\n"
    summary += f"{'='*70}\n"

    print(summary)
    with open(log_path, 'a') as log_file:
        log_file.write(summary)

    return result.returncode


def main():
    parser = argparse.ArgumentParser(description='HART OS Test Suite with Coverage')
    parser.add_argument('--html', action='store_true', help='Generate HTML coverage report')
    parser.add_argument('--ws-only', action='store_true', help='Only WS1-WS8 code coverage')
    parser.add_argument('--full', action='store_true', help='Run all tests (unit + regression)')
    args = parser.parse_args()

    if args.full:
        test_files = WS_TESTS + REGRESSION_TESTS + SECURITY_TESTS
    else:
        test_files = WS_TESTS + REGRESSION_TESTS + SECURITY_TESTS

    return run_tests(test_files, html=args.html, ws_only=args.ws_only)


if __name__ == '__main__':
    sys.exit(main())
