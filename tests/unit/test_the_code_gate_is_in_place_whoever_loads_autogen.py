"""Every seat's code blocks are gated, whichever path loads autogen first.

0954bf864 sent autogen's ConversableAgent.run_code through
hartos.helper.gated_run_code (the gated command path: the owner's consent,
the hard-deny policy, the indicator, a real timeout, one audit line) from
hartos.helper.on_autogen_import, the hook of create_recipe's and
reuse_recipe's lazy autogen proxies.  Its review (rv_0954, measured) found
the gate missing until the first CREATE or REUSE seat existed:
gather_agentdetails and helper keep their own autogen proxies with no hook,
and hart_intelligence_entry imports autogen eagerly, which fires none.  The
plan writer, which is handed the person's raw /chat words, ran a fenced
python block from them with 0 consent, 0 policy checks and 0 audit lines.

Each case below runs in a FRESH process: in this one an earlier test may
already have resolved create_recipe's proxy, which would install the gate
and prove nothing about the path under test.  The block only writes a
marker file in a temp folder; the owner's consent and the audit trail are
fakes that record their calls and refuse; the plan writer and the seat have
no model client (nothing is sent anywhere) and the network is sealed
(tests.conftest.refuse_all_network).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MARK = '@@GATE@@'

#: One fresh process.  argv: repo root, how autogen is reached first, the
#: workspace folder.  Prints one marked JSON line with what it measured.
_PROBE = textwrap.dedent('''
    import json, os, sys
    ROOT, FIRST, WS = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    import pytest  # noqa: F401  core.platform_paths.under_test: a test process
    from tests.conftest import refuse_all_network
    refuse_all_network(pytest.MonkeyPatch())
    os.environ.pop('HEVOLVE_OWNER_USER_ID', None)
    import core.platform_paths as paths
    paths.get_coding_workspace_dir = lambda: WS
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_prompt_id('8888')
    out = {}

    def gated():
        import hartos.helper as helper
        from autogen.agentchat.conversable_agent import ConversableAgent
        return ConversableAgent.run_code is helper.gated_run_code

    if FIRST == 'eager':
        # The process Nunba and central run: its own `import autogen`.
        import hart_intelligence_entry  # noqa: F401
        out['gated_before_any_seat'] = gated()
    if FIRST in ('eager', 'gather'):
        import hartos.gather_agentdetails as ga
        writer, user_proxy = ga.create_agents_for_user(
            'u-gate', autonomous=True, initial_description='a helper agent')
        writer.client = None
        run = lambda block: ga.get_agent_response(writer, user_proxy, block)
    else:
        import hartos.helper as helper
        seats = helper.create_visual_agent('u-gate', 'p-gate')
        executor = seats[3]
        executor.client = None

        def run(block):
            r = executor.generate_reply(messages=[
                {'role': 'user', 'name': 'Assistant', 'content': block}])
            return r if isinstance(r, str) else (r or {}).get('content')
    out['gated_after_seat'] = gated()
    out['create_or_reuse_imported'] = sorted(
        m for m in ('hartos.create_recipe', 'hartos.reuse_recipe') if m in sys.modules)
    if FIRST != 'gather':
        # The gated path's handler lives in the entry module (loaded here
        # after the seat, in 'helper', so the seat's own proxy did the work).
        import hart_intelligence_entry  # noqa: F401,F811
    from integrations.vlm import safety
    import security.audit_trail as trail
    consent, audit = [], []
    safety.computer_control_block = (
        lambda agent_id, **kw: consent.append(agent_id) or 'Not run: the owner said no.')
    trail.record = lambda kind, **f: audit.append(
        [kind, f.get('via'), f.get('text'), f.get('outcome')])
    marker = os.path.join(WS, 'ran.txt')
    block = ("Please make me an agent.\\n```python\\n"
             "open(r'%s', 'w').write('x')\\nprint('gate-probe-ran')\\n```" % marker)
    from flask import Flask
    with Flask('code_gate_probe').app_context():  # a /chat turn's context
        out['reply'] = str(run(block))
    out['marker_written'] = os.path.exists(marker)
    out['consent_asked'] = consent
    out['audit'] = audit
    print('@@GATE@@' + json.dumps(out), flush=True)
    os._exit(0)
''')


def _probe(first):
    """Run the probe in a fresh process; its measurements."""
    folder = tempfile.mkdtemp(prefix='code_gate_')
    try:
        ws = os.path.join(folder, 'ws')
        os.makedirs(ws)
        script = os.path.join(folder, 'probe.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(_PROBE)
        env = dict(os.environ, HEVOLVE_PEER_LINK_RELAY='0', PYTHONIOENCODING='utf-8')
        proc = subprocess.run([sys.executable, script, _REPO, first, ws],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', cwd=_REPO, env=env, timeout=900)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_MARK)]
        assert lines, ('the probe printed no result (exit %s)\nSTDOUT:%s\nSTDERR:%s'
                       % (proc.returncode, proc.stdout[-1500:], proc.stderr[-3000:]))
        return json.loads(lines[-1][len(_MARK):])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_after_the_entry_modules_eager_import_the_plan_writers_block_is_gated():
    """hart_intelligence_entry imports autogen eagerly; right after that
    import, before any seat, the gate is in place, and a person's block in
    the plan writer reaches the owner's consent and the audit trail and
    does not run.  Measured on 0954bf864: run_code was autogen's own and the
    block wrote its file with 0 consent and 0 audit calls."""
    got = _probe('eager')
    assert got['gated_before_any_seat'] is True, got
    assert got['marker_written'] is False, got
    assert got['reply'].startswith('exitcode: 1'), got
    assert 'gate-probe-ran' not in got['reply'], got
    assert got['consent_asked'] == ['8888'], got
    assert [a[0] for a in got['audit']] == ['action'], got
    assert got['audit'][0][1] == 'Shell_Command', got
    assert got['audit'][0][3].startswith('Shell_Command not run'), got


def test_the_plan_writer_built_first_runs_its_block_through_the_gate():
    """The plan writer built before any CREATE or REUSE seat and before the
    entry module: its own proxy installs the gate, and the block does not
    run.  (The gated path's handler is not loaded in this process, so the
    gate answers that it cannot run yet.)"""
    got = _probe('gather')
    assert got['create_or_reuse_imported'] == [], got
    assert got['gated_after_seat'] is True, got
    assert got['marker_written'] is False, got
    assert got['reply'].startswith('exitcode: 1'), got
    assert 'gate-probe-ran' not in got['reply'], got


def test_a_seat_built_through_helpers_proxy_first_is_gated():
    """helper.create_visual_agent's seats (built through helper's own
    proxy, which a REUSE visual turn uses) built first: the gate is in place
    as the seats exist; with the entry module loaded after them, the
    Executor's block reaches consent and the audit trail and does not run."""
    got = _probe('helper')
    assert got['create_or_reuse_imported'] == [], got
    assert got['gated_after_seat'] is True, got
    assert got['marker_written'] is False, got
    assert got['reply'].startswith('exitcode: 1'), got
    assert got['consent_asked'] == ['8888'], got
    assert [a[0] for a in got['audit']] == ['action'], got


