"""
Live test of ALL HARTOS agent types via the running server.
Run: PYTHONIOENCODING=utf-8 python tests/test_all_agents_live.py
Requires: HARTOS server running on port 6777, LLM on port 8080
"""
import sys
sys.stdout.reconfigure(line_buffering=True)
import requests, json, time

BASE = 'http://127.0.0.1:6777'
R = []


def t(name, method, path, data=None, headers=None, ok_codes=None, timeout=120):
    if ok_codes is None:
        ok_codes = [200]
    t0 = time.time()
    try:
        if method == 'GET':
            r = requests.get(f'{BASE}{path}', headers=headers, timeout=timeout)
        else:
            r = requests.post(f'{BASE}{path}', json=data, headers=headers, timeout=timeout)
        el = time.time() - t0
        ok = r.status_code in ok_codes
        st = 'PASS' if ok else 'FAIL'
        body = r.text[:200]
        print(f'{st:4s} | {name:55s} | {r.status_code:3d} | {el:5.1f}s | {body[:100]}', flush=True)
        R.append({'name': name, 'st': st, 'code': r.status_code, 'time': el})
        return r
    except Exception as e:
        el = time.time() - t0
        print(f'ERR  | {name:55s} | --- | {el:5.1f}s | {str(e)[:100]}', flush=True)
        R.append({'name': name, 'st': 'ERR', 'code': 0, 'time': el})
        return None


# ── Get JWT ──
r = requests.post(f'{BASE}/api/social/auth/login',
                   json={'username': 'test_agent_user', 'password': 'Test123!@#'})
if r.status_code != 200:
    requests.post(f'{BASE}/api/social/auth/register',
                  json={'username': 'test_agent_user', 'email': 't@h.l', 'password': 'Test123!@#'})
    r = requests.post(f'{BASE}/api/social/auth/login',
                      json={'username': 'test_agent_user', 'password': 'Test123!@#'})
tok = r.json().get('data', {}).get('token', '')
A = {'Authorization': f'Bearer {tok}'} if tok else {}
print(f'Auth: {"OK" if tok else "FAIL"}', flush=True)
print('=' * 130, flush=True)

# ═══════════════════ HEALTH ═══════════════════
t('Health: /status', 'GET', '/status')
t('Health: /health', 'GET', '/health')
t('Health: /ready', 'GET', '/ready')

# ═══════════════════ CREATE PIPELINE ═══════════════════
t('CREATE: basic chat', 'POST', '/chat',
  {'user_id': 'tu1', 'prompt_id': 'b1', 'prompt': 'What is 2+2?'})
t('CREATE: agent mode', 'POST', '/chat',
  {'user_id': 'tu1', 'prompt_id': 'b_ag1',
   'prompt': 'Create a joke-telling agent', 'create_agent': True},
  timeout=180)

# ═══════════════════ VISUAL AGENT (Computer Use) ═══════════════════
t('VISUAL: computer use - click Start', 'POST', '/visual_agent',
  {'user_id': 'tu1', 'prompt_id': 'bv1',
   'task_description': 'Click the Start button', 'mode': 'computer_use'},
  timeout=120)
t('VISUAL: computer use - describe', 'POST', '/visual_agent',
  {'user_id': 'tu1', 'prompt_id': 'bv2',
   'task_description': 'Describe what is on screen', 'mode': 'computer_use'},
  timeout=120)

# ═══════════════════ TIME AGENT ═══════════════════
t('TIME: /time_agent', 'POST', '/time_agent',
  {'user_id': 'tu1', 'prompt_id': 'bt1', 'task_description': 'Report current time'})

# ═══════════════════ ZEROSHOT (classifier) ═══════════════════
t('ZEROSHOT: sentiment', 'POST', '/zeroshot/',
  {'user_id': 'tu1', 'input_text': 'I love this product',
   'labels': ['positive', 'negative', 'neutral']})

# ═══════════════════ ADD HISTORY ═══════════════════
t('HISTORY: add msgs', 'POST', '/add_history',
  {'user_id': '1', 'human_msg': 'What is 2+2?', 'ai_msg': '4'})

# ═══════════════════ PROMPTS ═══════════════════
t('PROMPTS: list', 'GET', '/prompts?user_id=tu1')

# ═══════════════════ SYSTEM ═══════════════════
t('SYSTEM: pressure', 'GET', '/api/system/pressure')
t('REVENUE: dashboard', 'GET', '/api/revenue/dashboard')
t('SETTINGS: compute', 'GET', '/api/settings/compute')

