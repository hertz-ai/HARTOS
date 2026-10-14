"""Remotion set up at runtime, off by default, and the reel step around it (A14/A16).

* The RuntimeToolManager row 'reel_motion': an npm row of our own project
  (exact versions, nothing installed in the repo), on demand, OFF unless
  HEVOLVE_REEL_MOTION is set, set up only after the owner's yes on the one
  consent card, with a free-disk check.  With the flag unset nothing is
  asked or run, whoever calls setup_tool (an agent tool or POST
  /api/tools/<name>/setup).
* motion.runtime: ensure_ready (off / asked / declined / unavailable /
  setting up within a bounded wait / failed / ready) and render (node
  render.js in the tool's folder, bounded).
* motion.compose: the reel's step -- rendered clips laid as 'motion'
  overlays when Remotion is ready, the plain-ffmpeg versions otherwise, the
  EDL valid either way, and the words her agent passes on.

The Node / npm / browser boundary is mocked: core.subprocess_safe.
run_bounded, the Node locator, and the manager where a test says so.
"""
import concurrent.futures
import importlib
import json
import os
from types import SimpleNamespace

import pytest

from core.subprocess_safe import BoundedResult
from integrations.reel_studio import contracts as C
from integrations.reel_studio.motion import compose, layout, runtime
from tests.unit.reel_media_fixtures import golden_edl

runtime_manager = importlib.import_module('integrations.service_tools.runtime_manager')
model_storage = importlib.import_module('integrations.service_tools.model_storage')

FLAG = 'HEVOLVE_REEL_MOTION'
CARD = ['Bridal makeup', 'Bridal from Rs 15k', 'DM for bookings']


# ── the row ───────────────────────────────────────────────────────────────

def test_the_row_is_our_project_on_demand_off_by_default_with_consent_and_a_disk_check():
    row = runtime_manager.TOOL_CONFIGS[runtime.TOOL]
    assert row['download_type'] == 'npm' and row['on_demand'] is True
    assert row['enabled_by'] == FLAG
    assert row['consent'] == 'reel:motion' and len(row['consent_reason']) > 40
    assert row['npm_min_free_bytes'] >= 1 << 30
    project = row['npm_project']
    assert os.path.samefile(project, os.path.join(os.path.dirname(runtime.__file__), 'project'))
    with open(os.path.join(project, 'package.json'), encoding='utf-8') as fh:
        pkg = json.load(fh)
    assert pkg['license'] == 'Apache-2.0' and pkg['private'] is True
    for name, version in pkg['dependencies'].items():
        assert version[0].isdigit(), '%s %s: an exact version, not a range' % (name, version)
    for name in (row['npm_prepare'], 'render.js', 'templates.js', 'index.js'):
        assert os.path.isfile(os.path.join(project, name)), name
    # Its package-lock.json pins what package.json names, from the public
    # registry, hashed: test_runtime_npm_setup.py
    # test_every_shipped_npm_project_locks_what_its_package_json_asks.
    # Nothing installed or built lives in the repo (A16).
    for name in ('node_modules', 'bundle'):
        assert not os.path.exists(os.path.join(project, name)), name


@pytest.fixture
def no_npm(monkeypatch):
    runs = []
    monkeypatch.setattr(model_storage, 'run_bounded',
                        lambda cmd, timeout=5.0, **kw: runs.append(cmd) or BoundedResult(0, '', ''))
    return runs


def test_with_the_flag_unset_setup_asks_nobody_and_runs_nothing(monkeypatch, no_npm, tmp_path):
    monkeypatch.delenv(FLAG, raising=False)
    import integrations.agent_engine.capability_setup as capability_setup
    asked = []
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup',
                        lambda *a, **k: asked.append(a) or 'granted')
    rtm = runtime_manager.RuntimeToolManager(
        storage=model_storage.ModelStorageManager(base_dir=tmp_path / 'models'))
    monkeypatch.setattr(runtime_manager, 'STATE_FILE', tmp_path / 'tool_state.json')
    result = rtm.setup_tool(runtime.TOOL)
    assert result.get('disabled') is True, result
    assert asked == [] and no_npm == []
    assert not (tmp_path / 'models' / runtime.TOOL).exists()


