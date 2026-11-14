"""A deploy must not be able to fill central's disk.

Measured 2026-11-10 on deepbox (174 GB, 0 bytes free): deploy run
42864357071 failed writing an image layer with "no space left on device",
and Kong's health check failed because it could not create a temp dir.  Two
of the things holding the space were left by this deploy itself:

  * the failed build's step container, 7.05 GB of half-installed packages:
    the box builds with the legacy builder, which keeps a failed step's
    container unless the build is told --force-rm, and nothing ever removed
    it (the pre-build prune removes images, and an image a container uses is
    not prunable);
  * the HARTOS container's json log, 9.4 GB after 11 days: docker run set no
    log size, so the log grew without bound (logging config
    "{json-file map[]}").

The tests run the WHOLE deploy/deepbox_deploy.sh under bash, with stub docker,
sudo, git, curl, python3, sleep, ip and file commands first on PATH that record
their arguments and answer like the box does. What is asserted is what the
script asks docker to do.

Run from the repo root: `python -m pytest tests/unit/test_a_deploy_cannot_fill_centrals_disk.py -q`.
"""
import os
import subprocess
import tempfile
import unittest

from tests.unit.test_deploy_commerce_secret import (  # noqa: E402
    _PATH_FIRST, _find_bash, _forwarded_names,
)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SCRIPT = os.path.join(_ROOT, 'deploy', 'deepbox_deploy.sh')
_BASH = _find_bash()
_SEP = '\x1f'
_SHA = '0123456789abcdef0123456789abcdef01234567'

# Every stub records "name<US>arg<US>arg..." as one line, then answers.
_RECORD = ('{ printf "%%s" "%s"; for a in "$@"; do printf "\\037%%s" "$a"; done; '
           'printf "\\n"; } >> "$DEPLOY_TEST_CALLS"\n')

_STUBS = {
    'docker': r'''
case "$1" in
  build) exit "${DEPLOY_TEST_BUILD_EXIT:-0}" ;;
  inspect)
    case "$*" in
      *'{{.Image}}'*) echo sha256:old-image ;;
      *'{{range .Config.Env}}'*) printf 'HARTOS_GIT_SHA=old-sha\nHARTOS_IMAGE_TAG=old-tag\n' ;;
      *) echo 'Image=x ContainerID=y RestartCount=0' ;;
    esac ;;
  ps) echo 'c0ffee langchain_gpt langchain' ;;
  cp) printf '{"version":"v","git_sha":"%s","code_hash":"c","guardrail_hash":"g","master_signature":"s"}' "$DEPLOY_TEST_SHA" > "$3" ;;
esac
exit 0
''',
    # docker, cat and mkdir go through (to the stub docker, to a dummy key
    # file, to temp dirs); anything else sudo is asked to run is only recorded.
    'sudo': r'''
case "$1" in
  docker|cat|mkdir) exec "$@" ;;
esac
exit 0
''',
    'git': r'''
case "$*" in
  'rev-parse HEAD') echo "$DEPLOY_TEST_SHA" ;;
  'rev-parse --short HEAD') echo "$DEPLOY_TEST_SHA" | cut -c1-9 ;;
  *) echo "$DEPLOY_TEST_SHA test commit" ;;
esac
exit 0
''',
    'curl': r'''
[ "${DEPLOY_TEST_STATUS_UP:-1}" = 1 ] || exit 7
echo '{"status":"running","node_tier":"central"}'
exit 0
''',
    'python3': 'exit 0\n',
    'sleep': 'exit 0\n',
    'file': 'echo "$1: JSON data"\nexit 0\n',
    'ip': 'echo "1.0.0.0 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 0"\nexit 0\n',
}

# What the HARTOS container's log may grow to: 5 files of 200 MB.
_LOG_OPTS = [('--log-driver', 'json-file'), ('--log-opt', 'max-size=200m'),
             ('--log-opt', 'max-file=5')]


def _pairs(args):
    return list(zip(args, args[1:]))


@unittest.skipUnless(_BASH, 'no bash on PATH and none beside git.exe, so the '
                            'deploy script cannot run: none of these tests ran')
