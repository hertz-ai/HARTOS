"""The ONE place a llama-server's context geometry is decided and named.

WHY THIS MODULE EXISTS
──────────────────────
``--ctx-size`` is one number with one meaning — how much context the local
llama-server will accept — and on 2026-09-22 it was being decided in three
unrelated ladders and named by two different environment variables:

======================================  ==============================  ========
site                                    how it chose n_ctx              name read
======================================  ==============================  ========
Nunba ``llama_config._derive_ctx_size`` VRAM tiers, measured, capped    (writes
                                        (the only one that sees the box) both env
                                                                        vars)
``model_lifecycle``                     ``HEVOLVE_LLM_CTX_SIZE`` or     a name
``_launch_llama_server_direct``         the literal 8192                nothing
                                                                        writes
``llamacpp_manager.get_optimal_params`` its own 10240/8192/4096/2048    —
``vision/lightweight_backend``          the literal 512, twice          —
======================================  ==============================  ========

Two of those cannot ever be right by construction.  ``HEVOLVE_LLM_CTX_SIZE``
has no writer in either repo, so that branch always took its 8192 literal — a
declared override with no producer is dead config
(``memory/feedback_declaration_is_not_a_guard.md``).  And a ladder that never
measures the box cannot agree with one that does.

The cost of that disagreement is not a tidy truncation.  HARTOS's wire trimmer
(``core.llm_outbound_logger._get_budget_per_slot``) budgets every outbound
request against ONE of these numbers.  When the number it budgets against is
larger than the one the server was launched with, the "zero-tolerance context
overflow" guard *passes* requests the server then refuses:

  * 2026-08-07 — constants said 12288, the deploy shipped 4096: 78.7 % of
    1,407 real requests overflowed.
  * 2026-09-11 — the trimmer believed 12288, llama-server ran 8192: reuse
    sessions ..._18163818525 and ..._1923323102 never reached their goals.
  * 2026-09-22 08:15:49 — a 133 MB unit error moved the tier 8192 → 4096 and
    the measured 8,026-token tool schema stopped fitting for the life of the
    process.

MECHANISM HERE, POLICY IN THE TABLES
────────────────────────────────────
Everything below is pure: no I/O, no GPU probe, no ``os.environ`` mutation
except in the one function whose entire job is to publish.  Callers own the
measuring (they are the ones holding a VRAM manager and a .gguf path); this
module owns the decision, so the decision cannot differ between them.

The single chokepoint ruling (``memory/feedback_one_llama_server_single_
chokepoint.md``, owner 2026-09-13) says one llama-server reached through one
place.  This module is the geometry half of that: whoever ends up spawning —
Nunba on the desktop, systemd on HART OS, the G3 fallback in Docker — asks the
same table and publishes under the same name.
"""
from __future__ import annotations

import logging
import os

from core.constants import LLAMA_CTX_SIZE_DEFAULT, LLAMA_SLOTS_DEFAULT

logger = logging.getLogger(__name__)


# ── The vocabulary: one name per concept ─────────────────────────────────────
#: The ONE environment variable naming the running server's total n_ctx.
#: Written by :func:`publish_geometry` at every spawn, read by
#: :func:`ctx_size_from_env` and by
#: ``core.llm_outbound_logger._get_budget_per_slot``.  Documented in
#: ``core/constants.py`` beside the wire-trim budget it feeds.
CTX_SIZE_ENV = 'HEVOLVE_LLAMA_CTX_SIZE'

#: The ONE environment variable naming ``--parallel``.  n_ctx is partitioned
#: across slots under ``kv_unified``, so the trimmer needs both or its per-slot
#: budget is a guess.
SLOTS_ENV = 'HEVOLVE_LLAMA_SLOTS'

#: NOT a second name.  ``HART_LLM_CTX_SIZE`` is the systemd/Nix deploy
#: spelling: ``deploy/linux/systemd/hart-llm.service`` expands it in ExecStart
#: and ``nixos/modules/hart-llm.nix`` defaults it, both pinned to
#: ``LLAMA_CTX_SIZE_DEFAULT`` by ``tests/unit/
#: test_source_guard_llama_ctx_size_agrees.py``.  systemd expands it before any
#: Python exists, so no Python may read it — that is what would turn a
#: deploy-scoped variable into a second live name, and it is asserted against
#: in ``tests/unit/test_source_guard_one_ctx_size_authority.py``.
DEPLOY_CTX_SIZE_ENV = 'HART_LLM_CTX_SIZE'


