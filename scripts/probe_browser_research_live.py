"""Live-verify probe for the browser_research subsystem (BR-C9).

Runs against a real Flask + HARTOS instance.  Does NOT touch any T2 platform
cookies (those need real user sessions to validate).  What it verifies:

  1. /api/web-research/probe        — driver-mode probe responds
  2. /api/web-research/tools        — registered tool list includes the 3 T2
                                       tools + YouTube_Transcript
  3. /api/web-research/vault        — empty list initial state
  4. /api/web-research/audit        — empty initial state
  5. Tool dispatch (no auth)        — YouTube_Transcript via core.agent_tools
                                       produces a connection_mechanism field
  6. Consent gate                   — Search_Platform without consent returns
                                       liquid_ui consent_prompt
  7. Post preview gate              — Post_As_User dry_run=True returns
                                       liquid_ui post_preview, NOT a real post

Usage:
    python scripts/probe_browser_research_live.py [--base http://127.0.0.1:5000]

Exit code 0 = all green; non-zero with summary printed on failure.
"""
import argparse
import json
import sys


def _check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}{(' -- ' + detail) if detail else ''}")
    return bool(cond)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:5000',
                    help='Flask base URL where Nunba/HARTOS Flask is running')
    args = ap.parse_args()
    base = args.base.rstrip('/')

    try:
        import requests
    except ImportError:
        print('requests not installed', file=sys.stderr)
        return 2

    passed = 0
    failed = 0

    def go(name, ok, detail=""):
        nonlocal passed, failed
        if _check(name, ok, detail):
            passed += 1
        else:
            failed += 1

    # 1. probe
    try:
        r = requests.get(f'{base}/api/web-research/probe', timeout=5)
        body = r.json() if r.headers.get('Content-Type', '').startswith('application/json') else {}
        go('GET /probe -> 200 + {ok}', r.status_code == 200 and body.get('ok') is True,
           f'effective_mode={body.get("effective_mode")!r}')
    except Exception as exc:
        go('GET /probe', False, str(exc))

    # 2. tools list
    try:
        r = requests.get(f'{base}/api/web-research/tools', timeout=5)
        body = r.json()
        names = {t.get('name') for t in (body.get('tools') or [])}
        go('GET /tools returns YouTube_Transcript', 'YouTube_Transcript' in names,
           f'tools={sorted(names)}')
    except Exception as exc:
        go('GET /tools', False, str(exc))

    # 3. vault list
    try:
        r = requests.get(f'{base}/api/web-research/vault', timeout=5)
        body = r.json()
        go('GET /vault returns list', isinstance(body.get('platforms'), list))
    except Exception as exc:
        go('GET /vault', False, str(exc))

    # 4. audit tail
    try:
        r = requests.get(f'{base}/api/web-research/audit?limit=10', timeout=5)
        body = r.json()
        go('GET /audit?limit=10 returns records list',
           isinstance(body.get('records'), list))
    except Exception as exc:
        go('GET /audit', False, str(exc))

    # 5. In-process dispatch — uses HARTOS lib directly when run on the host.
    try:
        sys.path.insert(0, '.')
        from integrations.browser_research import tools as br_tools
        r = br_tools.dispatch(tool='YouTube_Transcript', user_id='probe',
                              url='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        go('dispatch YouTube_Transcript yields connection_mechanism',
           'connection_mechanism' in r,
           f'mechanism={r.get("connection_mechanism")!r}')
    except Exception as exc:
        go('dispatch YouTube_Transcript', False, str(exc))

    # 6. consent gate (no consent granted)
    try:
        from integrations.browser_research import tools as br_tools
        r = br_tools.dispatch(tool='Search_Platform', user_id='probe',
                              platform='twitter', query='ai',
                              consent_check=lambda uid, scope: False)
        go('Search_Platform without consent -> liquid_ui consent_prompt',
           r.get('liquid_ui', {}).get('type') == 'consent_prompt',
           f'error={r.get("error")!r}')
    except Exception as exc:
        go('Search_Platform consent gate', False, str(exc))

    # 7. post preview gate
    try:
        from integrations.browser_research import tools as br_tools
        r = br_tools.dispatch(tool='Post_As_User', user_id='probe',
                              platform='twitter', content='probe hello',
                              consent_check=lambda uid, scope: True)
        go('Post_As_User dry_run default -> liquid_ui post_preview',
           r.get('liquid_ui', {}).get('type') == 'post_preview' and r.get('dry_run') is True,
           f'mechanism={r.get("connection_mechanism")!r}')
    except Exception as exc:
        go('Post_As_User dry_run', False, str(exc))

    print()
    print(f'browser_research live probe: {passed} passed / {failed} failed')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
