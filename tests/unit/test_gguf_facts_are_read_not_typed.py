"""Architecture facts come FROM the file, so they cannot drift from it.

The sizes in MODEL_WEIGHT_BYTES are hand-typed estimates, and one of them is
1.58 GB wrong (Tiel-Coder's 22938 MiB against a real 22360478080 bytes). That
error inverts the size ordering of two real models, and because the priority
ladder is sorted by size, it changes which model a machine is offered. Typing
a fact that the artifact already states is how that happens.

Two facts were tracked NOWHERE and both change behaviour:

  moe / experts_used  -- generation throughput with weights resident is bounded
                         by memory bandwidth times ACTIVE parameters. A 35B MoE
                         using 8 of 256 experts is materially faster than a
                         dense 27B despite the larger file; speed_score says
                         the opposite.
  mtp                 -- only a model carrying a multi-token-prediction head
                         benefits from `--spec-type draft-mtp`.

These tests build REAL GGUF bytes rather than reading a model off disk: the
21 GB file this was developed against lives on an external drive, and a test
that needs a USB stick plugged in is a test that fails for the wrong reason.

    python -m pytest tests/unit/test_gguf_facts_are_read_not_typed.py -q
"""
import os
import struct
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools.model_catalog import (  # noqa: E402
    ModelCatalog, ModelEntry, ModelType, llama_gguf_compute_requirements,
    moe_offload_args, read_gguf_facts)


# ── minimal GGUF writer ───────────────────────────────────────────────
# Enough of the format to carry a metadata header, which is all the facts
# live in. Types: 4=uint32, 8=string.

def _kv_string(key, val):
    return (struct.pack('<Q', len(key)) + key.encode() +
            struct.pack('<I', 8) +
            struct.pack('<Q', len(val)) + val.encode())


def _kv_u32(key, val):
    return (struct.pack('<Q', len(key)) + key.encode() +
            struct.pack('<I', 4) + struct.pack('<I', val))


def _tensor_info(name, offset):
    """name, n_dims=1, dims=[1], type=0, offset — the layout the reader walks."""
    return (struct.pack('<Q', len(name)) + name.encode() +
            struct.pack('<I', 1) + struct.pack('<Q', 1) +
            struct.pack('<I', 0) + struct.pack('<Q', offset))


def write_gguf(path, arch=None, experts_total=None, experts_used=None,
               nextn=None, pad=0, tensors=None, data_bytes=0, align=32):
    """`tensors` is [(name, offset), ...]; sizes come from the GAPS between
    offsets, exactly as the reader derives them, with the last tensor
    running to `data_bytes`."""
    kvs = b''
    n = 0
    if arch is not None:
        kvs += _kv_string('general.architecture', arch); n += 1
    if experts_total is not None:
        kvs += _kv_u32(f'{arch}.expert_count', experts_total); n += 1
    if experts_used is not None:
        kvs += _kv_u32(f'{arch}.expert_used_count', experts_used); n += 1
    if nextn is not None:
        kvs += _kv_u32(f'{arch}.nextn_predict_layers', nextn); n += 1

    tensors = tensors or []
    infos = b''.join(_tensor_info(nm, off) for nm, off in tensors)
    header = (b'GGUF' + struct.pack('<IQQ', 3, len(tensors), n) + kvs + infos)
    with open(path, 'wb') as f:
        f.write(header)
        if tensors:
            start = (len(header) + align - 1) // align * align
            f.write(b'\0' * (start - len(header)))   # alignment padding
            f.write(b'\0' * data_bytes)
        else:
            f.write(b'\0' * pad)
    return path


@pytest.fixture
def moe_gguf(tmp_path):
    """A mixture-of-experts model that also carries an MTP head."""
    return write_gguf(str(tmp_path / 'moe.gguf'), arch='qwen35moe',
                      experts_total=256, experts_used=8, nextn=1, pad=1024)


@pytest.fixture
def dense_gguf(tmp_path):
    return write_gguf(str(tmp_path / 'dense.gguf'), arch='qwen35', pad=512)