# ── The policy: data, not branches ───────────────────────────────────────────
#: ``(minimum GiB left after the weights load, n_ctx)``, richest tier first.
#:
#: Read as "how much headroom does the card still have once this model is
#: resident".  The thresholds already reserve for the rest of the stack — TTS
#: (Indic Parler ~2 GB, F5, CosyVoice ~4 GB), the 0.8B draft, KV buffers — so
#: callers must NOT subtract a second reserve of their own before asking.
#: ``llamacpp_manager`` used to subtract a hardcoded 3.0 GB "TTS reserve" and
#: then apply its own ladder, which is the same reservation counted twice.
#:
#: KV cost that sets the spacing: ~1 GB per 8K of context for a 4B Q4 model,
#: ~0.5 GB per 8K for a 2B.
#:
#: Compared EXACTLY, no tolerance (owner 2026-09-22): a box that lands under a
#: gate gets the smaller tier, full stop.  The fix for landing under it by a
#: hair is to measure correctly, not to move the gate — a margin here would be
#: a second mechanism for one decision, which is how the first one rots.
CTX_TIERS: tuple[tuple[float, int], ...] = (
    (3.0, 16384),
    (2.0, 8192),
    (0.0, 4096),
)

#: Used when the box cannot be measured at all (no VRAM manager, probe raised).
#: Deliberately mid-table: the small tier would silently starve the agent
#: pipeline, the big one would promise headroom nobody confirmed.
CTX_FALLBACK = 8192

#: CPU-only inference: the KV cache lives in system RAM and there is no GPU
#: headroom reading to tier against.  Kept conservative and NAMED rather than
#: re-derived, so moving it is a deliberate act.
CPU_ONLY_CTX = 2048

#: Model classes that are sized by ROLE, not by headroom.
#:
#: ``caption`` is the 0.8B vision captioner in
#: ``integrations/vision/lightweight_backend.py``.  512 is not a cramped main
#: model — captioning sends one small image plus one sentence of prompt, and
#: the whole point of that backend is that it costs almost nothing.  It is a
#: legitimately separate model class, so it keeps a fixed small number; what it
#: does NOT get is its own copy of that number at two call sites.
ROLE_CTX: dict[str, int] = {
    'caption': 512,
}

#: ``(available system RAM in GiB, ceiling)`` — most restrictive wins.
#:
#: This is a SEPARATE constraint from :data:`CTX_TIERS`, not a competing one:
#: tiers ask "does the GPU have headroom", this asks "will the host survive the
#: allocation".  Both apply.
#:
#: The shape it replaces had a latent dead branch —
#: ``if avail < 4.0 and ctx > 4096: 4096 / elif avail < 2.0 and ctx > 2048:
#: 2048`` — where the ``elif`` is unreachable, because any box under 2.0 GiB is
#: also under 4.0 GiB and took the first branch.  A 1.5 GiB box therefore got
#: 4096, never the 2048 the code appears to promise.  Expressed as a table,
#: every row can fire.
RAM_CLAMPS: tuple[tuple[float, int], ...] = (
    (2.0, 2048),
    (4.0, 4096),
)


def ctx_cap() -> int:
    """The hard ceiling on any n_ctx: ``core.constants.LLAMA_CTX_SIZE_DEFAULT``.

    One constant, already canonical — the wire trimmer's backstop, the Nix
    module default, the systemd unit and ``hart.env.template`` are all pinned
    to it by ``test_source_guard_llama_ctx_size_agrees``.  Read through this
    function so no caller re-states ``12288``.
    """
    return LLAMA_CTX_SIZE_DEFAULT


def describe_tiers() -> str:
    """``"3.0/2.0"`` — the thresholds, for the spawn's decision log line.

    The log has to name the gate it compared against, or a boundary case is
    unreadable after the fact (2026-09-22: ``.1f`` printed ``remaining=2.0GB``
    beside a ``>= 2.0`` test that had just REJECTED it, and three separate
    investigations read that line as a pass).  Rendering the thresholds from
    the table means the log cannot claim a gate the code does not use.
    """
    return '/'.join(f'{floor:.1f}' for floor, _ctx in CTX_TIERS if floor > 0)


def derive_ctx_size(free_gib, model_gib, *, fallback=None) -> int:
    """n_ctx for a model that leaves ``free_gib - model_gib`` GiB of headroom.

    Args:
        free_gib: VRAM free RIGHT NOW, in **GiB**.  Must be a fresh probe: a
            spawn is exactly the moment a cached reading is worthless, because
            the server being replaced may still have held its weights when the
            sample was taken (measured 2026-09-10 — a 117-second-old sample
            read 4.76 GiB of real headroom as 0.1 GiB and pinned 4096 for the
            whole process).
        model_gib: the weights, in **GiB**, MEASURED from the .gguf wherever
            the file exists.  Not a preset literal divided by 1024: that mixes
            a decimal-MB count with a binary divisor and overstated a 4B model
            by 133 MB, which is the entire 8192 → 4096 regression of
            2026-09-22 08:15:49.
        fallback: returned when either input is unusable.  Defaults to
            :data:`CTX_FALLBACK`.

    Returns:
        A tier from :data:`CTX_TIERS`, never above :func:`ctx_cap`.
    """
    try:
        remaining = float(free_gib) - float(model_gib)
    except (TypeError, ValueError):
        return CTX_FALLBACK if fallback is None else int(fallback)
    for floor, ctx in CTX_TIERS:
        if remaining >= floor:
            return min(ctx, ctx_cap())
    # CTX_TIERS ends at 0.0, so this is reached only by a NEGATIVE remaining —
    # the weights do not fit the free VRAM at all.  The spawn is the caller's
    # decision (it may be about to run CPU-only); the smallest tier is the
    # honest geometry for it either way.
    return min(CTX_TIERS[-1][1], ctx_cap())


