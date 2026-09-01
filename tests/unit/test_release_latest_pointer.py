"""/releases/latest must never resolve to a build that failed its own gate.

release.yml stamps the gate verdict into every nightly's title and notes so a
consumer "can tell a green-gated preview from a red-gated one AT DOWNLOAD TIME"
-- and then, until 2026-09-01, nothing consulted that verdict when choosing the
pointer that decides what download time even means. The only check on the
latest pointer was desktop-ARTIFACT PRESENCE, which asks whether the files are
there, not whether the build passed.

Measured live 2026-09-01: /releases/latest resolved to
nightly-ead46e3-33450810556, publicly titled "HART OS nightly ead46e3 (gate
FAILURE)", prerelease=false. It had desktop ISOs, so it took the pointer. Every
other nightly was correctly prerelease=true, so that one red build WAS the
default download ahead of the signed v1.0.0, and it is what the
hevolve.ai/download "Releases" link resolves to.

These tests execute the real decision block out of release.yml against a stub
`gh`, so they check behaviour rather than grepping for strings.

Runs standalone (`python tests/unit/test_release_latest_pointer.py`).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_WORKFLOW = os.path.join(_REPO, '.github', 'workflows', 'release.yml')

_START = '# --- GATE-VERDICT gate'
_END = 'Pruning old nightly-'

# written into a stub `gh` on PATH; keeps backslashes out of this source
stub_body = '#!/bin/sh' + chr(10) + 'echo "$@" >> "%s"' + chr(10)


def _decision_block():
    """The real promote/skip logic, lifted verbatim from the workflow."""
    import yaml
    with open(_WORKFLOW, encoding='utf-8') as fh:
        doc = yaml.safe_load(fh)
    for job in doc['jobs'].values():
        for step in job.get('steps') or []:
            run = step.get('run') or ''
            if _START in run:
                frag = run[run.index(_START):run.index(_END)]
                # drop the trailing partial echo line before the prune section
                frag = frag[:frag.rindex('\n')]
                return re.sub(r'\$\{\{[^}]*\}\}', 'success', frag)
    raise AssertionError('GATE-VERDICT decision block not found in release.yml')


class ReleaseLatestPointerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.block = _decision_block()

    def _run(self, has_desktop, gate):
        """Execute the decision with a stub gh; return the gh args it invoked."""
        tmp = tempfile.mkdtemp()
        try:
            ghdir = os.path.join(tmp, 'bin')
            os.makedirs(ghdir)
            log = os.path.join(tmp, 'gh.log')
            gh = os.path.join(ghdir, 'gh')
            with open(gh, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(stub_body % log.replace(os.sep, '/'))
            os.chmod(gh, 0o755)

            script = (
                'set -e\n'
                f'HAS_DESKTOP={has_desktop}\n'
                f'GATE={gate}\n'
                'TAG=nightly-testsha-1\n'
                'REPO=hertz-ai/HARTOS\n'
                'SHORT_SHA=testsha\n'
                + self.block + '\n'
            )
            sp = os.path.join(tmp, 'run.sh')
            with open(sp, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(script)

            env = dict(os.environ, PATH=ghdir + os.pathsep + os.environ['PATH'])
            r = subprocess.run(['bash', sp], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, f'decision block failed: {r.stderr[:500]}')
            if not os.path.exists(log):
                return ''
            with open(log, encoding='utf-8') as fh:
                return fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── the regression that shipped ────────────────────────────────────
    def test_red_gate_with_desktop_never_takes_latest(self):
        """The exact ead46e3 case: artifacts present, gate failed."""
        out = self._run('true', 'failure')
        self.assertIn('--latest=false', out)
        self.assertNotIn('--latest=true', out)
        self.assertIn('--prerelease=true', out)
        self.assertNotIn('--prerelease=false', out)

    def test_red_gate_still_publishes(self):
        """Artifacts are real and a debugger needs them; only the pointer moves."""
        out = self._run('true', 'failure')
        self.assertIn('--draft=false', out)

    def test_cancelled_gate_is_also_not_success(self):
        out = self._run('true', 'cancelled')
        self.assertIn('--latest=false', out)
        self.assertNotIn('--latest=true', out)

    # ── the happy path must be unchanged ───────────────────────────────
    def test_green_gate_with_desktop_takes_latest(self):
        out = self._run('true', 'success')
        self.assertIn('--latest=true', out)
        self.assertIn('--prerelease=false', out)

    # ── the pre-existing desktop-presence contract must survive ────────
    def test_no_desktop_never_takes_latest_even_when_green(self):
        out = self._run('false', 'success')
        self.assertIn('--latest=false', out)
        self.assertNotIn('--latest=true', out)
        self.assertIn('partial: no desktop', out)

    def test_no_desktop_and_red_gate_never_takes_latest(self):
        out = self._run('false', 'failure')
        self.assertIn('--latest=false', out)
        self.assertNotIn('--latest=true', out)

    def test_only_one_combination_is_promotable(self):
        """Exactly one of the four states may take the pointer."""
        promoted = [
            (d, g) for d in ('true', 'false') for g in ('success', 'failure')
            if '--latest=true' in self._run(d, g)
        ]
        self.assertEqual(promoted, [('true', 'success')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
