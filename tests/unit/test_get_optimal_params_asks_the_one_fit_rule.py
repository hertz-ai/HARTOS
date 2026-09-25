"""llamacpp_manager.get_optimal_params places a model by the ONE fit rule.

THE FOURTH FIT AUTHORITY (F1, confirmed by two readers).  Three rules had
been collapsed into ``model_catalog.gguf_fits_gpu`` (4b16e8edf, "one GGUF
fit rule, asked at two knowledge levels"), and this spawn kept a fourth:

    if model_size_gb > 0 and free_vram >= model_size_gb * 1.1:  -> -1
    elif model_size_gb > 0:
        ratio = free_vram / model_size_gb
        estimated_layers = max(1, int(ratio * 40))  # assume ~40 layers

It survived three independent collapses -- the ctx-tier migration (which
rewrote the line BELOW it), the moe_offload_args rollout (which reached the
other three spawns), and the fit-rule unification -- because each enumerated
by SYMBOL, and a hand-rolled comparison references no symbol a grep can find.

Two consequences, both measured on the owner's box (4.7 GB free VRAM,
21.4 GB free RAM, Tiel-Coder-35B-A3B at 21.19 GiB):

  1. A mixture of experts was judged by its WHOLE file (all experts), so
     4.7 >= 21.19 * 1.1 was false and the model that had served from
     2.87 GiB of VRAM under --cpu-moe got a partial offload of 8 layers
     with the experts on the card -- and no --cpu-moe at all, because this
     spawn was the one of four that never asked moe_offload_args.
  2. The layer count was guessed as 40 for every model, while the file
     states its own block count.

These tests run the REAL get_optimal_params and mock only its boundaries:
the GPU probe, psutil, the GGUF reader, the catalog (never the owner's --
a fixture leaked into it once, #109) and, for one test, os.path.getsize.
No GiB-scale files are written: an earlier fixture wrote 29 GB to C: and
took the box to 1.8 GB free.

    python -m pytest tests/unit/test_get_optimal_params_asks_the_one_fit_rule.py -q
"""
import os
import sys
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools import llamacpp_manager as lcm  # noqa: E402
from integrations.service_tools import model_catalog as mc  # noqa: E402

GIB = 1024 ** 3

# The owner's card and the model that exposed the defect.
OWNER_FREE_VRAM_GB, OWNER_FREE_RAM_GB = 4.7, 21.4
TIEL_GIB, TIEL_EXPERTS_GIB, TIEL_BLOCKS = 21.19, 18.64, 40
TIEL_RESIDENCY = {'vram_gb': 2.87, 'ram_gb': 18.64, 'weight_file': 'model.gguf'}


def moe_facts(weight_gib=TIEL_GIB, experts_gib=TIEL_EXPERTS_GIB,
              blocks=TIEL_BLOCKS):
    """What read_gguf_facts returns for a MoE, injected rather than written."""
    return {'architecture': 'qwen35moe', 'weight_bytes': int(weight_gib * GIB),
            'moe': True, 'experts_total': 256, 'experts_used': 8,
            'expert_fraction': 8 / 256, 'mtp': True, 'mtp_layers': 1,
            'expert_bytes': int(experts_gib * GIB),
            'non_expert_bytes': int((weight_gib - experts_gib) * GIB),
            'block_count': blocks}


def dense_facts(weight_gib, blocks=None):
    f = {'architecture': 'qwen35', 'weight_bytes': int(weight_gib * GIB),
         'moe': False, 'mtp': False}
    if blocks is not None:
        f['block_count'] = blocks
    return f


class _Catalog:
    """Stands in for the OWNER'S catalog, which the real get_catalog() reads
    and can write.  Answers the two questions the footprint asks."""

    def __init__(self, residency=None):
        self._residency = residency

    def get_by_weight_file(self, path):
        return types.SimpleNamespace(id='m') if self._residency else None

    def residency(self, model_id):
        return dict(self._residency) if self._residency else None


