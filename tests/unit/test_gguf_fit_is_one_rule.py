"""One rule for "can this machine run this GGUF", at two knowledge levels.

CHARACTERISATION TEST. Every expectation here was captured from the
behaviour that shipped BEFORE the unification, so it pins both callers
exactly. If a cell changes, the refactor changed behaviour and is not the
zero-regression refactor it claims to be.

THE PARALLEL PATH THIS EXISTS TO CLOSE. Two hand-written answers to one
question:

  install-time   main.py::_gguf_install_files._fits_compute
                 The model is NOT downloaded, so only the FILE SIZE is
                 known. MoE arm: (free_vram + free_ram) >= whole * 1.35.

  selection-time ModelEntry.matches_compute
                 The model IS downloaded and read_gguf_facts has measured
                 the split, so the row carries vram_gb = non-expert * 1.35
                 and ram_gb = experts. MoE arm: per-pool, both must hold.

They agree everywhere except where the knowledge differs -- and they
disagree on the owner's own box. For Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL
(21.19 GiB whole, 2.53 non-expert, 18.64 experts) at 4.7 GB free VRAM and
21.4 GB free RAM, install says NO and selection says YES: the install path
refuses to fetch the model the selector would pick.

That asymmetry is CORRECT and is preserved, not "fixed". You cannot know
the split before you have the file, so install is conservative by
necessity. What was wrong was having two implementations of it.

    python -m pytest tests/unit/test_gguf_fit_is_one_rule.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools.model_catalog import (  # noqa: E402
    ModelEntry, ModelType, llama_gguf_compute_requirements)

# Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL. Every figure measured, not estimated:
# the file is 22,749,880,160 bytes and its tensor table splits 18.64 GiB of
# '_exps' from 2.53 GiB of everything else.
WHOLE_GB, NON_EXPERT_GB, EXPERT_GB = 21.19, 2.53, 18.64

# (free_vram, free_ram, install_fits, selection_mode)
#
# Captured from the shipped implementations before unification. The full
# MODE is pinned, not just "is it gpu" -- an early draft of this grid
# recorded "does it fit at all" in the mode column and this test caught it,
# which is the whole point of writing it before the refactor.
#
# gpu_available is (free_vram > 0) throughout.
GRID = [
    (4.7, 21.4, False, 'gpu'),    # *** the owner's box: they disagree ***
    (4.7, 6.5, False, 'impossible'),
    (8.0, 32.0, True, 'gpu'),
    (8.0, 16.0, False, 'impossible'),
    (24.0, 32.0, True, 'gpu'),
    (0.0, 48.0, True, 'cpu'),     # no GPU -> the gpu arm never runs
    (2.0, 40.0, True, 'cpu'),     # 2.0 < 3.4 non-expert -> falls to cpu
    (48.0, 64.0, True, 'gpu'),
]


def install_fits(free_vram, free_ram, *, gpu, whole_gb=WHOLE_GB, moe=True):
    """The install-time rule as it ships today, isolated from disk checks
    and Hub I/O so this measures the COMPUTE decision only."""
    vram, ram = llama_gguf_compute_requirements(whole_gb)
    if gpu and free_vram >= vram:
        return True
    if moe and gpu and (free_vram + free_ram) >= vram:
        return True
    return free_ram >= ram


def selection_entry(moe=True, **over):
    """A row as it looks AFTER read_gguf_facts has corrected the sizing:
    vram_gb is the non-expert weights, ram_gb the experts."""
    f = dict(id='m', name='M', model_type=ModelType.LLM,
             vram_gb=round(NON_EXPERT_GB * 1.35, 1),
             ram_gb=round(EXPERT_GB, 1),
             capabilities={'moe': True} if moe else {})
    f.update(over)
    return ModelEntry(**f)


class TestTheGridIsPinned:
    @pytest.mark.parametrize('fv,fr,want_install,want_mode', GRID)
    def test_install_time_answer(self, fv, fr, want_install, want_mode):
        assert install_fits(fv, fr, gpu=fv > 0) is want_install

    @pytest.mark.parametrize('fv,fr,want_install,want_mode', GRID)
    def test_selection_time_answer(self, fv, fr, want_install, want_mode):
        assert selection_entry().matches_compute(fv, fr, fv > 0) == want_mode

    def test_the_known_disagreement_is_still_there(self):
        """Pinned on purpose. If unification silently collapses these two
        onto one answer, this fails and says so -- the asymmetry is a
        consequence of what is KNOWABLE at each moment, not a bug to
        paper over."""
        fv, fr = 4.7, 21.4
        assert install_fits(fv, fr, gpu=True) is False
        assert selection_entry().matches_compute(fv, fr, True) == 'gpu'


class TestDenseIsUntouchedAtBothLevels:
    """The owner's constraint. A dense model touches every parameter on
    every token, so it never gets the combined or per-pool MoE arms."""

    @pytest.mark.parametrize('fv,fr', [(4.7, 21.4), (8, 32), (24, 32)])
    def test_install_dense_never_uses_the_combined_arm(self, fv, fr):
        vram, ram = llama_gguf_compute_requirements(WHOLE_GB)
        expected = (fv >= vram) or (fr >= ram)
        assert install_fits(fv, fr, gpu=True, moe=False) is expected

    def test_selection_dense_gpu_arm_ignores_ram(self):
        """Unchanged behaviour: a dense model that fits in VRAM does not
        also need its weights in RAM."""
        dense = selection_entry(moe=False)
        assert dense.matches_compute(8.0, 0.1, True) == 'gpu'

    def test_selection_moe_gpu_arm_does_not_ignore_ram(self):
        assert selection_entry().matches_compute(8.0, 0.1, True) != 'gpu'


class TestTheNonGpuArmsAreUnchanged:
    """Everything below the GPU arm predates this work and must stay put."""

    def test_cpu_arm(self):
        e = selection_entry(moe=False, vram_gb=99.0, ram_gb=4.0)
        assert e.matches_compute(0.0, 8.0, False) == 'cpu'

    def test_cpu_offload_arm(self):
        e = selection_entry(moe=False, vram_gb=8.0, ram_gb=99.0,
                            supports_cpu_offload=True)
        assert e.matches_compute(4.0, 1.0, True) == 'cpu_offload'

    def test_impossible(self):
        e = selection_entry(moe=False, vram_gb=99.0, ram_gb=99.0)
        assert e.matches_compute(1.0, 1.0, True) == 'impossible'

    def test_no_gpu_means_the_gpu_arm_never_runs(self):
        assert selection_entry().matches_compute(999.0, 999.0, False) == 'cpu'


class TestTheSizingHelperIsTheOneSource:
    def test_both_levels_derive_from_it(self):
        """install_fits uses it directly; the selection row's vram_gb is
        non_expert * the same 1.35. If the constant forks, a model gets
        two sizes depending on which path saw it."""
        from integrations.service_tools.model_catalog import (
            _MOE_VRAM_OVERHEAD)
        assert llama_gguf_compute_requirements(WHOLE_GB)[0] == round(
            WHOLE_GB * _MOE_VRAM_OVERHEAD, 1)
        assert selection_entry().vram_gb == round(
            NON_EXPERT_GB * _MOE_VRAM_OVERHEAD, 1)
