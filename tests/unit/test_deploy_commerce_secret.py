"""COMMERCE_SESSION_SECRET reaches central's container through the deploy.

POST /api/commerce/session answers 503 until the container has the secret, and
a repo secret alone changes nothing: the ssh step forwards only the names in
its `envs:` list, and deploy/deepbox_deploy.sh never rewrites a live .env
except for the keys it ensures one by one (the OTA host, the LLM trio, this).

Both halves are tested on the REAL artefacts:
  * the workflow is parsed as YAML and the ssh step's `with.envs` and `env`
    are read as data, the way test_ci_workflow_concurrency.py reads workflows;
  * the deploy script runs under bash from line 1 up to (not including) the
    `DEPLOY_COMMIT=` line, the first that needs git, in a temp dir, and the
    .env it leaves behind is what is asserted on, together with bash's exit
    status.

Why not two regex-extracted blocks (review of fd18677ee, hartos-87, 2026-10-12):
running the helper and the commerce block on their own let three broken
deploys pass every test. The block above the helper's definition, or the
helper back inside the LLM-trio `if`, exits 127 (command not found) on a real
deploy; the block above the create-block never writes the key on a fresh box.
A commented-out step-env line still passed a text match. Each of those is a
test case below.

Runs standalone (`python tests/unit/test_deploy_commerce_secret.py`).
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SCRIPT = os.path.join(_ROOT, 'deploy', 'deepbox_deploy.sh')
_WORKFLOW = os.path.join(_ROOT, '.github', 'workflows', 'deploy-hartos-deepbox.yml')
_NAME = 'COMMERCE_SESSION_SECRET'


def _find_bash():
    """bash on PATH, else the one Git for Windows installs beside git.exe.

    A Windows shell usually has git on PATH but not bash, and these tests
    would then skip on exactly the machines the deploy is written from.
    """
    found = shutil.which('bash')
    if found:
        return found
    git = shutil.which('git')
    if git:
        git_root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
        for rel in (('bin', 'bash.exe'), ('usr', 'bin', 'bash.exe')):
            candidate = os.path.join(git_root, *rel)
            if os.path.isfile(candidate):
                return candidate
    return None


_BASH = _find_bash()


def _read(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def _ssh_step():
    """The workflow's one step that forwards names over ssh (`with.envs`)."""
    workflow = yaml.safe_load(_read(_WORKFLOW))
    steps = [s for job in workflow['jobs'].values() for s in job.get('steps', [])
             if 'envs' in (s.get('with') or {})]
    assert len(steps) == 1, 'expected exactly one ssh step with envs:, found %d' % len(steps)
    return steps[0]


def _script_prefix():
    """deepbox_deploy.sh from line 1 up to the first line that needs git."""
    lines = _read(_SCRIPT).split('\n')
    cut = [i for i, line in enumerate(lines) if line.startswith('DEPLOY_COMMIT=')]
    assert cut, 'deepbox_deploy.sh has no DEPLOY_COMMIT= line to stop before'
    return '\n'.join(lines[:cut[0]]) + '\n'


def _keys(env_text):
    """Every KEY=value line of a .env, in order, as (key, value) pairs."""
    pairs = []
    for line in env_text.split('\n'):
        if '=' in line and not line.startswith('#'):
            key, _, value = line.partition('=')
            pairs.append((key, value))
    return pairs


def _values(env_text, key):
    return [v for k, v in _keys(env_text) if k == key]


class WorkflowForwardsTheSecret(unittest.TestCase):

    def test_named_in_the_forwarded_envs_and_set_in_the_step_env(self):
        step = _ssh_step()
        self.assertIn(_NAME, step['with']['envs'].split(','))
        self.assertEqual((step.get('env') or {}).get(_NAME),
                         '${{ secrets.%s }}' % _NAME)

    def test_every_forwarded_name_is_set_and_every_set_name_forwarded(self):
        """A name forwarded but never set reaches the box empty; a name set but
        not forwarded never leaves the runner. Either way the deploy is green
        and the value is missing."""
        step = _ssh_step()
        forwarded = set(step['with']['envs'].split(','))
        set_here = set(step.get('env') or {})
        self.assertEqual(sorted(forwarded - set_here), [], 'forwarded but never set')
        self.assertEqual(sorted(set_here - forwarded), [], 'set but never forwarded')


@unittest.skipUnless(_BASH, 'bash is not on PATH, so the deploy script prefix '
                            'cannot run: none of these deploy-script tests ran')
