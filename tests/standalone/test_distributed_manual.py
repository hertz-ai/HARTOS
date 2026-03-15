#!/usr/bin/env python3
"""
Manual Distributed API Test Script (Option B)

Tests the distributed task coordination endpoints against a live server.
Run this after starting the backend with HEVOLVE_DISTRIBUTED_MODE=true.

Usage:
    # Start backend with distributed mode + Redis:
    HEVOLVE_DISTRIBUTED_MODE=true HEVOLVE_AGENT_ENGINE_ENABLED=true python hart_intelligence_entry.py

    # In another terminal:
    python scripts/test_distributed_manual.py [--base-url http://localhost:6777]
"""
import os
import sys
import json
import time
import argparse
import requests


def main():
    parser = argparse.ArgumentParser(description='Manual distributed API test')
    parser.add_argument('--base-url', default='http://localhost:6777',
                        help='Base URL of the HART backend')
    parser.add_argument('--auth-token', default=None,
                        help='JWT auth token (if auth is enabled)')
    args = parser.parse_args()

    base = args.base_url.rstrip('/')
    headers = {'Content-Type': 'application/json'}
    if args.auth_token:
        headers['Authorization'] = f'Bearer {args.auth_token}'

    passed = 0
    failed = 0

    def check(name, condition, detail=''):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f'  [PASS] {name}')
        else:
            failed += 1
            print(f'  [FAIL] {name} -- {detail}')

    print(f'=== Distributed API Manual Test ===')
    print(f'Target: {base}')
    print()

    # 1. Health check
    print('--- 1. Health Check ---')
    try:
        r = requests.get(f'{base}/status', timeout=5)
        check('Backend reachable', r.status_code == 200, f'HTTP {r.status_code}')
    except requests.RequestException as e:
        check('Backend reachable', False, str(e))
        print('\nBackend not running. Start with:')
        print('  HEVOLVE_DISTRIBUTED_MODE=true python hart_intelligence_entry.py')
        sys.exit(1)
    print()

    # 2. List hosts (empty initially)
    print('--- 2. List Distributed Hosts ---')
    try:
        r = requests.get(f'{base}/api/distributed/hosts', headers=headers, timeout=5)
        if r.status_code == 503:
            print('  [WARN] Redis not available -- distributed features disabled')
            print('  Start Redis: docker run -d -p 6379:6379 redis:7-alpine')
            sys.exit(1)
        data = r.json()
        check('List hosts endpoint', r.status_code == 200, f'HTTP {r.status_code}')
        check('Hosts response has list', 'hosts' in data, json.dumps(data)[:100])
        print(f'  Hosts found: {len(data.get("hosts", []))}')
    except Exception as e:
        check('List hosts endpoint', False, str(e))
    print()

    # 3. Register this machine as a host
    print('--- 3. Register Host ---')
    try:
        reg_data = {
            'host_id': 'test-host-manual',
            'host_url': base,
            'capabilities': ['marketing', 'coding', 'news', 'finance'],
            'compute_budget': {'max_cpu_percent': 80, 'max_memory_gb': 4},
        }
        r = requests.post(f'{base}/api/distributed/hosts/register',
                          headers=headers, json=reg_data, timeout=5)
        data = r.json()
        check('Register host', data.get('success', False), json.dumps(data)[:100])
    except Exception as e:
        check('Register host', False, str(e))
    print()

    # 4. Submit a goal
    print('--- 4. Submit Distributed Goal ---')
    goal_id = None
    try:
        goal_data = {
            'objective': 'Test distributed marketing campaign',
            'tasks': [
                {
                    'task_id': 'test_mkt_001',
                    'description': 'Write a Twitter post about HART distributed computing',
                    'capabilities': ['marketing'],
                },
                {
                    'task_id': 'test_mkt_002',
                    'description': 'Write a LinkedIn post about HART OS features',
                    'capabilities': ['marketing'],
                },
            ],
            'context': {
                'goal_type': 'marketing',
                'user_id': 'manual_test',
                'prompt': 'Create social media content promoting HART distributed AI',
            },
        }
        r = requests.post(f'{base}/api/distributed/goals',
                          headers=headers, json=goal_data, timeout=5)
        data = r.json()
        check('Submit goal', data.get('success', False), json.dumps(data)[:100])
        goal_id = data.get('goal_id')
        print(f'  Goal ID: {goal_id}')
    except Exception as e:
        check('Submit goal', False, str(e))
    print()

    # 5. Claim a task
    print('--- 5. Claim Task ---')
    task_id = None
    try:
        claim_data = {
            'agent_id': 'manual-test-worker',
            'capabilities': ['marketing'],
        }
        r = requests.post(f'{base}/api/distributed/tasks/claim',
                          headers=headers, json=claim_data, timeout=5)
        data = r.json()
        check('Claim task endpoint', data.get('success', False), json.dumps(data)[:100])
        task_id = data.get('task_id')
        if task_id:
            print(f'  Claimed task: {task_id}')
            print(f'  Description: {data.get("description", "")[:80]}')
        else:
            print(f'  No tasks available (worker loop may have already claimed)')
    except Exception as e:
        check('Claim task endpoint', False, str(e))
    print()

    # 6. Submit result
    if task_id:
        print('--- 6. Submit Result ---')
        try:
            result_data = {
                'agent_id': 'manual-test-worker',
                'result': 'Manual test result: HART makes AI distributed! #HARTOS',
            }
            r = requests.post(f'{base}/api/distributed/tasks/{task_id}/submit',
                              headers=headers, json=result_data, timeout=5)
            data = r.json()
            check('Submit result', data.get('success', False), json.dumps(data)[:100])
            print(f'  Result hash: {data.get("result_hash", "")[:32]}...')
        except Exception as e:
            check('Submit result', False, str(e))
        print()

        # 7. Verify result
        print('--- 7. Verify Result ---')
        try:
            verify_data = {'agent_id': 'manual-test-verifier'}
            r = requests.post(f'{base}/api/distributed/tasks/{task_id}/verify',
                              headers=headers, json=verify_data, timeout=5)
            data = r.json()
            check('Verify result', data.get('success', False), json.dumps(data)[:100])
            check('Verification passed', data.get('verified', False),
                  f'verified={data.get("verified")}')
        except Exception as e:
            check('Verify result', False, str(e))
        print()

    # 8. Check progress
    if goal_id:
        print('--- 8. Goal Progress ---')
        try:
            r = requests.get(f'{base}/api/distributed/goals/{goal_id}/progress',
                             headers=headers, timeout=5)
            data = r.json()
            check('Goal progress endpoint', data.get('success', False),
                  json.dumps(data)[:100])
            print(f'  Total tasks: {data.get("total_tasks", 0)}')
            print(f'  Completed: {data.get("completed", 0)}')
            print(f'  Progress: {data.get("progress_pct", 0)}%')
            for t in data.get('tasks', []):
                print(f'    - {t["task_id"]}: {t["status"]} '
                      f'(claimed by: {t.get("claimed_by", "none")})')
        except Exception as e:
            check('Goal progress endpoint', False, str(e))
        print()

    # 9. Create baseline
    print('--- 9. Create Baseline ---')
    try:
        r = requests.post(f'{base}/api/distributed/baselines',
                          headers=headers, json={'label': 'manual_test'}, timeout=5)
        data = r.json()
        check('Create baseline', data.get('success', False), json.dumps(data)[:100])
        print(f'  Snapshot ID: {data.get("snapshot_id", "")}')
    except Exception as e:
        check('Create baseline', False, str(e))
    print()

    # Summary
    print('=' * 50)
    print(f'Results: {passed} passed, {failed} failed out of {passed + failed} checks')
    if failed == 0:
        print('All checks passed!')
    else:
        print(f'{failed} check(s) failed -- see details above')
    sys.exit(1 if failed > 0 else 0)


if __name__ == '__main__':
    main()