def test_bulk_setup_never_sets_it_up(monkeypatch, no_npm):
    monkeypatch.setenv(FLAG, '1')
    rtm = runtime_manager.RuntimeToolManager()
    monkeypatch.setattr(runtime_manager, 'TOOL_CONFIGS',
                        {runtime.TOOL: runtime_manager.TOOL_CONFIGS[runtime.TOOL]})
    assert 'skipped' in rtm.setup_available_tools()[runtime.TOOL] and no_npm == []


# ── ensure_ready ──────────────────────────────────────────────────────────

class _Manager:
    """RuntimeToolManager's surface runtime uses, scripted."""

    def __init__(self, *, enabled=True, ready=False, consent='granted', result=None, delay=None):
        self.enabled, self.ready, self.consent = enabled, ready, consent
        self.result = result if result is not None else {'ready': True}
        self.delay = delay
        self.calls = []
        self.storage = SimpleNamespace(get_tool_dir=lambda name: '/models/' + name)

    def tool_enabled(self, name):
        self.calls.append('enabled')
        return self.enabled

    def tool_ready(self, name):
        self.calls.append('ready')
        return self.ready

    def ask_tool_consent(self, name):
        self.calls.append('consent')
        return self.consent

    def setup_tool_async(self, name):
        self.calls.append('setup')
        future = concurrent.futures.Future()
        if self.delay is None:
            if 'error' not in self.result:
                self.ready = True
            future.set_result(self.result)
        return future


@pytest.fixture
def manager(monkeypatch):
    holder = {}

    def use(**kw):
        holder['m'] = _Manager(**kw)
        monkeypatch.setattr(runtime, '_manager', lambda: holder['m'])
        return holder['m']
    return use


def test_off_is_off_and_nothing_is_asked(manager):
    m = manager(enabled=False)
    assert runtime.ensure_ready(wait_s=5).state == runtime.OFF
    assert runtime.status() == runtime.OFF
    assert 'consent' not in m.calls and 'setup' not in m.calls


def test_ready_is_ready_without_asking(manager):
    m = manager(ready=True)
    assert runtime.ensure_ready(wait_s=5).ready
    assert runtime.status() == runtime.READY
    assert 'consent' not in m.calls


@pytest.mark.parametrize('answer, state', [('asked', runtime.ASKED), ('declined', runtime.DECLINED),
                                           ('unavailable', runtime.UNAVAILABLE)])
def test_without_the_owners_yes_nothing_is_set_up(manager, answer, state):
    m = manager(consent=answer)
    assert runtime.ensure_ready(wait_s=5).state == state
    assert 'setup' not in m.calls


def test_with_the_yes_the_setup_runs_and_she_is_told(manager):
    m = manager()
    told = []
    assert runtime.ensure_ready(wait_s=5, progress=told.append).ready
    assert m.calls.count('setup') == 1
    assert told and 'animated overlays' in told[0].lower()


def test_a_setup_longer_than_the_wait_keeps_running_and_says_so(manager, monkeypatch):
    m = manager(delay=True)
    monkeypatch.setattr(runtime, 'PROGRESS_EVERY_S', 0.05)
    told = []
    readiness = runtime.ensure_ready(wait_s=0.2, progress=told.append)
    assert readiness.state == runtime.SETTING_UP
    assert len(told) >= 2, 'started, then still going'


def test_a_failed_setup_says_why(manager):
    manager(result={'error': 'setting it up needs about 1.1 GB of free disk'})
    readiness = runtime.ensure_ready(wait_s=5)
    assert readiness.state == runtime.FAILED and 'free disk' in readiness.detail


