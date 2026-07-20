#!/usr/bin/env python3
"""HART OS dev-loop verify runner -- the "verify logs + see" leg of the
fix -> deploy(OTA) -> test -> verify -> see loop.

It pulls a peer node's boot journal over the token-gated LAN diagnostic
endpoint (hart-net-diag, GET http://<peer>:6699/diag?t=<TOKEN>), extracts the
signals that matter for iterating on the shell/OS (FAILED units, [shell-client]
JS errors now that console->journal is wired, the active theme, first-scanout,
LLM-model gate, Personalize/wifi health), diffs against the previous cycle's
snapshot, and prints a compact report. Exit code: 0 healthy/improved, 1 if a
new regression appeared (so it can gate a loop).

Modes:
  live   : --peer <ip> --token <tok>          fetch from :6699 and parse
  file   : --file <path>                        parse a captured journal/diag bundle
                                                (e.g. the HARTJRNL export)
  watch  : add --watch <seconds>                repeat the live fetch on an interval
                                                (the actual loop; Ctrl-C to stop)

Self-contained (std-lib only), cross-platform (dev box: Windows/macOS/Linux).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DIAG_PORT = 6699
DEFAULT_SNAPSHOT = os.path.join(
    os.environ.get('TMPDIR') or os.environ.get('TEMP') or '/tmp',
    'hart_loop_snapshot.json')

# ── signal patterns (matched line-by-line against the journal bundle) ──────────
_RE = {
    # a systemd unit that failed this boot
    'failed_unit': re.compile(
        r'(\S+\.service): (?:Failed with result|Main process exited, code=exited, status=[1-9])'),
    'failed_start': re.compile(r'Failed to start (.+?)\.?$'),
    # client-side JS errors, now forwarded by the console->journal hook
    'client_error': re.compile(r'\[shell-client\]\s*(.*)'),
    # the compositor's first real page-flip (the display went live)
    'first_scanout': re.compile(r'first real scanout|#131 first-scanout|physical display is LIVE'),
    # the local LLM model gate
    'llm_gated': re.compile(r'LLM stays gated|Model download failed|No module named .llama.'),
    # active theme (theme.changed event / apply)
    'theme': re.compile(r'Theme applied:\s*(\S+)|theme\.changed.*?theme_id[\'"]?\s*[:=]\s*[\'"]?(\w[\w-]*)'),
}


def fetch_diag(peer: str, token: str, timeout: float = 20.0, port: int = DIAG_PORT) -> str:
    """GET the token-gated diag bundle from a peer's :<port>/diag endpoint."""
    import urllib.parse
    url = 'http://%s:%d/diag?t=%s' % (peer, port, urllib.parse.quote(token, safe=''))
    req = urllib.request.Request(url, headers={'User-Agent': 'hart-loop-verify'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


# Console/serial journals carry inline ANSI SGR colour codes (the boot-console
# capture from a VM or a serial cable does -- systemd colourizes [ OK ]/[FAILED]).
# Strip them before matching so a unit name like "\x1b[0;1;39mfoo.service" parses
# as "foo.service" and not a coloured blob (real bug seen verifying a VM boot.log).
_ANSI = re.compile(r'\x1b\[[0-9;:]*m')


def parse_bundle(text: str) -> dict:
    """Extract the loop-relevant signals from a journal/diag bundle."""
    text = _ANSI.sub('', text)
    failed, client_errors, targets = set(), [], 0
    first_scanout = False
    llm_gated = False
    theme = None
    for line in text.splitlines():
        m = _RE['failed_unit'].search(line)
        if m:
            failed.add(m.group(1))
        m = _RE['failed_start'].search(line)
        if m and '.service' not in m.group(1):
            # 'Failed to start HART OS GPU Scheduler.' -> a human unit name
            failed.add(m.group(1).strip())
        m = _RE['client_error'].search(line)
        if m:
            msg = m.group(1).strip()
            if msg:
                client_errors.append(msg[:240])
        if _RE['first_scanout'].search(line):
            first_scanout = True
        if _RE['llm_gated'].search(line):
            llm_gated = True
        if 'Reached target' in line:
            targets += 1
        m = _RE['theme'].search(line)
        if m:
            theme = m.group(1) or m.group(2) or theme
    # de-dup client errors preserving order
    seen, uniq = set(), []
    for e in client_errors:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return {
        'failed_units': sorted(failed),
        'client_errors': uniq[:50],
        'first_scanout': first_scanout,
        'llm_gated': llm_gated,
        'active_theme': theme,
        'reached_targets': targets,
    }


def load_prev(path: str) -> dict | None:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_snapshot(path: str, parsed: dict) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(parsed, f, indent=2)
    except OSError:
        pass


def diff(prev: dict | None, cur: dict) -> dict:
    """What changed vs the previous cycle."""
    if not prev:
        return {'first_run': True, 'new_failures': cur['failed_units'],
                'resolved_failures': [], 'new_client_errors': cur['client_errors']}
    pf, cf = set(prev.get('failed_units', [])), set(cur['failed_units'])
    pe, ce = set(prev.get('client_errors', [])), set(cur['client_errors'])
    return {
        'first_run': False,
        'new_failures': sorted(cf - pf),
        'resolved_failures': sorted(pf - cf),
        'new_client_errors': [e for e in cur['client_errors'] if e not in pe],
        'theme_changed': (prev.get('active_theme') != cur.get('active_theme')),
    }


def report(parsed: dict, d: dict) -> int:
    """Print a compact report; return an exit code (1 == new regression)."""
    def line(x=''):
        print(x, flush=True)
    line('== HART loop verify ==')
    line('  active theme     : %s' % (parsed['active_theme'] or '(unknown)'))
    line('  display live      : %s' % ('YES (first-scanout)' if parsed['first_scanout'] else 'no'))
    line('  LLM model         : %s' % ('GATED (no model)' if parsed['llm_gated'] else 'ok'))
    line('  reached targets   : %d' % parsed['reached_targets'])
    line('  FAILED units (%d) : %s' % (len(parsed['failed_units']),
                                       ', '.join(parsed['failed_units']) or '(none)'))
    if parsed['client_errors']:
        line('  [shell-client] JS errors (%d):' % len(parsed['client_errors']))
        for e in parsed['client_errors'][:10]:
            line('      - ' + e)
    else:
        line('  [shell-client] JS errors : (none captured)')
    line('  -- since last cycle --')
    if d.get('first_run'):
        line('     (first snapshot; nothing to diff)')
    else:
        line('     resolved failures : %s' % (', '.join(d['resolved_failures']) or '(none)'))
        line('     NEW failures      : %s' % (', '.join(d['new_failures']) or '(none)'))
        if d['new_client_errors']:
            line('     NEW JS errors     :')
            for e in d['new_client_errors'][:10]:
                line('        - ' + e)
        if d.get('theme_changed'):
            line('     theme changed     : -> %s' % parsed['active_theme'])
    # First run is the BASELINE, never a regression. Only a later cycle that
    # introduces NEW failures/errors relative to the prior snapshot regresses.
    regressed = (not d.get('first_run')) and bool(d.get('new_failures') or d.get('new_client_errors'))
    if d.get('first_run'):
        verdict = 'baseline captured'
    else:
        verdict = 'REGRESSION (new failures/errors)' if regressed else 'ok / improved'
    line('  verdict           : %s' % verdict)
    return 1 if regressed else 0


def run_once(args) -> int:
    if args.file:
        with open(args.file, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    else:
        if not args.peer or not args.token:
            sys.stderr.write('live mode needs --peer <ip> and --token <tok> (or use --file)\n')
            return 2
        try:
            text = fetch_diag(args.peer, args.token, args.timeout, args.port)
        except (urllib.error.URLError, OSError) as e:
            sys.stderr.write('diag fetch failed: %s\n' % e)
            return 2
    parsed = parse_bundle(text)
    prev = load_prev(args.snapshot)
    d = diff(prev, parsed)
    code = report(parsed, d)
    save_snapshot(args.snapshot, parsed)
    return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='HART OS dev-loop verify runner (verify-logs leg).')
    p.add_argument('--peer', help='peer LAN IP/host (live mode)')
    p.add_argument('--token', help='diag token from the peer login MOTD (live mode)')
    p.add_argument('--file', help='parse a captured journal/diag bundle instead of fetching')
    p.add_argument('--snapshot', default=DEFAULT_SNAPSHOT, help='JSON snapshot for cross-cycle diff')
    p.add_argument('--timeout', type=float, default=20.0)
    p.add_argument('--port', type=int, default=int(os.environ.get('HART_DIAG_PORT', DIAG_PORT)),
                   help='diag port (default 6699; override for a relayed/tunnelled peer)')
    p.add_argument('--watch', type=int, default=0,
                   help='live loop: re-fetch + report every N seconds (Ctrl-C to stop)')
    args = p.parse_args(argv)
    if args.watch and not args.file:
        try:
            while True:
                run_once(args)
                print('  (next in %ds; Ctrl-C to stop)\n' % args.watch, flush=True)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    return run_once(args)


if __name__ == '__main__':
    raise SystemExit(main())
