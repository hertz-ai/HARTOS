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
lock's name and the age under which it is held are one definition both sides
read, Qwen08BBackend.start_lock_path and START_LOCK_STALE_S (120 s): Nunba's
start_caption_server writes the lock there and waits on it while it is
younger, HARTOS only reads it.  An older one was left by a start that died (a
Nunba killed mid-start) and stops nobody -- not Nunba's next start, and not
HARTOS's (review of 6067e8806: a lock of any age stopped HARTOS for good).  A
HARTOS-only node (no subscriber, no lock) keeps the standalone launch, and
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

The standalone launch sizes and places that server the way the other start of
it does (A13 proof D2, 2026-10-14: it ran at --ctx-size 512, and a reel's
550-token keyframe rating got HTTP 400 on every frame; it always passed
--n-gpu-layers 99).  Its context is the one Nunba's start_caption_server gives
the 0.8B (llama_config._derive_ctx_size: core.llama_geometry.derive_ctx_size
over a forced VRAM sample and the weights measured from the file).  On a CUDA
card its GPU layers are llamacpp_manager.get_optimal_params', HARTOS's one
placement of a GGUF on the box: all of them when it fits, part of them on a
card short of room.  On any other reading -- Apple Metal, AMD ROCm, a card
whose driver cannot run CUDA 12, no GPU, a reading that failed -- the launch
passes no --n-gpu-layers and llama-server places the layers itself (its
default, 'auto', fitted to the device's memory), as Nunba's start of the same
server does on a GPU that is not CUDA (review of a44883fe2: get_optimal_params
reads only cuda_available, and sent a Metal box's 0.8B to the CPU).

Real code throughout: the real backend, the real platform EventBus and
registry, the real model catalog and completeness rule, the real context
tiers and placement rule.  Replaced: the caption port's HTTP (pooled_get), the
event delivery (synchronous, to the real bus), the process spawn
(subprocess.Popen and the llama-server lookup), the GPU reading (vram_manager's
refresh_gpu_info and detect_gpu, where nvidia-smi would run), sleep (a clock,
for the thread the start runs on only: time.sleep is process-wide), the home
folder (a temp dir), and the 0.8B's registered size and the rule's 100 MB
floor (scaled to WEIGHTS bytes, so whole weights are real files of a kilobyte
rather than half a gigabyte; the geometry tests measure the file at the real
0.8B's size).

    python -m pytest tests/unit/test_caption_server_one_launch.py -q -p no:cacheprovider
"""
import contextlib
import os
import socket
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest
import requests

import integrations.service_tools.model_catalog as mc
import integrations.vision.lightweight_backend as lvb
from core.llama_geometry import ctx_for_role, derive_ctx_size
from integrations.reel_studio.media import moments
from tests.conftest import refuse_all_network

PORT = 18491
MODEL = 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf'
#: The 0.8B's registered size in these tests (the real row is 558,772,480).
WEIGHTS = 1000
#: The real 0.8B weight file's size, which the geometry tests measure it at.
REAL_WEIGHTS = 558_772_480
GIB = 1024 ** 3
#: One reel keyframe rating, measured 2026-10-14 (A13 proof D2): 550 prompt
#: tokens (336 image + 206 rubric) plus the answer budget moments asks for.
RATING_TOKENS = 550 + moments.MAX_TOKENS
#: GPU readings, as vram_manager.detect_gpu reports them.
ROOMY_GPU = {'name': 'NVIDIA GeForce RTX 3070', 'total_gb': 8.0, 'free_gb': 7.5,
             'cuda_available': True}
NO_GPU = {'name': None, 'total_gb': 0.0, 'free_gb': 0.0, 'cuda_available': False}
#: An Apple Silicon Mac: unified memory, its free RAM read as the GPU's
#: (vram_manager.detect_gpu's Metal branch).
METAL_GPU = {'name': 'Apple Metal (Apple Silicon)', 'total_gb': 16.0, 'free_gb': 9.0,
             'cuda_available': False, 'metal_available': True}
#: An AMD card read by rocm-smi (vram_manager.detect_gpu's ROCm branch).
ROCM_GPU = {'name': 'AMD GPU (ROCm)', 'total_gb': 16.0, 'free_gb': 12.0,
            'cuda_available': False, 'rocm_available': True}
#: An NVIDIA card whose driver is too old for the CUDA 12 runtime.
OLD_DRIVER_GPU = {'name': 'NVIDIA GeForce 940MX', 'total_gb': 2.0, 'free_gb': 1.9,
                  'cuda_available': False, 'driver_version': '391.35',
                  'driver_cuda_ok': False}
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
        #: How long a server HARTOS spawns takes to load (None: it never does).
        self.load_s = 3.0                   # a 0.8B loads in a few seconds
        #: Called with the spawned command, as the spawn happens.
        self.on_spawn = None

    def start(self, after_s):
        self.servers.append(None if after_s is None else self.clock.t + after_s)

    def health(self, url, timeout=None, **kw):
        assert url == f'http://127.0.0.1:{PORT}/health', url
        if any(at is not None and at <= self.clock.t for at in self.servers):
            return MagicMock(status_code=200)
        raise requests.ConnectionError('refused')

    def popen(self, cmd, **kw):
        self.spawned.append(list(cmd))
        self.start(self.load_s)
        if self.on_spawn is not None:
            self.on_spawn(cmd)
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
    ns = types.SimpleNamespace(home=home, models=home / '.nunba' / 'models', bus=bus,
                               clock=clock, port=port, tmp=tmp_path, mp=monkeypatch,
                               gpu=dict(ROOMY_GPU), forced=[])
    _gpu_reads_from(ns)
    return ns


def _gpu_reads_from(box):
    """vram_manager reads the GPU as ``box.gpu`` says (an exception: the
    probe raises).  ``box.forced`` records, for each refresh_gpu_info, whether
    it was forced past the memo; detect_gpu is the memo's own read."""
    from integrations.service_tools.vram_manager import vram_manager

    def reading():
        if isinstance(box.gpu, Exception):
            raise box.gpu
        return dict(box.gpu)

    def refresh_gpu_info(force=False):
        box.forced.append(force)
        return reading()
    box.mp.setattr(vram_manager, 'refresh_gpu_info', refresh_gpu_info)
    box.mp.setattr(vram_manager, 'detect_gpu', reading)


def _measured_as(box, nbytes):
    """The 0.8B weight file measures ``nbytes`` on disk (os.path.getsize), as
    the real one does; every other file measures what it is."""
    real = os.path.getsize
    box.mp.setattr(os.path, 'getsize',
                   lambda p: nbytes if os.path.basename(str(p)) == MODEL else real(p))


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


@contextlib.contextmanager
def _the_box(box):
    """Every boundary of a start replaced by ``box`` for the block; the
    events asked for are in ``box.asked``.

    The clock is the start's: it runs on this thread, and only this
    thread's sleeps move the clock.  Another thread's sleep is its own,
    really slept -- finding F1 (2026-10-14): a VisionService that
    hart_intelligence_entry's import-time vision_init thread started keeps a
    description loop sleeping every 4 s."""
    box.asked = []
    me = threading.get_ident()

    def deliver(topic, data=None, *a, **kw):
        box.asked.append(topic)
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
        yield


def _close_log(backend):
    log = getattr(backend, '_log_fh', None)
    if log:
        log.close()


def _ensure(box, backend=None):
    """ensure_running on the real backend; returns (ok, backend)."""
    backend = backend or lvb.Qwen08BBackend(port=PORT)
    with _the_box(box):
        ok = backend.ensure_running()
    _close_log(backend)
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
    """A lock under START_LOCK_STALE_S (120 s) old is a start in progress;
    an older one was left by a start that died.  Nunba's
    start_caption_server ages the lock by this same number (it reads
    Qwen08BBackend.START_LOCK_STALE_S; Nunba's
    tests/test_caption_server_spawn_lock.py pins its side at 118 / 122 s),
    so writer and reader cannot disagree about whether a start is in
    progress."""
    _files_in(box.models)
    _lock_aged(box, age_s)
    box.port.start(12.0 if held else None)      # Nunba's start, when there is one
    ok, _ = _ensure(box)
    assert len(box.port.spawned) == (0 if held else 1)
    assert ok is True


def test_the_age_rule_is_the_one_number(box):
    """HARTOS ages the lock by START_LOCK_STALE_S itself, not a copy of
    120: with the number at 300 s, a 200 s lock is a start in progress."""
    _files_in(box.models)
    _lock_aged(box, 200)
    box.mp.setattr(lvb.Qwen08BBackend, 'START_LOCK_STALE_S', 300)
    box.port.start(12.0)
    ok, _ = _ensure(box)
    assert box.port.spawned == [] and ok is True


def test_the_lock_has_one_name_for_both_sides(box):
    """start_lock_path names Nunba's per-port start lock for Nunba (which
    writes it in its config folder) and for HARTOS (which reads it in
    Nunba's default config folder, ~/.nunba)."""
    assert lvb.Qwen08BBackend.start_lock_path(PORT) == str(_lock(box))
    other = box.tmp / 'cfg'
    assert lvb.Qwen08BBackend.start_lock_path(PORT, other) == \
        os.path.join(str(other), f'.caption_server_starting.{PORT}.lock')


def test_hartos_reads_the_lock_where_start_lock_path_says(box):
    """No private spelling of the lock's name: a fresh lock at the place
    start_lock_path names is a start in progress, and HARTOS spawns
    nothing beside it."""
    elsewhere = box.tmp / 'moved' / f'start.{PORT}.lock'
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text('31337')
    box.mp.setattr(lvb.Qwen08BBackend, 'start_lock_path',
                   staticmethod(lambda port, config_dir=None: str(elsewhere)))
    _files_in(box.models)
    box.port.start(12.0)
    ok, _ = _ensure(box)
    assert box.port.spawned == [], 'HARTOS looked for the lock under its own name'
    assert ok is True


def test_a_server_already_up_spawns_nothing_and_asks_nothing(box):
    _files_in(box.models)
    box.port.start(0.0)
    ok, _ = _ensure(box)
    assert ok is True
    assert box.asked == [] and box.port.spawned == [] and box.clock.sleeps == 0


# ── a port something already listens on: no second server ──────────────

def _socket_table_readable() -> bool:
    """Whether this interpreter can read the OS socket table at all: psutil
    installed, and allowed to list it (macOS needs root)."""
    try:
        import psutil
        psutil.net_connections(kind='inet')
        return True
    except Exception:
        return False


def test_a_port_already_listened_on_gets_no_second_server(box):
    """Task #936, measured 2026-10-21: five llama-servers LISTENING on
    127.0.0.1:8081 at once -- the desktop app's own, and four 0.8B caption
    servers this launch spawned from test processes.  The live server did not
    answer their /health (the tests' network seal refused it), so
    _is_serving() said no, nobody was starting it, and on Windows a second
    llama-server binds and listens beside the first (both set SO_REUSEADDR).
    Here a real socket listens the way llama-server's does and answers no
    HTTP: the launch spawns nothing and waits for whatever holds the port,
    the 30 s it waits for a server of its own."""
    if not _socket_table_readable():
        pytest.skip('this interpreter cannot read the socket table '
                    '(no psutil, or macOS without root)')
    _files_in(box.models)
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(('127.0.0.1', 0))
    held.listen(1)

    def no_http(url, timeout=None, **kw):
        raise requests.ConnectionError('refused')
    box.port.health = no_http
    try:
        ok, _ = _ensure(box, lvb.Qwen08BBackend(port=held.getsockname()[1]))
    finally:
        held.close()
    assert box.port.spawned == [], (
        'a second llama-server was spawned on a port something already listens on')
    assert ok is False and box.clock.t <= BOUND_S


def test_a_socket_table_that_cannot_be_read_keeps_the_launch(box):
    """No psutil, or psutil refused the table (AccessDenied on macOS without
    root): whether the port is held cannot be told, and the launch goes on as
    it did before it asked -- spawning nothing would end captions, page reads
    and a reel's keyframe ratings on every such node."""
    import core.resource_governor as rg

    def unreadable(ports):
        raise RuntimeError('the socket table cannot be read')
    box.mp.setattr(rg, '_listen_rows', unreadable, raising=False)
    _files_in(box.models)
    ok, _ = _ensure(box)
    assert len(box.port.spawned) == 1, 'an unreadable socket table stopped the launch'
    assert ok is True


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


# ── the standalone launch's geometry: as the other start of this server ──

@pytest.mark.parametrize('gpu', [
    dict(ROOMY_GPU),                            # 7.5 GiB free: the richest tier, capped
    dict(ROOMY_GPU, free_gb=2.6),               # 2.08 GiB left after the weights
    dict(ROOMY_GPU, free_gb=2.4),               # 1.88 left: the smaller tier
    dict(NO_GPU),                               # no GPU: the weights do not fit any VRAM
], ids=['roomy', 'tier_8192', 'tier_4096', 'no_gpu'])
def test_the_standalone_launch_sizes_its_context_as_nunbas_caption_start_does(box, gpu):
    """A13 proof D2 (2026-10-14, HARTOS-only node): the standalone launch ran
    the 0.8B at --ctx-size 512, and all 3 of a reel's keyframe ratings came
    back HTTP 400 'request (550 tokens) exceeds the available context size
    (512 tokens)': the start said True and every frame fell back.  Nunba's
    start_caption_server sizes the same server by _derive_ctx_size --
    core.llama_geometry.derive_ctx_size over a FORCED VRAM sample and the
    weights measured from the file -- and so does this launch now: one
    server, one port, one size whoever starts it."""
    _files_in(box.models)
    _measured_as(box, REAL_WEIGHTS)
    box.gpu = gpu
    ok, _ = _ensure(box)
    assert ok is True and len(box.port.spawned) == 1
    ctx = _arg(box.port.spawned[0], '--ctx-size')
    assert ctx == str(derive_ctx_size(gpu['free_gb'], REAL_WEIGHTS / GIB))
    assert box.forced[:1] == [True], 'the size was read off a memoized VRAM sample'
    assert int(ctx) >= RATING_TOKENS, 'a reel keyframe rating does not fit the window'


def test_a_gpu_probe_that_fails_launches_at_the_unmeasured_default(box):
    """The box cannot be measured (the probe raises): the context is the one
    Nunba's _derive_ctx_size falls back to, ctx_for_role('main'), and the
    layers are llama-server's own placement (no --n-gpu-layers), not 0.
    a44883fe2 pinned 0 here: an nvidia-smi that timed out once under load
    put the 0.8B on the CPU for the life of the server, where the parent's 99
    had run it on the card (review of a44883fe2).  The launch still happens."""
    _files_in(box.models)
    box.gpu = RuntimeError('nvidia-smi did not answer')
    ok, _ = _ensure(box)
    assert ok is True and len(box.port.spawned) == 1
    cmd = box.port.spawned[0]
    assert _arg(cmd, '--ctx-size') == str(ctx_for_role('main'))
    assert '--n-gpu-layers' not in cmd and '-ngl' not in cmd, cmd


@pytest.mark.parametrize('gpu', [METAL_GPU, ROCM_GPU, OLD_DRIVER_GPU, NO_GPU],
                         ids=['apple_metal', 'amd_rocm', 'nvidia_driver_too_old', 'no_gpu'])
def test_a_gpu_that_is_not_cuda_leaves_the_layers_to_llama_server(box, gpu):
    """Review of a44883fe2, measured on the real 0.8B weights: a Metal reading
    launched the 0.8B with --n-gpu-layers 0 although
    vram_manager.suggest_offload_mode calls that box 'gpu', and so did a
    ROCm reading.  get_optimal_params decides "GPU or not" from
    cuda_available alone, so a Mac's captions and a reel's keyframe ratings
    moved from the GPU (the parent's 99) to the CPU, 9-10 s a frame (A13
    proof) against a 90 s budget.

    Nunba's start of this server passes -ngl only on CUDA
    (llama_config._do_start_caption_server) and leaves every other box to
    llama-server, whose --n-gpu-layers defaults to 'auto', fitted to the
    device's memory (--fit on; llama-server --help).  So does this launch:
    no --n-gpu-layers, and HARTOS's CUDA placement is not asked.  The context
    is still the one derived from the reading, as Nunba's."""
    from integrations.service_tools import llamacpp_manager as lcm
    _files_in(box.models)
    _measured_as(box, REAL_WEIGHTS)
    box.gpu = dict(gpu)
    asked = []
    real = lcm.LlamaCppManager.get_optimal_params

    def optimal(self, model_path):
        asked.append(model_path)
        return real(self, model_path)
    box.mp.setattr(lcm.LlamaCppManager, 'get_optimal_params', optimal)
    ok, _ = _ensure(box)
    assert ok is True and len(box.port.spawned) == 1
    cmd = box.port.spawned[0]
    assert '--n-gpu-layers' not in cmd and '-ngl' not in cmd, cmd
    assert asked == [], 'the CUDA-only placement was asked to place a box it cannot read'
    assert _arg(cmd, '--ctx-size') == str(derive_ctx_size(gpu['free_gb'], REAL_WEIGHTS / GIB))


@pytest.mark.parametrize('gpu, layers', [
    (dict(ROOMY_GPU), '-1'),
    (dict(ROOMY_GPU, free_gb=0.2),
     str(mc.gguf_partial_offload_layers(0.2, REAL_WEIGHTS / GIB, None))),
], ids=['fits_all_layers', 'low_vram_partial'])
def test_the_standalone_launch_places_the_0_8b_by_the_one_placement_rule(box, gpu, layers):
    """It passed --n-gpu-layers 99 whatever the card held.  On a CUDA card
    the layers are llamacpp_manager.get_optimal_params' now
    (model_catalog.gguf_fits_gpu, then a partial offload sized from the
    file): every layer (-1) on a card with room, part of them on a card short
    of room (0.2 GiB free for a 0.52 GiB file)."""
    _files_in(box.models)
    _measured_as(box, REAL_WEIGHTS)
    box.gpu = gpu
    ok, _ = _ensure(box)
    assert ok is True and len(box.port.spawned) == 1
    got = _arg(box.port.spawned[0], '--n-gpu-layers')
    assert got == layers
    if gpu['free_gb'] == 0.2:
        assert 0 < int(got) < mc.LAYER_COUNT_FALLBACK, 'not a partial offload'


def test_the_layers_are_the_placement_helpers_answer_not_a_copy(box):
    """Delegation, proven: whatever get_optimal_params answers for the 0.8B's
    file is what the launch passes, so it cannot drift from the placement of
    every other llama-server HARTOS starts."""
    from integrations.service_tools import llamacpp_manager as lcm
    model, _ = _files_in(box.models)
    asked = []

    def optimal(self, model_path):
        asked.append(model_path)
        return {'n_gpu_layers': 7, 'ctx_size': 1}
    box.mp.setattr(lcm.LlamaCppManager, 'get_optimal_params', optimal)
    ok, _ = _ensure(box)
    assert asked == [str(model)]
    assert _arg(box.port.spawned[0], '--n-gpu-layers') == '7'
    assert _arg(box.port.spawned[0], '--ctx-size') != '1', \
        'the context came from the placement helper, not from the caption start rule'


# ── one backend per process: every caller shares one start ─────────────

def _callers_on_the_test_port(box):
    """get_vision_backend hands out the 0.8B on this test's port, with no
    backend forced by the environment."""
    box.mp.setenv('HEVOLVE_VLM_CAPTION_PORT', str(PORT))
    box.mp.delenv('HEVOLVE_VISION_BACKEND', raising=False)


def test_the_reel_the_camera_and_the_book_reader_share_one_start(box):
    """Reviewer F1, a HARTOS-only node: the reel
    (get_vision_backend(REEL_VLM_DEFAULT)), VisionService's describe and the
    book reader (both on the auto pick) each built their own Qwen08BBackend,
    each with its own record of a start under way.  A 0.8B that loads slower
    than the 5 s + 30 s a start waits left the reel's start False with its
    server still loading; VisionService's next frame and the book reader's
    next page each spawned their own -- three servers on one port.  One
    backend per process now (owner ruling 2026-10-15): the later callers are
    inside the first start's cooldown and spawn nothing."""
    _files_in(box.models)
    _callers_on_the_test_port(box)
    box.port.load_s = 60.0                  # longer than a start waits
    with _the_box(box):
        reel = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT)
        assert reel.ensure_running() is False         # still loading
        assert lvb.get_vision_backend().describe(b'a camera frame') is None
        page_reader = lvb.get_document_readers()[0]
        assert page_reader(b'a book page', 'read every line') is None
    _close_log(reel)
    assert len(box.port.spawned) == 1, 'a second 0.8B server was spawned on the port'
    assert box.asked == ['vlm_caption.requested']


def test_a_caller_that_comes_while_a_start_is_under_way_waits_for_it(box):
    """VisionService's description loop asks for the 0.8B while the reel's
    start is waiting for the server it has just spawned.  It spawns nothing
    and is not turned away: it waits for that start, is told it is waiting
    (on_wait -- a reel in its place says the vision model is being made
    ready), and is ready when that start's server is."""
    _files_in(box.models)
    _callers_on_the_test_port(box)
    at_the_door = threading.Event()
    other = {}

    def vision_service_meanwhile(cmd):
        box.port.on_spawn = None            # the one spawn starts the one caller

        def description_loop():
            waited = []

            def on_wait():
                waited.append(1)
                at_the_door.set()
            other['ok'] = lvb.get_vision_backend().ensure_running(on_wait=on_wait)
            other['waited'] = waited
        other['thread'] = threading.Thread(target=description_loop,
                                           name='vision-desc', daemon=True)
        other['thread'].start()
        at_the_door.wait(10)                # the spawn returns once it is waiting
    box.port.on_spawn = vision_service_meanwhile
    with _the_box(box):
        reel = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT)
        ok = reel.ensure_running()
        other['thread'].join(30)
    _close_log(reel)
    assert not other['thread'].is_alive()
    assert ok is True and other.get('ok') is True, other
    assert other['waited'] == [1], 'the waiting caller was not told it waited'
    assert len(box.port.spawned) == 1, 'a second 0.8B server was spawned on the port'
    assert box.asked == ['vlm_caption.requested']


def test_a_caller_that_comes_during_a_cooldown_probe_is_not_told_it_waits(box):
    """Review of 1db4503c3, probed (told == [1]): an earlier start failed and
    its cooldown holds.  VisionService's description loop asks for the 0.8B
    and holds the start only to ask /health once, as the cooldown says; a
    reel that comes in that moment was told it waited -- her chat said the
    vision model was being made ready -- though nothing was being started.
    It still waits for that caller and takes the same answer, and it is not
    told."""
    _files_in(box.models)
    _callers_on_the_test_port(box)
    _nunba_in_this_process(box, start_s=None)       # its server never comes up
    with _the_box(box):
        camera = lvb.get_vision_backend()
        assert camera.ensure_running() is False     # a failed start: the cooldown
    main = threading.get_ident()
    on_main, reel_probed, told, reel = [], threading.Event(), [], {}

    def reel_asks():
        reel['ok'] = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT).ensure_running(
            on_wait=lambda: told.append(1))

    def health(url, timeout=None, **kw):
        if threading.get_ident() != main:
            reel_probed.set()
        else:
            on_main.append(url)
            if len(on_main) == 2:       # the cooldown's probe: the camera holds the start
                reel['thread'] = threading.Thread(target=reel_asks, name='reel', daemon=True)
                reel['thread'].start()
                reel_probed.wait(10)
                REAL_SLEEP(0.5)         # the reel goes on to the start and finds it held
        return _Port.health(box.port, url, timeout, **kw)
    box.port.health = health
    with _the_box(box):
        assert camera.ensure_running() is False
        reel['thread'].join(10)
    assert not reel['thread'].is_alive()
    assert reel.get('ok') is False
    assert told == [], 'the reel was told the vision model was being made ready; nothing was'
    assert box.asked == [] and box.port.spawned == []


