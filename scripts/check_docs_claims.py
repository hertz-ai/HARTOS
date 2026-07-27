#!/usr/bin/env python3
"""Check the public docs for the mistakes we actually made.

Every rule here exists because a real claim in this repo was wrong in that
exact way. This is not a style linter, it is a regression test for
documentation, and the failures it catches were all found by hand first:

  broken path        CONTRIBUTING pointed at autoresearch_loop.py, which does
                     not exist. CAPABILITIES pointed at core/security/, which
                     does not exist. Both sent readers somewhere empty.

  bare number        "90% faster" in the comparison table, with the condition
                     ("on cache-equivalent inputs") dropped. "21 subcommands"
                     when there were 24. A number with no file next to it is
                     a number nobody can check.

  known tells        "load-bearing" got the docs identified as AI-written on
                     Hacker News. So did em dashes, "No X, no Y" runs, and
                     "honest" / "genuine" used as filler.

Usage:
    python scripts/check_docs_claims.py            # report
    python scripts/check_docs_claims.py --strict   # exit 1 on any finding
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The docs a stranger actually lands on. Internal notes are out of scope on
# purpose: docs/STEWARD_INSTRUCTION_LOG.md alone holds 8,272 em dashes and
# nobody arrives through it.
PUBLIC_DOCS = [
    'README.md',
    'CAPABILITIES.md',
    'OPEN_PROBLEMS.md',
    'CONTRIBUTING.md',
    'SECURITY.md',
    os.path.join('docs', 'IS_IT_AN_OS.md'),
]

SKIP_DIRS = {'.git', 'venv', '__pycache__', 'node_modules', 'target', '.mypy_cache'}

EM_DASH = '—'
EN_DASH = '–'

PATH_RE = re.compile(r'`([^`\n]+)`')
PATHY = re.compile(r'\.(py|rs|toml|nix|sh|json|md|txt|db|yml|yaml)$')
# Percentages only, deliberately.
#
# The first version of this flagged any integer and produced 24 findings,
# nearly all noise: "31 adapters" and "300 seconds" are facts, and a fact does
# not need a citation on the same line to be checkable. A percentage is
# different. It is almost always a performance or share claim doing
# persuasion, and every one that went wrong in this repo went wrong by having
# its condition removed ("90% faster", minus "on cache-equivalent inputs").
NUMBER_RE = re.compile(r'\b(\d+(?:\.\d+)?%)\s+(\w+)')
NO_X_NO_Y = re.compile(r'\bno [a-z]+.{0,40}?,\s*no [a-z]+', re.I | re.S)
TELLS = ('load-bearing', 'load bearing')
FILLER = ('honest', 'genuine')


def resolve(token: str) -> bool:
    """True if a cited path exists. Bare filenames may live anywhere."""
    token = token.strip().rstrip(':.,').split(':')[0]
    if not token or '<' in token or '*' in token or token.startswith(('http', '/')):
        return True  # template, glob or URL, not a repo path
    if os.path.exists(os.path.join(REPO, token)):
        return True
    base = os.path.basename(token.rstrip('/'))
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if base in files or base in dirs:
            return True
    return False


def check(path: str) -> list:
    full = os.path.join(REPO, path)
    if not os.path.exists(full):
        return [(path, 0, 'missing', 'document does not exist')]
    text = open(full, encoding='utf-8', errors='replace').read()
    lines = text.split('\n')
    out = []

    for i, line in enumerate(lines, 1):
        if EM_DASH in line or EN_DASH in line:
            # A verbatim quote of source may legitimately contain one.
            if '"' not in line:
                out.append((path, i, 'dash', line.strip()[:70]))
        for t in TELLS:
            if t in line.lower():
                out.append((path, i, 'tell', t))
        for m in NO_X_NO_Y.finditer(line):
            out.append((path, i, 'no-x-no-y', m.group(0)[:60]))
        for f in FILLER:
            if re.search(r'\b' + f + r'\w*\b', line, re.I):
                out.append((path, i, 'filler', f))

        # A number claim with no file cited on the same line is uncheckable.
        for m in NUMBER_RE.finditer(line):
            # A file citation on the same line makes it checkable.
            if PATH_RE.search(line) or 'http' in line:
                continue
            out.append((path, i, 'unsourced-percent',
                        '%s %s' % (m.group(1), m.group(2))))

    for m in PATH_RE.finditer(text):
        for part in re.split(r'[,\s]+', m.group(1).strip()):
            if PATHY.search(part) or part.endswith('/'):
                if not resolve(part):
                    ln = text[:m.start()].count('\n') + 1
                    out.append((path, ln, 'broken-path', part))
    return out


def main() -> int:
    strict = '--strict' in sys.argv
    findings = []
    for doc in PUBLIC_DOCS:
        findings.extend(check(doc))

    if not findings:
        print('check_docs_claims: clean across %d public docs' % len(PUBLIC_DOCS))
        return 0

    by_kind = {}
    for f in findings:
        by_kind.setdefault(f[2], []).append(f)

    # Broken paths first. They are the only kind that is always a defect.
    order = ['broken-path', 'missing', 'tell', 'no-x-no-y',
             'unsourced-percent', 'dash', 'filler']
    for kind in order:
        items = by_kind.get(kind)
        if not items:
            continue
        print('\n%s  (%d)' % (kind.upper(), len(items)))
        for path, line, _, detail in items[:20]:
            print('  %s:%s  %s' % (path, line, detail))
        if len(items) > 20:
            print('  ... and %d more' % (len(items) - 20))

    print('\n%d findings across %d public docs' % (findings and len(findings), len(PUBLIC_DOCS)))
    hard = len(by_kind.get('broken-path', [])) + len(by_kind.get('missing', []))
    if hard:
        print('%d are broken paths, which are always defects' % hard)
    return 1 if (strict and findings) or hard else 0


if __name__ == '__main__':
    sys.exit(main())