class ADeployCannotFillCentralsDisk(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._remove)
        self.calls = os.path.join(self.dir, '.calls')
        with open(_SCRIPT, encoding='utf-8') as fh:
            script = fh.read()
        with open(os.path.join(self.dir, 'deploy.sh'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(_PATH_FIRST + script)
        bin_dir = os.path.join(self.dir, 'bin')
        os.mkdir(bin_dir)
        for name, body in _STUBS.items():
            path = os.path.join(bin_dir, name)
            with open(path, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('#!/bin/sh\n' + _RECORD % name + body)
            os.chmod(path, 0o755)
        # A live .env, so the create-block is skipped; '*' so no host is added.
        with open(os.path.join(self.dir, '.env'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write('ALLOWED_HOSTS=*\n')
        self.key = os.path.join(self.dir, 'dummy-key')
        with open(self.key, 'w', encoding='utf-8') as fh:
            fh.write('not-a-key')

    def _remove(self):
        # Bottom-up by hand: rmtree is broken in one of this repo's test venvs.
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.dir)

    def _deploy(self, **extra):
        """Run the whole script; (exit status, output, docker calls)."""
        forwarded = set(_forwarded_names())
        env = {k: v for k, v in os.environ.items() if k not in forwarded}
        env.update({
            'DEPLOY_TEST_CALLS': self.calls, 'DEPLOY_TEST_SHA': _SHA,
            'DEPLOY_MASTER_KEY_PATH': self.key,
            'DEPLOY_AGENT_DATA_PATH': os.path.join(self.dir, 'agent_data'),
            'DEPLOY_DATA_PATH': os.path.join(self.dir, 'data'),
            'DEPLOY_LOGS_PATH': os.path.join(self.dir, 'logs'),
            'DEPLOY_IMAGES_PATH': os.path.join(self.dir, 'images'),
        })
        env.update(extra)
        proc = subprocess.run([_BASH, 'deploy.sh'], cwd=self.dir, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=300)
        out = proc.stdout.decode('utf-8', 'replace')
        docker = []
        if os.path.exists(self.calls):
            with open(self.calls, encoding='utf-8') as fh:
                for line in fh.read().split('\n'):
                    parts = line.split(_SEP)
                    if parts[0] == 'docker':
                        docker.append(parts[1:])
        return proc.returncode, out, docker

    @staticmethod
    def _hartos_runs(docker):
        """Every `docker run` that starts the HARTOS container itself."""
        return [a for a in docker if a and a[0] == 'run'
                and ('--name', 'langchain') in _pairs(a)]

    def test_the_build_removes_its_own_container_when_it_fails(self):
        code, out, docker = self._deploy()
        self.assertEqual(code, 0, out[-3000:])
        builds = [a for a in docker if a and a[0] == 'build']
        self.assertEqual(len(builds), 1, out[-3000:])
        self.assertIn('--force-rm', builds[0])

    def test_the_hartos_container_starts_with_a_bounded_log(self):
        code, out, docker = self._deploy()
        self.assertEqual(code, 0, out[-3000:])
        runs = self._hartos_runs(docker)
        self.assertEqual(len(runs), 1, out[-3000:])
        self.assertEqual(runs[0][-1], 'langchain_gpt:' + _SHA[:9])
        for pair in _LOG_OPTS:
            self.assertIn(pair, _pairs(runs[0]))

    def test_a_rolled_back_container_starts_with_a_bounded_log_too(self):
        """/status never answers: the script puts the previous image back.
        That container serves until the next good deploy, so its log is
        bounded the same way."""
        code, out, docker = self._deploy(DEPLOY_TEST_STATUS_UP='0')
        self.assertEqual(code, 1, out[-3000:])
        self.assertIn('rolled back to sha256:old-image', out)
        runs = self._hartos_runs(docker)
        self.assertEqual([r[-1] for r in runs],
                         ['langchain_gpt:' + _SHA[:9], 'sha256:old-image'])
        for pair in _LOG_OPTS:
            self.assertIn(pair, _pairs(runs[1]))

    def test_a_failed_build_leaves_the_live_container_alone(self):
        """The serving container is replaced only after a good build: a failed
        one must neither remove it nor start another."""
        code, out, docker = self._deploy(DEPLOY_TEST_BUILD_EXIT='1')
        self.assertNotEqual(code, 0, out[-3000:])
        self.assertEqual(self._hartos_runs(docker), [])
        self.assertNotIn(['rm', '-f', 'langchain'], docker)


if __name__ == '__main__':
    unittest.main(verbosity=2)
