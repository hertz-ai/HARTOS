"""Two deploy scripts must never run on the box at the same time.

The workflow's cancel-in-progress cancels the GitHub RUN. It does not stop the
remote bash that appleboy/ssh-action started, so a cancelled run's script keeps
building, booting and promoting on the box while the next run's script does
the same in the same checkout. Measured 2026-09-22 (#133): the cancelled
7eb0810f script booted its image at 15:37Z, AFTER the 5fe8f8901 run's
checkout at 15:32Z, and the code-hash cache it wrote into the shared agent_data
mount made the newer image fail its boot check as tampered. Central was down
for 41 minutes.

The guard is a flock taken in the workflow's inline script, before the
checkout, on a file descriptor that deploy/deepbox_deploy.sh inherits: one
lock encloses the reset, the build, the run and the rollback branch. A second
run waits, and says who it is waiting for.

Runs standalone (`python tests/unit/test_deploy_lock.py`).
"""
import os
import re
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SCRIPT = os.path.join(_ROOT, 'deploy', 'deepbox_deploy.sh')
_WORKFLOW = os.path.join(_ROOT, '.github', 'workflows', 'deploy-hartos-deepbox.yml')


def _read(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def _inline_block():
    """The `script: |` block of the ssh step, dedented, comments kept."""
    lines = _read(_WORKFLOW).splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == 'script: |')
    indent = len(lines[start + 1]) - len(lines[start + 1].lstrip())
    body = []
    for l in lines[start + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) < indent:
            break
        body.append(l[indent:] if len(l) >= indent else '')
    return body


def _inline_script():
    """The same block as command lines only."""
    return [l for l in _inline_block()
            if l.strip() and not l.strip().startswith('#')]


