"""COMMERCE_SESSION_SECRET reaches central's container through the deploy.

POST /api/commerce/session answers 503 until the container has the secret, and
a repo secret alone changes nothing: the ssh step forwards only the names in
its `envs:` list, and deploy/deepbox_deploy.sh never rewrites a live .env
except for the keys it ensures one by one (the LLM trio, now this one).

Runs standalone (`python tests/unit/test_deploy_commerce_secret.py`).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SCRIPT = os.path.join(_ROOT, 'deploy', 'deepbox_deploy.sh')
_WORKFLOW = os.path.join(_ROOT, '.github', 'workflows', 'deploy-hartos-deepbox.yml')
_NAME = 'COMMERCE_SESSION_SECRET'


def _read(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def _ensure_snippet():
    """The script's own _set_env_key function and its commerce block."""
    text = _read(_SCRIPT)
    fn = re.search(r'^_set_env_key\(\) \{.*?^\}\n', text, re.S | re.M).group(0)
    blk = re.search(r'^if \[ -n "\$\{%s\}" \].*?^fi\n' % _NAME,
                    text, re.S | re.M).group(0)
    return fn + blk


class WorkflowForwardsTheSecret(unittest.TestCase):

    def test_named_in_the_forwarded_envs_and_the_step_env(self):
        text = _read(_WORKFLOW)
        envs = re.search(r'^\s*envs:\s*(\S+)', text, re.M).group(1).split(',')
        self.assertIn(_NAME, envs)
        self.assertIn('%s: ${{ secrets.%s }}' % (_NAME, _NAME), text)


@unittest.skipUnless(shutil.which('bash'), 'needs bash')
class ScriptEnsuresTheKey(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = os.path.join(self.dir, '.env')
        self.snippet = os.path.join(self.dir, 'ensure.sh')
        self.addCleanup(self._remove)

    def _remove(self):
        for p in (self.env, self.snippet):
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(self.dir)

    def _run(self, secret, env_text=None):
        if env_text is not None:
            with open(self.env, 'w', newline='\n') as fh:
                fh.write(env_text)
        # A file, not `bash -c <text>`: Windows argv quoting rewrites the
        # backslashes in the script's sed expression.
        with open(self.snippet, 'w', newline='\n') as fh:
            fh.write(_ensure_snippet())
        subprocess.run(['bash', 'ensure.sh'], cwd=self.dir,
                       env=dict(os.environ, **{_NAME: secret}),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return _read(self.env) if os.path.exists(self.env) else None

    def test_appended_to_a_live_env_without_touching_the_rest(self):
        out = self._run('abc123', 'A=1\nB=2\n')
        self.assertEqual(out, 'A=1\nB=2\n%s=abc123\n' % _NAME)

    def test_replaced_not_duplicated_and_sed_metacharacters_survive(self):
        out = self._run('n&w|v\\x', 'A=1\n%s=old\n' % _NAME)
        self.assertEqual(out, 'A=1\n%s=n&w|v\\x\n' % _NAME)

    def test_absent_secret_leaves_the_env_exactly_as_it_is(self):
        self.assertEqual(self._run('', 'A=1\n%s=keep\n' % _NAME),
                         'A=1\n%s=keep\n' % _NAME)

    def test_no_env_file_means_none_is_created_here(self):
        self.assertIsNone(self._run('abc123'))


if __name__ == '__main__':
    unittest.main()
