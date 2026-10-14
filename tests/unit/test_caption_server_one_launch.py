"""One 0.8B caption server per port: HARTOS never races Nunba's start.

Qwen08BBackend.ensure_running sends 'vlm_caption.requested' (Nunba answers it
with llama_config.start_caption_server, main.py ~:649-671), waits about 5 s,
and used to run its own standalone llama-server spawn when the server was
not up by then.  That spawn ignored Nunba's per-port start lock
(<Nunba config dir>/.caption_server_starting.<port>.lock, llama_config.py
~:2850), so a Nunba start slower than 5 s plus a reel put two 0.8B servers
on one port -- the 2026-09-26 incident class (two servers on :8081, the
loser holding its VRAM).

Now, when Nunba is handling the start -- its subscriber is on this process's
event bus, or its per-port lock is held -- HARTOS spawns nothing and waits
for Nunba's server within the bound its own launch had (5 s + 30 s).  The
lock is held by Nunba's own rule (start_caption_server, llama_config.py
~:2851-2872): only while it is under 120 s old.  An older one was left by a
start that died (a Nunba killed mid-start) and stops nobody -- not Nunba's
next start, and not HARTOS's (review of 6067e8806: a lock of any age stopped
HARTOS for good).  A HARTOS-only node (no subscriber, no lock) keeps the
standalone launch, and
that launch finds the 0.8B's weights and projector by the catalog row's file
names (model_catalog 'llm-qwen3.5-0.8b'): in the folder the row recorded, in
Nunba's models folder (LlamaInstaller.resolve_models_dir) when that is
importable, else its default ~/.nunba/models, and ~/.trueflow/models; the
projector under the installer's name (mmproj-Qwen3.5-0.8B-F16.gguf) or where
scripts/setup_vlm.py puts it (qwen08b/mmproj-F16.gguf).  Selection
(is_available, which get_vision_backend asks) finds the weights by that same
lookup, so a 0.8B the launch would start is never passed over.  Weights count
only when whole, by the one completeness rule (model_catalog.gguf_complete at
gguf_min_bytes of the registered size, Nunba's get_model_path rule): a
partial download is neither selected nor spawned, and does not hide a whole
copy in a later folder.

Real code throughout: the real backend, the real platform EventBus and
registry, the real model catalog and completeness rule.  Replaced: the caption
port's HTTP (pooled_get), the event delivery (synchronous, to the real bus),
the process spawn (subprocess.Popen and the llama-server lookup), sleep (a
clock, for the thread the start runs on only: time.sleep is process-wide), the
home folder (a temp dir), and the 0.8B's registered size and the rule's
100 MB floor (scaled to WEIGHTS bytes, so whole weights are real files of a
kilobyte rather than half a gigabyte).

    python -m pytest tests/unit/test_caption_server_one_launch.py -q -p no:cacheprovider
"""
import os
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest
import requests

import integrations.service_tools.model_catalog as mc
import integrations.vision.lightweight_backend as lvb
from tests.conftest import refuse_all_network

PORT = 18491
MODEL = 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf'
#: The 0.8B's registered size in these tests (the real row is 558,772,480).
WEIGHTS = 1000
STANDALONE_BINARY = ('integrations.service_tools.model_lifecycle.'
                     'ModelLifecycleManager._find_llama_server_binary')
#: The bound the start had before: the 5 x 1 s event wait, then 30 x 1 s.
BOUND_S = 35
#: time.sleep as it is, before any test replaces it.
REAL_SLEEP = time.sleep


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_caption_server_one_launch: HTTP is mocked')


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def sleep(self, s=0):
        self.sleeps += 1
        self.t += s


class _Port:
    """The caption port.  Each server started on it is a time it answers
    /health from (None: never); ``spawned`` are HARTOS's own spawns."""

    def __init__(self, clock):
        self.clock = clock
        self.servers = []
        self.spawned = []

    def start(self, after_s):
        self.servers.append(None if after_s is None else self.clock.t + after_s)

    def health(self, url, timeout=None, **kw):
        assert url == f'http://127.0.0.1:{PORT}/health', url
        if any(at is not None and at <= self.clock.t for at in self.servers):
            return MagicMock(status_code=200)
        raise requests.ConnectionError('refused')

    def popen(self, cmd, **kw):
        self.spawned.append(list(cmd))
        self.start(3.0)                     # a 0.8B loads in a few seconds
        return MagicMock(pid=4242)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """This machine with its home at ``tmp_path``, a fresh platform event bus
    and no Nunba in it unless a test puts one there."""
    home = tmp_path / 'home'
    (home / '.nunba' / 'models').mkdir(parents=True)
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TEMP', str(tmp_path))
    monkeypatch.delenv('NUNBA_MODELS_DIR', raising=False)
    # The one completeness rule, at a kilobyte: whole is WEIGHTS bytes.
    monkeypatch.setitem(mc.MODEL_WEIGHT_BYTES, MODEL, (WEIGHTS, 'test: scaled down'))
    monkeypatch.setattr(mc, 'GGUF_MIN_BYTES', 0)
    from core.platform import registry as reg
    from core.platform.events import EventBus
    bus = EventBus()
    fresh = reg.ServiceRegistry()
    fresh.register('events', lambda: bus)
    monkeypatch.setattr(reg, '_registry', fresh)
    clock = _Clock()
    port = _Port(clock)
    return types.SimpleNamespace(home=home, models=home / '.nunba' / 'models', bus=bus,
                                 clock=clock, port=port, tmp=tmp_path, mp=monkeypatch)