@pytest.fixture
def big_moe(tmp_path, monkeypatch):
    """A 21 GiB MoE -- 1 GiB non-expert, 20 GiB experts -- WITHOUT writing
    21 GiB.

    Sizing and placement only become interesting at GiB scale, and an
    earlier draft of these tests wrote those bytes for real: 144 files and
    29 GB of them, which filled the disk mid-run. The parser is proven
    against real GGUF bytes above; everything downstream consumes a facts
    dict, so that is what gets injected here.

    Returns a path that exists (callers may stat it) but holds nothing."""
    p = tmp_path / 'big.gguf'
    p.write_bytes(b'GGUF')
    G = 1024 ** 3
    facts = {'architecture': 'qwen35moe', 'weight_bytes': 21 * G,
             'moe': True, 'experts_total': 256, 'experts_used': 8,
             'expert_fraction': 8 / 256, 'mtp': True, 'mtp_layers': 1,
             'expert_bytes': 20 * G, 'non_expert_bytes': 1 * G}
    import integrations.service_tools.model_catalog as mc
    monkeypatch.setattr(mc, 'read_gguf_facts',
                        lambda path: dict(facts) if str(path) == str(p) else {})
    return str(p)


class TestTheFactsThatWereTrackedNowhere:
    def test_moe_and_the_fraction_that_predicts_throughput(self, moe_gguf):
        f = read_gguf_facts(moe_gguf)
        assert f['moe'] is True
        assert f['experts_total'] == 256
        assert f['experts_used'] == 8
        assert f['expert_fraction'] == pytest.approx(8 / 256)

    def test_mtp_head_is_reported(self, moe_gguf):
        assert read_gguf_facts(moe_gguf)['mtp'] is True

    def test_a_dense_model_is_not_moe_and_has_no_mtp(self, dense_gguf):
        """The POSITIVE case's opposite. Without this the tests above would
        pass just as well against a parser that always answered True."""
        f = read_gguf_facts(dense_gguf)
        assert f['moe'] is False
        assert f['mtp'] is False
        assert 'experts_used' not in f

    def test_size_is_the_file_not_an_estimate(self, moe_gguf):
        assert read_gguf_facts(moe_gguf)['weight_bytes'] == \
            os.path.getsize(moe_gguf)

    def test_keys_are_found_by_suffix_not_a_guessed_prefix(self, tmp_path):
        """Metadata keys are namespaced by architecture, so a parser that
        assumed 'qwen35moe.' would go blind on any other model."""
        p = write_gguf(str(tmp_path / 'other.gguf'), arch='llama4moe',
                       experts_total=64, experts_used=2)
        f = read_gguf_facts(p)
        assert f['architecture'] == 'llama4moe'
        assert f['experts_total'] == 64 and f['experts_used'] == 2