class ScriptEnsuresTheKey(unittest.TestCase):
    """The real script prefix, run the way a deploy runs it."""

    # Every name the workflow forwards is removed from the inherited
    # environment, so this machine's own settings cannot leak into the run.
    _FORWARDED = ('DEPLOY_LOGS_PATH DEPLOY_IMAGES_PATH DEPLOY_MASTER_KEY_PATH '
                  'DEPLOY_ADVERTISE_URL DEPLOY_AGENT_DATA_PATH LLM_GATEWAY_URL '
                  'HEVOLVE_LLM_ENDPOINT_URL HEVOLVE_LLM_MODEL_NAME '
                  'HEVOLVE_LLM_API_KEY GROQ_API_KEY LANGCHAIN_API_KEY '
                  'ALLOWED_HOSTS CORS_ORIGINS HART_OTA_HOST '
                  'HEVOLVE_ENFORCEMENT_MODE HEVOLVE_NODE_TIER LANGCHAIN_PROJECT '
                  + _NAME).split()

    # What a fresh box needs or the create-block exits 1 before the commerce
    # block is reached. Placeholders only.
    _FRESH_BOX = {'LANGCHAIN_API_KEY': 'lc-placeholder',
                  'GROQ_API_KEY': 'groq-placeholder',
                  'HEVOLVE_LLM_MODEL_NAME': 'model-placeholder'}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._remove)
        self.env_path = os.path.join(self.dir, '.env')
        # A file, not `bash -c <text>`: Windows argv quoting rewrites the
        # backslashes in the script's sed expression.
        with open(os.path.join(self.dir, 'prefix.sh'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(_script_prefix())

    def _remove(self):
        # File by file, not shutil.rmtree: the prefix writes only flat files
        # here, and rmtree is broken in one of this repo's test venvs
        # (os._walk_symlinks_as_files missing).
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _write_env(self, text):
        with open(self.env_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def _deploy(self, **values):
        """Run the prefix; (exit status, combined output, .env text or None)."""
        env = {k: v for k, v in os.environ.items() if k not in self._FORWARDED}
        env.update(values)
        proc = subprocess.run([_BASH, 'prefix.sh'], cwd=self.dir, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60)
        out = proc.stdout.decode('utf-8', 'replace')
        text = _read(self.env_path) if os.path.exists(self.env_path) else None
        return proc.returncode, out, text

    def _assert_ran(self, code, out):
        self.assertEqual(code, 0, 'the deploy prefix exited %d:\n%s' % (code, out))

    def test_a_live_env_without_the_llm_trio_gets_the_key(self):
        """No HEVOLVE_LLM_MODEL_NAME: the trio block is skipped, and the
        helper must still exist for this key (exit 127 otherwise)."""
        self._write_env('A=1\nB=2\n')
        code, out, text = self._deploy(**{_NAME: 'abc123'})
        self._assert_ran(code, out)
        self.assertEqual(_keys(text)[:2], [('A', '1'), ('B', '2')])
        self.assertEqual(_values(text, _NAME), ['abc123'])

    def test_replaced_once_and_sed_metacharacters_survive(self):
        self._write_env('A=1\n%s=old\n' % _NAME)
        code, out, text = self._deploy(**{_NAME: 'n&w|v\\x'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['n&w|v\\x'])
        self.assertEqual(_values(text, 'A'), ['1'])

    def test_a_fresh_box_gets_the_key_once(self):
        """No .env: the create-block writes one, and the key must land in it."""
        code, out, text = self._deploy(**dict(self._FRESH_BOX, **{_NAME: 'fresh1'}))
        self._assert_ran(code, out)
        self.assertIsNotNone(text, 'the deploy prefix wrote no .env:\n' + out)
        self.assertEqual(_values(text, _NAME), ['fresh1'])

    def test_an_absent_secret_leaves_the_key_as_it_is(self):
        self._write_env('A=1\n%s=keep\n' % _NAME)
        code, out, text = self._deploy(**{_NAME: ''})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['keep'])

    def test_an_absent_secret_adds_no_key(self):
        self._write_env('A=1\n')
        code, out, text = self._deploy(**{_NAME: ''})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), [])

    def test_a_key_sharing_the_prefix_is_untouched(self):
        self._write_env('%s_OLD=x\n' % _NAME)
        code, out, text = self._deploy(**{_NAME: 'new1'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME + '_OLD'), ['x'])
        self.assertEqual(_values(text, _NAME), ['new1'])

    def test_a_second_deploy_changes_nothing(self):
        self._write_env('A=1\n')
        self._deploy(**{_NAME: 'same1'})
        first = _read(self.env_path)
        code, out, second = self._deploy(**{_NAME: 'same1'})
        self._assert_ran(code, out)
        self.assertEqual(second, first)
        self.assertEqual(_values(second, _NAME), ['same1'])

    def test_the_value_never_reaches_the_log(self):
        self._write_env('A=1\n')
        code, out, text = self._deploy(**{_NAME: 'S3cr3t-value-77'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['S3cr3t-value-77'])
        self.assertNotIn('S3cr3t-value-77', out)


if __name__ == '__main__':
    unittest.main()
