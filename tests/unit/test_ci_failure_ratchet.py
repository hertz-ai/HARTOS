"""scripts/ci_failure_ratchet.py: the Python gate fails on a NEW failing test,
not on the failures main already carries.

The gate's shards have been red on every run for weeks, so "red" stopped
distinguishing a PR that broke something from one that didn't; each PR's CI
had to be diffed against main's by hand, file by file.  The ratchet turns that
diff into the verdict: every failing test ID is compared with
tests/ci_known_failures.txt (main's list), and only an ID outside it fails.

These run the REAL script on REAL pytest JUnit output (a child pytest over a
throwaway test file), and on the report layout the workflow uploads.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    'ci_failure_ratchet', os.path.join(_REPO, 'scripts', 'ci_failure_ratchet.py'))
ratchet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ratchet)


def _run_pytest(tmp_path, body, name='test_sample.py'):
    """Run a real pytest over `body`; return (rc, test path, junit path)."""
    pkg = tmp_path / 'tests' / 'unit'
    pkg.mkdir(parents=True, exist_ok=True)
    test_file = pkg / name
    test_file.write_text(textwrap.dedent(body))
    xml = tmp_path / (name + '.xml')
    rc = subprocess.run(
        [sys.executable, '-m', 'pytest', str(test_file.relative_to(tmp_path)),
         '-q', '-p', 'no:cacheprovider', '--rootdir', str(tmp_path),
         '--junitxml', str(xml)],
        cwd=tmp_path, capture_output=True, timeout=120).returncode
    return rc, str(test_file.relative_to(tmp_path)), str(xml)


def _shard(root, n, rows, complete=True):
    """The layout the shard step uploads: its reports next to red_files.tsv,
    which names them relative to itself, closed by the end marker the loop
    writes after its last file (absent when the shard was killed part-way)."""
    d = root / f'pytest-red-shard-{n}'
    d.mkdir(parents=True)
    lines = ['# exit code\ttest file\tjunit report\n']
    for rc, path, xml in rows:
        name = os.path.basename(xml)
        if os.path.isfile(xml):
            shutil.move(xml, d / name)
        lines.append(f'{rc}\t{path}\t{name}\n')
    if complete:
        lines.append('# complete: 3 files\n')
    (d / 'red_files.tsv').write_text(''.join(lines))
    return d


_SAMPLE = """
    import pytest

    def test_ok():
        pass

    def test_known_bad():
        assert 1 == 2

    class TestGroup:
        def test_new_bad(self):
            assert False

        @pytest.mark.parametrize('x', [1, 2])
        def test_param(self, x):
            assert x == 1