# ── render ────────────────────────────────────────────────────────────────

@pytest.fixture
def node(monkeypatch, tmp_path, manager):
    manager(ready=True)
    import core.node_bin
    import core.subprocess_safe
    monkeypatch.setattr(core.node_bin, 'find_node', lambda beside=None: '/opt/node/bin/node')
    runs = []
    answer = {'result': BoundedResult(0, '{"event":"rendered"}', ''), 'write': True}

    def run_bounded(cmd, timeout=5.0, **kw):
        runs.append(SimpleNamespace(cmd=list(cmd), timeout=timeout, cwd=kw.get('cwd')))
        if answer['write']:
            with open(cmd[2], encoding='utf-8') as fh:
                for item in json.load(fh)['items']:
                    with open(item['out'], 'wb') as out:
                        out.write(b'mov')
        return answer['result']
    monkeypatch.setattr(core.subprocess_safe, 'run_bounded', run_bounded)
    return SimpleNamespace(runs=runs, answer=answer, work=tmp_path)


def _items(work):
    return [{'id': 'Title', 'props': {'text': 'Glow', 'width': 770, 'height': 300, 'fps': 30,
                                      'durationInFrames': 75},
             'out': str(work / 'm0.mov')}]


def test_render_runs_node_render_js_in_the_tools_folder_bounded(node):
    outs = runtime.render(_items(node.work), str(node.work))
    assert outs == [str(node.work / 'm0.mov')]
    [run] = node.runs
    assert run.cmd[:2] == ['/opt/node/bin/node', 'render.js'] and run.cwd == '/models/reel_motion'
    assert 0 < run.timeout <= runtime.RENDER_TIMEOUT_S
    with open(run.cmd[2], encoding='utf-8') as fh:
        job = json.load(fh)
    assert job['items'][0]['props']['durationInFrames'] == 75


@pytest.mark.parametrize('result, write, said', [
    (BoundedResult(1, '', 'Error: no composition Title'), True, 'renderer failed'),
    (BoundedResult(-1, '', '', timed_out=True), False, 'longer than'),
    (BoundedResult(0, '', ''), False, 'wrote no clip'),
])
def test_a_failed_render_raises_motion_failed(node, result, write, said):
    node.answer.update(result=result, write=write)
    with pytest.raises(runtime.MotionFailed, match=said):
        runtime.render(_items(node.work), str(node.work))


def test_no_node_no_render(node, monkeypatch):
    import core.node_bin
    monkeypatch.setattr(core.node_bin, 'find_node', lambda beside=None: None)
    with pytest.raises(runtime.MotionFailed, match='Node.js'):
        runtime.render(_items(node.work), str(node.work))
    assert node.runs == []


# ── the reel's step ───────────────────────────────────────────────────────

REQUEST = {'overlays': ['title', 'logo_sting', 'end_card'], 'hook_text': 'Bridal look in 20s',
           'card_text': CARD}


def _texts(edl):
    return [ov for ov in edl['overlays'] if ov['type'] == 'text']


def test_nothing_asked_changes_nothing(tmp_path, manager):
    m = manager()
    edl = golden_edl()
    out, notes = compose.add_motion(edl, {'overlays': []}, workdir=str(tmp_path))
    assert out == edl and notes == [] and m.calls == []


@pytest.mark.parametrize('state', [runtime.OFF, runtime.ASKED, runtime.DECLINED,
                                   runtime.UNAVAILABLE, runtime.SETTING_UP, runtime.FAILED])
