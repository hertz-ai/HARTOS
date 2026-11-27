"""The face runs in the mode this machine's resources allow, measured, never guessed (owner 2026-11-26:
"the cpu offloading vs fully gpu accelerated ... shd be based on resource available").

Measured 2026-11-26 on an RTX 3070 Laptop (AvatarForcing results, cpu_offload_bench_summary.md): gpu 1.46x
real time at a 535 MB CUDA peak; cpu_audio 0.40x at 450 MB; cpu_audio_ft_int8 0.30x at 272 MB.  A live face
(a person watching) needs real time, so on this laptop only gpu is live; the offload modes serve faces made
to be played later.  Reviews: hartos-17 (resource_based_offload_design.md, 5 MUSTs).
"""
import json

import pytest

import importlib

# The modules themselves: the package exports instances under some of these names
# (integrations.service_tools.vram_manager is the VRAMManager singleton there).
vm_mod = importlib.import_module('integrations.service_tools.vram_manager')
af = importlib.import_module('integrations.service_tools.avatarforcing_tool')
rm = importlib.import_module('integrations.service_tools.runtime_manager')


@pytest.fixture
def vm(monkeypatch, tmp_path):
    """A fresh VRAMManager on a CUDA card whose memo reads ``free`` GB (set per test); its measured
    store lives in tmp_path."""
    monkeypatch.setattr(vm_mod.VRAMManager, '_resolve_measured_path',
                        staticmethod(lambda: tmp_path / 'vram_measured.json'))
    m = vm_mod.VRAMManager()
    state = {'free': 2.0, 'cuda': True}
    monkeypatch.setattr(m, 'detect_gpu', lambda: {'cuda_available': state['cuda'], 'free_gb': state['free'],
                                                  'total_gb': 8.0, 'name': 'test'})
    m.state = state
    return m


# ── need=None answers exactly as before, for every model (the fold of the two 0.5 rules) ──────

def _todays_rule(free, size):
    """suggest_offload_mode's rule as it stood before this change, written out (not a parent run)."""
    if free >= size:
        return 'gpu'
    if free >= size * 0.5:
        return 'cpu_offload'
    return 'cpu_only'


@pytest.mark.parametrize('free', [0.0, 0.2, 0.5, 0.9, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 16.0])
def test_no_need_gives_todays_answer_for_every_model(vm, free):
    vm.state['free'] = free
    for tool, (_min, size) in vm_mod.VRAM_BUDGETS.items():
        assert vm.suggest_offload_mode(tool) == _todays_rule(free, size), (tool, free)


@pytest.mark.parametrize('free,size', [(0.5, 1.0), (0.49, 1.0), (2.0, 4.0), (1.99, 4.0), (0.0, 0.0)])
def test_the_one_offload_rule(free, size):
    assert vm_mod.offload_fits(free, size) == (free >= size * 0.5)


# ── the measured store, keyed by (model, mode) ───────────────────────────────────────────────

def test_the_peak_kept_is_the_highest_and_the_speed_the_slowest_of_the_last_runs(vm):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.50, rtf=1.5)
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.45, rtf=1.4)
    for r in (1.6, 1.6, 1.6, 1.6, 1.6):  # pushes 1.5 and 1.4 out of the last RTF_SAMPLES
        vm.record_mode_measurement('avatarforcing', 'gpu', rtf=r)
    assert vm.mode_measurement('avatarforcing', 'gpu') == (0.5, 1.6)
    vm.record_mode_measurement('avatarforcing', 'gpu', rtf=0.9)
    assert vm.mode_measurement('avatarforcing', 'gpu') == (0.5, 0.9)
    assert vm.mode_measurement('avatarforcing', 'cpu_audio') == (None, None)


@pytest.mark.parametrize('bad', [0, -1, 65, None, 'x'])
def test_an_out_of_range_peak_is_ignored(vm, bad):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=bad, rtf=1.2)
    assert vm.mode_measurement('avatarforcing', 'gpu') == (None, 1.2)


@pytest.mark.parametrize('bad', [0, -0.5, 101, None, 'x'])
def test_an_out_of_range_speed_is_ignored(vm, bad):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.5, rtf=bad)
    assert vm.mode_measurement('avatarforcing', 'gpu') == (0.5, None)