def clamp_ctx_for_ram(ctx_size, avail_gib) -> int:
    """Lower ``ctx_size`` when the HOST is short of RAM.  Never raises it.

    Applied on top of :func:`derive_ctx_size`, not instead of it — see
    :data:`RAM_CLAMPS`.  An unreadable ``avail_gib`` leaves the value alone,
    because guessing downward would silently shrink a correctly-derived window.
    """
    try:
        avail = float(avail_gib)
        ctx = int(ctx_size)
    except (TypeError, ValueError):
        return int(ctx_size)
    for threshold, ceiling in RAM_CLAMPS:
        if avail < threshold and ctx > ceiling:
            return ceiling
    return ctx


def ctx_for_role(role, *, free_gib=None, model_gib=None, on_gpu=True,
                 fallback=None) -> int:
    """n_ctx for a named model role — the entry point every spawn site uses.

    ``role`` is one of :data:`ROLE_CTX` (fixed by model class, e.g.
    ``'caption'``) or anything else, which means "a main model, size it to the
    box".  Routing even the fixed sizes through here is the point: the 512 the
    captioner needs is a policy decision that belongs beside the tiers, not a
    literal repeated at each ``subprocess.Popen``.

    ``on_gpu=False`` selects :data:`CPU_ONLY_CTX`; the KV cache is then host
    RAM and there is no VRAM headroom to tier against.
    """
    fixed = ROLE_CTX.get(role)
    if fixed is not None:
        return min(fixed, ctx_cap())
    if not on_gpu:
        return min(CPU_ONLY_CTX, ctx_cap())
    if free_gib is None or model_gib is None:
        return min(CTX_FALLBACK if fallback is None else int(fallback),
                   ctx_cap())
    return derive_ctx_size(free_gib, model_gib, fallback=fallback)


# ── Publishing: the one writer, the one reader ───────────────────────────────

def publish_geometry(ctx_size, slots=1) -> None:
    """Announce the geometry a llama-server was just launched with.

    THE ONLY writer of :data:`CTX_SIZE_ENV` / :data:`SLOTS_ENV`.  Call it on
    the line above the ``--ctx-size`` / ``--parallel`` flags being handed to
    the process, so the published number and the launched number cannot be
    different statements.

    Why it matters: ``core.llm_outbound_logger._get_budget_per_slot`` trusts
    this env pair ahead of its live ``/props`` probe.  Nothing wrote the slot
    count until 2026-08-30, so the trimmer assumed 1 slot while the spawn
    passed ``--parallel 2``; it allowed ~12288-token requests against a
    6144-token slot and 31 measured requests died at the server with "Context
    size has been exceeded" (18:11–18:12, source autogen.reuse).

    Never raises: a spawn must not fail because an env write did.
    """
    try:
        os.environ[CTX_SIZE_ENV] = str(int(ctx_size))
        os.environ[SLOTS_ENV] = str(max(1, int(slots)))
    except (TypeError, ValueError):
        logger.warning(
            'llama geometry not published: ctx_size=%r slots=%r are not '
            'integers, so the wire trimmer will fall back to probing /props',
            ctx_size, slots)


def ctx_size_from_env(default=None) -> int:
    """The n_ctx a running or about-to-launch server uses, per the published env.

    For spawn sites that do NOT measure the box themselves (the standalone and
    Docker fallbacks).  Reading the published value means such a spawn inherits
    whatever the real derivation last decided instead of inventing a third
    number.

    ``default`` applies when nothing has been published — :data:`CTX_FALLBACK`
    unless the caller knows better.
    """
    raw = os.environ.get(CTX_SIZE_ENV)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return min(value, ctx_cap())
        except ValueError:
            logger.warning('%s=%r is not an integer — ignoring it',
                           CTX_SIZE_ENV, raw)
    return int(CTX_FALLBACK if default is None else default)


def slots_from_env(default=None) -> int:
    """``--parallel`` as last published, else ``LLAMA_SLOTS_DEFAULT``."""
    raw = os.environ.get(SLOTS_ENV)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning('%s=%r is not an integer — ignoring it',
                           SLOTS_ENV, raw)
    return max(1, int(LLAMA_SLOTS_DEFAULT if default is None else default))