# ═══════════════════ INSTRUCTION QUEUE ═══════════════════
t('INSTR: enqueue', 'POST', '/api/instructions/enqueue',
  {'user_id': 'tu1', 'text': 'Research Python 3.13', 'priority': 5})
t('INSTR: pending', 'GET', '/api/instructions/pending?user_id=tu1')
t('INSTR: batch', 'POST', '/api/instructions/batch',
  {'user_id': 'tu1', 'max_tokens': 500})

# ═══════════════════ CODING AGENT ═══════════════════
t('CODING: list goals', 'GET', '/api/coding/goals', headers=A)

# ═══════════════════ GOAL ENGINE ═══════════════════
t('GOALS: list', 'GET', '/api/goals', headers=A)
t('GOALS: create marketing', 'POST', '/api/goals',
  {'goal_type': 'marketing', 'title': 'Awareness test',
   'description': 'Post about HARTOS', 'config': {'channels': ['platform']}},
  headers=A, ok_codes=[200, 201])
t('GOALS: create coding', 'POST', '/api/goals',
  {'goal_type': 'coding', 'title': 'Fix README',
   'description': 'Update README', 'config': {}},
  headers=A, ok_codes=[200, 201])
t('GOALS: create self_heal', 'POST', '/api/goals',
  {'goal_type': 'self_heal', 'title': 'Self-check',
   'description': 'Check test coverage', 'config': {}},
  headers=A, ok_codes=[200, 201])
t('GOALS: create autoresearch', 'POST', '/api/goals',
  {'goal_type': 'autoresearch', 'title': 'Research Python trends',
   'description': 'Find latest Python ecosystem news', 'config': {}},
  headers=A, ok_codes=[200, 201])

# ═══════════════════ SOCIAL API ═══════════════════
t('SOCIAL: communities', 'GET', '/api/social/communities', headers=A)
t('SOCIAL: feed all', 'GET', '/api/social/feed/all', headers=A)
t('SOCIAL: create post', 'POST', '/api/social/posts',
  {'title': 'Agent Test', 'content': 'Automated test post'},
  headers=A, ok_codes=[200, 201])
t('SOCIAL: notifications', 'GET', '/api/social/notifications', headers=A)
t('SOCIAL: search', 'GET', '/api/social/search?q=test', headers=A)
t('SOCIAL: theme presets', 'GET', '/api/social/theme/presets', headers=A)
t('SOCIAL: recipes', 'GET', '/api/social/recipes', headers=A)
t('SOCIAL: tasks', 'GET', '/api/social/tasks', headers=A)
t('SOCIAL: moderation reports', 'GET', '/api/social/moderation/reports',
  headers=A, ok_codes=[200, 403])

# ═══════════════════ A2A PROTOCOL ═══════════════════
t('A2A: well-known', 'GET', '/a2a/tu1_b1/.well-known/agent.json',
  ok_codes=[200, 404])

# ═══════════════════ HART CHALLENGE ═══════════════════
t('CHALLENGE: with nonce', 'GET', '/.well-known/hart-challenge?nonce=test123')

# ═══════════════════ CREDENTIALS ═══════════════════
t('CREDS: list', 'GET', '/api/credentials', ok_codes=[200, 403])

# ═══════════════════ AUTO-EVOLVE ═══════════════════
t('EVOLVE: start', 'POST', '/api/auto_evolve/start',
  {'max_experiments': 1}, headers=A, ok_codes=[200, 404])

# ═══════════════════ SUMMARY ═══════════════════
print('\n' + '=' * 80, flush=True)
passed = sum(1 for r in R if r['st'] == 'PASS')
failed = sum(1 for r in R if r['st'] == 'FAIL')
errored = sum(1 for r in R if r['st'] == 'ERR')
total = len(R)
print(f'TOTAL: {total} | PASS: {passed} ({100 * passed // total}%) | FAIL: {failed} | ERR: {errored}',
      flush=True)
if failed + errored > 0:
    print('\nFAILURES:', flush=True)
    for r in R:
        if r['st'] != 'PASS':
            print(f'  {r["st"]:4s} {r["name"]:55s} HTTP {r["code"]}', flush=True)

with open('tests/agent_test_results.json', 'w') as f:
    json.dump(R, f, indent=2)
print('\nSaved to tests/agent_test_results.json', flush=True)