def _bash_has(tool):
    import subprocess
    r = subprocess.run(['bash', '-c', 'command -v ' + tool],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return r.returncode == 0


class LockPlacementTest(unittest.TestCase):

    def test_lock_is_taken_before_anything_touches_the_checkout(self):
        cmds = _inline_script()
        lock = next(i for i, l in enumerate(cmds) if l.startswith('exec 9>'))
        rm = next(i for i, l in enumerate(cmds)
                  if 'rm -rf release_manifest.json' in l)
        reset = next(i for i, l in enumerate(cmds)
                     if l.strip() == 'git reset --hard origin/main')
        run = next(i for i, l in enumerate(cmds)
                   if l.strip() == 'bash deploy/deepbox_deploy.sh')
        self.assertLess(lock, rm)
        self.assertLess(lock, reset)
        self.assertLess(lock, run)

    def test_the_lock_is_held_not_just_probed(self):
        """`flock -n` alone would make the second run FAIL, i.e. a push during a
        deploy would deploy nothing. It must wait (bounded) and then proceed."""
        src = '\n'.join(_inline_script())
        self.assertIn('flock -n 9', src)
        self.assertRegex(src, r'flock -w \d+ 9')

    def test_a_timed_out_wait_fails_closed(self):
        """`flock -w` returns non-zero when it gives up. Falling through that
        line unlocked would reproduce the interleave exactly, now with a log
        line saying it waited. The wait must end in exit 1, holder named."""
        line = next(l for l in _inline_script() if re.search(r'flock -w \d+ 9', l))
        self.assertRegex(line, r'flock -w \d+ 9 \|\| \{.*\$_holder.*exit 1; \}',
                         'the timed-out wait must exit, not fall through: %r' % line)

    def test_the_wait_is_bounded_below_the_job_timeout(self):
        """A silent kill at command_timeout names nothing; the lock wait must
        give up first, with the holder in the log."""
        src = '\n'.join(_inline_script())
        wait = int(re.search(r'flock -w (\d+) 9', src).group(1))
        timeout_m = int(re.search(r'command_timeout:\s*(\d+)m', _read(_WORKFLOW)).group(1))
        self.assertLess(wait, timeout_m * 60)

    def test_a_waiting_run_says_who_it_waits_for(self):
        """The next incident's log must read "held by run X since T", not
        nothing. The holder file is written by whoever holds the lock and
        read by whoever waits."""
        src = '\n'.join(_inline_script())
        self.assertIn('deploy.lock held by $_holder', src)
        self.assertIn('acquired after', src)
        self.assertRegex(src, r'echo "run \$\{\{ github\.run_id \}\} sha \$\{\{ github\.sha \}\} since .*" > "\$DEPLOY_LOCK\.holder"')

    def test_holder_is_recorded_only_once_the_lock_is_held(self):
        cmds = _inline_script()
        acquire = next(i for i, l in enumerate(cmds) if l.startswith('exec 9>'))
        holder = next(i for i, l in enumerate(cmds)
                      if l.strip().endswith('> "$DEPLOY_LOCK.holder"'))
        self.assertLess(acquire, holder)


class ScriptDoesNotReopenTheLockTest(unittest.TestCase):

    def test_deploy_script_inherits_the_lock_and_never_reopens_fd_9(self):
        """`exec 9>` in the child would open a NEW file description and a
        flock on it would block against the parent's own lock: a deadlock
        that looks like a hung deploy."""
        src = _read(_SCRIPT)
        self.assertNotRegex(src, r'^\s*exec 9', )
        self.assertNotIn('flock', src)


class InlineScriptParsesTest(unittest.TestCase):

    def test_inline_block_still_parses(self):
        """`bash -n` the workflow's inline script with the ${{ }} expressions
        replaced by a placeholder, the way test_deploy_manifest_guard checks
        the deploy script: via stdin, no path crosses the shell boundary."""
        import shutil
        import subprocess
        if not shutil.which('bash'):
            self.skipTest('no bash on this host to syntax-check with')
        block = re.sub(r'\$\{\{[^}]*\}\}', 'PLACEHOLDER',
                       '\n'.join(_inline_block())) + '\n'
        r = subprocess.run(['bash', '-n'], input=block.encode('utf-8'),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 0,
                         r.stderr.decode('utf-8', 'replace')[:400])


class TwoRunsSerializeTest(unittest.TestCase):
    """The static tests above prove the lock line is present and parses. This
    one proves what matters: two invocations of the REAL inline block, started
    one second apart with docker/git/sudo and the deploy script stubbed to
    record timestamps, do not overlap. Against the pre-lock workflow the
    second run's first action lands ~1 s after the first run's start, inside
    its deploy; with the lock it lands after the first run's last action.

    Needs util-linux `flock` (Linux; skipped on a Windows/macOS dev host).
    """

    def test_second_run_waits_for_the_first_to_finish(self):
        import shutil
        import subprocess
        import tempfile
        import time
        if not shutil.which('bash'):
            self.skipTest('no bash on this host')
        if not _bash_has('flock'):
            self.skipTest('no flock on this host (util-linux); runs on Linux')
        tmp = tempfile.mkdtemp(prefix='deploy_lock_')
        repo = os.path.join(tmp, 'repo')
        os.makedirs(os.path.join(repo, 'deploy'))
        log = os.path.join(tmp, 'events.log')

        block = '\n'.join(_inline_block())
        block = block.replace('${{ secrets.DEPLOY_REPO_PATH }}', repo)
        block = block.replace('${{ secrets.HARTOS_OAUTH_ENV }}', 'X=1')
        block = block.replace('${{ github.run_id }}', '$RUN_ID')
        block = re.sub(r'\$\{\{[^}]*\}\}', 'PLACEHOLDER', block)
        # A private lock path so two test hosts never share one. Left as is
        # when the block has no lock at all (the pre-fix workflow): the test
        # must then fail on the OVERLAP, not on a missing line.
        block = block.replace('DEPLOY_LOCK=/tmp/deepbox_deploy.lock',
                              'DEPLOY_LOCK=' + os.path.join(tmp, 'lock'))
        # Stubs: every side effect becomes a timestamped line. `bash` is the
        # deploy script itself (invoked as `bash deploy/deepbox_deploy.sh`),
        # stubbed as two seconds of "deploying".
        prelude = '\n'.join([
            'LOG=' + log,
            '_ev() { printf "%s %s %s\\n" "$RUN_ID" "$(date +%s.%N)" "$*" >> "$LOG"; }',
            'git() { _ev git "$@"; }',
            'sudo() { _ev sudo "$@"; }',
            'bash() { _ev script-start; sleep 2; _ev script-end; }',
            '',
        ])
        script = os.path.join(tmp, 'run.sh')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(prelude + block + '\n')

        def launch(run_id):
            env = dict(os.environ)
            env['RUN_ID'] = run_id
            return subprocess.Popen(['bash', script], cwd=tmp, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        a = launch('A')
        time.sleep(1)
        b = launch('B')
        out_a = a.communicate(timeout=120)[0].decode('utf-8', 'replace')
        out_b = b.communicate(timeout=120)[0].decode('utf-8', 'replace')
        self.assertEqual(a.returncode, 0, out_a)
        self.assertEqual(b.returncode, 0, out_b)

        events = []
        with open(log, encoding='utf-8') as fh:
            for line in fh:
                run_id, t, action = line.rstrip('\n').split(' ', 2)
                events.append((run_id, float(t), action))
        a_end = max(t for r, t, act in events if r == 'A' and act == 'script-end')
        b_first = min(t for r, t, act in events if r == 'B')
        self.assertGreater(b_first, a_end,
                           'B acted %.2fs BEFORE A finished: the runs overlapped\n%s'
                           % (a_end - b_first, '\n'.join(map(str, events))))
        # and the log of the run that waited names the holder
        self.assertIn('deploy.lock held by run A', out_b)
        self.assertIn('acquired after', out_b)
        self.assertNotIn('deploy.lock held by', out_a)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