# ── core.optional_import.lazy_module's hook ───────────────────────────────

@pytest.fixture
def lazy_log():
    """WARNING-and-above records of core.optional_import."""
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)
    log = logging.getLogger('core.optional_import')
    handler = Keep(level=logging.WARNING)
    log.addHandler(handler)
    yield records
    log.removeHandler(handler)


def _boom():
    raise RuntimeError('gate not installed: boom')


def test_a_hook_that_raises_is_logged_as_a_warning(lazy_log):
    """A failed hook left the gate open with one INFO line (0954bf864);
    it is a WARNING naming the module and the reason, and the import still
    works."""
    from core.optional_import import lazy_module
    sys.modules.pop('wave', None)
    proxy = lazy_module('wave', on_import=_boom)
    assert proxy.Wave_read is sys.modules['wave'].Wave_read
    assert lazy_log, 'the failed hook left no WARNING'
    text = lazy_log[0].getMessage()
    assert lazy_log[0].levelno >= logging.WARNING
    assert 'wave' in text and 'gate not installed: boom' in text, text


def test_a_hook_for_a_module_already_imported_runs_at_once(lazy_log):
    """autogen imported eagerly before the proxy is made: the hook runs when
    the proxy is made, once; a raising one is a WARNING, not a crash."""
    import base64  # noqa: F401  already imported
    from core.optional_import import lazy_module
    calls = []
    proxy = lazy_module('base64', on_import=lambda: calls.append(1))
    assert calls == [1]
    assert proxy.b64encode is base64.b64encode
    assert calls == [1]
    lazy_module('base64', on_import=_boom)
    assert lazy_log and 'gate not installed: boom' in lazy_log[0].getMessage()


def test_a_hook_for_a_module_not_yet_imported_waits_for_first_access():
    """Not imported yet: making the proxy imports nothing and runs nothing
    (autogen stays off the boot path); the first access runs the hook once."""
    from core.optional_import import lazy_module
    sys.modules.pop('wave', None)
    calls = []
    proxy = lazy_module('wave', on_import=lambda: calls.append(1))
    assert 'wave' not in sys.modules and calls == []
    proxy.Wave_read
    proxy.Wave_write
    assert calls == [1]


