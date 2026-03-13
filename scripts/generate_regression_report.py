#!/usr/bin/env python3
"""
HART OS — Consolidated Regression Report Generator

Parses JUnit XML files from test-reports/junit/ and produces a
deterministic consolidated report with per-group counts.

Usage:
    python scripts/generate_regression_report.py [--junit-dir test-reports/junit]
    python scripts/generate_regression_report.py --output test-reports/consolidated_report.txt
"""

import os
import sys
import xml.etree.ElementTree as ET
import argparse
from datetime import datetime
from collections import OrderedDict


def classify_test_category(classname):
    """Classify a testcase into unit/integration/e2e/standalone category."""
    cn = classname.lower().replace('\\', '.').replace('/', '.')
    if 'tests.e2e.' in cn or '.e2e.' in cn:
        return 'e2e'
    if 'tests.integration.' in cn or '.integration.' in cn:
        return 'integration'
    if 'tests.standalone.' in cn or '.standalone.' in cn:
        return 'standalone'
    if 'realworld' in cn:
        return 'e2e'
    return 'unit'


def parse_junit_xml(filepath):
    """Parse a single JUnit XML file and return test counts + category breakdown."""
    empty = {'file': os.path.basename(filepath), 'error': None,
             'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0,
             'skipped': 0, 'time': 0.0, 'test_details': [],
             'by_category': {}}
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except (ET.ParseError, FileNotFoundError) as e:
        empty['error'] = str(e)
        return empty

    # Handle both <testsuites> wrapper and direct <testsuite>
    if root.tag == 'testsuites':
        suites = root.findall('testsuite')
    elif root.tag == 'testsuite':
        suites = [root]
    else:
        empty['error'] = 'Unknown XML root'
        return empty

    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0
    total_time = 0.0
    failed_tests = []
    # Category breakdown: {category: {tests, passed, failures, errors, skipped}}
    by_category = {}

    for suite in suites:
        tests = int(suite.get('tests', 0))
        failures = int(suite.get('failures', 0))
        errors = int(suite.get('errors', 0))
        skipped = int(suite.get('skipped', 0))
        time_s = float(suite.get('time', 0))

        total_tests += tests
        total_failures += failures
        total_errors += errors
        total_skipped += skipped
        total_time += time_s

        # Walk individual testcases for category classification + failure details
        for tc in suite.findall('testcase'):
            cn = tc.get('classname', '')
            cat = classify_test_category(cn)
            if cat not in by_category:
                by_category[cat] = {'tests': 0, 'passed': 0, 'failures': 0,
                                    'errors': 0, 'skipped': 0}
            by_category[cat]['tests'] += 1

            if tc.find('failure') is not None:
                by_category[cat]['failures'] += 1
                failed_tests.append({
                    'name': f"{cn}.{tc.get('name', '')}",
                    'type': 'FAIL',
                    'category': cat,
                    'message': (tc.find('failure').get('message', '') or '')[:120],
                })
            elif tc.find('error') is not None:
                by_category[cat]['errors'] += 1
                failed_tests.append({
                    'name': f"{cn}.{tc.get('name', '')}",
                    'type': 'ERROR',
                    'category': cat,
                    'message': (tc.find('error').get('message', '') or '')[:120],
                })
            elif tc.find('skipped') is not None:
                by_category[cat]['skipped'] += 1
            else:
                by_category[cat]['passed'] += 1

    total_passed = total_tests - total_failures - total_errors - total_skipped

    return {
        'file': os.path.basename(filepath),
        'tests': total_tests,
        'passed': total_passed,
        'failures': total_failures,
        'errors': total_errors,
        'skipped': total_skipped,
        'time': total_time,
        'test_details': failed_tests,
        'by_category': by_category,
    }


# Canonical group display order matching run_regression.bat groups
GROUP_DISPLAY_ORDER = [
    'ws_workstream',
    'security_hardening',
    'core_perf',
    'social',
    'p2p_security',
    'channel_infra',
    'channel_adapters',
    'channel_e2e',
    'agent_recipe',
    'session',
    'tools_ai',
    'integration',
    'distro',
    'resonance',
    'realworld',
]


def group_sort_key(filename):
    """Sort groups by canonical order, unknowns at end."""
    base = filename.replace('.xml', '').lower()
    # Strip timestamp suffix if present (e.g., "core_perf_20260228_143000")
    for name in GROUP_DISPLAY_ORDER:
        if base.startswith(name):
            return (GROUP_DISPLAY_ORDER.index(name), base)
    return (len(GROUP_DISPLAY_ORDER), base)


def friendly_group_name(filename):
    """Convert JUnit XML filename to friendly display name."""
    name_map = {
        'ws_workstream': 'WS Workstream (metered API, compute, revenue)',
        'security_hardening': 'Security Hardening (audit, DLP, classifier)',
        'core_perf': 'Core + Performance',
        'social': 'Social Platform',
        'p2p_security': 'P2P Network + Security',
        'channel_infra': 'Channel Infrastructure',
        'channel_adapters': 'Channel Adapters',
        'channel_e2e': 'Channel E2E',
        'agent_recipe': 'Agent + Recipe Pipeline',
        'session': 'Session + Messaging',
        'tools_ai': 'Tools + AI',
        'integration': 'Integration + Data',
        'distro': 'Distro + Deployment',
        'resonance': 'Resonance Tuning + Personality',
        'realworld': 'E2E Realworld Scenarios',
    }
    base = filename.replace('.xml', '').lower()
    for key, display in name_map.items():
        if base.startswith(key):
            return display
    return base.replace('_', ' ').title()