class TestTheSplitThatDecidesPlacement:
    """llama.cpp's --cpu-moe keeps the '_exps' tensors in system RAM and
    leaves attention, embeddings and norms on the GPU. So a MoE's VRAM cost
    is the NON-expert bytes, not the file size, and that is the difference
    between "this machine cannot run a 35B" and "it can".

    Measured on the real Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL: 18.64 GiB of
    experts against 2.53 GiB of everything else, from a 21.19 GiB file.
    Sizing it at weights * 1.35 claims 28.6 GB of VRAM -- about 11x the
    truth."""

    @pytest.fixture
    def split_gguf(self, tmp_path):
        # gaps: attn 1024, gate_exps 4096, down_exps 4096, output 1024
        return write_gguf(
            str(tmp_path / 'split.gguf'), arch='qwen35moe',
            experts_total=256, experts_used=8,
            tensors=[('blk.0.attn_q.weight', 0),
                     ('blk.0.ffn_gate_exps.weight', 1024),
                     ('blk.0.ffn_down_exps.weight', 5120),
                     ('output.weight', 9216)],
            data_bytes=10240)

    def test_expert_and_non_expert_bytes_are_measured(self, split_gguf):
        f = read_gguf_facts(split_gguf)
        assert f['expert_bytes'] == 8192         # the two _exps tensors
        assert f['non_expert_bytes'] == 2048     # attn + output

    def test_the_two_halves_account_for_the_whole_data_section(self,
                                                               split_gguf):
        f = read_gguf_facts(split_gguf)
        assert f['expert_bytes'] + f['non_expert_bytes'] == 10240

    def test_sizes_come_from_offset_gaps_not_a_quant_type_table(self,
                                                                tmp_path):
        """Every tensor here declares ggml type 0 and a single dim of 1. If
        sizes were computed from the type they would all be tiny and equal;
        they are not, because the file's own layout states them."""
        p = write_gguf(str(tmp_path / 'g.gguf'), arch='qwen35moe',
                       experts_total=8, experts_used=2,
                       tensors=[('blk.0.ffn_up_exps.weight', 0),
                                ('output.weight', 7000)],
                       data_bytes=8000)
        f = read_gguf_facts(p)
        assert f['expert_bytes'] == 7000
        assert f['non_expert_bytes'] == 1000

    def test_a_dense_model_pays_none_of_this(self, dense_gguf):
        """No split is emitted for a dense model -- every byte has to be
        resident anyway, so the number would mean nothing. It also means
        the tensor table is not walked for the common case."""
        f = read_gguf_facts(dense_gguf)
        assert 'expert_bytes' not in f
        assert 'non_expert_bytes' not in f

    def test_a_moe_with_no_tensor_table_still_reports_its_kv_facts(self,
                                                                   moe_gguf):
        """The existing fixture writes tensor_count=0. The split is absent
        but moe/experts/mtp must survive -- a missing tensor table is not a
        reason to lose the metadata."""
        f = read_gguf_facts(moe_gguf)
        assert f['moe'] is True and f['experts_used'] == 8
        assert f['expert_bytes'] == 0


class TestUnreadableMeansUnknownNeverFalse:
    def test_absent_file(self, tmp_path):
        assert read_gguf_facts(str(tmp_path / 'nope.gguf')) == {}

    def test_not_a_gguf(self, tmp_path):
        p = tmp_path / 'x.json'
        p.write_text('{"not": "a model"}')
        assert read_gguf_facts(str(p)) == {}

    def test_truncated_header(self, tmp_path):
        p = tmp_path / 't.gguf'
        p.write_bytes(b'GGUF' + b'\x03\x00')     # dies mid-header
        assert read_gguf_facts(str(p)) == {}

    def test_empty_is_not_read_as_dense(self, tmp_path):
        """{} must mean "not known". A caller seeing moe=False when the file
        was simply unreadable would rank a MoE model as dense."""
        p = tmp_path / 'e.gguf'
        p.write_bytes(b'')
        assert read_gguf_facts(str(p)) == {}


