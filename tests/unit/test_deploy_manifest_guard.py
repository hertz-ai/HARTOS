"""The deploy must not be able to pin itself at an old SHA, silently.

deploy/deepbox_deploy.sh bind-mounts ./release_manifest.json into the container.
If that host path is missing when `docker run` executes, Docker CREATES IT AS A
ROOT-OWNED DIRECTORY. The container then writes its manifest inside it, and the
workflow's cleanup step -- running as the deploy user -- can no longer unlink
it. With `set -e` that aborts BEFORE `git reset --hard origin/main`, so the
checkout freezes at the previously deployed SHA while every later push appears
to deploy and changes nothing.

Measured 2026-09-01: the box sat at 3abea78b for hours, the workflow red on
"rm: cannot remove 'release_manifest.json/release_manifest.json': Permission
denied", and the directory's contents named "version": "3abea78b". The deploy
was pinned by its own leftover artifact, and four pushed fixes never landed.

Runs standalone (`python tests/unit/test_deploy_manifest_guard.py`).
"""
import os
import re
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SCRIPT = os.path.join(_ROOT, 'deploy', 'deepbox_deploy.sh')
_WORKFLOW = os.path.join(_ROOT, '.github', 'workflows', 'deploy-hartos-deepbox.yml')


def _read(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


class WorkflowCleanupTest(unittest.TestCase):

    def test_cleanup_uses_sudo(self):
        """A bare rm cannot remove a root-owned directory Docker created."""
        src = _read(_WORKFLOW)
        self.assertIn('sudo rm -rf release_manifest.json', src)

    def test_no_bare_rm_of_the_manifest_remains(self):
        src = _read(_WORKFLOW)
        for line in src.splitlines():
            s = line.strip()
            if s.startswith('rm -rf release_manifest.json'):
                self.fail('bare `rm -rf release_manifest.json` still present: '
                          'it cannot clear a root-owned Docker-created dir')

    def test_cleanup_runs_before_the_checkout(self):
        """Ordering is the whole bug: cleanup failing must not pre-empt the
        reset, and the reset must still come after it."""
        # Match COMMAND lines only: the surrounding comment quotes both
        # strings, so a plain str.index() finds the prose first.
        cmds = [l.strip() for l in _read(_WORKFLOW).splitlines()
                if l.strip() and not l.strip().startswith('#')]
        rm = next(i for i, l in enumerate(cmds)
                  if l.endswith('rm -rf release_manifest.json'))
        reset = next(i for i, l in enumerate(cmds)
                     if l == 'git reset --hard origin/main')
        self.assertLess(rm, reset)


class ScriptGuardTest(unittest.TestCase):

    def test_script_rejects_a_directory(self):
        src = _read(_SCRIPT)
        self.assertIn('if [ -d release_manifest.json ]; then', src)
        self.assertIn('is a DIRECTORY', src)

    def test_script_rejects_a_missing_manifest(self):
        """Continuing would let docker run recreate it as a directory."""
        src = _read(_SCRIPT)
        self.assertIn('if [ ! -f release_manifest.json ]; then', src)

    def test_guards_abort_rather_than_warn(self):
        src = _read(_SCRIPT)
        start = src.index('if [ -d release_manifest.json ]; then')
        end = src.index('ls -l release_manifest.json', start)
        self.assertEqual(src.count('exit 1', start, end), 2,
                         'both guards must abort the deploy, not warn')

    def test_guards_run_before_the_bind_mount(self):
        src = _read(_SCRIPT)
        guard = src.index('if [ -d release_manifest.json ]; then')
        mount = src.index('release_manifest.json:/app/release_manifest.json')
        self.assertLess(guard, mount,
                        'the guard must run before docker run can create a dir')

    def test_script_still_parses(self):
        """`bash -n` the script, feeding it on STDIN rather than by path.

        Passing _SCRIPT as an argument fails on a Windows dev host: git-bash
        eats the backslashes as escapes, so the path arrives as
        `C:Userssathihartos-reflashdeploydeepbox_deploy.sh` and bash reports
        `No such file or directory` (exit 127). That reads exactly like a
        missing deploy script, which is alarming and untrue. `bash -n` with no
        file argument reads the script from stdin, so the syntax check is the
        same and no path crosses the shell boundary at all.
        """
        import shutil
        import subprocess
        if not shutil.which('bash'):
            self.skipTest('no bash on this host to syntax-check with')
        # BYTES, not text. Two separate traps live in text mode here, and both
        # produce failures that look like a broken deploy script when the
        # script is perfectly fine (`bash -n` on the file passes):
        #   * the locale codec, cp1252 on this box, cannot encode the
        #     box-drawing characters in the script's comments;
        #   * Windows newline translation rewrites \n as \r\n, so `}` arrives
        #     as `}\r`, stops terminating its block, and bash reports
        #     "syntax error near unexpected token `}'".
        # Reading and writing raw bytes sidesteps both, on every platform.
        with open(_SCRIPT, 'rb') as fh:
            script_bytes = fh.read()
        # CR stripped. git checks this file out with CRLF on a Windows working
        # tree, and bash then sees `}\r`, which no longer terminates its block:
        # "syntax error near unexpected token `}'" for a script that is
        # perfectly valid (bash -n on the file passes under git-bash, and CI
        # checks it out with LF, so this never fires there). Shell syntax does
        # not depend on carriage returns, so removing them tests the script
        # rather than the checkout.
        r = subprocess.run(['bash', '-n'], input=script_bytes.replace(b'\r\n', b'\n'),
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         r.stderr.decode('utf-8', 'replace')[:400])


if __name__ == '__main__':
    unittest.main(verbosity=2)