def test_old_files_and_old_readers_stay_valid(vm, tmp_path, monkeypatch):
    # an old file (model-only records) loads as before
    (tmp_path / 'vram_measured.json').write_text(json.dumps({'whisper': 1.2}), encoding='utf-8')
    fresh = vm_mod.VRAMManager()
    assert fresh.get_measured_usage() == {'whisper': 1.2}
    assert fresh.mode_measurement('avatarforcing', 'gpu') == (None, None)
    # a new file keeps the model-only records readable by the OLD loader (numeric values only)
    fresh.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.55, rtf=1.46)
    data = json.loads((tmp_path / 'vram_measured.json').read_text(encoding='utf-8'))
    old_loader = {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v > 0}
    assert old_loader == {'whisper': 1.2}
    again = vm_mod.VRAMManager()
    assert again.mode_measurement('avatarforcing', 'gpu') == (0.55, 1.46)


def test_one_malformed_mode_row_never_empties_the_other_measurements(vm, tmp_path):
    """Review rv_lowvram D: a bad __modes__ row emptied every model's measurement (and the next
    persist wrote that loss back).  Now only the bad row is skipped."""
    (tmp_path / 'vram_measured.json').write_text(json.dumps({
        'whisper': 1.2,
        '__modes__': {'avatarforcing@gpu': {'peak_gb': 0.55, 'rtf': [1.46]},
                      'avatarforcing@cpu_audio': {'peak_gb': 'lots', 'rtf': [0.4]},
                      'broken': 'not a row'}}), encoding='utf-8')
    fresh = vm_mod.VRAMManager()
    assert fresh.get_measured_usage() == {'whisper': 1.2}
    assert fresh.mode_measurement('avatarforcing', 'gpu') == (0.55, 1.46)
    assert fresh.mode_measurement('avatarforcing', 'cpu_audio') == (None, None)
    fresh.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.56, rtf=1.5)
    data = json.loads((tmp_path / 'vram_measured.json').read_text(encoding='utf-8'))
    assert data['whisper'] == 1.2  # the persist after a bad row keeps the model records


# ── the decision: fit by measured peak, live needs measured real time ────────────────────────

def _this_laptop(vm):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.535, rtf=1.46)
    vm.record_mode_measurement('avatarforcing', 'cpu_audio', peak_gb=0.450, rtf=0.40)
    vm.record_mode_measurement('avatarforcing', 'cpu_audio_ft_int8', peak_gb=0.272, rtf=0.30)


@pytest.mark.parametrize('free,live,later', [
    (2.0, 'gpu', 'gpu'),                                # room: the full face
    (0.83, 'cpu_only', 'cpu_audio'),                    # gpu needs 0.535 + 0.3 = 0.835: no room
    (0.76, 'cpu_only', 'cpu_audio'),                    # cpu_audio fits (0.75); not real time
    (0.60, 'cpu_only', 'cpu_audio_ft_int8'),            # only int8 fits (0.572)
    (0.40, 'cpu_only', 'cpu_only'),                     # nothing fits
])
def test_this_laptop_measured(vm, free, live, later):
    _this_laptop(vm)
    vm.state['free'] = free
    assert vm.suggest_offload_mode('avatarforcing', need='live') == live
    assert vm.suggest_offload_mode('avatarforcing', need='async') == later


def test_a_machine_whose_offload_is_real_time_gets_it_live(vm):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.535, rtf=1.46)
    vm.record_mode_measurement('avatarforcing', 'cpu_audio', peak_gb=0.450, rtf=1.10)
    vm.state['free'] = 0.76
    assert vm.suggest_offload_mode('avatarforcing', need='live') == 'cpu_audio'


def test_with_no_measurement_live_takes_gpu_by_the_budget_only(vm):
    vm.state['free'] = 1.0  # VRAM_BUDGETS avatarforcing = 1.0 GB
    assert vm.suggest_offload_mode('avatarforcing', need='live') == 'gpu'
    vm.state['free'] = 0.99
    assert vm.suggest_offload_mode('avatarforcing', need='live') == 'cpu_only'   # never a guessed offload
    assert vm.suggest_offload_mode('avatarforcing', need='async') == 'cpu_only'


def test_a_gpu_measured_under_real_time_is_not_live(vm):
    vm.record_mode_measurement('avatarforcing', 'gpu', peak_gb=0.535, rtf=0.8)
    vm.state['free'] = 4.0
    assert vm.suggest_offload_mode('avatarforcing', need='live') == 'cpu_only'
    assert vm.suggest_offload_mode('avatarforcing', need='async') == 'gpu'


def test_no_cuda_is_heard_and_never_tries_a_mode(vm):
    _this_laptop(vm)
    vm.state.update(cuda=False, free=8.0)
    assert vm.suggest_offload_mode('avatarforcing', need='live') == 'cpu_only'
    assert vm.suggest_offload_mode('avatarforcing', need='async') == 'cpu_only'