class TestEnrichmentCannotMoveSelection:
    def _cat(self):
        c = ModelCatalog.__new__(ModelCatalog)
        c._entries, c._populators = {}, {}
        c._lock = __import__('threading').RLock()
        c._entries['m'] = ModelEntry(
            id='m', name='M', model_type=ModelType.LLM, source='huggingface',
            vram_gb=28.6, ram_gb=42.4, disk_gb=21.19, priority=85,
            quality_score=0.92, speed_score=0.34,
            capabilities={'chat': True})
        return c

    def test_facts_are_added_and_scoring_fields_are_not_touched(self, moe_gguf):
        """This fixture has no tensor table, so there is no measured split
        and nothing may be rewritten. Ranking inputs are never touched in
        any case."""
        c = self._cat()
        before = (c._entries['m'].vram_gb, c._entries['m'].ram_gb,
                  c._entries['m'].disk_gb, c._entries['m'].priority,
                  c._entries['m'].speed_score, c._entries['m'].quality_score)
        c.mark_downloaded('m', True, gguf_path=moe_gguf)
        e = c._entries['m']
        assert e.capabilities['moe'] is True        # added
        assert e.capabilities['chat'] is True       # preserved
        assert (e.vram_gb, e.ram_gb, e.disk_gb, e.priority,
                e.speed_score, e.quality_score) == before

    def test_a_dense_model_never_has_its_sizing_rewritten(self, dense_gguf):
        """The owner's constraint: only a MoE may put weights in RAM. A
        dense model touches every parameter every token, so overflowing it
        means a PCIe round trip per token. Its numbers must not move."""
        c = self._cat()
        before = (c._entries['m'].vram_gb, c._entries['m'].ram_gb)
        c.mark_downloaded('m', True, gguf_path=dense_gguf)
        assert (c._entries['m'].vram_gb, c._entries['m'].ram_gb) == before

    def test_a_measured_moe_is_resized_to_its_actual_placement(self, big_moe):
        """vram_gb becomes the NON-expert weights (what --cpu-moe leaves on
        the GPU) and ram_gb the experts (what must stay resident).

        The facts are injected rather than written to disk. A model big
        enough to exercise this is GiB-scale, and an earlier draft of this
        test wrote those bytes for real -- 144 files and 29 GB of them,
        which filled the disk. What is under test here is arithmetic on
        facts; the PARSER that produces them is covered above with real
        bytes at KB scale."""
        c = self._cat()
        c.mark_downloaded('m', True, gguf_path=big_moe)
        e = c._entries['m']
        assert e.vram_gb == pytest.approx(1.0 * 1.35, abs=0.05)
        assert e.ram_gb == pytest.approx(20.0, abs=0.05)
        # ranking inputs still untouched
        assert (e.priority, e.speed_score, e.quality_score) == (85, 0.34, 0.92)

    def test_ranking_is_never_rewritten_even_for_a_moe(self, big_moe):
        c = self._cat()
        c.mark_downloaded('m', True, gguf_path=big_moe)
        e = c._entries['m']
        assert e.priority == 85 and e.quality_score == 0.92
        assert e.disk_gb == 21.19


    def test_the_old_two_arg_call_is_unchanged(self):
        """Every existing caller passes no path. It must behave exactly as
        before -- downloaded set, capabilities untouched."""
        c = self._cat()
        c.mark_downloaded('m', True)
        assert c._entries['m'].downloaded is True
        assert c._entries['m'].capabilities == {'chat': True}

    def test_an_unreadable_path_still_marks_downloaded(self, tmp_path):
        """A bad path is a bookkeeping problem, not a reason to lose the
        download flag."""
        c = self._cat()
        c.mark_downloaded('m', True, gguf_path=str(tmp_path / 'gone.gguf'))
        assert c._entries['m'].downloaded is True
        assert c._entries['m'].capabilities == {'chat': True}

    def test_unknown_model_id_is_a_no_op(self, moe_gguf):
        c = self._cat()
        c.mark_downloaded('nope', True, gguf_path=moe_gguf)   # must not raise


class TestResidentNotMerelyFitting:
    """Measured live: Tiel-Coder-35B-A3B with 18.64 GiB of experts against
    ~6.5 GiB of free RAM served 0.95 tokens/sec, because every token faults
    8 of 256 experts back off disk. The experts are mmapped, so a RAM
    shortfall does not fail the load -- it quietly destroys throughput.
    So the GPU arm has to check RAM too, for a MoE and only for a MoE."""

    def _entry(self, **over):
        f = dict(id='m', name='M', model_type=ModelType.LLM,
                 vram_gb=3.4, ram_gb=18.6, capabilities={'moe': True})
        f.update(over)
        return ModelEntry(**f)

    def test_a_moe_whose_experts_fit_runs_on_the_gpu(self):
        assert self._entry().matches_compute(8.0, 32.0, True) == 'gpu'

    def test_a_moe_whose_experts_do_not_fit_is_not_called_gpu(self):
        """VRAM alone is satisfied here; RAM is not. Before this check the
        selector would have picked it and served a token per second."""
        assert self._entry().matches_compute(8.0, 6.5, True) != 'gpu'

    def test_a_dense_model_still_ignores_ram_on_the_gpu_arm(self):
        """Unchanged behaviour. A dense model that fits in VRAM does not
        need its weights in RAM as well, and asking would refuse models
        that run today."""
        dense = self._entry(capabilities={})
        assert dense.matches_compute(8.0, 0.5, True) == 'gpu'

    def test_a_row_with_no_moe_key_behaves_exactly_as_before(self):
        assert self._entry(capabilities=None).matches_compute(
            8.0, 0.1, True) == 'gpu'

    def test_the_moe_still_falls_through_to_the_cpu_arm(self):
        """Failing the resident check must not strand the model -- the
        remaining arms still apply."""
        e = self._entry(ram_gb=4.0)
        assert e.matches_compute(1.0, 8.0, True) == 'cpu'


