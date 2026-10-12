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

# The owner-set keys the deploy writes into central's live .env when their
# repo secret is set (the expert tier, its Claude day budget, the autonomous
# marketing switch, the node's own API key). Same contract as _NAME, minus the
# absent-secret note.
_OWNER_KEYS = ('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', 'HEVOLVE_EXPERT_LLM_MODEL_NAME',
               'HEVOLVE_EXPERT_LLM_API_KEY', 'HEVOLVE_CLAUDE_BUDGET_USD_DAY',
               'HEVOLVE_AUTONOMOUS_MARKETING', 'HEVOLVE_API_KEY')


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


def _forwarded_names():
    """Every name the ssh step forwards to the box, read from the workflow."""
    return _ssh_step()['with']['envs'].split(',')


def _script_prefix():
    """deepbox_deploy.sh from line 1 up to the first line that needs git."""
    lines = _read(_SCRIPT).split('\n')
    cut = [i for i, line in enumerate(lines) if line.startswith('DEPLOY_COMMIT=')]
    assert cut, 'deepbox_deploy.sh has no DEPLOY_COMMIT= line to stop before'
    return '\n'.join(lines[:cut[0]]) + '\n'


# The commands a real deploy runs on the box, which a developer machine must
# never run from a unit test. Each is shadowed by an executable of the same name
# in the run dir's bin/, which only records that it was called: a PATH lookup
# also catches `command git`, `env git`, `xargs git` and `sh -c 'git'`, which a
# shell function of the same name would not (review of bb1893b99, F3).
#
# The PATH entry is set by the FIRST LINE of the script run (_PATH_FIRST), not
# only in the environment handed to bash: Git for Windows' bin\bash.exe, which
# _find_bash picks from a PowerShell or IDE shell, puts /mingw64/bin ahead of
# whatever PATH it was given, so the real git and curl won there (review of
# ce31f5925, F1, measured). Exported PATH set inside the script reaches every
# child the prefix starts.
#
# Not caught, by construction: an absolute path (/usr/bin/git), git.exe, or a
# PATH reset inside the script. The prefix needs none of these commands today; a
# later edit that moves one above the cut fails _assert_ran instead of touching
# docker or git on the dev host.
_BOX_COMMANDS = ('git', 'docker', 'sudo', 'curl')
_PATH_FIRST = 'PATH="$PWD/bin:$PATH"\n'


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
        self.assertIn(_NAME, _forwarded_names())
        self.assertEqual((_ssh_step().get('env') or {}).get(_NAME),
                         '${{ secrets.%s }}' % _NAME)

    def test_every_forwarded_name_is_set_and_every_set_name_forwarded(self):
        """A name forwarded but never set reaches the box empty; a name set but
        not forwarded never leaves the runner. Either way the deploy is green
        and the value is missing."""
        forwarded = set(_forwarded_names())
        set_here = set(_ssh_step().get('env') or {})
        self.assertEqual(sorted(forwarded - set_here), [], 'forwarded but never set')
        self.assertEqual(sorted(set_here - forwarded), [], 'set but never forwarded')

    def test_every_owner_set_key_is_forwarded_and_set_from_its_secret(self):
        step_env = _ssh_step().get('env') or {}
        for key in _OWNER_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, _forwarded_names())
                self.assertEqual(step_env.get(key), '${{ secrets.%s }}' % key)


@unittest.skipUnless(_BASH, 'no bash on PATH and none beside git.exe, so the '
                            'deploy script prefix cannot run: none of these '
                            'deploy-script tests ran')