def test_a_caller_that_comes_before_the_start_begins_is_told_when_it_does(box):
    """The start is held but not begun: its holder is first stopping the
    server it spawned last time, still not serving after a whole cooldown.
    A reel that comes in that moment waits for that start, is told once that
    the vision model is being made ready, and takes the start's answer.  A
    reel told only when it finds a start already begun would wait here with
    nothing said."""
    _files_in(box.models)
    _callers_on_the_test_port(box)
    _nunba_in_this_process(box, start_s=1.0)
    main = threading.get_ident()
    reel_probed, told, reel = threading.Event(), [], {}

    def reel_asks():
        reel['ok'] = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT).ensure_running(
            on_wait=lambda: told.append(1))

    def health(url, timeout=None, **kw):
        if threading.get_ident() != main:
            reel_probed.set()
        return _Port.health(box.port, url, timeout, **kw)
    box.port.health = health

    def stopping(timeout=None):
        """The wedged server's wait inside stop(): the start is held, not begun."""
        reel['thread'] = threading.Thread(target=reel_asks, name='reel', daemon=True)
        reel['thread'].start()
        reel_probed.wait(10)
        REAL_SLEEP(0.5)             # the reel finds the start held and waits
        return 0
    camera = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT)
    camera._launch_attempted = True
    camera._launch_attempted_at = time.time() - camera.LAUNCH_RETRY_S - 1
    camera._server_proc = MagicMock(pid=4141, wait=MagicMock(side_effect=stopping))
    with _the_box(box):
        assert camera.ensure_running() is True
        reel['thread'].join(10)
    assert not reel['thread'].is_alive()
    assert reel.get('ok') is True
    assert told == [1], 'the reel waited for the start without being told'
    assert box.port.spawned == []