def test_not_ready_lays_the_plain_versions_and_says_why(tmp_path, monkeypatch, state):
    monkeypatch.setattr(runtime, 'ensure_ready',
                        lambda **kw: runtime.Readiness(state, 'why' if state == runtime.FAILED else ''))
    rendered = []
    monkeypatch.setattr(runtime, 'render', lambda *a, **k: rendered.append(a))
    edl = golden_edl()
    out, notes = compose.add_motion(edl, REQUEST, workdir=str(tmp_path))
    assert C.validate_edl(out) == []
    assert rendered == [] and not [ov for ov in out['overlays'] if ov['type'] == 'motion']
    hook, card = _texts(out)
    assert hook['text'] == 'Bridal look in 20s', 'the title is the hook text'
    assert card['text'] == ' · '.join(CARD) and card['to_s'] == 20.0
    assert [ov for ov in out['overlays'] if ov['type'] == 'logo'][0]['from_s'] == 0.0
    assert notes[-1] == compose.NOTE_MOTION % state
    said = compose.message(notes)
    assert said and 'plain' in said


def test_off_tells_her_agent_it_waits_for_the_owner_and_the_license(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_ready', lambda **kw: runtime.Readiness(runtime.OFF))
    _out, notes = compose.add_motion(golden_edl(), REQUEST, workdir=str(tmp_path))
    said = compose.message(notes)
    assert 'license' in said.lower() and 'owner' in said.lower()


def test_ready_lays_the_rendered_clips_and_the_edl_stays_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_ready', lambda **kw: runtime.Readiness(runtime.READY))
    jobs = []

    def render(items, workdir, **kw):
        jobs.extend(items)
        return [i['out'] for i in items]
    monkeypatch.setattr(runtime, 'render', render)
    edl = golden_edl()
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
    edl['overlays'][1]['src'] = str(logo)
    out, notes = compose.add_motion(edl, REQUEST, workdir=str(tmp_path))
    assert notes == [compose.NOTE_MOTION % compose.RENDERED]
    assert C.validate_edl(out) == []
    motion = [ov for ov in out['overlays'] if ov['type'] == 'motion']
    assert [m['template'] for m in motion] == ['logo_sting', 'title', 'end_card']
    for m in motion:
        assert os.path.isabs(m['src']) and os.path.dirname(m['src']) == str(tmp_path)
    assert _texts(out) == [], 'the animated title stands in for the still hook text'
    still = [ov for ov in out['overlays'] if ov['type'] == 'logo'][0]
    assert still['from_s'] == layout.LOGO_STING_S, 'the still logo appears as the sting ends'
    by_id = {j['id']: j for j in jobs}
    assert by_id['LogoSting']['props']['logo'].startswith('data:image/png;base64,')
    assert by_id['Title']['props']['text'] == 'Bridal look in 20s'
    assert by_id['EndCard']['props']['lines'] == CARD
    for j in jobs:
        p = j['props']
        assert p['width'] % 2 == 0 and p['height'] % 2 == 0 and p['fps'] == 30
    assert by_id['Title']['props']['durationInFrames'] == round(layout.TITLE_S * 30)
    assert compose.message(notes) == ''


def test_a_failed_render_lays_the_plain_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_ready', lambda **kw: runtime.Readiness(runtime.READY))

    def render(items, workdir, **kw):
        raise runtime.MotionFailed('the renderer failed (exit 1)')
    monkeypatch.setattr(runtime, 'render', render)
    edl = golden_edl()
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'\x89PNG\r\n\x1a\n')
    edl['overlays'][1]['src'] = str(logo)
    out, notes = compose.add_motion(edl, REQUEST, workdir=str(tmp_path))
    assert notes[-1] == compose.NOTE_MOTION % runtime.FAILED
    assert C.validate_edl(out) == [] and len(_texts(out)) == 2


def test_a_skipped_template_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_ready', lambda **kw: runtime.Readiness(runtime.OFF))
    edl = golden_edl()
    edl['final_look_found'] = False
    _out, notes = compose.add_motion(edl, {'overlays': ['wipe']}, workdir=str(tmp_path))
    assert notes == [layout.SKIP_NOTE % ('wipe', layout.NO_FINAL_LOOK)]
    assert 'finished look' in compose.message(notes)