def test_the_decision_reads_no_new_gpu_reading(vm, monkeypatch):
    def no_refresh(*a, **k):
        raise AssertionError('the mode decision refreshed the GPU reading')
    monkeypatch.setattr(vm, 'refresh_gpu_info', no_refresh)
    _this_laptop(vm)
    vm.suggest_offload_mode('avatarforcing', need='live')


# ── the server starts each mode with its own flags, and refuses one without CUDA ─────────────

def test_each_mode_names_its_flags_and_a_cpu_only_start_is_refused(monkeypatch, tmp_path):
    from integrations.service_tools.servers import avatarforcing_server as srv
    assert srv.OFFLOAD_FLAGS['gpu'] == []
    assert srv.OFFLOAD_FLAGS['cpu_audio'] == ['--audio_device', 'cpu']
    assert srv.OFFLOAD_FLAGS['cpu_audio_ft_int8'] == ['--audio_device', 'cpu', '--ft_device', 'cpu',
                                                      '--cpu_int8']
    assert set(vm_mod.MODE_LADDER['avatarforcing']) <= set(srv.OFFLOAD_FLAGS)
    (tmp_path / 'configs').mkdir()
    (tmp_path / 'configs' / 'inference.yaml').write_text('x', encoding='utf-8')
    monkeypatch.setenv('AVATARFORCING_MODEL_DIR', str(tmp_path))
    monkeypatch.setenv('AVATARFORCING_OFFLOAD', 'cpu_only')
    assert srv.main() == 3


# ── the face tool: live mode, and the last run measured against its mode once ───────────────

@pytest.mark.parametrize('engine,mode', [
    ({'audio_device': 'cuda:0', 'ft_device': 'cuda:0', 'cpu_int8': False}, 'gpu'),
    ({'audio_device': 'cpu', 'ft_device': 'cuda:0', 'cpu_int8': False}, 'cpu_audio'),
    ({'audio_device': 'cpu', 'ft_device': 'cpu', 'cpu_int8': True}, 'cpu_audio_ft_int8'),
    ({'audio_device': 'cpu', 'ft_device': 'cpu', 'cpu_int8': False}, None),
    (None, None),
])
def test_the_mode_a_sidecar_reports(engine, mode):
    assert af._mode_of(engine) == mode


def test_the_last_finished_run_is_recorded_once_and_never_while_running(monkeypatch):
    stats = {'session': 3, 'running': False, 'rate_fps': 36.5, 'cuda_peak_mb': 548.0,
             'engine': {'audio_device': 'cuda:0', 'ft_device': 'cuda:0', 'cpu_int8': False}}
    got = []

    class Resp:
        def json(self):
            return dict(stats)
    monkeypatch.setattr('core.http_pool.pooled_get', lambda url, timeout=None: Resp())
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'record_mode_measurement',
                        lambda *a, **k: got.append((a, k)))
    monkeypatch.setattr(af, '_measured_session', {'n': None})
    af._record_last_run(4321)
    af._record_last_run(4321)  # the same session again: not recorded twice
    assert len(got) == 1
    (tool, mode), kw = got[0]
    assert (tool, mode) == ('avatarforcing', 'gpu')
    assert kw['peak_gb'] == pytest.approx(548 / 1024) and kw['rtf'] == pytest.approx(36.5 / 25)
    stats.update(session=4, running=True)
    af._record_last_run(4321)  # still making frames: its speed is not final
    assert len(got) == 1


def test_a_live_face_runs_only_where_a_live_mode_exists(monkeypatch):
    seen = []
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'detect_gpu', lambda: {'cuda_available': True})

    def advisor(tool, need=None):
        seen.append(need)
        return 'cpu_only'
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', advisor)
    assert af._can_run_here(rm.runtime_tool_manager) is False
    assert seen == ['live']


# ── the runtime manager starts the face in its live mode, every other row as before ─────────

def test_only_a_row_naming_a_need_gets_the_measured_pick(monkeypatch):
    asked, started = [], []
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode',
                        lambda tool, need=None: asked.append((tool, need)) or 'gpu')
    monkeypatch.setattr(rm.runtime_tool_manager.storage, 'is_downloaded', lambda t: True)
    monkeypatch.setattr(rm.runtime_tool_manager, '_start_sidecar',
                        lambda tool, config, mode: started.append((tool, mode)) or {'running': True})
    monkeypatch.setattr(rm.runtime_tool_manager, 'save_state', lambda: None)
    rm.runtime_tool_manager.start_tool('avatarforcing')
    rm.runtime_tool_manager.start_tool('acestep')
    assert asked == [('avatarforcing', 'live'), ('acestep', None)]
