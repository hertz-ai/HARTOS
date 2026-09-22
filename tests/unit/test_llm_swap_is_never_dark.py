"""Replacing the main LLM never leaves the node without one.

Owner directive, 2026-09-22: "at any point in time some LLM shd be running
for the nunba to work locally except central", and "eviction of main model
and swapping main model are two different concerns".

Both main-LLM swap implementations were break-before-make on 2026-09-22:

  * HARTOS llamacpp_manager.swap_model -- _stop_locked() then
    _start_locked() on the SAME port.  If the newcomer fails to start, the
    port is empty and the node has no LLM.  Measured: a 21.19 GiB model
    takes ~7 minutes to become ready off the external drive, so the dark
    window is minutes, and on a start failure it is permanent.

  * Nunba LlamaConfig.switch_model -- stop (a no-op on a per-request
    instance), write the config, then start (which ADOPTS the incumbent
    and returns True).  It reports success having changed nothing.

The sequence under test is the shared one both owners drive.  What it
guarantees is an ORDERING: nothing that can take the incumbent away
happens until the newcomer has been observed SERVING.  Every test here is
about that ordering, because the ordering is the whole property -- there
is no amount of admission cleverness that saves a node whose incumbent was
stopped first.

    python -m pytest tests/unit/test_llm_swap_is_never_dark.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.service_tools.llm_swap import SwapOutcome, swap_main_llm


class Recorder:
    """The four primitives each server owner supplies, instrumented.

    Records the ORDER they are called in, which is what the tests assert
    on.  Defaults describe the happy path: a newcomer that starts and
    serves, on a machine with room for it.
    """

    def __init__(self, *, spawn_ok=True, serves_ok=True, port=8099,
                 repoint_raises=False):
        self.calls = []
        self._spawn_ok = spawn_ok
        self._serves_ok = serves_ok
        self._port = port
        self._repoint_raises = repoint_raises
        self.abandoned = []

    def pick_port(self):
        self.calls.append('pick_port')
        return self._port

    def spawn(self, port):
        self.calls.append(f'spawn:{port}')
        return object() if self._spawn_ok else None

    def serves(self, handle, port):
        assert handle is not None, 'the prober was not given the newcomer'
        self.calls.append(f'serves:{port}')
        return self._serves_ok

    def repoint(self, handle, port):
        assert handle is not None, 'the repoint was not given the newcomer'
        self.calls.append(f'repoint:{port}')
        if self._repoint_raises:
            raise RuntimeError('config write failed')

    def retire(self):
        self.calls.append('retire')

    def abandon(self, handle):
        self.calls.append('abandon')
        self.abandoned.append(handle)

    def as_kwargs(self):
        return {
            'pick_port': self.pick_port, 'spawn': self.spawn,
            'serves': self.serves, 'repoint': self.repoint,
            'retire': self.retire, 'abandon': self.abandon,
        }


def run(rec, **over):
    """A swap on a machine with ample room, unless a test says otherwise."""
    kw = {
        'free_vram_gb': 8.0, 'free_ram_gb': 32.0, 'gpu_available': True,
        'vram_need_gb': 3.0, 'label': 'newcomer',
    }
    kw.update(over)
    kw.update(rec.as_kwargs())
    return swap_main_llm(**kw)


class TestTheIncumbentOutlivesEveryFailure:
    """Nothing stops the incumbent until the newcomer has SERVED."""

    def test_retire_never_precedes_a_successful_serve(self):
        rec = Recorder()
        out = run(rec)
        assert out.ok, out.reason
        assert rec.calls.index('retire') > rec.calls.index('serves:8099'), (
            f'the incumbent was retired before the newcomer served: '
            f'{rec.calls}')

    def test_a_newcomer_that_never_serves_does_not_cost_the_incumbent(self):
        """#99: a sidecar is running when it SERVES, not when its launcher
        survived.  A process that starts and never answers is the exact
        case break-before-make turns into an outage."""
        rec = Recorder(serves_ok=False)
        out = run(rec)
        assert out.ok is False and out.reason == 'newcomer_never_served'
        assert 'retire' not in rec.calls, rec.calls
        assert rec.abandoned, 'the newcomer was left running'

    def test_a_spawn_that_fails_does_not_cost_the_incumbent(self):
        rec = Recorder(spawn_ok=False)
        out = run(rec)
        assert out.ok is False and out.reason == 'spawn_failed'
        assert 'retire' not in rec.calls and 'repoint:8099' not in rec.calls

    def test_no_free_port_does_not_cost_the_incumbent(self):
        rec = Recorder(port=None)
        out = run(rec)
        assert out.ok is False and out.reason == 'no_free_port'
        assert rec.calls == ['pick_port'], rec.calls

    def test_a_failed_repoint_keeps_the_incumbent_and_abandons_the_newcomer(
            self):
        """If the endpoint cannot be moved, the newcomer is useless and the
        incumbent is still the one serving.  Retiring it here would go dark
        with a healthy server nobody can reach."""
        rec = Recorder(repoint_raises=True)
        out = run(rec)
        assert out.ok is False and out.reason == 'repoint_failed'
        assert 'retire' not in rec.calls, rec.calls
        assert rec.abandoned

    def test_the_endpoint_moves_before_the_incumbent_goes(self):
        """Ordering the other way round leaves a window where the config
        names a port that has just been killed."""
        rec = Recorder()
        run(rec)
        assert rec.calls.index('repoint:8099') < rec.calls.index('retire'), \
            rec.calls


class TestAdmissionIsJudgedBesideTheIncumbent:
    """The swap asks "can the newcomer come up ALONGSIDE?" -- eviction asks
    "what do I get back if this goes?".  Same footprint data, opposite
    questions, and the reclaim must never enter this budget: spending it is
    precisely what makes the node dark."""

    def test_a_refusal_touches_nothing_at_all(self):
        rec = Recorder()
        out = run(rec, free_vram_gb=1.0, vram_need_gb=6.0)
        assert out.ok is False and out.reason == 'admission'
        assert rec.calls == [], rec.calls

    def test_the_incumbents_reclaim_is_not_in_the_budget(self):
        """The incumbent holds 4 GB and 1 GB is free.  A 4.5 GB newcomer
        fits only if you spend what the incumbent would give back -- which
        you cannot, because it is still serving and must keep serving.

        There is no parameter to pass a reclaim through, and that is the
        point: the coordinator cannot be told to count it."""
        rec = Recorder()
        out = run(rec, free_vram_gb=1.0, vram_need_gb=4.5)
        assert out.ok is False and out.reason == 'admission'
        assert rec.calls == []

    def test_an_unknown_footprint_refuses_rather_than_assuming_zero(self):
        """An unknown footprint is not a free model.  Refusing costs the
        user a swap; guessing costs them the node."""
        rec = Recorder()
        out = run(rec, vram_need_gb=None, ram_need_gb=None,
                  whole_need_gb=None)
        assert out.ok is False and out.reason == 'admission'
        assert rec.calls == []

    def test_a_moe_may_spend_ram_but_a_dense_model_may_not(self):
        """Delegates to gguf_fits_gpu rather than carrying a second rule --
        the owner's constraint ("only MOE can overflow to RAM when GPU is
        present") lives in exactly one place."""
        dense = Recorder()
        assert run(dense, free_vram_gb=4.0, free_ram_gb=32.0,
                   vram_need_gb=None, whole_need_gb=18.0).ok is False

        moe = Recorder()
        assert run(moe, free_vram_gb=4.0, free_ram_gb=32.0, moe=True,
                   vram_need_gb=None, whole_need_gb=18.0).ok is True

    def test_a_moe_still_needs_both_pools_once_the_split_is_measured(self):
        """Split KNOWN: experts want RAM, attention wants VRAM, and getting
        the kind wrong is not a load failure but 0.95 tok/s of paging."""
        rec = Recorder()
        out = run(rec, free_vram_gb=8.0, free_ram_gb=2.0, moe=True,
                  vram_need_gb=3.0, ram_need_gb=19.0)
        assert out.ok is False and out.reason == 'admission'


class TestTheOutcomeIsReportedHonestly:
    """The defect this replaces returned True having changed nothing."""

    def test_success_names_the_port_the_newcomer_is_on(self):
        out = run(Recorder(port=8123))
        assert out.ok is True and out.port == 8123

    def test_a_refusal_is_never_reported_as_a_swap(self):
        for rec, over in [
            (Recorder(), {'free_vram_gb': 0.1, 'vram_need_gb': 9.0}),
            (Recorder(spawn_ok=False), {}),
            (Recorder(serves_ok=False), {}),
        ]:
            out = run(rec, **over)
            assert isinstance(out, SwapOutcome)
            assert out.ok is False
            assert out.port is None, (
                'a failed swap named a port, which a caller would repoint to')
