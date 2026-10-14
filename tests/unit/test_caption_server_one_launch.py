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
for Nunba's server within the bound its own launch had (5 s + 30 s).  A
HARTOS-only node (no subscriber, no lock) keeps the standalone launch, and
that launch finds the 0.8B's weights and projector by the catalog row's file
names (model_catalog 'llm-qwen3.5-0.8b'): in the folder the row recorded, in
Nunba's models folder (LlamaInstaller.resolve_models_dir) when that is
importable, else its default ~/.nunba/models, and ~/.trueflow/models; the
projector under the installer's name (mmproj-Qwen3.5-0.8B-F16.gguf) or where
scripts/setup_vlm.py puts it (qwen08b/mmproj-F16.gguf).

Real code throughout: the real backend, the real platform EventBus and
registry, the real model catalog.  Replaced: the caption port's HTTP
(pooled_get), the event delivery (synchronous, to the real bus), the process
spawn (subprocess.Popen and the llama-server lookup), sleep (a clock, for the
thread the start runs on only: time.sleep is process-wide), and the home
folder (a temp dir).

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

import integrations.vision.lightweight_backend as lvb
from tests.conftest import refuse_all_network

PORT = 18491
MODEL = 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf'
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


def _files_in(folder, mmproj=('mmproj-Qwen3.5-0.8B-F16.gguf',)):
    """The 0.8B weights and its projector in ``folder`` (projector path parts)."""
    return _put(folder / MODEL), _put(folder.joinpath(*mmproj))


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


def test_nunbas_models_folder_rule_decides_when_its_installer_is_importable(box):
    """Where Nunba's llama_installer can be imported, its one storage rule
    (LlamaInstaller.resolve_models_dir: explicit, NUNBA_MODELS_DIR, saved
    config, default) names the models folder."""
    chosen = box.tmp / 'chosen_models'
    model, proj = _files_in(chosen)
    installer = types.ModuleType('llama.llama_installer')

    class LlamaInstaller:
        @staticmethod
        def resolve_models_dir(models_dir=None, *, config_dir=None):
            from pathlib import Path
            return Path(chosen)
    installer.LlamaInstaller = LlamaInstaller
    package = types.ModuleType('llama')
    package.llama_installer = installer
    box.mp.setitem(sys.modules, 'llama', package)
    box.mp.setitem(sys.modules, 'llama.llama_installer', installer)
    ok, _ = _ensure(box)
    cmd = box.port.spawned[0]
    assert (_arg(cmd, '--model'), _arg(cmd, '--mmproj')) == (str(model), str(proj))
    assert ok is True


def test_no_projector_on_disk_means_no_spawn(box):
    _put(box.models / MODEL)
    _put(box.models / 'mmproj-F16.gguf')        # a projector of unknown model: not taken
    ok, _ = _ensure(box)
    assert ok is False and box.port.spawned == []