class ScriptEnsuresTheKey(unittest.TestCase):
    """The real script prefix, run the way a deploy runs it."""

    # What a fresh box needs or the create-block exits 1 before the commerce
    # block is reached. Placeholders only.
    _FRESH_BOX = {'LANGCHAIN_API_KEY': 'lc-placeholder',
                  'GROQ_API_KEY': 'groq-placeholder',
                  'HEVOLVE_LLM_MODEL_NAME': 'model-placeholder'}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._remove)
        self.env_path = os.path.join(self.dir, '.env')
        self.calls = os.path.join(self.dir, '.calls')
        # A file, not `bash -c <text>`: Windows argv quoting rewrites the
        # backslashes in the script's sed expression.
        with open(os.path.join(self.dir, 'prefix.sh'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(_PATH_FIRST + _script_prefix())
        self.bin = os.path.join(self.dir, 'bin')
        os.mkdir(self.bin)
        for name in _BOX_COMMANDS:
            with open(os.path.join(self.bin, name), 'w', encoding='utf-8',
                      newline='\n') as fh:
                fh.write('#!/bin/sh\necho "%s $*" >> "$DEPLOY_TEST_CALLS"\n' % name)
            os.chmod(os.path.join(self.bin, name), 0o755)

    def _remove(self):
        # Bottom-up by hand, not shutil.rmtree: rmtree is broken in one of
        # this repo's test venvs (os._walk_symlinks_as_files missing).
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.dir)

    def _write_env(self, text):
        with open(self.env_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def _deploy(self, **values):
        """Run the prefix; (exit status, combined output, .env text or None).

        Every name the workflow forwards is removed from the inherited
        environment first, so this machine's own settings cannot leak in."""
        forwarded = set(_forwarded_names())
        env = {k: v for k, v in os.environ.items() if k not in forwarded}
        env.update(values)
        env['DEPLOY_TEST_CALLS'] = self.calls
        proc = subprocess.run([_BASH, 'prefix.sh'], cwd=self.dir, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60)
        out = proc.stdout.decode('utf-8', 'replace')
        text = _read(self.env_path) if os.path.exists(self.env_path) else None
        return proc.returncode, out, text

    def _assert_ran(self, code, out):
        self.assertEqual(code, 0, 'the deploy prefix exited %d:\n%s' % (code, out))
        self.assertFalse(os.path.exists(self.calls),
                         'the prefix ran box commands: %s'
                         % (_read(self.calls) if os.path.exists(self.calls) else ''))

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

    def test_a_last_line_without_a_newline_keeps_its_value(self):
        """The append must not glue the key onto an unterminated last line:
        `B=2COMMERCE_SESSION_SECRET=x` sets neither. ALLOWED_HOSTS=* keeps the
        OTA block from appending first, so this is the commerce append alone
        (review of 5100ca104: measured on the helper, `A=1` + append gave
        `A=1COMMERCE_SESSION_SECRET=new`)."""
        self._write_env('ALLOWED_HOSTS=*\nB=2')
        code, out, text = self._deploy(**{_NAME: 'tail1'})
        self._assert_ran(code, out)
        self.assertEqual(text, 'ALLOWED_HOSTS=*\nB=2\n%s=tail1\n' % _NAME)

    def test_the_ota_append_keeps_an_unterminated_last_line_too(self):
        """Same file, no ALLOWED_HOSTS: the OTA block appends first."""
        self._write_env('A=1')
        code, out, text = self._deploy(**{_NAME: 'tail2'})
        self._assert_ran(code, out)
        self.assertEqual(text, 'A=1\nALLOWED_HOSTS=etime.hertzai.com\n%s=tail2\n' % _NAME)

    def test_a_terminated_file_gets_no_blank_line(self):
        """Exact bytes, both appends: a file that ends in a newline already is
        the usual live .env, and a blank line there would be the regression
        (an always-write-the-newline helper passed the key-only checks)."""
        cases = [('ALLOWED_HOSTS=*\nB=2\n', 'ALLOWED_HOSTS=*\nB=2\n%s=t3\n' % _NAME, 't3'),
                 ('A=1\n', 'A=1\nALLOWED_HOSTS=etime.hertzai.com\n%s=t4\n' % _NAME, 't4')]
        for before, after, secret in cases:
            with self.subTest(before=before):
                self._write_env(before)
                code, out, text = self._deploy(**{_NAME: secret})
                self._assert_ran(code, out)
                self.assertEqual(text, after)

    def test_an_empty_env_gets_no_leading_blank_line(self):
        self._write_env('')
        code, out, text = self._deploy(**{_NAME: 'e1'})
        self._assert_ran(code, out)
        self.assertEqual(text, 'ALLOWED_HOSTS=etime.hertzai.com\n%s=e1\n' % _NAME)

    def test_the_exact_key_is_replaced_beside_a_prefix_sharing_key(self):
        """The replace path must match `KEY=`, not every line starting KEY."""
        self._write_env('%s=old\n%s_OLD=x\n' % (_NAME, _NAME))
        code, out, text = self._deploy(**{_NAME: 'new2'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['new2'])
        self.assertEqual(_values(text, _NAME + '_OLD'), ['x'])

    def test_a_commented_example_does_not_block_the_write(self):
        """Only a line that STARTS with KEY= counts as the key being set."""
        self._write_env('# %s=example\nA=1\n' % _NAME)
        code, out, text = self._deploy(**{_NAME: 'c1'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['c1'])
        self.assertIn('# %s=example' % _NAME, text)

    def test_an_absent_secret_and_no_key_is_named_in_the_log(self):
        """No repo secret and no key in .env: the container has none from the
        environment, and the deploy log says so, the way the create-block names
        a missing LangChain or Groq key. (config.json could still carry it, so
        the note says that rather than promising a 503.)"""
        self._write_env('A=1\n')
        code, out, text = self._deploy(**{_NAME: ''})
        self._assert_ran(code, out)
        self.assertIn('%s secret absent and not in .env' % _NAME, out)

    def test_an_absent_secret_with_the_key_in_env_says_it_is_kept(self):
        """The .env still holds the key, so the container still has it: the
        log must not claim the route is unconfigured (review of bb1893b99, F1).
        No repo secret means nothing for GitHub to mask in this log, so the
        kept value must not appear in it either (review of ce31f5925, F2)."""
        self._write_env('A=1\n%s=Kept-Value-91\n' % _NAME)
        code, out, text = self._deploy(**{_NAME: ''})
        self._assert_ran(code, out)
        self.assertIn('.env keeps its existing value', out)
        self.assertNotIn('not in .env', out)
        self.assertNotIn('Kept-Value-91', out)
        self.assertEqual(_values(text, _NAME), ['Kept-Value-91'])

    def test_only_a_real_key_with_a_value_counts_as_kept(self):
        """A commented example, a prefix-sharing key and an empty value are
        not the secret (get_secret treats '' as unset): the note must say
        "not in .env", never "kept" (review of ce31f5925, F3 and F4)."""
        for before in ('# %s=example\nA=1\n' % _NAME,
                       '%s_OLD=x\n' % _NAME,
                       '%s=\n' % _NAME):
            with self.subTest(before=before):
                self._write_env(before)
                code, out, text = self._deploy(**{_NAME: ''})
                self._assert_ran(code, out)
                self.assertIn('%s secret absent and not in .env' % _NAME, out)
                self.assertNotIn('keeps its existing value', out)

    def test_a_set_secret_prints_no_absent_note(self):
        self._write_env('A=1\n')
        code, out, text = self._deploy(**{_NAME: 'set1'})
        self._assert_ran(code, out)
        self.assertNotIn('secret absent', out)

    def test_the_value_never_reaches_the_log(self):
        self._write_env('A=1\n')
        code, out, text = self._deploy(**{_NAME: 'S3cr3t-value-77'})
        self._assert_ran(code, out)
        self.assertEqual(_values(text, _NAME), ['S3cr3t-value-77'])
        self.assertNotIn('S3cr3t-value-77', out)

    # ── The owner-set keys ──

    def test_every_owner_set_key_is_written_once_and_never_logged(self):
        """All six set: each lands exactly once with its own value, a key
        already in .env is replaced (not duplicated), and no value is
        printed (the repo secret is masked by GitHub, a log copy of it in a
        file or a rerun is not)."""
        self._write_env('ALLOWED_HOSTS=*\nA=1\nHEVOLVE_API_KEY=old-key\n')
        values = {k: 'Own-%d-zq' % i for i, k in enumerate(_OWNER_KEYS)}
        code, out, text = self._deploy(**values)
        self._assert_ran(code, out)
        self.assertEqual(_values(text, 'A'), ['1'])
        for key, value in values.items():
            with self.subTest(key=key):
                self.assertEqual(_values(text, key), [value])
                self.assertNotIn(value, out)
                self.assertIn('%s set from the secret (value not shown)' % key, out)

    def test_one_owner_set_key_writes_only_itself(self):
        """Setting the budget alone must not write the other five (an empty
        expert key or an empty API key in .env would read as configured)."""
        self._write_env('ALLOWED_HOSTS=*\nA=1\n')
        code, out, text = self._deploy(HEVOLVE_CLAUDE_BUDGET_USD_DAY='3.30')
        self._assert_ran(code, out)
        self.assertEqual(text, 'ALLOWED_HOSTS=*\nA=1\nHEVOLVE_CLAUDE_BUDGET_USD_DAY=3.30\n')

    def test_absent_owner_set_secrets_leave_env_exactly_as_it_is(self):
        """No secret, no write: turning a key off is an edit on the box, so an
        unset secret must not blank a value the owner put there by hand."""
        before = 'ALLOWED_HOSTS=*\nA=1\nHEVOLVE_AUTONOMOUS_MARKETING=0\n'
        self._write_env(before)
        code, out, text = self._deploy(**{_NAME: 'c9'})
        self._assert_ran(code, out)
        self.assertEqual(text, before + '%s=c9\n' % _NAME)
        for key in _OWNER_KEYS:
            self.assertNotIn('%s set from the secret' % key, out)


if __name__ == '__main__':
    unittest.main()