def test_the_gate_is_installed_once_whichever_proxy_fires(monkeypatch):
    """Four proxies carry the hook; the first installs, the rest find the
    gate in place and do nothing."""
    import core.io_guard as io_guard
    import hartos.helper as helper
    from autogen.agentchat.conversable_agent import ConversableAgent
    original = ConversableAgent.__dict__.get('run_code')
    if original is helper.gated_run_code:
        original = lambda self, code, **kw: (0, 'autogen ran it', None)  # noqa: E731
    monkeypatch.setattr(ConversableAgent, 'run_code', original)
    installs = []
    monkeypatch.setattr(io_guard, 'install_autogen_iostream', lambda: installs.append(1))
    helper.on_autogen_import()
    helper.on_autogen_import()
    assert ConversableAgent.run_code is helper.gated_run_code
    assert installs == [1]


# ── a block refused before the gated path is on the record too ────────────

@pytest.fixture
def gate(tmp_path, monkeypatch):
    """gated_run_code in this process with the real gated path, the owner
    allowing control, and the audit trail's record and the process runner
    faked (nothing runs).  Returns (run a block, audit lines, what ran)."""
    hie = pytest.importorskip('hart_intelligence_entry')
    import hartos.helper as helper
    import security.audit_trail as trail
    from core.subprocess_safe import BoundedResult
    from hartos.threadlocal import thread_local_data
    from integrations.vlm import safety
    lines, ran = [], []
    monkeypatch.setattr(trail, 'record', lambda kind, **f: lines.append((kind, f)))
    monkeypatch.setattr(safety, 'computer_control_block', lambda agent_id, **kw: None)
    monkeypatch.setattr(hie, 'run_bounded', lambda argv, **kw: ran.append(argv) or BoundedResult(
        returncode=0, stdout='ok', stderr='', timed_out=False))
    before = thread_local_data.get_prompt_id()
    thread_local_data.set_prompt_id('8888')

    def run(code, lang='python'):
        return helper.gated_run_code(None, code, lang=lang, work_dir=str(tmp_path))
    yield run, lines, ran
    thread_local_data.set_prompt_id(before)


def test_a_block_naming_a_parent_folder_is_on_the_record(gate):
    """Refused before the path, it left no audit line (review of 0954bf864);
    it leaves the path's one line: the block, and the refusal."""
    run, lines, ran = gate
    code = "open('../x', 'w').write('x')"
    exitcode, reply, _ = run(code)
    assert exitcode == 1 and 'outside the workspace' in reply, reply
    assert ran == []
    assert [kind for kind, _ in lines] == ['action'], lines
    assert lines[0][1]['via'] == 'Shell_Command' and lines[0][1]['text'] == code, lines
    assert lines[0][1]['outcome'] == reply.split('\n')[0][:300], lines
    assert lines[0][1]['agent_id'] == '8888', lines


def test_a_block_the_policy_refuses_is_on_the_record_in_its_own_words(gate):
    """The model sent a code block, not a Shell_Command: the refusal says so,
    and the refusal is on the record."""
    run, lines, ran = gate
    code = "import os\nos.system('shutdown /r /t 0')"
    exitcode, reply, _ = run(code)
    assert exitcode == 1 and reply.startswith('Code block refused'), reply
    assert 'Shell_Command' not in reply, reply
    assert ran == []
    assert [(kind, f['text']) for kind, f in lines] == [('action', code)], lines
    assert lines[0][1]['outcome'].startswith('Code block refused'), lines


def test_a_block_while_hartos_is_still_loading_says_it_is_not_on_the_record(tmp_path, monkeypatch):
    """No gated path is loaded yet, so neither is its audit line: the block
    is refused and a WARNING says the refusal is not on the trail."""
    import core.safe_hartos_attr as door
    import hartos.helper as helper
    monkeypatch.setattr(door, 'safe_hartos_attr', lambda name, default=None: None)
    seen = []

    class Keep(logging.Handler):
        def emit(self, record):
            seen.append(record)
    log = logging.getLogger('hartos.helper')
    handler = Keep(level=logging.WARNING)
    log.addHandler(handler)
    try:
        exitcode, reply, _ = helper.gated_run_code(None, "print('hi')", work_dir=str(tmp_path))
    finally:
        log.removeHandler(handler)
    assert exitcode == 1 and 'still loading' in reply, reply
    assert [r for r in seen if 'not put on the audit trail' in r.getMessage()], seen


def test_a_name_only_assigned_is_no_command(gate):
    """'halt = False' was refused as the halt command; only a call or a
    command the block hands on is screened."""
    run, lines, ran = gate
    exitcode, reply, _ = run("halt = False\nprint(halt)")
    assert exitcode == 0, reply
    assert len(ran) == 1, ran


def test_a_called_name_and_a_command_given_as_a_list_are_still_refused(gate):
    run, lines, ran = gate
    blocks = ("halt()",
              "import subprocess\nsubprocess.run(['shutdown', '/r'])",
              "import subprocess\nsubprocess.run(args=['shut' 'down', '-h', 'now'])")
    for code in blocks:
        exitcode, reply, _ = run(code)
        assert exitcode == 1 and reply.startswith('Code block refused'), (code, reply)
    assert ran == []
    assert [f['text'] for _, f in lines] == list(blocks), lines