"""


def test_failing_ids_come_from_real_junit(tmp_path):
    rc, path, xml = _run_pytest(tmp_path, _SAMPLE)
    assert rc == 1
    assert ratchet.red_file_failures(rc, path, xml) == {
        'tests.unit.test_sample.test_known_bad',
        'tests.unit.test_sample.TestGroup.test_new_bad',
        'tests.unit.test_sample.TestGroup.test_param[2]',
    }


def test_a_collection_error_is_its_own_id(tmp_path):
    rc, path, xml = _run_pytest(tmp_path, 'import no_such_module_xyz\n',
                                name='test_broken.py')
    assert rc == 2
    ids = ratchet.red_file_failures(rc, path, xml)
    assert ids == {'tests.unit.test_broken'}


def test_a_hung_interpreter_is_red_even_if_every_test_passed(tmp_path):
    rc, path, xml = _run_pytest(tmp_path, 'def test_ok():\n    pass\n')
    assert rc == 0
    # The workflow's `timeout` kill turns the exit code into 124 after pytest
    # already wrote a green report.
    assert ratchet.red_file_failures(124, path, xml) == {
        'tests/unit/test_sample.py::INTERPRETER_HANG'}


def test_a_red_file_without_a_report_is_not_lost(tmp_path):
    assert ratchet.red_file_failures(1, 'tests/unit/test_x.py',
                                     str(tmp_path / 'missing.xml')) == {
        'tests/unit/test_x.py::NO_REPORT_EXIT_1'}


def test_only_new_failures_fail_the_gate(tmp_path, capsys):
    rc, path, xml = _run_pytest(tmp_path, _SAMPLE)
    reports = tmp_path / 'reports'
    _shard(reports, 0, [(rc, path, xml)])
    _shard(reports, 1, [])
    baseline = tmp_path / 'known.txt'
    baseline.write_text(
        '# main\n'
        'tests.unit.test_sample.test_known_bad\n'
        'tests.unit.test_sample.TestGroup.test_param[2]\n'
        'tests.unit.test_gone.test_fixed_on_this_branch\n')

    code = ratchet.main(['--reports', str(reports), '--baseline', str(baseline),
                         '--shards', '2'])

    out = capsys.readouterr().out
    assert code == 1
    errors = [l for l in out.splitlines() if l.startswith('::error::')]
    assert errors == [
        '::error::new failing test: tests.unit.test_sample.TestGroup.test_new_bad']
    # A baseline entry that no longer fails is reported, so it gets deleted.
    notices = [l for l in out.splitlines() if l.startswith('::notice::')]
    assert any('tests.unit.test_gone.test_fixed_on_this_branch' in l for l in notices)


def test_known_failures_only_is_green(tmp_path, capsys):
    rc, path, xml = _run_pytest(tmp_path, """
        def test_known_bad():
            assert 1 == 2
    """)
    reports = tmp_path / 'reports'
    _shard(reports, 0, [(rc, path, xml)])
    baseline = tmp_path / 'known.txt'
    baseline.write_text('tests.unit.test_sample.test_known_bad\n')

    assert ratchet.main(['--reports', str(reports), '--baseline', str(baseline),
                         '--shards', '1']) == 0
    assert '::error::' not in capsys.readouterr().out


def test_a_missing_shard_report_fails_closed(tmp_path, capsys):
    """A shard that died before uploading has no verdict; treating it as
    'no new failures' would be a green gate that tested nothing."""
    reports = tmp_path / 'reports'
    _shard(reports, 0, [])
    baseline = tmp_path / 'known.txt'
    baseline.write_text('')

    assert ratchet.main(['--reports', str(reports), '--baseline', str(baseline),
                         '--shards', '2']) == 1
    assert 'pytest-red-shard-1' in capsys.readouterr().out


def test_write_current_records_the_new_baseline(tmp_path):
    rc, path, xml = _run_pytest(tmp_path, _SAMPLE)
    reports = tmp_path / 'reports'
    _shard(reports, 0, [(rc, path, xml)])
    baseline = tmp_path / 'known.txt'
    baseline.write_text('')
    out = tmp_path / 'current.txt'

    ratchet.main(['--reports', str(reports), '--baseline', str(baseline),
                  '--shards', '1', '--write-current', str(out)])

    assert ratchet.load_baseline(str(out)) == {
        'tests.unit.test_sample.test_known_bad',
        'tests.unit.test_sample.TestGroup.test_new_bad',
        'tests.unit.test_sample.TestGroup.test_param[2]',
    }


def test_the_checked_in_baseline_parses_and_names_real_test_files():
    """Every entry must point at a test file that exists, or it can never be
    matched and only hides a real regression under a dead name."""
    known = ratchet.load_baseline(os.path.join(_REPO, 'tests', 'ci_known_failures.txt'))
    # An empty list is the goal (every test green on main), not a vacuous
    # check: the reading of each line is test_baseline_lines' job.
    for test_id in known:
        module_path = test_id.split('::')[0]
        if not module_path.endswith('.py'):
            parts = test_id.split('.')
            module_path = next(
                (os.sep.join(parts[:i]) + '.py' for i in range(len(parts), 0, -1)
                 if os.path.isfile(os.path.join(_REPO, os.sep.join(parts[:i]) + '.py'))),
                None)
        assert module_path and os.path.isfile(os.path.join(_REPO, module_path)), test_id


@pytest.mark.parametrize('line, expected', [
    ('  tests.unit.test_a.test_b  ', {'tests.unit.test_a.test_b'}),
    ('# a comment', set()),
    ('tests.unit.test_a.test_b  # why it fails', {'tests.unit.test_a.test_b'}),
    ('', set()),
])
def test_baseline_lines(tmp_path, line, expected):
    p = tmp_path / 'b.txt'
    p.write_text(line + '\n')
    assert ratchet.load_baseline(str(p)) == expected


def test_a_file_level_id_says_why(tmp_path, capsys):
    xml = tmp_path / 'broken.xml'
    xml.write_text('<not junit')
    ratchet.red_file_failures(1, 'tests/unit/test_x.py', str(xml))
    ratchet.red_file_failures(1, 'tests/unit/test_y.py', str(tmp_path / 'none.xml'))
    ratchet.red_file_failures(124, 'tests/unit/test_z.py', str(xml))
    warnings = [l for l in capsys.readouterr().out.splitlines()
                if l.startswith('::warning::')]
    assert any('test_x.py' in w and 'unreadable' in w for w in warnings)
    assert any('test_y.py' in w and 'no JUnit report' in w for w in warnings)
    assert any('test_z.py' in w and 'did not exit' in w for w in warnings)


def test_a_shard_killed_part_way_fails_closed(tmp_path, capsys):
    """No end marker: the files after the kill were never checked, so a
    report listing only known failures must not read as a clean shard."""
    rc, path, xml = _run_pytest(tmp_path, """
        def test_known_bad():
            assert 1 == 2
    """)
    reports = tmp_path / 'reports'
    _shard(reports, 0, [(rc, path, xml)], complete=False)
    baseline = tmp_path / 'known.txt'
    baseline.write_text('tests.unit.test_sample.test_known_bad\n')

    assert ratchet.main(['--reports', str(reports), '--baseline', str(baseline),
                         '--shards', '1']) == 1
    out = capsys.readouterr().out
    assert 'stopped part-way' in out and 'pytest-red-shard-0' in out


def test_an_early_pytest_exit_is_flagged_even_if_its_failures_are_known(tmp_path):
    """pytest.exit() after a known failure: the report lists only that
    failure, but the tests after it never ran."""
    rc, path, xml = _run_pytest(tmp_path, """
        import pytest

        def test_known_bad():
            assert 1 == 2

        def test_stops_the_run():
            pytest.exit('stopping')

        def test_never_runs():
            pass
    """)
    assert rc == 2
    ids = ratchet.red_file_failures(rc, path, xml)
    assert 'tests.unit.test_sample.test_known_bad' in ids
    assert 'tests/unit/test_sample.py::ABNORMAL_EXIT_2' in ids