def _put(path, data=b'GGUF'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _weights(path, size=WEIGHTS, magic=b'GGUF'):
    """0.8B weights at ``path``: whole by default (WEIGHTS bytes, the GGUF
    magic first); fewer bytes or other first bytes for a bad download."""
    return _put(path, magic + b'\0' * (size - len(magic)))


def _files_in(folder, mmproj=('mmproj-Qwen3.5-0.8B-F16.gguf',)):
    """Whole 0.8B weights and its projector in ``folder`` (projector path
    parts)."""
    return _weights(folder / MODEL), _put(folder.joinpath(*mmproj))


def _lock(box):
    return box.home / '.nunba' / f'.caption_server_starting.{PORT}.lock'


def _nunba_in_this_process(box, start_s):
    """Nunba's main.py subscriber: on the event it runs start_caption_server,
    which takes the per-port lock and brings a server up ``start_s`` later
    (None: never)."""
    def on_requested(topic, data):
        assert data['port'] == PORT
        _lock(box).write_text(str(os.getpid()))
        box.port.start(start_s)
    box.bus.on('vlm_caption.requested', on_requested)


def _ensure(box, backend=None):
    """ensure_running on the real backend; returns (ok, backend).

    The clock is the start's: it runs on this thread, and only this
    thread's sleeps move the clock.  Another thread's sleep is its own,
    really slept -- finding F1 (2026-10-14): a VisionService that
    hart_intelligence_entry's import-time vision_init thread started keeps a
    description loop sleeping every 4 s."""
    backend = backend or lvb.Qwen08BBackend(port=PORT)
    asked = []
    me = threading.get_ident()

    def deliver(topic, data=None, *a, **kw):
        asked.append(topic)
        box.bus.emit(topic, data)

    def sleep(seconds=0):
        if threading.get_ident() == me:
            box.clock.sleep(seconds)
        else:
            REAL_SLEEP(seconds)
    with patch.object(lvb, 'pooled_get', side_effect=box.port.health), \
         patch('core.platform.events.emit_event', side_effect=deliver), \
         patch(STANDALONE_BINARY, return_value=str(box.tmp / 'llama-server.exe')), \
         patch('subprocess.Popen', side_effect=box.port.popen), \
         patch('time.sleep', sleep):
        ok = backend.ensure_running()
    box.asked = asked
    log = getattr(backend, '_log_fh', None)
    if log:
        log.close()
    return ok, backend


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


# ── Nunba is starting it: HARTOS waits, never spawns ─────────────────────

def test_nunba_answering_slowly_with_its_lock_held_gets_no_second_server(box):
    """Nunba's subscriber answers the event and holds its per-port lock, but
    its server takes 20 s (a cold load): longer than the 5 s HARTOS waited
    before it spawned its own.  HARTOS spawns nothing, and it is ready when
    Nunba's server is -- one server on the port."""
    _files_in(box.models, ('qwen08b', 'mmproj-F16.gguf'))
    _files_in(box.models)
    _nunba_in_this_process(box, start_s=20.0)
    ok, _ = _ensure(box)
    assert box.asked == ['vlm_caption.requested']
    assert box.port.spawned == [], 'HARTOS spawned a second server while Nunba was starting one'
    assert ok is True and len(box.port.servers) == 1
    assert box.clock.t <= BOUND_S


def test_a_nunba_start_that_never_finishes_is_waited_for_within_the_bound(box):
    _files_in(box.models, ('qwen08b', 'mmproj-F16.gguf'))
    _files_in(box.models)
    _nunba_in_this_process(box, start_s=None)
    ok, _ = _ensure(box)
    assert ok is False
    assert box.port.spawned == []
    assert box.clock.sleeps == BOUND_S and box.clock.t == BOUND_S


def test_another_threads_sleeps_do_not_move_the_starts_clock(box):
    """Finding F1 (2026-10-14), simulated: time.sleep is process-wide, and a
    VisionService started by hart_intelligence_entry's import-time
    vision_init thread keeps a description loop that sleeps every 4 s
    (vision_service.py ~:905).  It ticks while a start waits for a Nunba
    start that never finishes: the start still waits its whole bound, by
    its own clock, and spawns nothing."""
    _files_in(box.models, ('qwen08b', 'mmproj-F16.gguf'))
    _files_in(box.models)
    _nunba_in_this_process(box, start_s=None)
    ticks = []

    def description_loop():
        for _ in range(3):
            time.sleep(0.01)
            ticks.append(1)

    def vision_service_meanwhile(topic, data):
        loop = threading.Thread(target=description_loop, name='vision-desc', daemon=True)
        loop.start()
        loop.join(10)
    box.bus.on('vlm_caption.requested', vision_service_meanwhile)
    ok, _ = _ensure(box)
    assert ticks == [1, 1, 1]
    assert ok is False and box.port.spawned == []
    assert box.clock.sleeps == BOUND_S and box.clock.t == BOUND_S


def test_the_2026_09_26_shape_a_start_in_progress_elsewhere_is_not_raced(box):
    """Measured 2026-09-26: two 0.8B servers on :8081, started in the same
    minute by two parents.  Here Nunba's boot thread, in its own process, is
    loading the draft server on this port (its lock is held; no subscriber
    in HARTOS's process) when a reel asks for the vision model.  No second
    server is spawned on the port."""
    _files_in(box.models, ('qwen08b', 'mmproj-F16.gguf'))
    _files_in(box.models)
    _lock(box).write_text('31337')
    box.port.start(12.0)
    ok, _ = _ensure(box)
    assert box.port.spawned == []
    assert len(box.port.servers) == 1, 'a second 0.8B server was spawned on the port'
    assert ok is True and box.clock.t <= BOUND_S


def test_nunbas_subscriber_alone_before_it_takes_its_lock_gets_no_second_server(box):
    """Nunba's subscriber took the event, but its start has not written the
    per-port lock yet (it is still probing the port or building its
    LlamaConfig), and its server takes 20 s.  The subscriber alone says Nunba
    is starting it: HARTOS spawns nothing.  (Review of 6067e8806, mutant M1:
    with the subscriber check deleted every other test here still passed --
    each one that had a subscriber also had the lock.)"""
    _files_in(box.models)

    def start_caption_server_before_its_lock(topic, data):
        box.port.start(20.0)
    box.bus.on('vlm_caption.requested', start_caption_server_before_its_lock)
    ok, _ = _ensure(box)
    assert not _lock(box).exists()
    assert box.port.spawned == [], 'HARTOS spawned beside a start Nunba had taken'
    assert ok is True and len(box.port.servers) == 1
    assert box.clock.t <= BOUND_S


def _lock_aged(box, age_s):
    """Nunba's per-port start lock, last written ``age_s`` seconds ago."""
    lock = _lock(box)
    lock.write_text('31337')
    then = time.time() - age_s
    os.utime(lock, (then, then))
    return lock


def test_a_stale_lock_left_by_a_killed_nunba_does_not_stop_the_standalone_launch(box):
    """Review of 6067e8806, measured: a HARTOS with no Nunba subscriber (a
    standalone hart_intelligence_entry) on a box where Nunba was hard-killed
    mid-start, so its lock is a day old.  It took any lock as 'Nunba is
    starting it': every ensure_running waited 35 s, spawned nothing and said
    False, then a 120 s cooldown, until Nunba ran again -- captions,
    read_document and a reel's frame ratings dead.  Nunba's own rule calls
    that lock stale (its next start removes it); so does HARTOS now: one
    spawn, ready.  The lock is Nunba's: HARTOS leaves it where it is."""
    _files_in(box.models)
    lock = _lock_aged(box, 24 * 3600)
    ok, _ = _ensure(box)
    assert len(box.port.spawned) == 1, 'a stale Nunba lock stopped the standalone launch'
    assert ok is True
    assert lock.exists(), "HARTOS removed Nunba's lock file"


@pytest.mark.parametrize('age_s, held', [(118, True), (122, False)],
                         ids=['under_120s_held', 'over_120s_stale'])
def test_the_lock_is_held_by_nunbas_own_age_rule(box, age_s, held):
    """Nunba's start_caption_server waits on a lock under 120 s old and
    removes an older one (llama_config.py ~:2853-2872, `lock_age < 120`;
    Nunba's tests/test_caption_server_spawn_lock.py::
    test_a_stale_lock_does_not_block_the_spawn).  HARTOS reads the same lock
    by the same rule, so the two never disagree about whether a start is in
    progress: 2 s either side of 120 s."""
    _files_in(box.models)
    _lock_aged(box, age_s)
    box.port.start(12.0 if held else None)      # Nunba's start, when there is one
    ok, _ = _ensure(box)
    assert len(box.port.spawned) == (0 if held else 1)
    assert ok is True


def test_a_server_already_up_spawns_nothing_and_asks_nothing(box):
    _files_in(box.models)
    box.port.start(0.0)
    ok, _ = _ensure(box)
    assert ok is True
    assert box.asked == [] and box.port.spawned == [] and box.clock.sleeps == 0


# ── no Nunba: the standalone launch, with the catalog's files ─────────────

@pytest.mark.parametrize('mmproj', [
    ('mmproj-Qwen3.5-0.8B-F16.gguf',),          # Nunba's installer (this machine)
    ('qwen08b', 'mmproj-F16.gguf'),             # scripts/setup_vlm.py
], ids=['installer_name', 'setup_vlm_layout'])
def test_without_nunba_the_standalone_launch_spawns_with_the_resolved_files(box, mmproj):
    model, proj = _files_in(box.models, mmproj)
    ok, backend = _ensure(box)
    assert box.asked == ['vlm_caption.requested']       # asked first; nobody answered
    assert len(box.port.spawned) == 1, 'a HARTOS-only node lost its standalone launch'
    cmd = box.port.spawned[0]
    assert _arg(cmd, '--model') == str(model)
    assert _arg(cmd, '--mmproj') == str(proj)
    assert _arg(cmd, '--port') == str(PORT)
    assert ok is True and backend._server_proc is not None


def test_the_folder_the_catalog_recorded_is_asked_first(box):
    """The catalog row's local_path (where the loader that fetched the
    weights put them) wins over the default folder."""
    from integrations.service_tools.model_catalog import get_catalog
    elsewhere = box.tmp / 'drive_f' / 'models'
    model, proj = _files_in(elsewhere)
    _files_in(box.models)
    entry = get_catalog().get(lvb.Qwen08BBackend.CATALOG_ID)
    box.mp.setattr(entry, 'local_path', str(model))
    ok, _ = _ensure(box)
    cmd = box.port.spawned[0]
    assert (_arg(cmd, '--model'), _arg(cmd, '--mmproj')) == (str(model), str(proj))
    assert ok is True


def _nunbas_models_dir_is(box, folder):
    """Nunba's llama_installer importable here, its one storage rule
    (LlamaInstaller.resolve_models_dir: explicit, NUNBA_MODELS_DIR, saved
    config, default) naming ``folder``."""
    installer = types.ModuleType('llama.llama_installer')

    class LlamaInstaller:
        @staticmethod
        def resolve_models_dir(models_dir=None, *, config_dir=None):
            from pathlib import Path
            return Path(folder)
    installer.LlamaInstaller = LlamaInstaller
    package = types.ModuleType('llama')
    package.llama_installer = installer
    box.mp.setitem(sys.modules, 'llama', package)
    box.mp.setitem(sys.modules, 'llama.llama_installer', installer)


def test_nunbas_models_folder_rule_decides_when_its_installer_is_importable(box):
    """Where Nunba's llama_installer can be imported, its one storage rule
    names the models folder."""
    chosen = box.tmp / 'chosen_models'
    model, proj = _files_in(chosen)
    _nunbas_models_dir_is(box, chosen)
    ok, _ = _ensure(box)
    cmd = box.port.spawned[0]
    assert (_arg(cmd, '--model'), _arg(cmd, '--mmproj')) == (str(model), str(proj))
    assert ok is True


def test_no_projector_on_disk_means_no_spawn(box):
    _weights(box.models / MODEL)
    _put(box.models / 'mmproj-F16.gguf')        # a projector of unknown model: not taken
    ok, _ = _ensure(box)
    assert ok is False and box.port.spawned == []


# ── a weight file counts only when whole: the one completeness rule ──────

#: Bad downloads of the 0.8B: one byte under 90% of its registered size (the
#: rule's floor, int(WEIGHTS * 0.90) = 900), and a whole-size file whose first
#: bytes are not the GGUF magic (an error page saved under the weight's name).
BAD_WEIGHTS = {'partial': dict(size=899), 'not_a_gguf': dict(magic=b'<htm')}


@pytest.mark.parametrize('bad', sorted(BAD_WEIGHTS))
def test_a_bad_download_is_never_spawned(box, bad):
    """It took any file of the right name, so a partial download was spawned
    and failed to load: 30 s, False, a 120 s cooldown, every time
    (77a8b166b's minor).  Nunba's get_model_path has refused it all along;
    the 0.8B lookup now asks the same rule."""
    _weights(box.models / MODEL, **BAD_WEIGHTS[bad])
    _put(box.models / 'mmproj-Qwen3.5-0.8B-F16.gguf')
    ok, _ = _ensure(box)
    assert box.port.spawned == [], 'a download that is not whole was spawned'
    assert ok is False


def test_the_rules_floor_itself_is_whole(box):
    """Exactly 90% of the registered size is complete (Nunba: >= the floor)."""
    model, proj = _weights(box.models / MODEL, size=900), \
        _put(box.models / 'mmproj-Qwen3.5-0.8B-F16.gguf')
    ok, _ = _ensure(box)
    assert _arg(box.port.spawned[0], '--model') == str(model)
    assert ok is True


def test_a_partial_copy_does_not_hide_a_whole_one_further_on(box):
    """A partial in Nunba's folder, whole weights in ~/.trueflow/models: the
    launch takes the whole ones (as Nunba's walk skips a short file and goes
    on to the next folder)."""
    _weights(box.models / MODEL, size=899)
    model, proj = _files_in(box.home / '.trueflow' / 'models')
    ok, _ = _ensure(box)
    cmd = box.port.spawned[0]
    assert (_arg(cmd, '--model'), _arg(cmd, '--mmproj')) == (str(model), str(proj))
    assert ok is True


# ── selection: the 0.8B is found where the launch finds it ───────────────

def _selectable(box):
    """is_available() on the real backend, its server not answering."""
    with patch.object(lvb, 'pooled_get', side_effect=box.port.health):
        return lvb.Qwen08BBackend(port=PORT).is_available()


@pytest.mark.parametrize('where', ['nunba_default', 'trueflow', 'catalog_recorded',
                                   'nunbas_models_dir_rule'])
def test_the_0_8b_is_selectable_wherever_the_launch_would_find_it(box, where):
    """get_vision_backend() selects qwen08b by is_available().  It looked for
    the weights by a literal name in ~/.nunba/models and ~/.trueflow/models
    only, while the launch (_model_files) also asks the folder the catalog
    recorded and Nunba's models-folder rule (NUNBA_MODELS_DIR, a saved
    models_dir): weights there were launched but never selected (review of
    6067e8806).  One lookup now answers both."""
    folder = {'nunba_default': box.models,
              'trueflow': box.home / '.trueflow' / 'models',
              'catalog_recorded': box.tmp / 'drive_f' / 'models',
              'nunbas_models_dir_rule': box.tmp / 'chosen_models'}[where]
    model, _ = _files_in(folder)
    if where == 'catalog_recorded':
        from integrations.service_tools.model_catalog import get_catalog
        box.mp.setattr(get_catalog().get(lvb.Qwen08BBackend.CATALOG_ID),
                       'local_path', str(model))
    if where == 'nunbas_models_dir_rule':
        _nunbas_models_dir_is(box, folder)
    assert _selectable(box) is True


def test_no_0_8b_anywhere_is_not_selectable(box):
    _files_in(box.tmp / 'somewhere_nobody_looks')
    assert _selectable(box) is False


def test_the_weights_without_their_projector_are_still_selectable(box):
    """Selection asks for the weights only, as before: Nunba's start fetches
    a missing projector itself (llama_config._do_start_caption_server,
    _download_mmproj_only).  Only HARTOS's own spawn needs both."""
    _weights(box.models / MODEL)
    assert _selectable(box) is True


@pytest.mark.parametrize('bad', sorted(BAD_WEIGHTS))
def test_a_bad_download_is_not_selectable(box, bad):
    """A file of the right name that is not whole made qwen08b selectable
    (23848e35a's point 6), so VisionService picked a backend whose launch
    could only fail to load it, over Qwen3-VL / MiniCPM."""
    _weights(box.models / MODEL, **BAD_WEIGHTS[bad])
    assert _selectable(box) is False


def test_a_lookup_that_fails_is_not_selectable_and_never_raises(box):
    """The catalog cannot be read: the launch could not find the files
    either, so the backend is not offered (it was, by the literal name)."""
    _files_in(box.models)
    with patch('integrations.service_tools.model_catalog.get_catalog',
               side_effect=RuntimeError('catalog unreadable')):
        assert _selectable(box) is False
