"""No tracked Python file may begin with a UTF-8 BOM.

This is not a style nicety, it is the cheap half of a cross-repo build break.

Nunba's `Build & Sign Installers` links HARTOS in as a sibling and runs a
pre-build syntax gate over every file it is about to freeze. CPython rejects a
leading U+FEFF (`invalid non-printable character U+FEFF`), so ONE BOM anywhere in
HARTOS aborts the whole Windows installer build. MEASURED 2026-09-22: the
Windows job failed at "Build Nunba app (cx_Freeze + slim)" with

    [ERROR] Pre-build syntax gate: 1/4002 files failed to parse
    [ERROR]   _deps\\HARTOS\\tests\\unit\\test_agent_lightning_trace_path_is_one_dir.py:
              invalid non-printable character U+FEFF (line 1)

and the Windows installer had not built since 2026-09-20 because of it. Signing,
the Inno Setup step, the smoke tests and the artifact upload are all downstream,
so every one of them was skipped. Linux and macOS built fine, which is why this
was easy to miss.

That feedback arrives ~26 minutes into a build, on another repo's runner, in a log
nobody reads unless installs are already known to be broken. This test moves the
same finding to a few seconds inside HARTOS's own suite.

WHERE BOMs COME FROM HERE: PowerShell. `Out-File -Encoding utf8`, `>` and `>>`
write a BOM by default on this project's shells, and a redirect is the natural way
to create a file from a terminal. Use `-Encoding utf8NoBOM` (PowerShell 6+), or
`[IO.File]::WriteAllText` with a BOM-less encoding, or just write the file with an
editor tool.

To fix a file: drop its first three bytes (EF BB BF) and leave the rest alone.
"""
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOM = b'\xef\xbb\xbf'


def _tracked_python_files():
    """Paths git knows about, so vendored/ignored trees never fail this test.

    Returns None when git cannot answer (no git, not a checkout), which the test
    reports as a skip rather than a pass -- a check that silently examined zero
    files would be exactly the vacuous green this repo keeps getting burned by.
    """
    try:
        out = subprocess.run(
            ['git', 'ls-files', '*.py'],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [_REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def _starts_with_bom(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read(3) == _BOM
    except OSError:
        return False


class TestNoUtf8Bom(unittest.TestCase):

    def test_the_detector_actually_detects_a_bom(self):
        """Red-first, inlined: prove the check can FAIL before trusting it pass.

        A BOM is invisible in every editor and diff, so a detector that silently
        matched nothing would look identical to a clean tree."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bommed = Path(d) / 'bommed.py'
            bommed.write_bytes(_BOM + b'x = 1\n')
            clean = Path(d) / 'clean.py'
            clean.write_bytes(b'x = 1\n')
            self.assertTrue(_starts_with_bom(bommed),
                            'the detector missed a real BOM')
            self.assertFalse(_starts_with_bom(clean),
                             'the detector flagged a clean file')

    def test_no_tracked_python_file_has_a_bom(self):
        files = _tracked_python_files()
        if files is None:
            self.skipTest('git could not list tracked files')
        self.assertGreater(len(files), 100,
                           'suspiciously few tracked .py files -- the listing '
                           'is probably wrong, and a check over nothing passes '
                           'for the wrong reason')
        offenders = [str(p.relative_to(_REPO_ROOT)).replace('\\', '/')
                     for p in files if _starts_with_bom(p)]
        self.assertEqual(offenders, [], (
            'These files start with a UTF-8 BOM and will abort Nunba\'s Windows '
            'installer build at its pre-build syntax gate:\n  '
            + '\n  '.join(offenders)
            + '\nFix: remove the first three bytes (EF BB BF). Cause: almost '
              'certainly a PowerShell redirect or Out-File without -Encoding '
              'utf8NoBOM.'))


if __name__ == '__main__':
    unittest.main()