class TestTheExpertsGoToRamOnlyWhenTheyMustAndOnlyForAMoE:
    """moe_offload_args is the ONE answer to "should this model's experts
    go to CPU". Three spawn paths ask it -- the main server, the
    caption/draft server, and model_lifecycle's restart -- and each already
    carries its own copy of `-ngl 99`; a second answer here is exactly how
    those drifted apart.

    Without it the fix is half-shipped: the catalog now admits a 35B on an
    8 GB card BECAUSE it is sized for --cpu-moe placement, and a spawn that
    omits the flag would try to put all 21 GiB on the card."""

    def test_a_moe_too_big_for_vram_sends_its_experts_to_ram(self, big_moe):
        assert moe_offload_args(big_moe, free_vram_gb=8.0) == ['--cpu-moe']

    def test_a_moe_that_fits_whole_keeps_them_on_the_gpu(self, big_moe):
        """On a card with room, --cpu-moe would give away performance for
        nothing -- the experts are faster in VRAM."""
        assert moe_offload_args(big_moe, free_vram_gb=64.0) == []

    def test_a_dense_model_never_gets_the_flag(self, dense_gguf):
        """The owner's constraint, at the spawn. Offloading a dense model
        costs a PCIe round trip per token because every parameter is
        touched every token."""
        assert moe_offload_args(dense_gguf, free_vram_gb=0.1) == []

    def test_an_unreadable_model_launches_unchanged(self, tmp_path):
        """No facts means no flag: a probe failure must not alter a spawn
        that works today."""
        assert moe_offload_args(str(tmp_path / 'gone.gguf'), 8.0) == []

    def test_the_threshold_uses_the_whole_file_not_the_non_expert_part(
            self, big_moe):
        """21 GiB * 1.35 = 28.4, so 28 GB of VRAM is still not enough to
        hold it whole and the experts still belong in RAM. Comparing
        against the 1 GiB non-expert figure instead would wrongly conclude
        it fits and drop the flag."""
        assert moe_offload_args(big_moe, free_vram_gb=28.0) == ['--cpu-moe']
        assert moe_offload_args(big_moe, free_vram_gb=29.0) == []


class TestTheSizingFunctionThatDidNotExist:
    """llama_gguf_compute_requirements was imported twice by Nunba's
    main.py -- the quant picker and the hub-install handler -- and defined
    nowhere. `git log -S "def llama_gguf_compute_requirements"` finds no
    definition in either repo's history.

    The import sits unconditionally inside _gguf_install_files, before any
    fit check, and the caller catches only ValueError. So the ImportError
    propagated and EVERY GGUF install through the Model Management page
    raised before it could pick a quant. Proven by calling the picker with
    a real Hub manifest."""

    def test_it_exists_and_returns_a_pair(self):
        vram, ram = llama_gguf_compute_requirements(10.0)
        assert (vram, ram) == (13.5, 20.0)

    def test_it_matches_the_literals_the_populator_used(self):
        """_populate_llm_models computed weights*1.35 and weights*2.0 from
        bare literals. One function owns that now; if these drift apart,
        two rows for the same model get two different sizes."""
        for gb in (0.5, 2.71, 17.6, 21.19):
            assert llama_gguf_compute_requirements(gb) == (
                round(gb * 1.35, 1), round(gb * 2.0, 1))

    def test_the_vram_overhead_is_the_same_constant_the_moe_path_uses(self):
        from integrations.service_tools.model_catalog import (
            _MOE_VRAM_OVERHEAD)
        assert llama_gguf_compute_requirements(100.0)[0] == round(
            100.0 * _MOE_VRAM_OVERHEAD, 1)

    def test_zero_is_not_a_crash(self):
        assert llama_gguf_compute_requirements(0.0) == (0.0, 0.0)