def generate_report(junit_dir, output_file=None):
    """Generate consolidated regression report from JUnit XML files."""
    if not os.path.isdir(junit_dir):
        print(f"ERROR: JUnit directory not found: {junit_dir}")
        return 1

    xml_files = sorted(
        [f for f in os.listdir(junit_dir) if f.endswith('.xml')],
        key=group_sort_key,
    )

    if not xml_files:
        print(f"ERROR: No JUnit XML files found in {junit_dir}")
        return 1

    # Parse all XML files
    results = OrderedDict()
    for xf in xml_files:
        data = parse_junit_xml(os.path.join(junit_dir, xf))
        results[xf] = data

    # Build report
    lines = []
    lines.append('=' * 80)
    lines.append('  HART OS — Consolidated Regression Report')
    lines.append(f'  Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  Source:     {os.path.abspath(junit_dir)}')
    lines.append('=' * 80)
    lines.append('')

    # Summary table header
    hdr = f'{"Group":<50} {"Tests":>6} {"Pass":>6} {"Fail":>6} {"Err":>5} {"Skip":>5} {"Time":>8}'
    lines.append(hdr)
    lines.append('-' * 80)

    grand_tests = 0
    grand_pass = 0
    grand_fail = 0
    grand_err = 0
    grand_skip = 0
    grand_time = 0.0
    all_failures = []

    for xf, data in results.items():
        name = friendly_group_name(xf)
        status_char = ''
        if data.get('error') and data['tests'] == 0:
            status_char = ' [PARSE ERROR]'
        elif data['failures'] > 0 or data['errors'] > 0:
            status_char = ' [!]'

        row = (f'{name:<50} {data["tests"]:>6} {data["passed"]:>6} '
               f'{data["failures"]:>6} {data["errors"]:>5} {data["skipped"]:>5} '
               f'{data["time"]:>7.1f}s{status_char}')
        lines.append(row)

        grand_tests += data['tests']
        grand_pass += data['passed']
        grand_fail += data['failures']
        grand_err += data['errors']
        grand_skip += data['skipped']
        grand_time += data['time']

        for td in data.get('test_details', []):
            all_failures.append({'group': name, **td})

    lines.append('-' * 80)
    lines.append(
        f'{"TOTAL":<50} {grand_tests:>6} {grand_pass:>6} '
        f'{grand_fail:>6} {grand_err:>5} {grand_skip:>5} '
        f'{grand_time:>7.1f}s')
    lines.append('')

    # ===== BY TEST CATEGORY =====
    # Aggregate category counts across all groups
    cat_totals = {}
    for xf, data in results.items():
        for cat, counts in data.get('by_category', {}).items():
            if cat not in cat_totals:
                cat_totals[cat] = {'tests': 0, 'passed': 0, 'failures': 0,
                                   'errors': 0, 'skipped': 0}
            for k in ('tests', 'passed', 'failures', 'errors', 'skipped'):
                cat_totals[cat][k] += counts[k]

    cat_display = {'unit': 'Unit Tests', 'integration': 'Integration Tests',
                   'e2e': 'E2E Tests', 'standalone': 'Standalone Tests'}
    cat_order = ['unit', 'integration', 'e2e', 'standalone']

    if cat_totals:
        lines.append('')
        lines.append('  BY TEST CATEGORY')
        lines.append('  ' + '-' * 76)
        cat_hdr = f'  {"Category":<30} {"Tests":>6} {"Pass":>6} {"Fail":>6} {"Err":>5} {"Skip":>5}'
        lines.append(cat_hdr)
        lines.append('  ' + '-' * 76)
        for cat in cat_order:
            if cat in cat_totals:
                c = cat_totals[cat]
                lines.append(
                    f'  {cat_display.get(cat, cat):<30} {c["tests"]:>6} {c["passed"]:>6} '
                    f'{c["failures"]:>6} {c["errors"]:>5} {c["skipped"]:>5}')
        # Any unlisted categories
        for cat in sorted(cat_totals.keys()):
            if cat not in cat_order:
                c = cat_totals[cat]
                lines.append(
                    f'  {cat:<30} {c["tests"]:>6} {c["passed"]:>6} '
                    f'{c["failures"]:>6} {c["errors"]:>5} {c["skipped"]:>5}')
        lines.append('  ' + '-' * 76)
        lines.append('')

    # Verdict
    if grand_fail == 0 and grand_err == 0:
        verdict = 'PASS'
    else:
        verdict = 'FAIL'
    lines.append(f'  Verdict: {verdict}')
    lines.append(f'  Pass rate: {grand_pass}/{grand_tests} '
                 f'({(grand_pass / grand_tests * 100) if grand_tests else 0:.1f}%)')
    lines.append('')

    # Failed test details
    if all_failures:
        lines.append('=' * 80)
        lines.append('  FAILURES / ERRORS')
        lines.append('=' * 80)
        for i, ft in enumerate(all_failures, 1):
            cat_label = ft.get('category', 'unit').upper()
            lines.append(f'  {i}. [{ft["type"]}] [{cat_label}] {ft["name"]}')
            lines.append(f'     Group: {ft["group"]}')
            if ft.get('message'):
                lines.append(f'     {ft["message"]}')
            lines.append('')

    lines.append('=' * 80)
    lines.append('  End of Report')
    lines.append('=' * 80)

    report_text = '\n'.join(lines)

    # Output
    print(report_text)

    if output_file:
        os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report_text)
        print(f'\nReport saved to: {output_file}')

    return 0 if verdict == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(
        description='HART OS Consolidated Regression Report Generator')
    parser.add_argument('--junit-dir', default='test-reports/junit',
                        help='Directory containing JUnit XML files')
    parser.add_argument('--output', '-o', default=None,
                        help='Save report to file (in addition to stdout)')
    args = parser.parse_args()

    sys.exit(generate_report(args.junit_dir, args.output))


if __name__ == '__main__':
    main()
