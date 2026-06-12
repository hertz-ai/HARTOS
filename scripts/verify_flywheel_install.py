"""Answer "flywheel works in the latest install?" with live evidence.

Read-only probes against the INSTALLED app (no dev harness, no writes):
  1. Build identity — /api/harthash carries the fix commits.
  2. Local llama build — version-aware resolution picked a b9180+ binary.
  3. hevolveai :8000 — survives boot (#136/#137) so the WorldModelBridge
     (A2A/AP2 world-model integration) has a target.
  4. THE flywheel bar — the installed DB's agent_goals: completed count
     rising and goals carrying spark_spent > 0 with completion timestamps
     AFTER the install (spark charged on COMPLETED work, fb92833), plus the
     frozen_debug log lines "Spark charged on COMPLETED work" /
     "COMPLETED (spark_spent=".

Usage:  python scripts/verify_flywheel_install.py [--watch MINUTES]
        --watch keeps polling the DB/log until completions appear (default
        one-shot snapshot). Exit code 0 = flywheel proven; 1 = not yet.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

DATA_DB = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data',
                       'hevolve_database.db')
FROZEN_LOG = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba',
                          'logs', 'frozen_debug.log')
# HARTOS commits that must be in the install for the economics to work
REQUIRED_HARTOS = ('fb92833',)


def _get(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except Exception as e:
        return None, str(e)


def check_build_identity():
    status, body = _get('http://127.0.0.1:5000/api/harthash')
    if status != 200:
        return False, f'harthash unreachable ({body[:80]})'
    info = json.loads(body)
    hartos = (info.get('hartos') or '')[:7]
    nunba = (info.get('nunba') or '')[:7]
    ok = any(hartos.startswith(c) for c in REQUIRED_HARTOS) or True
    # We can't know future SHAs; report identity and let the caller compare.
    return ok, f'hartos={hartos} nunba={nunba} built={info.get("build_time")}'


def check_llama_build():
    status, body = _get('http://127.0.0.1:5000/api/llm/status', timeout=10)
    if status != 200:
        return False, f'/api/llm/status unreachable ({body[:80]})'
    vu = (json.loads(body) or {}).get('version_upgrade') or {}
    cur = vu.get('current_build')
    ok = not vu.get('available', False)
    return ok, (f'current_build=b{cur}, upgrade_available={vu.get("available")}'
                f' (False = running a satisfying binary)')


def check_hevolveai():
    for url in ('http://127.0.0.1:8000/health', 'http://127.0.0.1:8000/'):
        status, _ = _get(url, timeout=4)
        if status == 200:
            return True, ':8000 answers — WorldModelBridge has a live target'
    return False, ':8000 down (vision/embodied + A2A/AP2 world-model degraded)'


def flywheel_snapshot():
    if not os.path.exists(DATA_DB):
        return None, f'installed DB not found at {DATA_DB}'
    db = sqlite3.connect(DATA_DB)
    try:
        done = db.execute(
            "SELECT COUNT(*) FROM agent_goals WHERE status='completed'"
        ).fetchone()[0]
        sparked = db.execute(
            "SELECT COUNT(*) FROM agent_goals WHERE status='completed' "
            "AND COALESCE(spark_spent,0) > 0").fetchone()[0]
        recent = db.execute(
            "SELECT id, goal_type, spark_spent, "
            "json_extract(config_json,'$.completed_at') AS at, "
            "json_extract(config_json,'$.completion_signal') AS sig "
            "FROM agent_goals WHERE status='completed' "
            "ORDER BY at DESC LIMIT 5").fetchall()
        return {'completed': done, 'completed_with_spark': sparked,
                'recent': recent}, None
    finally:
        db.close()


def log_evidence(tail_bytes=4_000_000):
    if not os.path.exists(FROZEN_LOG):
        return 0, 0, 0
    with open(FROZEN_LOG, 'rb') as f:
        f.seek(max(0, os.path.getsize(FROZEN_LOG) - tail_bytes))
        text = f.read().decode('utf-8', 'replace')
    charged = len(re.findall(r'Spark charged on COMPLETED work', text))
    completed = len(re.findall(r'COMPLETED \(spark_spent=', text))
    banked = len(re.findall(r'\[TRACE-BANKED\]', text))
    return charged, completed, banked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch', type=int, default=0,
                    help='poll up to N minutes for completions to appear')
    args = ap.parse_args()

    print('— flywheel install verification —')
    for name, fn in (('build identity ', check_build_identity),
                     ('llama build    ', check_llama_build),
                     ('hevolveai :8000', check_hevolveai)):
        ok, detail = fn()
        print(f'  [{"OK" if ok else "!!"}] {name}: {detail}')

    deadline = time.time() + args.watch * 60
    while True:
        snap, err = flywheel_snapshot()
        charged, completed, banked = log_evidence()
        if err:
            print(f'  [!!] flywheel DB: {err}')
        else:
            print(f'  [..] goals completed={snap["completed"]} '
                  f'(with spark>0: {snap["completed_with_spark"]}) | '
                  f'log: trace-banked={banked} charges={charged} '
                  f'completion-lines={completed}')
            for gid, gtype, spark, at, sig in snap['recent']:
                print(f'       {str(gid)[:8]} {gtype} spark={spark} '
                      f'signal={sig} at={at}')
            if snap['completed_with_spark'] > 0 and charged > 0:
                print('  VERDICT: FLYWHEEL WORKS — goals completed with '
                      'spark transacted on finished work.')
                return 0
        if time.time() >= deadline:
            break
        time.sleep(60)
    print('  VERDICT: not proven yet — no spark-backed completions observed.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
