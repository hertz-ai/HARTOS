"""Replacing the main LLM, as one sequence, for every server owner.

Owner directives, 2026-09-22:

    "if there is a job which needs another model loading then we shd
     dynamically change model to free up headroom"
    "at any point in time some LLM shd be running for the nunba to work
     locally except central"
    "eviction of main model and swapping main model are two different
     concerns"

SWAP IS NOT EVICTION.  ``model_lifecycle.request_swap`` and its queue are
the eviction side: they make room under memory pressure by evicting GPU
*sidecars*, and they exclude the main LLM in so many words -- "we never
burn down the active LLM in exchange for a TTS that still won't fit".
That exclusion is correct and is not bypassed here.  The two concerns
share only the footprint DATA, and they ask opposite questions of it:

    eviction:  what do I get back if this goes?
    swap:      can the newcomer come up ALONGSIDE?

Which is why the incumbent's reclaim must never enter a swap budget --
spending it is precisely what makes the node dark.  There is deliberately
no parameter here through which a reclaim could be passed.

WHY THIS IS SHARED RATHER THAN WRITTEN TWICE.  Two processes own a main
llama-server: HARTOS's ``llamacpp_manager`` on a standalone node, and
Nunba's ``LlamaConfig`` on the desktop.  On 2026-09-22 both had their own
swap, both were break-before-make, and they had drifted into two different
failure modes (one went dark for minutes, the other reported success
having changed nothing).  The ORDERING below is the safety property, so it
lives once; the primitives that differ per owner -- how you spawn, how you
move the endpoint, how you stop what you own -- are passed in.  Same split
``core.llama_geometry`` already uses for the context-size decision, and
for the same reason: HARTOS cannot import Nunba, so shared policy lives
downstream and Nunba reaches down for it.

Engine-agnostic by construction: nothing here knows what a GGUF is, or
that llama.cpp exists.  A future engine supplies its own five callables.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .model_catalog import gguf_fits_gpu

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwapOutcome:
    """What happened, stated so a caller cannot mistake a refusal for a swap.

    ``port`` is set ONLY on success.  The defect this replaces returned a
    bare ``True`` from a path that had adopted the incumbent and changed
    nothing, and the caller then told the catalog a different model was
    loaded.  A refusal carries no port, so there is nothing to repoint to.
    """

    ok: bool
    reason: str
    port: Optional[int] = None


def swap_main_llm(
    *,
    # ── admission facts, measured WITH THE INCUMBENT RESIDENT ──
    free_vram_gb: float,
    free_ram_gb: float,
    gpu_available: bool,
    moe: bool = False,
    vram_need_gb: Optional[float] = None,
    ram_need_gb: Optional[float] = None,
    whole_need_gb: Optional[float] = None,
    # ── the per-owner primitives ──
    pick_port: Callable[[], Optional[int]],
    spawn: Callable[[int], Optional[Any]],
    serves: Callable[[Any, int], bool],
    repoint: Callable[[Any, int], None],
    retire: Callable[[], None],
    abandon: Callable[[Any], None],
    label: str = '',
) -> SwapOutcome:
    """Bring a new main LLM up beside the incumbent, then retire it.

    The ordering IS the guarantee: nothing that can take the incumbent away
    happens until the newcomer has been observed SERVING.  Break-before-make
    is not an admissible form for the main LLM -- a 21.19 GiB model measured
    ~7 minutes to become ready off an external drive, and on a start failure
    the node simply has no LLM.

    Measured feasible on 8 GB of VRAM during the --cpu-moe spike: two
    llama-servers resident at once (3321 MiB -> 6256 MiB used, the incumbent
    untouched throughout).

    The primitives, each doing exactly one thing:

      pick_port() -> int|None        a free port, NOT the incumbent's
      spawn(port) -> handle|None     start the newcomer there; None if it
                                     did not launch
      serves(handle, port) -> bool   the CAPABILITY probe.  A sidecar is
                                     running when it SERVES, not when its
                                     launcher survived (#99) -- process
                                     liveness is not an answer to this
                                     question
      repoint(handle, port) -> None  make that server the canonical endpoint
      retire() -> None               stop the incumbent, release its
                                     reservation
      abandon(handle) -> None        stop a newcomer that will not be used

    Everything about the newcomer is passed to the primitives that act on
    it, so no caller has to smuggle the handle out through a closure cell.
    ``retire`` takes nothing: the incumbent is the caller's own, and this
    sequence deliberately never learns how to identify it.

    Admission delegates to ``gguf_fits_gpu`` rather than carrying a second
    rule, so the owner's constraint -- "only MOE can overflow to RAM when
    GPU is present else the previous calc we had is correct", because
    "overflowing a dense model makes the user experience go for a toss" --
    is enforced in exactly one place.  With no footprint known at all that
    helper returns False, which is the outcome we want: an unknown footprint
    is not a free model, and refusing costs a swap where guessing costs the
    node.
    """
    who = label or 'newcomer'

    # 1. ADMISSION.  The incumbent STAYS UP, so the room it occupies is not
    #    available and is not counted.  These figures are the caller's live
    #    reading taken while it is resident.
    if not gguf_fits_gpu(free_vram_gb, free_ram_gb,
                         gpu_available=gpu_available, moe=moe,
                         vram_need_gb=vram_need_gb, ram_need_gb=ram_need_gb,
                         whole_need_gb=whole_need_gb):
        logger.info(
            "swap refused for %s: does not fit BESIDE the running model "
            "(%.2f GB VRAM / %.2f GB RAM free, needs %s VRAM / %s RAM / %s "
            "whole, moe=%s).  The running model keeps serving.",
            who, free_vram_gb, free_ram_gb, vram_need_gb, ram_need_gb,
            whole_need_gb, moe)
        return SwapOutcome(False, 'admission')

    # 2. A PORT OF ITS OWN.  Sharing the incumbent's port is what makes a
    #    swap break-before-make in the first place.
    port = pick_port()
    if not port:
        logger.warning("swap refused for %s: no free port", who)
        return SwapOutcome(False, 'no_free_port')

    # 3. SPAWN BESIDE.
    handle = spawn(port)
    if handle is None:
        logger.warning(
            "swap failed for %s: did not start on port %s.  The running "
            "model was never stopped.", who, port)
        return SwapOutcome(False, 'spawn_failed')

    # 4. VERIFY IT SERVES -- not that it launched.
    if not serves(handle, port):
        logger.warning(
            "swap failed for %s: started on port %s but never served.  "
            "Abandoning it; the running model keeps serving.", who, port)
        _safely(abandon, handle, what='abandon')
        return SwapOutcome(False, 'newcomer_never_served')

    # 5. REPOINT, then retire.  This order has no window in which the
    #    endpoint names a port that has just been killed.
    try:
        repoint(handle, port)
    except Exception as exc:
        logger.error(
            "swap failed for %s: could not move the endpoint to port %s "
            "(%r).  Abandoning the newcomer -- retiring the incumbent here "
            "would go dark with a healthy server nobody can reach.",
            who, port, exc)
        _safely(abandon, handle, what='abandon')
        return SwapOutcome(False, 'repoint_failed')

    # 6. RETIRE.  A failure here leaks a process; it does not go dark,
    #    because the endpoint already names the newcomer.
    _safely(retire, what='retire')

    logger.info("swapped to %s on port %s", who, port)
    return SwapOutcome(True, 'swapped', port=port)


def _safely(fn: Callable, *args, what: str) -> None:
    """Run a cleanup primitive; report a failure rather than swallowing it.

    Not a silent gulp: by this point the swap's outcome is already decided
    and re-raising would turn a leaked process into a lost return value.
    The failure is logged with its exception so it is attributable.
    """
    try:
        fn(*args)
    except Exception:
        logger.exception("swap: %s failed", what)