class _Raising:
    def get_by_weight_file(self, path):
        raise RuntimeError('catalog on fire')


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A manager on a box whose readings the test sets.

    ``box`` is a dict the test mutates before calling ``params()``:
    gpu (the vram_manager reading), ram_gb (psutil), facts (the GGUF
    reader's answer for this file), catalog, and size_bytes (what
    os.path.getsize reports when the reader could not parse the file).
    """
    path = tmp_path / 'model.gguf'
    path.write_bytes(b'GGUF')          # exists, so the spawn's isfile passes
    state = {
        'gpu': {'name': 'NVIDIA GeForce RTX 3070', 'total_gb': 8.0,
                'free_gb': 8.0, 'cuda_available': True},
        'ram_gb': 32.0,
        'facts': {},
        'catalog': _Catalog(),
        'size_bytes': None,
        'path': str(path),
    }
    mgr = lcm.LlamaCppManager()
    state['mgr'] = mgr

    monkeypatch.setattr(mgr, '_get_gpu_info', lambda: dict(state['gpu']))
    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(
            available=state['ram_gb'] * GIB))
    monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)
    monkeypatch.setattr(
        mc, 'read_gguf_facts',
        lambda p: dict(state['facts']) if str(p) == state['path'] else {})
    monkeypatch.setattr(mc, 'get_catalog', lambda: state['catalog'])
    real_getsize = os.path.getsize

    def _getsize(p):
        if str(p) == state['path'] and state['size_bytes'] is not None:
            return state['size_bytes']
        return real_getsize(p)
    monkeypatch.setattr(os.path, 'getsize', _getsize)

    def params():
        return mgr.get_optimal_params(state['path'])
    state['params'] = params
    return state


def _cpu_moe(params):
    return '--cpu-moe' in (params.get('extra_args') or [])


class TestAMoeIsPlacedLikeTheOtherThreeSpawns:
    def test_a_moe_that_fits_under_cpu_moe_gets_full_offload_and_the_flag(
            self, box):
        """The owner's box.  MEASURED knowledge level: a residency record
        says 2.87 GB of VRAM and 18.64 GB of RAM, both of which the box
        has.  The old rule answered 4.7 >= 23.3 -> partial, 8 layers, no
        flag."""
        box['facts'] = moe_facts()
        box['gpu']['free_gb'] = OWNER_FREE_VRAM_GB
        box['ram_gb'] = OWNER_FREE_RAM_GB
        box['catalog'] = _Catalog(residency=TIEL_RESIDENCY)
        p = box['params']()
        assert p['n_gpu_layers'] == -1
        assert _cpu_moe(p), p.get('extra_args')

    def test_a_moe_with_no_residency_is_judged_on_the_combined_budget(
            self, box):
        """ESTIMATED level: 21.19 * 1.35 = 28.6 GB, against 8 + 32 = 40 GB
        of VRAM + RAM.  The combined arm of gguf_fits_gpu admits it, and
        the experts go to RAM."""
        box['facts'] = moe_facts()
        p = box['params']()
        assert p['n_gpu_layers'] == -1
        assert _cpu_moe(p)

    def test_a_moe_that_fits_whole_keeps_its_experts_on_the_gpu(self, box):
        """64 GB of VRAM holds the whole 28.6: moe_offload_args says [] and
        the flag would only give away performance."""
        box['facts'] = moe_facts()
        box['gpu']['free_gb'] = 64.0
        p = box['params']()
        assert p['n_gpu_layers'] == -1
        assert not _cpu_moe(p)

    def test_a_moe_that_fits_nowhere_is_placed_like_a_dense_model(self, box):
        """2 GB of VRAM and 4 GB of RAM cannot hold 28.6 anywhere.  The
        MoE arm already accounted for the RAM split, so its "no" means
        partial offload, sized from the file, and no --cpu-moe."""
        box['facts'] = moe_facts()
        box['gpu']['free_gb'] = 2.0
        box['ram_gb'] = 4.0
        p = box['params']()
        assert p['n_gpu_layers'] == max(1, int(2.0 / TIEL_GIB * TIEL_BLOCKS))
        assert p['n_gpu_layers'] != -1
        assert not _cpu_moe(p)

    def test_the_flag_comes_from_moe_offload_args_not_a_copy(self, box,
                                                             monkeypatch):
        """Delegation, proven: whatever the one helper answers is what the
        spawn passes on, so it cannot drift from the other three."""
        box['facts'] = moe_facts()
        seen = []

        def _spy(path, free_vram_gb):
            seen.append((path, free_vram_gb))
            return ['--cpu-moe', '--sentinel-from-the-helper']
        monkeypatch.setattr(mc, 'moe_offload_args', _spy)
        p = box['params']()
        assert seen == [(box['path'], 8.0)]
        assert '--sentinel-from-the-helper' in p['extra_args']


class TestADenseModelIsUntouchedExceptForTheRule:
    def test_a_dense_model_that_fits_gets_full_offload_and_no_flag(self, box):
        box['facts'] = dense_facts(4.0, blocks=36)
        p = box['params']()                    # 4 * 1.35 = 5.4 <= 8
        assert p['n_gpu_layers'] == -1
        assert not _cpu_moe(p)

    def test_a_dense_model_never_asks_for_the_flag(self, box, monkeypatch):
        box['facts'] = dense_facts(4.0)
        monkeypatch.setattr(mc, 'moe_offload_args',
                            lambda *a: pytest.fail('asked for a dense model'))
        assert box['params']()['n_gpu_layers'] == -1

    def test_a_dense_model_that_does_not_fit_keeps_partial_offload_sized_from_the_file(
            self, box):
        """20 GiB against 8 GB: ratio 0.4 of the file's 64 blocks is 25
        layers.  The old guess would have said 16 (0.4 * 40)."""
        box['facts'] = dense_facts(20.0, blocks=64)
        p = box['params']()
        assert p['n_gpu_layers'] == 25
        assert not _cpu_moe(p)

    def test_the_threshold_is_the_one_helper_not_one_point_one(self, box):
        """6 GiB on 7 GB free.  The retired rule admitted it (7 >= 6.6); the
        one rule needs 6 * 1.35 = 8.1 and refuses.  Two rules, two answers
        for the same file on the same card -- which is the finding."""
        box['facts'] = dense_facts(6.0, blocks=32)
        box['gpu']['free_gb'] = 7.0
        p = box['params']()
        assert p['n_gpu_layers'] != -1
        assert p['n_gpu_layers'] == max(1, int(7.0 / 6.0 * 32))


class TestWhatTheReaderCouldNotTell:
    def test_an_unparseable_file_falls_back_to_the_old_forty(self, box):
        """read_gguf_facts -> {}: not a MoE as far as anyone knows, sized by
        stat, and the layer guess is the fallback 40 -- exactly the old
        arithmetic, now reached only when the file gave no better number."""
        box['facts'] = {}
        box['size_bytes'] = 20 * GIB
        p = box['params']()
        assert p['n_gpu_layers'] == 16
        assert not _cpu_moe(p)

    def test_the_fallback_is_forty_and_named_once(self):
        """Named once, in model_catalog beside the fit rule -- not in the
        spawner, where a second copy could drift."""
        from integrations.service_tools import model_catalog as mc
        assert mc.LAYER_COUNT_FALLBACK == 40
        assert not hasattr(lcm, '_LAYER_COUNT_FALLBACK')

    def test_a_file_that_cannot_be_sized_tries_full_offload_as_before(
            self, box, monkeypatch):
        box['facts'] = {}
        monkeypatch.setattr(os.path, 'getsize',
                            lambda p: (_ for _ in ()).throw(OSError('gone')))
        p = box['params']()
        assert p['n_gpu_layers'] == -1

    def test_a_catalog_failure_does_not_stop_the_spawn(self, box):
        """The catalog is one knowledge source, not the spawn.  Without it
        the file is still sized and the rule still answers."""
        box['facts'] = dense_facts(4.0)
        box['catalog'] = _Raising()
        assert box['params']()['n_gpu_layers'] == -1


class TestNoGpu:
    def test_cpu_only_as_before(self, box):
        from core.llama_geometry import ctx_for_role
        box['facts'] = dense_facts(4.0)
        box['gpu'] = {'name': None, 'total_gb': 0.0, 'free_gb': 0.0,
                      'cuda_available': False}
        p = box['params']()
        assert p['n_gpu_layers'] == 0
        assert p['ctx_size'] == ctx_for_role('main', on_gpu=False)
        assert not _cpu_moe(p)


class TestTheSwapAndTheSpawnReadOneFootprint:
    def test_admission_facts_is_a_projection_of_the_same_reading(
            self, box, monkeypatch):
        """DRY, proven at runtime: both consumers go through _gguf_footprint,
        and the swap receives exactly the keys swap_main_llm accepts."""
        box['facts'] = moe_facts()
        calls = []
        real = box['mgr']._gguf_footprint

        def _spy(path):
            calls.append(path)
            return real(path)
        monkeypatch.setattr(box['mgr'], '_gguf_footprint', _spy)
        admission = box['mgr']._admission_facts(box['path'])
        box['params']()
        assert calls == [box['path'], box['path']]
        assert set(admission) == set(lcm.LlamaCppManager._ADMISSION_KEYS)
        # And swap_main_llm really does accept them, by name.
        import inspect
        from integrations.service_tools.llm_swap import swap_main_llm
        accepted = set(inspect.signature(swap_main_llm).parameters)
        assert set(admission) <= accepted

    def test_the_swap_sees_the_same_needs_the_spawn_judges(self, box):
        box['facts'] = moe_facts()
        box['catalog'] = _Catalog(residency=TIEL_RESIDENCY)
        a = box['mgr']._admission_facts(box['path'])
        assert (a['vram_need_gb'], a['ram_need_gb'], a['moe']) == (
            2.87, 18.64, True)
        assert a['whole_need_gb'] is None      # measured beats estimated

    def test_unknown_ram_refuses_the_swap_but_does_not_shrink_the_window(
            self, box, monkeypatch):
        """No psutil: the swap counts RAM as none (a MoE's experts must be
        RESIDENT), while the spawn leaves the derived window alone rather
        than clamping it against a number it never read."""
        from core.llama_geometry import derive_ctx_size
        box['facts'] = dense_facts(4.0)
        monkeypatch.setitem(sys.modules, 'psutil', None)
        assert box['mgr']._admission_facts(box['path'])['free_ram_gb'] == 0.0
        p = box['params']()
        assert p['ctx_size'] == derive_ctx_size(8.0, 4.0)

    def test_known_low_ram_still_clamps_the_window(self, box):
        from core.llama_geometry import RAM_CLAMPS
        box['facts'] = dense_facts(4.0)
        box['ram_gb'] = 1.5
        assert box['params']()['ctx_size'] == RAM_CLAMPS[0][1]


class TestTheFlagReachesTheCommandLine:
    def test_extra_args_flow_into_the_spawned_command(self, box, monkeypatch):
        """get_optimal_params is only useful if _spawn_locked hands its
        extra_args to llama-server.  Popen is the boundary here."""
        box['facts'] = moe_facts()
        mgr = box['mgr']
        monkeypatch.setattr(mgr, 'get_server_binary',
                            lambda: box['path'])   # any existing file
        seen = {}

        class _Proc:
            pid = 4242

        def _popen(cmd, **kw):
            seen['cmd'] = cmd
            return _Proc()
        monkeypatch.setattr(lcm.subprocess, 'Popen', _popen)
        import core.llama_geometry as geo
        monkeypatch.setattr(geo, 'publish_geometry', lambda *a: None)
        spawned = mgr._spawn_locked(box['path'], 8099)
        assert spawned is not None
        cmd = seen['cmd']
        assert '--cpu-moe' in cmd
        assert cmd[cmd.index('--n-gpu-layers') + 1] == '-1'
