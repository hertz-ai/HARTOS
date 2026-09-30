#!/usr/bin/env python3
"""The Python gate's failure RATCHET: fail on a NEW failing test, not on the
failures main already carries.

WHY
    The flake-checks Python shards have been red on every run for weeks, so a
    red shard no longer tells a PR that broke something from one that didn't.
    Each PR's CI had to be diffed against main's run by hand, red file by red
    file.  This script is that diff, made mechanical:

        failing IDs on this run  -  tests/ci_known_failures.txt  =  NEW

    Any NEW id fails the job.  A baseline entry that now passes is reported as
    a notice so it gets deleted; the list only ever shrinks.

INPUT (written by the shard step in .github/workflows/flake-checks.yml)
    <reports>/pytest-red-shard-<N>/red_files.tsv, one line per red test file:
        <pytest exit code>\t<test file path>\t<junit xml, relative to the tsv>
    It starts with a `#` header, so it is never empty, and ends with a
    `# complete:` line written after the shard ran every selected file.  A
    shard with no file, or a file without that line (killed part-way), has
    no verdict for the files it never reached, so the ratchet fails closed.

IDS
    ``classname.name`` exactly as the JUnit report spells it, parsed by the
    one JUnit reader in this repo, generate_regression_report.parse_junit_xml
    (e.g. tests.unit.test_x.TestY.test_z[param]).  A red file that yields no
    failing testcase still gets an id, so it can't hide:
        <file>::INTERPRETER_HANG     the per-file `timeout` fired (124/137)
        <file>::NO_REPORT_EXIT_<rc>  red, but no report or no failing testcase
        <file>::ABNORMAL_EXIT_<rc>   pytest stopped early (2 interrupted with no
                                     collection error, 3 internal error, 4
                                     usage): the report's failures, if any,
                                     are kept, but the tests it never ran are
                                     not vouched for by them

Usage:
    python scripts/ci_failure_ratchet.py --reports DIR --baseline FILE \
        --shards 8 [--write-current OUT]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_regression_report import parse_junit_xml  # noqa: E402

SHARD_DIR = 'pytest-red-shard-{}'
RED_FILES = 'red_files.tsv'
COMPLETE_MARKER = '# complete'
_HANG_EXIT_CODES = (124, 137)
_PYTEST_INTERRUPTED = 2       # also what a collection error exits with
_PYTEST_EARLY_EXITS = (_PYTEST_INTERRUPTED, 3, 4)


def red_file_failures(rc, path, xml_path):
    """The failing test ids of ONE red test file."""
    if rc in _HANG_EXIT_CODES:
        print(f'::warning::{path}: the interpreter did not exit (rc={rc}); '
              f'counted as one failure, its report (if any) is not trusted')
        return {f'{path}::INTERPRETER_HANG'}
    ids = set()
    collection_error = False
    if not os.path.isfile(xml_path):
        why = 'no JUnit report was written'
    else:
        report = parse_junit_xml(xml_path)
        for failed in report['test_details']:
            # A collection error has an empty classname, so the reader's
            # "classname.name" starts with the dot.
            collection_error |= failed['name'].startswith('.')
            ids.add(failed['name'].lstrip('.'))
        why = (f'its JUnit report is unreadable: {report["error"]}'
               if report.get('error') else
               'its JUnit report lists no failing testcase')
    early = rc in _PYTEST_EARLY_EXITS and not (
        rc == _PYTEST_INTERRUPTED and collection_error)
    if ids and early:
        # The report lists what failed before pytest stopped; the tests it
        # never reached are unaccounted for, so the file is flagged too.
        print(f'::warning::{path}: pytest stopped early (rc={rc}); its '
              f'report covers only the tests that ran')
        return ids | {f'{path}::ABNORMAL_EXIT_{rc}'}
    if ids:
        return ids
    print(f'::warning::{path}: red (rc={rc}) but {why}; counted as one '
          f'failure under a file-level id')
    return {f'{path}::NO_REPORT_EXIT_{rc}'}


def current_failures(reports, shards):
    """(failing ids across every shard, shard report dirs that are missing)."""
    ids, missing = set(), []
    for n in range(shards):
        tsv = os.path.join(reports, SHARD_DIR.format(n), RED_FILES)
        if not os.path.isfile(tsv):
            missing.append(SHARD_DIR.format(n))
            continue
        with open(tsv, encoding='utf-8') as fp:
            lines = fp.read().splitlines()
        if not any(line.startswith(COMPLETE_MARKER) for line in lines):
            print(f'::warning::{SHARD_DIR.format(n)}: its report has no '
                  f'"{COMPLETE_MARKER}" line, so the shard stopped part-way')
            missing.append(SHARD_DIR.format(n))
            continue
        for line in lines:
            if not line.strip() or line.startswith('#'):
                continue
            rc, path, xml_name = line.split('\t')
            ids |= red_file_failures(
                int(rc), path, os.path.join(os.path.dirname(tsv), xml_name))
    return ids, missing


def load_baseline(path):
    """Known failing ids: one per line; `#` starts a comment."""
    known = set()
    with open(path, encoding='utf-8') as fp:
        for line in fp:
            test_id = line.split('#', 1)[0].strip()
            if test_id:
                known.add(test_id)
    return known


def _summary(lines):
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a', encoding='utf-8') as fp:
            fp.write('\n'.join(lines) + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--reports', required=True)
    ap.add_argument('--baseline', required=True)
    ap.add_argument('--shards', type=int, required=True)
    ap.add_argument('--write-current', default=None,
                    help='also write this run\'s failing ids (a candidate baseline)')
    args = ap.parse_args(argv)

    current, missing = current_failures(args.reports, args.shards)
    known = load_baseline(args.baseline)
    new = sorted(current - known)
    fixed = sorted(known - current)

    if args.write_current:
        with open(args.write_current, 'w', encoding='utf-8') as fp:
            fp.write(''.join(f'{i}\n' for i in sorted(current)))

    for shard in missing:
        print(f'::error::no report from {shard}: that shard reached no verdict, '
              f'so this run cannot say nothing new failed there')
    for test_id in new:
        print(f'::error::new failing test: {test_id}')
    for test_id in fixed:
        print(f'::notice::known failure now passes, delete it from '
              f'{args.baseline}: {test_id}')

    _summary(
        ['## Python failure ratchet',
         f'{len(current)} failing on this run, {len(known)} known on main: '
         f'**{len(new)} new**, {len(fixed)} now passing, '
         f'{len(missing)} shard(s) without a report.']
        + ([''] + [f'- NEW `{i}`' for i in new] if new else [])
        + ([''] + [f'- missing report: `{s}`' for s in missing] if missing else [])
        + ([''] + [f'- now passes (delete from the baseline): `{i}`' for i in fixed]
           if fixed else []))
    print(f'{len(new)} new failing, {len(fixed)} known now passing, '
          f'{len(missing)} missing shard report(s)')
    return 1 if new or missing else 0


if __name__ == '__main__':
    sys.exit(main())