def test_a_server_the_reel_started_is_not_idled_out_from_under_it(box):
    """VisionService's description loop unloads a 0.8B that HARTOS started
    once it has gone IDLE_TIMEOUT_S unused (check_idle, on the backend it
    holds -- now also the reel's).  A start and a keyframe rating are uses:
    a camera frame described long ago does not unload the server the reel
    has just started, nor one it is rating frames on.  Unused for
    IDLE_TIMEOUT_S, it is still unloaded."""
    _files_in(box.models)
    _callers_on_the_test_port(box)
    with _the_box(box):
        camera = lvb.get_vision_backend()
        camera._last_describe_time = time.time() - 10 * camera.IDLE_TIMEOUT_S
        reel = lvb.get_vision_backend(moments.REEL_VLM_DEFAULT)
        assert reel.ensure_running() is True
        proc = reel._server_proc
        camera.check_idle()
        assert not proc.terminate.called, 'the server was unloaded as it came up'
        reel._last_describe_time -= camera.IDLE_TIMEOUT_S - 1     # a while later
        reel.rate_frame(b'a keyframe', 'rate it', timeout_s=1.0)
        reel._last_describe_time -= camera.IDLE_TIMEOUT_S - 1
        camera.check_idle()
        assert not proc.terminate.called, 'the server was unloaded while frames were rated'
        reel._last_describe_time -= camera.IDLE_TIMEOUT_S + 1     # then nobody uses it
        camera.check_idle()
    assert proc.terminate.called, 'an unused server HARTOS started was never unloaded'


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
    model = _weights(box.models / MODEL, size=900)
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
