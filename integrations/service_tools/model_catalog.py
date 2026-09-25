"""
ModelCatalog — single source of truth for ALL model types.

One schema covers LLM, TTS, STT, VLM, image gen, video gen, etc.
JSON-backed so the admin UI can CRUD entries at runtime.

Adding a new model of ANY type:
  1. catalog.register(ModelEntry(...))        — programmatic
  2. POST /api/admin/models                   — via admin UI
  3. Edit model_catalog.json in the data dir  — manual

The catalog does NOT load/unload models — that's the orchestrator's job.
This is purely metadata + state tracking.
"""

import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger('ModelCatalog')


# ── Model type enum — single source of truth ─────────────────────
# Use ModelType.LLM (not 'llm') everywhere. The .value IS the string
# so JSON serialization and dict key usage work unchanged.
#
# Usage:
#   ModelType.LLM          → <ModelType.LLM: 'llm'>
#   ModelType.LLM.value    → 'llm'
#   ModelType.LLM.label    → 'Large Language Model'
#   ModelType('llm')       → ModelType.LLM  (lookup from string)
#   str(ModelType.LLM)     → 'llm'          (clean string for JSON/logs)

class ModelType(str, Enum):
    """Canonical model type identifiers. Inherits from str so
    ModelType.LLM == 'llm' is True — backwards compatible with
    all existing string comparisons, dict keys, and JSON."""

    LLM       = 'llm'
    TTS       = 'tts'
    STT       = 'stt'
    VLM       = 'vlm'
    IMAGE_GEN = 'image_gen'
    VIDEO_GEN = 'video_gen'
    AUDIO_GEN = 'audio_gen'
    EMBEDDING = 'embedding'
    EMBODIED  = 'embodied'   # VLA / world-model robot policy (Qwen RobotSuite class)

    @property
    def label(self) -> str:
        return _MODEL_TYPE_LABELS[self]

    def __str__(self) -> str:
        return self.value


_MODEL_TYPE_LABELS = {
    ModelType.LLM:       'Large Language Model',
    ModelType.TTS:       'Text-to-Speech',
    ModelType.STT:       'Speech-to-Text',
    ModelType.VLM:       'Vision-Language Model',
    ModelType.IMAGE_GEN: 'Image Generation',
    ModelType.VIDEO_GEN: 'Video Generation',
    ModelType.AUDIO_GEN: 'Audio/Music Generation',
    ModelType.EMBEDDING: 'Embedding Model',
    ModelType.EMBODIED:  'Embodied VLA / World Model',
}

# Backwards-compatible dict for code that iterates MODEL_TYPES
MODEL_TYPES = {mt.value: mt.label for mt in ModelType}

# Backend runtimes
BACKENDS = {
    'llama.cpp':  'llama.cpp server (GGUF)',
    'torch':      'PyTorch (HuggingFace)',
    'onnx':       'ONNX Runtime',
    'piper':      'Piper TTS (ONNX, CPU)',
    'api':        'Remote API endpoint',
    'sidecar':    'Subprocess sidecar',
    'in_process': 'In-process Python module',
}

# Backends whose runtime is positively known NOT to be PyTorch.  Anything else
# — including an unrecognised or missing value — conservatively counts as
# needing torch.
#
# WHY (R1, measured live 2026-08-11): CUDA-torch provisioning was gated on the
# model's TYPE ("is this TTS/STT on a CUDA box?") instead of its RUNTIME.  A
# sherpa-onnx STT engine (Moonshine) therefore blocked on a 221s pip resolve
# plus a multi-GB CUDA PyTorch download for a runtime that never imports torch.
# The deciding field already existed — `ModelEntry.backend`, set correctly by
# whisper_tool.py — it just wasn't the thing being read.
#
# The set is deliberately an ALLOW-LIST of torch-free backends rather than a
# deny-list of torch ones: an unclassified backend keeps today's behaviour
# (install), so this can never under-install and break an engine at load time.
# Over-installing wastes minutes; under-installing breaks the feature.
TORCHLESS_BACKENDS = frozenset({'onnx', 'piper', 'llama.cpp', 'api'})


def backend_requires_torch(backend) -> bool:
    """True iff a model on this backend needs PyTorch installed to run.

    SINGLE source for "does this engine need torch?" — both the language
    bootstrap and the STT loader consult it, so the answer cannot diverge
    between the pre-install path and the download path.

    Unknown / empty / None => True (fail safe: install rather than risk an
    engine that cannot load).
    """
    return (backend or 'torch') not in TORCHLESS_BACKENDS


# ── How big is a model? ONE table, ONE unit ──────────────────────
#
# Keyed by GGUF file name because that is the identity both repos already
# share: HARTOS's LLM ladder below and Nunba's MODEL_PRESETS name the same
# artifacts, and until 2026-09-22 each kept its own column of sizes. The
# literals were byte-identical (550, 1100, 1340, 2910, 6113, 18022, 22733,
# 22630, 22938) and four months apart in age: Nunba's since its first commit
# (96661414e, 2026-03-16), HARTOS's since 80c703b6c (2026-07-27). Two tables
# of the same numbers always drift; this is the surviving one.
#
# WHY HERE and not in Nunba, which wrote them first: the import direction
# decides. Nunba imports HARTOS (models/catalog.py imports this module at
# module scope); HARTOS imports nothing from Nunba (MEASURED: zero hits for
# `ModelPreset` in this tree). A shared table can only live at the end both
# sides can reach.
#
# THE UNIT IS BYTES, and for good reason. The field this replaces was named
# `size_mb` and its meaning changed row to row. MEASURED 2026-09-22 against
# every .gguf on the author's box:
#
#   file                                    literal   bytes          MiB      MB(dec)
#   Qwen3.5-4B-UD-Q4_K_XL.gguf                2910   2,912,109,728   2777.2   2912.1
#   Qwen3.5-2B-UD-Q4_K_XL.gguf                1340   1,339,752,704   1277.7   1339.8
#   Qwen3.5-0.8B-UD-Q4_K_XL.gguf               550     558,772,480    532.9    558.8
#   Qwen3-VL-2B-Instruct-UD-Q4_K_XL.gguf      1500   1,129,709,248   1077.4   1129.7
#
# The 4B and 2B rows are the decimal-MB reading (copied from HuggingFace's
# file listing, which is decimal). The 1500 row matches NEITHER — 39% over
# MiB, 33% over decimal — it was simply wrong. And the large rows were typed
# the other way, carrying their author's own arithmetic in the comment:
# `6113,  # 5.97 GB` is 6113/1024, i.e. MiB. One field, three vocabularies,
# no single divisor correct for all of them.
#
# Bytes is the only reading that cannot be misread, so bytes is what is
# stored. Every row states its PROVENANCE, because the distinction between
# a measurement and an estimate is exactly what got lost before:
#
#   'measured …'  the file was stat'd; the number is that file's size.
#   'estimate …'  NOT CHECKED — no file on the box that produced this table.
#                 The literal is preserved and the unit it was typed in is
#                 named, so the guess is never mistaken for a fact.
_MIB = 1024 ** 2

MODEL_WEIGHT_BYTES = {
    # ── MEASURED 2026-09-22 — files present, stat'd, byte-exact ──
    'Qwen3.5-4B-UD-Q4_K_XL.gguf':
        (2_912_109_728, 'measured 2026-09-22 (~/.trueflow/models)'),
    'Qwen3.5-2B-UD-Q4_K_XL.gguf':
        (1_339_752_704, 'measured 2026-09-22 (~/.trueflow/models)'),
    'Qwen3.5-0.8B-UD-Q4_K_XL.gguf':
        (558_772_480, 'measured 2026-09-22 (~/.nunba/models)'),
    'Qwen3-VL-2B-Instruct-UD-Q4_K_XL.gguf':
        (1_129_709_248, 'measured 2026-09-22 (~/.trueflow/models); the 1500 '
                        'literal it replaces matched neither MB nor MiB'),

    # ── NOT CHECKED — no file on this box. Literals preserved, each
    # converted from the unit its author used. Do not promote any of
    # these to "measured" without stat'ing the actual download.
    'Qwen3-2B-Instruct-Q4_K_M.gguf':
        (1100 * _MIB, 'estimate: literal 1100, unit undeclared by its author '
                      '- read as MiB, the over-stating reading'),
    'Qwen3.5-9B-UD-Q4_K_XL.gguf':
        (6113 * _MIB, 'estimate: literal 6113 MiB (author comment "# 5.97 GB" '
                      '= 6113/1024)'),
    'Qwen3.5-27B-UD-Q4_K_XL.gguf':
        (18022 * _MIB, 'estimate: literal 18022 MiB (author comment '
                       '"# 17.6 GB" = 18022/1024)'),
    'Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf':
        (22733 * _MIB, 'estimate: literal 22733 MiB (author comment '
                       '"# 22.2 GB" = 22733/1024)'),
    'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf':
        (22630 * _MIB, 'estimate: literal 22630, unit undeclared - read as '
                       'MiB, matching the sibling 35B rows it was typed with'),
    'Tiel-Coder-35B-A3B-UD-Q4_K_XL.gguf':
        (22938 * _MIB, 'estimate: literal 22938, unit undeclared - read as '
                       'MiB, matching the sibling 35B rows it was typed with'),
    # The ONLY measured row in this table.  Every other entry above is an
    # estimate inherited from a literal whose unit had to be inferred; this
    # one is the exact Content-Length the file downloaded at, confirmed
    # against os.path.getsize after the fetch (2026-09-22).  Recorded in
    # bytes rather than N * _MIB precisely so the rounding cannot creep back.
    'Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf':
        (22749880160, 'MEASURED: HF Content-Length and on-disk size both '
                      '22749880160 bytes exactly (2026-09-22)'),
}


#: GGUF metadata value types, by the enum the format defines.  Only the
#: fixed-width ones need a struct code; strings and arrays are read by shape.
_GIB = 1024 ** 3

#: VRAM headroom over a MoE's non-expert weights, covering KV cache and
#: compute buffers. Same 1.35 the dense path uses, and measured comfortably
#: above the real ratio: Tiel-Coder-35B-A3B took 2.87 GiB for 2.53 GiB of
#: non-expert weights at ctx 4096, i.e. 1.135.
_MOE_VRAM_OVERHEAD = 1.35

_GGUF_SCALAR = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i',
                6: '<f', 7: '<?', 10: '<Q', 11: '<q', 12: '<d'}


def read_gguf_facts(path: str) -> dict:
    """Architecture facts read FROM the file, never typed by a human.

    Everything this returns is stated in the GGUF's own metadata header, so
    it cannot drift from the artifact the way a hand-written table does.
    That matters here: the sizes in MODEL_WEIGHT_BYTES above are estimates
    inherited from literals whose unit had to be guessed, and one of them is
    1.58 GB wrong -- which inverts the size ordering of two real models and,
    through the priority ladder, changes which one a machine is offered.

    The two facts that drive behaviour and were previously tracked NOWHERE:

    ``moe`` / ``experts_used`` / ``experts_total``
        Generation throughput with weights resident is bounded by memory
        bandwidth times ACTIVE parameters per token. A 35B mixture-of-experts
        using 8 of 256 experts touches a small fraction of what a dense 27B
        touches per token, so it is materially FASTER despite being the
        larger file. speed_score currently says the opposite.

    ``mtp``
        Whether the weights carry a multi-token-prediction head. Only a model
        that has one benefits from ``--spec-type draft-mtp``; passing the
        flag for a plain GGUF is accepted and buys nothing.

    Returns {} for anything unreadable -- an absent file, a truncated header,
    a non-GGUF. Callers treat {} as "not known", never as "not MoE".
    """
    facts: dict = {}
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            magic, _ver, _n_tensor, n_kv = struct.unpack('<4sIQQ', f.read(24))
            if magic != b'GGUF':
                return {}

            def _u(fmt):
                return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

            def _s():
                return f.read(_u('<Q')).decode('utf-8', 'replace')

            def _v(t):
                if t == 8:
                    return _s()
                if t == 9:                       # array: elem type, count
                    et, n = _u('<I'), _u('<Q')
                    return [_v(et) for _ in range(n)]
                return _u(_GGUF_SCALAR[t])

            kv = {}
            for _ in range(n_kv):
                key = _s()
                kv[key] = _v(_u('<I'))

            # The tensor table follows the metadata. Read it ONLY for a
            # mixture of experts, where the expert/non-expert split is the
            # number that decides placement -- for a dense model every byte
            # has to be resident anyway, so the split says nothing.
            spans = None
            if any(k.endswith('.expert_count') for k in kv):
                offsets = []
                for _ in range(_n_tensor):
                    t_name = _s()
                    for _ in range(_u('<I')):    # dims
                        _u('<Q')
                    _u('<I')                     # ggml type
                    offsets.append((t_name, _u('<Q')))
                align = kv.get('general.alignment') or 32
                data_start = (f.tell() + align - 1) // align * align
                spans = (offsets, size - data_start)
    except (OSError, struct.error, KeyError, UnicodeDecodeError) as e:
        logger.warning("read_gguf_facts(%s): unreadable (%s); returning no "
                       "facts rather than guessing", path, e)
        return {}

    arch = kv.get('general.architecture')
    if arch:
        facts['architecture'] = arch
    facts['weight_bytes'] = size

    # Keys are namespaced by architecture (qwen35moe.expert_count), so find
    # them by suffix rather than assuming the prefix.
    def _by_suffix(suffix):
        for k, v in kv.items():
            if k.endswith('.' + suffix):
                return v
        return None

    # How many transformer blocks the file holds -- the unit ``--n-gpu-layers
    # N`` counts in.  A partial offload that has to pick N used to assume
    # "~40 layers" for every model it met (llamacpp_manager.get_optimal_params
    # carried ``int(ratio * 40)``); the file states the real number, so that
    # guess is now only the fallback for a file this reader could not parse.
    blocks = _by_suffix('block_count')
    if blocks is not None:
        try:
            facts['block_count'] = int(blocks)
        except (TypeError, ValueError):
            logger.warning("read_gguf_facts(%s): block_count %r is not an "
                           "integer; leaving it unknown", path, blocks)

    used, total = _by_suffix('expert_used_count'), _by_suffix('expert_count')
    if total:
        facts['moe'] = True
        facts['experts_total'] = total
        if used:
            facts['experts_used'] = used
            # The number that actually predicts throughput. Stored exact --
            # both operands came out of the file, so rounding here would be
            # re-introducing the hand-typed approximation this replaces.
            facts['expert_fraction'] = used / total
        # What actually has to sit in VRAM. llama.cpp's --cpu-moe keeps the
        # expert tensors (the '_exps' ones) in system RAM and leaves
        # attention, embeddings and norms on the GPU, so a MoE's VRAM cost
        # is the NON-expert bytes plus KV cache -- not the file size. For
        # Tiel-Coder-35B-A3B that is 2.53 GiB of a 21.19 GiB file: sizing it
        # at weights * 1.35 overstates the requirement by about 11x and is
        # why a machine that can run this model is told it cannot.
        #
        # Sizes come from consecutive tensor OFFSETS rather than a GGML
        # quant-type table: the file states its own layout, so this cannot
        # drift from upstream type definitions. Inter-tensor padding counts
        # into the preceding tensor, which errs high -- the safe direction
        # for a fit decision.
        if spans:
            offsets, data_bytes = spans
            ordered = sorted(offsets, key=lambda t: t[1])
            expert = 0
            for i, (t_name, off) in enumerate(ordered):
                nxt = ordered[i + 1][1] if i + 1 < len(ordered) else data_bytes
                if '_exps' in t_name:
                    expert += nxt - off
            facts['expert_bytes'] = expert
            facts['non_expert_bytes'] = data_bytes - expert
    elif arch:
        facts['moe'] = False

    nextn = _by_suffix('nextn_predict_layers')
    if nextn is not None:
        facts['mtp'] = bool(nextn)
        facts['mtp_layers'] = nextn
    elif arch:
        facts['mtp'] = False
    return facts


def llama_gguf_compute_requirements(size_gb: float) -> tuple:
    """(vram_gb, ram_gb) a llama.cpp GGUF of this size needs, fully resident.

    THIS FUNCTION DID NOT EXIST. Nunba's main.py imported it twice --
    `_gguf_install_files` (the quant picker) and the hub-install handler --
    and `git log -S` finds no definition anywhere in either repo's history.
    The import sits unconditionally in the picker, before any fit check, and
    the caller catches only ValueError, so the ImportError propagated: every
    GGUF install through the Model Management page raised before it could
    choose a quant. Proven by calling the picker with a real manifest.

    Defined here because the same two numbers were already being computed
    from bare literals in _populate_llm_models (weights * 1.35, weights *
    2.0). One function now owns "a GGUF of size N needs this much", and
    _MOE_VRAM_OVERHEAD is the same 1.35.

    Fully resident is the DENSE answer, and it is the right default: a dense
    model touches every parameter on every token, so anything not on the GPU
    costs a PCIe round trip per token. A mixture of experts is the exception
    and is handled where the placement is chosen, not here -- see
    moe_offload_args and ModelEntry.matches_compute.
    """
    return (round(size_gb * _MOE_VRAM_OVERHEAD, 1), round(size_gb * 2.0, 1))


def gguf_fits_gpu(free_vram_gb: float, free_ram_gb: float, *,
                  gpu_available: bool, moe: bool = False,
                  vram_need_gb: Optional[float] = None,
                  ram_need_gb: Optional[float] = None,
                  whole_need_gb: Optional[float] = None) -> bool:
    """Can this machine run this GGUF with attention resident on the GPU?

    ONE rule, asked at TWO knowledge levels. It used to be two hand-written
    rules in two repos, and they disagreed: for Tiel-Coder-35B-A3B at 4.7 GB
    free VRAM and 21.4 GB free RAM the install path answered no while the
    selector answered yes -- the install path refusing to fetch the model
    the selector would pick.

    SPLIT KNOWN (``vram_need_gb`` given). The model is downloaded and
    read_gguf_facts has measured it, so the row states what actually goes
    where: attention in VRAM, experts in system RAM under --cpu-moe. Both
    pools must hold for a mixture of experts. A dense model has no split
    and tests VRAM alone -- it touches every parameter on every token, so
    moving any of it to RAM costs a PCIe round trip per token.

    SPLIT UNKNOWN (``whole_need_gb`` given). Pre-download, only the file
    size is knowable, so a MoE is judged on the COMBINED budget -- the
    "fits" figure a GGUF publisher quotes. This is deliberately more
    conservative than the measured rule (it demands 28.6 GB where the truth
    is 3.4 + 18.6) and that asymmetry is kept, not smoothed away: you
    cannot know the split before you have the file, and over-demanding
    picks a smaller quant rather than a model that will not run.

    Returns only whether the GPU arm holds. Callers own their own
    fallbacks -- matches_compute continues down its mode ladder, the
    installer falls back to its RAM-only arm.
    """
    if not gpu_available:
        return False
    if vram_need_gb is not None:
        if free_vram_gb < vram_need_gb:
            return False
        if moe and ram_need_gb is not None and free_ram_gb < ram_need_gb:
            return False
        return True
    if whole_need_gb is None:
        return False
    if free_vram_gb >= whole_need_gb:
        return True
    return bool(moe) and (free_vram_gb + free_ram_gb) >= whole_need_gb


#: Transformer-block count assumed for a PARTIAL offload of a file whose
#: header ``read_gguf_facts`` could not read.  The real count is
#: ``read_gguf_facts()['block_count']``; this is reached only for a file the
#: reader returned {} on.  It is the guess llamacpp_manager applied to EVERY
#: model as ``int(ratio * 40)  # assume ~40 layers``, kept so an unreadable
#: file still launches the way it did before, and named so nothing re-types it.
LAYER_COUNT_FALLBACK = 40


def gguf_partial_offload_layers(free_vram_gb: float, size_gb: float,
                                block_count: Optional[int] = None) -> int:
    """How many blocks of a model that does NOT fit whole go on the GPU.

    The second half of the placement question ``gguf_fits_gpu`` answers the
    first half of, and it lives beside it for the same reason: every piece of
    "how much of this model goes on the GPU" arithmetic has one home, so a
    source guard can keep it there.  Only called after ``gguf_fits_gpu`` said
    no.  At least one block, so a spawn that got this far still uses the card.
    """
    n_layers = block_count or LAYER_COUNT_FALLBACK
    if not size_gb or size_gb <= 0:
        return 1
    return max(1, int(free_vram_gb / size_gb * n_layers))


def moe_offload_args(gguf_path: str, free_vram_gb: float) -> List[str]:
    """llama.cpp flags placing a MoE's experts in system RAM, or [].

    THE one answer to "should this model's experts go to CPU". Three places
    decide how to place a model -- the main server spawn, the caption/draft
    spawn, and model_lifecycle's restart -- and each already carries its own
    copy of `-ngl 99`. A fourth independent answer here is how those got out
    of step; they call this instead.

    Only a mixture of experts qualifies. A dense model touches every
    parameter on every token, so moving any of it off the GPU costs a PCIe
    round trip per token and the user experience collapses. A MoE activates
    8 of 256 experts and keeps attention resident, so the trade is sound.

    Returns [] when the whole model already fits in VRAM: at that point
    keeping the experts on the GPU is strictly faster, and --cpu-moe would
    be giving away performance for nothing.

    Answers from the artifact -- the caller has the path, and the file
    states whether it is a MoE -- so this cannot disagree with the catalog
    row that admitted the model, which was sized from the same read.
    """
    facts = read_gguf_facts(gguf_path)
    if not facts.get('moe'):
        return []
    whole_model_gb = facts.get('weight_bytes', 0) / _GIB * _MOE_VRAM_OVERHEAD
    if free_vram_gb >= whole_model_gb:
        logger.info(
            "%s: MoE fits VRAM whole (%.1f GB free >= %.1f GB); keeping "
            "experts on the GPU", os.path.basename(gguf_path),
            free_vram_gb, whole_model_gb)
        return []
    logger.info(
        "%s: MoE experts to system RAM (--cpu-moe); %.2f GiB of experts "
        "off the GPU, %.2f GiB of attention stays",
        os.path.basename(gguf_path),
        facts.get('expert_bytes', 0) / _GIB,
        facts.get('non_expert_bytes', 0) / _GIB)
    return ['--cpu-moe']


#: Draft depth for multi-token prediction when nothing overrides it.  The
#: value the MTP launch block documented (``--spec-draft-n-max 3``); the
#: live tokens/s + acceptance measurement on a model that fits is what
#: should tune it.
_MTP_DRAFT_N_DEFAULT = 3

#: (abspath, mtime_ns, size) -> whether that llama-server accepts draft-mtp.
#: Keyed on the file's identity so a rebuilt binary is probed again.
_spec_type_cache: Dict[tuple, bool] = {}


def _server_accepts_draft_mtp(server_binary: str) -> bool:
    """Whether THIS llama-server binary accepts ``--spec-type draft-mtp``.

    Asked of the serving binary, once, because an unknown --spec-type makes
    llama-server exit at startup: an MTP model on an older build would go
    from working to dark.  This box carries builds 7909 and 8200 without it
    beside the serving 10330 (hartos-3a, 2026-09-23).  Anything that cannot
    answer -- missing binary, probe timeout -- is "no".
    """
    try:
        st = os.stat(server_binary)
    except (OSError, TypeError):
        return False
    key = (os.path.abspath(server_binary), st.st_mtime_ns, st.st_size)
    if key in _spec_type_cache:
        return _spec_type_cache[key]
    from core.subprocess_safe import run_probe
    res = run_probe([server_binary, '--help'], timeout=20.0)
    ok = bool(res) and 'draft-mtp' in (
        (getattr(res, 'stdout', '') or '') + (getattr(res, 'stderr', '') or ''))
    _spec_type_cache[key] = ok
    return ok


def mtp_spec_args(gguf_path: str, server_binary: str) -> List[str]:
    """llama.cpp flags turning on multi-token prediction, or [].

    THE one answer to "should this spawn use MTP", beside moe_offload_args
    and called at the same spawn sites.  Switched on by the MODEL: the GGUF's
    own nextn_predict_layers (read_gguf_facts()['mtp']), and only on a
    serving binary that accepts the flag.  It used to be an env opt-in that
    was set nowhere, so the MTP preset loaded as a plain MoE (owner,
    2026-09-24: "automatic from model").

    HEVOLVE_LLAMA_MTP_N is an override, not the switch: 0 turns MTP off, N
    sets the draft depth.  It cannot add MTP to a file without the head.

    Live proof (hartos-3a): with MTP inactive llama-server logs "model has
    unused tensor blk.N.nextn.* -- ignoring"; active, that line is gone and
    draft acceptance is reported.
    """
    if not read_gguf_facts(gguf_path).get('mtp'):
        return []
    depth = _MTP_DRAFT_N_DEFAULT
    raw = os.environ.get('HEVOLVE_LLAMA_MTP_N')
    if raw not in (None, ''):
        try:
            depth = int(raw)
        except ValueError:
            logger.warning("HEVOLVE_LLAMA_MTP_N=%r is not an integer; using "
                           "the default draft depth %d", raw, depth)
        if depth <= 0:
            logger.info("%s carries an MTP head; MTP turned off by "
                        "HEVOLVE_LLAMA_MTP_N=%s", os.path.basename(gguf_path), raw)
            return []
    if not _server_accepts_draft_mtp(server_binary):
        logger.warning(
            "%s carries an MTP head but %s does not accept --spec-type "
            "draft-mtp; starting without MTP rather than failing to start",
            os.path.basename(gguf_path), server_binary)
        return []
    logger.info("%s: MTP on (--spec-type draft-mtp, draft depth %d)",
                os.path.basename(gguf_path), depth)
    return ['--spec-type', 'draft-mtp', '--spec-draft-n-max', str(depth)]


def model_weight_bytes(file_name: str) -> Optional[int]:
    """Size of a model's weight file in BYTES, or None if unregistered.

    The single reader of MODEL_WEIGHT_BYTES. Returns bytes because bytes is
    the one unit that needs no divisor and can carry no ambiguity; callers
    that want GiB or MiB divide once, at the point of comparison, against a
    figure whose unit they can state.

    None (not 0) for an unknown file: a missing size must be visible to the
    caller, because 0 silently "fits" every budget check in the codebase.
    """
    row = MODEL_WEIGHT_BYTES.get(file_name)
    return row[0] if row else None


def model_weight_provenance(file_name: str) -> Optional[str]:
    """Where a registered weight size came from — 'measured …' or 'estimate …'.

    Kept beside the number rather than in a comment so a caller (or a test)
    can tell a stat'd fact from a preserved guess at runtime.
    """
    row = MODEL_WEIGHT_BYTES.get(file_name)
    return row[1] if row else None


# Download sources
SOURCES = {
    'huggingface': 'HuggingFace Hub',
    'ollama':      'Ollama registry',
    'github':      'GitHub release',
    'pip':         'Python package (pip)',
    'api':         'Remote API (no download)',
    'local':       'Already on disk',
    'custom_url':  'Custom download URL',
}


@dataclass
class ModelEntry:
    """Universal model descriptor — works for any model type."""

    # ── Identity ──────────────────────────────────────────────────
    id: str                              # Unique slug: "qwen3.5-4b-vl", "chatterbox-turbo"
    name: str                            # Human-readable display name
    model_type: str                      # Key from MODEL_TYPES
    version: str = '1.0'                 # Semver or commit hash

    # ── Source & Files ────────────────────────────────────────────
    source: str = 'huggingface'          # Key from SOURCES
    repo_id: str = ''                    # HuggingFace repo, Ollama model name, pip package
    files: Dict[str, str] = field(default_factory=dict)
    download_url: str = ''               # For custom_url source
    # Where the weights actually landed. The loader that fetched them sets
    # it; everything that needs to READ the artifact goes through here.
    #
    # This field is not new in intent, only in existence. LlamaInstaller
    # .get_model_path already documents "1. Canonical ModelCatalog entry by
    # display name -- if HARTOS has the model registered as installed with a
    # local_path, use that", and that branch has never run: ModelEntry had
    # no local_path, so the lookup fell through to a filename walk across
    # ~/.nunba, ~/.trueflow, ~/.ollama and the HF cache on every call.
    local_path: str = ''

    # ── Compute Requirements ──────────────────────────────────────
    vram_gb: float = 0.0                 # GPU VRAM needed (0 = CPU-capable)
    ram_gb: float = 1.0                  # System RAM needed
    disk_gb: float = 0.0                 # Disk space for model files
    min_capability_tier: str = 'lite'    # 'lite', 'standard', 'full'

    # ── Runtime ───────────────────────────────────────────────────
    backend: str = 'torch'               # Key from BACKENDS
    supports_gpu: bool = True
    supports_cpu: bool = True
    supports_cpu_offload: bool = False
    cpu_offload_method: str = 'none'     # 'torch_to_cpu', 'restart_cpu', 'none'
    idle_timeout_s: float = 600.0
    min_build: Optional[int] = None

    # ── Capabilities (generic key-value) ──────────────────────────
    capabilities: Dict[str, Any] = field(default_factory=dict)

    # ── Selection metadata ────────────────────────────────────────
    quality_score: float = 0.5
    speed_score: float = 0.5
    cost_per_1k: float = 0.0
    priority: int = 50

    # ── Routing (for TTS/STT language-based routing) ──────────────
    languages: List[str] = field(default_factory=list)
    language_priority: Dict[str, int] = field(default_factory=dict)

    # ── State (runtime, NOT persisted to JSON) ────────────────────
    downloaded: bool = False
    loaded: bool = False
    device: str = 'unloaded'
    active_since: Optional[float] = None
    error: Optional[str] = None

    # ── Tags for filtering ────────────────────────────────────────
    tags: List[str] = field(default_factory=list)

    # ── User-configurable flags ───────────────────────────────────
    enabled: bool = True
    auto_load: bool = False
    pinned: bool = False
    purposes: List[str] = field(default_factory=list)  # e.g. ['draft', 'main', 'caption']

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict (excludes runtime state)."""
        d = asdict(self)
        for key in ('downloaded', 'loaded', 'device', 'active_since', 'error'):
            d.pop(key, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'ModelEntry':
        """Deserialize from JSON dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    # Backends whose models are FETCHED as weight files.  Only these need
    # the download fields below; an `api` / `in_process` model has nothing
    # to download and must not be blocked by the rule.
    _DOWNLOADED_BACKENDS = ('llama.cpp',)

    def validate(self) -> list:
        """Return a list of reasons this entry can never work, [] if sound.

        Registering an entry that cannot be downloaded is worse than
        refusing it: the caller sees success, the row persists, and the
        failure only surfaces later somewhere unrelated.

        2026-08-15 live: an entry was accepted with files={} and
        repo_id='unsloth/Qwen3.8-27B-UD-Q4_K_XL.gguf' (a FILE name, not a
        repo).  POST /api/admin/models returned 200 {"success": true} and
        persisted it; the download then failed for the rest of the
        install's life with

            LLM download: no preset for Qwen3.8-27B-UD-Q4_K_XL.gguf

        because Nunba's models/orchestrator.py::_entry_to_preset returns
        None precisely when files['model'] is empty.  This method moves
        that consumer-side requirement up to the producer, so the same
        invariant is enforced once, wherever an entry is created (admin
        register, hub install, catalog populator).
        """
        problems = []
        if not self.id:
            problems.append('id is required')
        if not self.model_type:
            problems.append('model_type is required')

        if self.backend in self._DOWNLOADED_BACKENDS:
            if not (self.files or {}).get('model'):
                problems.append(
                    f"files['model'] is required for backend "
                    f"'{self.backend}' - without the weight file name the "
                    f"model can never be downloaded or loaded")
            # A HuggingFace repo id is 'org/repo'.  It never names a
            # weight file, so a '.gguf' suffix means the file name was
            # pasted where the repo belongs and can never resolve.
            repo = (self.repo_id or '').strip()
            if repo.lower().endswith(('.gguf', '.bin', '.safetensors')):
                problems.append(
                    f"repo_id '{repo}' looks like a FILE, not a repository "
                    f"('org/repo') - put the file name in files['model']")
        return problems

    def matches_compute(self, budget_vram_gb: float, budget_ram_gb: float,
                        gpu_available: bool) -> str:
        """Check if this model can run given current compute.

        Returns: 'gpu', 'cpu', 'cpu_offload', or 'impossible'
        """
        # The GPU arm is gguf_fits_gpu's to answer -- the same function the
        # installer asks, so selection and install cannot drift apart. This
        # row is downloaded, so it passes the MEASURED split: vram_gb is the
        # non-expert weights, ram_gb the experts that --cpu-moe puts in
        # system RAM. Both must hold for a MoE, because the experts are
        # mmapped and a shortfall does not fail the load, it silently
        # destroys throughput (measured: 0.95 tok/s with 18.64 GiB of
        # experts against ~6.5 GiB free). A dense model never sets the moe
        # capability and still tests VRAM alone.
        if gguf_fits_gpu(budget_vram_gb, budget_ram_gb,
                         gpu_available=gpu_available,
                         moe=bool((self.capabilities or {}).get('moe')),
                         vram_need_gb=self.vram_gb,
                         ram_need_gb=self.ram_gb):
            return 'gpu'
        if self.supports_cpu_offload and gpu_available and budget_vram_gb >= self.vram_gb * 0.5:
            return 'cpu_offload'
        if self.supports_cpu and budget_ram_gb >= self.ram_gb:
            return 'cpu'
        return 'impossible'


class ModelCatalog:
    """Central registry of all models across all subsystems.

    JSON-persisted. Thread-safe for concurrent reads; write-locked for mutations.

    Subsystem population is pluggable: call register_populator() to add
    a callback that discovers models from a subsystem (LLM presets, TTS engines,
    etc.). This avoids hard dependencies on application-layer modules.
    """

    def __init__(self, catalog_path: Optional[str] = None):
        try:
            from core.platform_paths import get_db_dir
            data_dir = Path(get_db_dir())
        except ImportError:
            data_dir = Path.home() / 'Documents' / 'Nunba' / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = Path(catalog_path) if catalog_path else data_dir / 'model_catalog.json'
        self._entries: Dict[str, ModelEntry] = {}
        self._lock = threading.Lock()
        self._dirty = False
        #: Ids a populator has emitted or claimed during the CURRENT
        #: populate_from_subsystems run; None outside one.  See
        #: `already_registered` for why claiming exists.
        self._claimed_this_run: Optional[set] = None
        self._populators: List = []  # list of (name, callable)
        self._load()

    # ── Populator registration ─────────────────────────────────────

    def register_populator(self, name: str, fn) -> None:
        """Register a subsystem populator callback.

        The callback receives the catalog as its only argument and should call
        catalog.register(entry, persist=False) for each model it discovers.
        It must return the count of new entries added.
        """
        self._populators.append((name, fn))

    # ── CRUD ──────────────────────────────────────────────────────

    def register(self, entry: ModelEntry, persist: bool = True) -> None:
        """Add or update a model entry."""
        with self._lock:
            self._entries[entry.id] = entry
            self._dirty = True
            if self._claimed_this_run is not None:
                # Inside a populate: whoever registered this still owns it,
                # so the stale sweep must not treat it as abandoned.  An
                # overwrite of an existing id changes nothing about the key
                # set, which is how the sweep came to delete entries the
                # same run had just re-emitted.
                self._claimed_this_run.add(entry.id)
        if persist:
            self._save()
        logger.info(f"Registered model: {entry.id} ({entry.model_type}, {entry.backend})")

    def unregister(self, model_id: str, persist: bool = True) -> bool:
        """Remove a model entry. Returns True if found."""
        with self._lock:
            removed = self._entries.pop(model_id, None)
            if removed:
                self._dirty = True
        if removed and persist:
            self._save()
            logger.info(f"Unregistered model: {model_id}")
        return removed is not None

    def override(self, model_id: str, *, persist: bool = False, **fields) -> bool:
        """Apply field-level overrides to an already-registered entry.

        Use this when one populator needs to narrow another populator's
        entry (e.g. Nunba's populate_media_gen amending HARTOS's fallback
        audio_gen-acestep surface). Unlike direct ``entry.field = value``
        mutation, override() takes the catalog lock, validates field names
        against the ModelEntry dataclass, sets the dirty flag, and logs
        the change — so the single-writer semantics of register/unregister
        extend to cross-populator amendments.

        Unknown fields raise ValueError. Returns False if model_id is not
        registered (no-op). Defaults to persist=False because overrides
        typically happen during populator boot (same convention as
        register(persist=False)).
        """
        allowed = set(ModelEntry.__dataclass_fields__)
        unknown = [k for k in fields if k not in allowed]
        if unknown:
            raise ValueError(
                f"override(): unknown field(s) for ModelEntry: {sorted(unknown)}",
            )
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return False
            for key, value in fields.items():
                setattr(entry, key, value)
            self._dirty = True
        if persist:
            self._save()
        logger.info(
            f"Overrode model {model_id} fields: {sorted(fields.keys())}",
        )
        return True

    def get(self, model_id: str) -> Optional[ModelEntry]:
        """Get a model by ID."""
        return self._entries.get(model_id)

    def already_registered(self, model_id: str) -> bool:
        """Whether ``model_id`` is in the catalogue -- the question a
        populator asks about an entry it has emitted before.

        Asking during a populate run also CLAIMS the entry.  A populator
        that skips an id to preserve the owner's admin-UI edits still owns
        it, and without the claim the sweep below reads the skip as
        abandonment: `touched_this_boot` is `ids_after - ids_before`, which
        holds only NEW entries, so anything that already existed was
        deleted by the very run that was populating it.  MEASURED
        2026-09-21 on the owner's 40-entry catalogue: one populate added 18
        and removed 9, among them every real TTS engine.

        Outside a populate run it is just a question and records nothing,
        so no caller elsewhere can accidentally protect an entry.
        """
        present = self._entries.get(model_id) is not None
        if present and self._claimed_this_run is not None:
            self._claimed_this_run.add(model_id)
        return present

    def list_all(self) -> List[ModelEntry]:
        """All registered models."""
        return list(self._entries.values())

    def list_types(self) -> List[str]:
        """All distinct model types that have at least one enabled entry."""
        return list({e.model_type for e in self._entries.values() if e.enabled})

    def list_by_type(self, model_type: str) -> List[ModelEntry]:
        """All models of a given type (e.g. 'tts', 'llm')."""
        return [e for e in self._entries.values()
                if e.model_type == model_type and e.enabled]

    def list_by_tag(self, tag: str) -> List[ModelEntry]:
        """All models with a given tag."""
        return [e for e in self._entries.values() if tag in e.tags]

    # ── Compute-aware selection ───────────────────────────────────

    def select_best(self, model_type: str, budget_vram_gb: float = 0,
                    budget_ram_gb: float = 4, gpu_available: bool = False,
                    language: Optional[str] = None,
                    require_capability: Optional[Dict[str, Any]] = None,
                    exclude: Optional[List[str]] = None,
                    ) -> Optional[ModelEntry]:
        """Select the best model of a given type for current compute.

        Selection priority:
          1. Filter by type + enabled + compute fit + capability tier
          2. If language specified, prefer models that serve it
          3. Sort by quality_score * speed_score * priority
          4. Return top pick

        ``exclude`` — set of model_ids to skip (used by fallback walks
        when a previously-selected entry just failed synth/load).  None
        or empty list means "no exclusions" (default).
        """
        candidates = self.list_by_type(model_type)

        # Fallback exclusion — caller-supplied IDs are filtered before
        # any scoring so the second-best engine surfaces cleanly when
        # the primary fails at runtime (e.g. TTS engine raised, walk
        # to the next entry in language_priority order).
        if exclude:
            _exclude_set = set(exclude)
            candidates = [e for e in candidates if e.id not in _exclude_set]

        # Get current capability tier to enforce min_capability_tier
        current_tier = self._get_capability_tier()

        # Filter by compute fit + capability tier
        scored = []
        for entry in candidates:
            # Capability tier gate
            if not self._tier_sufficient(current_tier, entry.min_capability_tier):
                continue

            # Already-loaded models always fit — they're using resources we
            # already allocated, so never skip them due to budget calculations
            if entry.loaded:
                fit = entry.device or 'cpu'
            else:
                fit = entry.matches_compute(budget_vram_gb, budget_ram_gb, gpu_available)
                if fit == 'impossible':
                    continue

            score = entry.quality_score * 100 + entry.priority

            if fit == 'gpu':
                score += 200
            elif fit == 'cpu_offload':
                score += 100

            if language and entry.languages:
                if language in entry.languages:
                    lang_prio = entry.language_priority.get(language, 50)
                    # Language preference is dominant — rank 0 (preferred engine
                    # for this language) gets +300, rank 1 gets +270, default +150.
                    # This ensures tts_router's LANG_ENGINE_PREFERENCE order wins
                    # over small quality_score differences between engines.
                    score += (300 - lang_prio * 3)
                else:
                    score -= 500

            if require_capability:
                cap_match = all(
                    entry.capabilities.get(k) == v
                    for k, v in require_capability.items()
                )
                if not cap_match:
                    continue

            if entry.downloaded:
                score += 50

            # Strongly prefer already-loaded models — avoids downloading a
            # second model when one of the same type is already running
            if entry.loaded:
                score += 1000

            scored.append((score, fit, entry))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_fit, best = scored[0]
        logger.info(f"Selected {best.id} ({best.model_type}) — "
                    f"fit={best_fit}, score={best_score:.0f}")
        return best

    def select_all_fitting(self, model_type: str, budget_vram_gb: float = 0,
                           budget_ram_gb: float = 4, gpu_available: bool = False,
                           ) -> List[tuple]:
        """Return all fitting models with their run modes, sorted by score."""
        candidates = self.list_by_type(model_type)
        result = []
        for entry in candidates:
            fit = entry.matches_compute(budget_vram_gb, budget_ram_gb, gpu_available)
            if fit != 'impossible':
                result.append((entry, fit))
        result.sort(key=lambda x: x[0].quality_score * 100 + x[0].priority, reverse=True)
        return result

    # ── State updates ─────────────────────────────────────────────

    def mark_downloaded(self, model_id: str, downloaded: bool = True,
                        local_path: Optional[str] = None) -> None:
        """Mark a row downloaded, and — when we know where the file landed —
        record what the file SAYS about itself.

        ``local_path`` is optional and defaults to the old behaviour
        exactly, so every existing caller is unchanged. It is persisted on
        the row, which is what makes LlamaInstaller.get_model_path's
        documented "canonical catalog lookup first" branch able to run at
        all — it reads entry.local_path, a field that until now did not
        exist, so that lookup always fell through to a filename walk across
        ~/.nunba, ~/.trueflow, ~/.ollama and the HF cache.

        A caller that does not pass one falls back to whatever the row
        already holds, so the loader that fetched the weights can record
        the path itself and every later call benefits without threading it
        through. The row then gains architecture facts read FROM the
        artifact rather than typed into a table: moe / experts_used /
        experts_total / mtp and the expert split.

        Engine-agnostic by construction: read_gguf_facts returns {} for
        anything that is not a GGUF, so a torch, onnx or sidecar model
        records its path and learns nothing further, which is exactly
        right. No per-backend code.

        For a DENSE model it only ADDS capability keys and never touches
        vram_gb, ram_gb, disk_gb, priority or the scores, so no dense
        selection can move because of this call.

        For a MIXTURE OF EXPERTS it also corrects vram_gb and ram_gb,
        because the inherited figures describe a placement that nobody
        would ever use. ``vram_gb = weights * 1.35`` assumes every weight
        sits in VRAM, and for a MoE that is the one thing you would not do:
        the experts belong in system RAM (--cpu-moe) while attention stays
        on the GPU. For Tiel-Coder-35B-A3B the inherited row claimed 28.6 GB
        of VRAM against a measured 2.87 GiB -- about 11x, and the reason a
        machine that can run the model is told it cannot.

        The constants are taken from that measurement, not invented:
        loading it with ``-ngl 99 --cpu-moe`` moved GPU use from 3321 to
        6256 MiB, i.e. 2.87 GiB for 2.53 GiB of non-expert weights plus KV
        cache at ctx 4096 -- a ratio of 1.135. _MOE_VRAM_OVERHEAD keeps the
        1.35 the dense path already uses, which is comfortably above that
        and leaves room for a larger context. ram_gb becomes the expert
        bytes at 1.0: they are mmapped rather than copied, so what matters
        is that they can stay RESIDENT, and the same run proved what
        happens when they cannot -- 0.95 tokens/sec.
        """
        entry = self._entries.get(model_id)
        if not entry:
            return
        persisted_before = entry.to_dict()
        try:
            self._apply_download(entry, downloaded, local_path)
        finally:
            # local_path, the facts and the MoE sizing are persisted fields;
            # `downloaded` is runtime state (to_dict drops it).  Save only on
            # a real change: boot calls this with a bare id per model, and
            # rewriting the whole catalog each time for nothing is waste.
            if entry.to_dict() != persisted_before:
                self._dirty = True
                self._save()

    @staticmethod
    def _apply_download(entry: 'ModelEntry', downloaded: bool,
                        local_path: Optional[str]) -> None:
        """mark_downloaded's in-memory half (it owns persistence)."""
        model_id = entry.id
        entry.downloaded = downloaded
        if local_path:
            entry.local_path = str(local_path)
        path = local_path or entry.local_path
        if downloaded and path:
            facts = read_gguf_facts(path)
            if facts:
                entry.capabilities = {**(entry.capabilities or {}), **facts}
                logger.info(
                    "%s: read from the file -- arch=%s moe=%s experts=%s/%s "
                    "mtp=%s bytes=%s", model_id, facts.get('architecture'),
                    facts.get('moe'), facts.get('experts_used'),
                    facts.get('experts_total'), facts.get('mtp'),
                    facts.get('weight_bytes'))
                # Gated on EXPERT bytes, not non-expert: a MoE whose tensor
                # table could not be read reports expert_bytes == 0 and a
                # non_expert_bytes of whatever trailed the header, which
                # would rewrite vram_gb to ~0 and make the model look free.
                # No measured split means no correction.
                if facts.get('moe') and facts.get('expert_bytes'):
                    was = (entry.vram_gb, entry.ram_gb)
                    entry.vram_gb = round(
                        facts['non_expert_bytes'] / _GIB
                        * _MOE_VRAM_OVERHEAD, 1)
                    entry.ram_gb = round(facts['expert_bytes'] / _GIB, 1)
                    logger.info(
                        "%s: MoE placement -- vram %.1f -> %.1f GB (non-expert "
                        "weights only), ram %.1f -> %.1f GB (experts, which "
                        "must stay resident)", model_id, was[0], entry.vram_gb,
                        was[1], entry.ram_gb)

    def get_by_weight_file(self, weight_file: str) -> Optional['ModelEntry']:
        """The entry whose ``files['model']`` is this weight file, or None.

        A KEY, not a fifth heuristic. Four places already match a preset to
        an entry and they use three different rules -- name-only, name-or-
        file, and name-or-substring-of-id (see #112) -- so a caller holding
        only a path would otherwise have to pick one and add a fourth. A
        weight file is unambiguous where a display name is not: the name can
        drift, be re-cased or be edited by the admin UI, while the file is
        what the loader actually opens.

        Matches on the BASENAME, because callers hold a full path (the
        spawn) or a bare name (the catalog row), and the row stores the
        bare name.

        Returns None when two rows claim the same file. The catalog has
        known self-duplicate pairs (#107), and "I do not know which" is the
        honest answer -- guessing would attach a measurement to the wrong
        model, which is worse than having none.
        """
        name = os.path.basename(str(weight_file or '').strip())
        if not name:
            return None
        hits = [e for e in self._entries.values()
                if (e.files or {}).get('model') == name]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            logger.info(
                "get_by_weight_file(%r): %d rows claim this file (%s); "
                "answering 'unknown' rather than picking one", name,
                len(hits), ', '.join(e.id for e in hits))
        return None

    def record_residency(self, model_id: str, vram_gb: Optional[float] = None,
                         ram_gb: Optional[float] = None,
                         weight_file: Optional[str] = None) -> bool:
        """Record what THIS model actually took when we brought it up.

        The reclaim figure has to come from what we started, not from the
        driver and not from a slot. Measured: `nvidia-smi
        --query-compute-apps=pid,used_memory` returns [N/A] on Windows
        WDDM, so per-process VRAM is not obtainable from hardware at all.
        And vram_manager's ledger is keyed by TOOL -- VRAM_BUDGETS holds
        "acestep"/"wan2gp"/..., record_actual_usage takes the GPUWorker's
        name -- so every model loading into the `llm` slot overwrites one
        entry. That makes can_fit('llm') unable to tell "can the 4B fit"
        from "can the 35B fit", and leaves the reclaim figure describing
        whichever model happened to load last.

        This record is keyed by MODEL and lives on the model's own row, so
        it sits beside the PREDICTED facts read_gguf_facts took from the
        file. Predicted and observed, one place, per model.

        ``weight_file`` is recorded and checked on read because one
        catalog row can be re-pointed at a different quant, and a Q4 and a
        Q8 of the same repo have wildly different footprints. A record
        that describes a quant the row no longer uses is worse than no
        record.

        Callers obtain the numbers by DELTA around the spawn -- free
        memory before, free memory once the server answers -- which is how
        the 2.87 GiB figure for Tiel-Coder was measured. Nothing else can
        supply it on this platform.

        Returns True when something was recorded.
        """
        entry = self._entries.get(model_id)
        if entry is None:
            logger.info("record_residency: %r is not in the catalog",
                        model_id)
            return False
        if vram_gb is None and ram_gb is None:
            return False

        rec = dict(entry.capabilities.get('residency') or {})
        # A negative or absurd delta means something else moved during the
        # measurement window. Drop it rather than poison the record: the
        # next load measures again, while a bad number would be persisted
        # and planned against.
        for key, val in (('vram_gb', vram_gb), ('ram_gb', ram_gb)):
            if val is None:
                continue
            try:
                gb = float(val)
            except (TypeError, ValueError):
                continue
            if gb <= 0 or gb > 512:
                logger.info("record_residency: %s %s=%r out of range; "
                            "ignored", model_id, key, val)
                continue
            rec[key] = round(gb, 2)
        if not rec:
            return False

        rec['weight_file'] = (weight_file
                              or (entry.files or {}).get('model') or '')
        rec['at'] = time.time()
        entry.capabilities = {**(entry.capabilities or {}),
                              'residency': rec}
        self._dirty = True
        # The next swap plans from this without re-measuring, which it can
        # only do if the record outlives the process (#110: it never reached
        # the disk on its own).
        self._save()
        logger.info("%s: residency recorded -- vram %s GB, ram %s GB (%s)",
                    model_id, rec.get('vram_gb'), rec.get('ram_gb'),
                    rec['weight_file'] or 'no weight file')
        return True

    def residency(self, model_id: str) -> Optional[dict]:
        """What this model took last time we brought it up, or None.

        None means "not known" and callers fall back to the catalog
        estimate -- never to zero. A model whose footprint is unknown is
        not a model that is free.

        Returns None when the row now points at a DIFFERENT weight file
        than the record was taken against: the row was re-pointed at
        another quant and the old number describes weights that are no
        longer there.
        """
        entry = self._entries.get(model_id)
        if entry is None:
            return None
        rec = (entry.capabilities or {}).get('residency')
        if not rec:
            return None
        current = (entry.files or {}).get('model') or ''
        recorded = rec.get('weight_file') or ''
        if current and recorded and current != recorded:
            logger.info(
                "%s: residency record is for %r but the row now uses %r; "
                "treating as unknown", model_id, recorded, current)
            return None
        return dict(rec)

    def mark_loaded(self, model_id: str, device: str = 'gpu') -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.loaded = True
            entry.device = device
            entry.active_since = time.time()
            entry.error = None

    def mark_unloaded(self, model_id: str) -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.loaded = False
            entry.device = 'unloaded'
            entry.active_since = None

    def mark_error(self, model_id: str, error: str) -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.error = error
            entry.loaded = False

    # ── Purpose assignment ──────────────────────────────────────

    # Universal purpose list — every task Nunba supports.  A single model
    # can serve ANY combination (e.g. Qwen3.5-0.8B → ['draft','caption',
    # 'grounding']; Qwen3-4B-Omni → ['main','tts','stt']).
    # Purposes are NOT gated by model_type — model capabilities drive it,
    # not an artificial type label.
    ALL_PURPOSES: List[str] = [
        'draft',        # Fast classifier / speculative decode LLM
        'main',         # Primary LLM for chat/reasoning
        'vision',       # Image understanding (VLM — generic)
        'caption',      # Image/video captioning (VLM)
        'grounding',    # GUI element grounding (VLM — click targets)
        'tts',          # Text-to-speech
        'stt',          # Speech-to-text / ASR
        'diarization',  # Speaker segmentation
        'vad',          # Voice activity detection
        'embedding',    # Text embeddings (retrieval, RAG)
        'rerank',       # Cross-encoder reranking for retrieval
        'ocr',          # Text extraction from images
        'music',        # Music generation
        'image-gen',    # Text-to-image
        'video-gen',    # Text-to-video
        'translate',    # Machine translation (when dedicated model)
    ]

    def get_by_purpose(self, purpose: str) -> Optional[ModelEntry]:
        """Return the model assigned to *purpose*, or None."""
        for entry in self._entries.values():
            if purpose in entry.purposes and entry.enabled:
                return entry
        return None

    def set_purpose(self, model_id: str, purpose: str, enabled: bool = True) -> bool:
        """Toggle a purpose on/off for a model.

        When enabling: clears the same purpose from any other model
        (one model per purpose globally), then adds it.
        When disabling: removes the purpose from this model.
        Persists to disk.  Returns True on success.
        """
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return False
            if purpose not in self.ALL_PURPOSES:
                return False
            if enabled:
                # Clear the same purpose from any other model
                for other in self._entries.values():
                    if other.id != model_id and purpose in other.purposes:
                        other.purposes = [p for p in other.purposes if p != purpose]
                if purpose not in entry.purposes:
                    entry.purposes.append(purpose)
            else:
                entry.purposes = [p for p in entry.purposes if p != purpose]
            self._dirty = True
        self._save()
        logger.info(f"Model {model_id} purpose {purpose!r} {'enabled' if enabled else 'disabled'} "
                    f"→ purposes={entry.purposes}")
        return True

    # ── Auto-populate from registered subsystem populators ─────────

    def populate_from_subsystems(self) -> int:
        """Run all registered populators + built-in STT/VLM entries.

        Called on first run or when catalog is empty. Does NOT overwrite
        existing entries (user edits via admin UI are preserved).
        Returns number of new entries added.
        """
        # Snapshot IDs BEFORE populator run so we can detect stale auto-entries
        ids_before = set(self._entries.keys())

        added = 0
        # Collect what the populators emit or claim, for the sweep below.
        self._claimed_this_run = set()
        try:
            # Run application-registered populators (LLM, TTS, etc.)
            for name, fn in self._populators:
                try:
                    count = fn(self)
                    added += count
                    if count:
                        logger.info(f"Populator '{name}' added {count} entries")
                except Exception as e:
                    logger.debug(f"Populator '{name}' failed: {e}")
            # Built-in entries that don't depend on application modules
            added += self._populate_llm_models()
            added += self._populate_tts_models()
            added += self._populate_stt_models()
            added += self._populate_vlm_models()
            added += self._populate_embodied_models()
            added += self._populate_videogen_models()
            added += self._populate_audiogen_models()
            claimed = self._claimed_this_run
        finally:
            # Claims never outlive the run that made them, or an entry
            # abandoned later would stay protected by a stale claim.
            self._claimed_this_run = None

        # Cleanup: remove stale auto-entries that no populator emitted this boot.
        # An entry is "auto-populated" if its ID starts with a known prefix and
        # it wasn't modified by the user (no custom tags, no non-default purposes,
        # not pinned).  Stale = prefix-matched but not re-registered this boot.
        ids_after = set(self._entries.keys())
        # Everything a populator emitted (register) or claimed
        # (already_registered) this run, NEW OR NOT.  It used to be
        # `ids_after - ids_before`, which holds only new entries: an entry
        # that already existed was never in it, whether its populator
        # re-registered it (register overwrites in place, so the key set
        # does not change) or deliberately skipped it to preserve the
        # owner's admin-UI edits.  Either way the sweep read "still owned"
        # as "abandoned" and deleted it.  MEASURED 2026-09-21 on the
        # owner's catalogue: one populate added 18 entries and removed 9,
        # including every TTS engine and vlm-minicpm-v2.
        touched_this_boot = (ids_after - ids_before) | claimed
        AUTO_PREFIXES = ('tts-', 'stt-', 'vlm-', 'video_gen-', 'audio_gen-', 'embodied-')
        stale = []
        for eid, entry in list(self._entries.items()):
            if eid in touched_this_boot:
                continue  # freshly registered this boot
            if not any(eid.startswith(p) for p in AUTO_PREFIXES):
                continue  # not an auto-prefix (e.g. llm-* user-registered)
            if entry.pinned or entry.purposes or (entry.tags and set(entry.tags) - {'local', 'tts', 'stt', 'vision', 'cpu-friendly'}):
                continue  # user customized — preserve
            stale.append(eid)

        if stale:
            for eid in stale:
                self._entries.pop(eid, None)
                self._dirty = True
            logger.info(f"Cleaned {len(stale)} stale auto-entries: {stale}")

        if added > 0 or stale:
            self._save()
            logger.info(f"Auto-populated {added} entries, cleaned {len(stale)} stale")
        return added

    def _populate_tts_models(self) -> int:
        """Populate TTS engine entries from tts_router.ENGINE_REGISTRY.

        Lazy-imports populate_tts_catalog to avoid circular imports at
        module load time. tts_router → model_catalog direction is only
        present inside function bodies (never at module scope).
        """
        try:
            from integrations.channels.media.tts_router import populate_tts_catalog
            return populate_tts_catalog(self)
        except Exception as e:
            logger.debug(f"TTS catalog population skipped: {e}")
            return 0

    def _populate_stt_models(self) -> int:
        """STT model entries — delegated to whisper_tool.populate_stt_catalog().

        whisper_tool is the single source of truth for STT model specs
        (engine names, VRAM thresholds, sherpa-onnx archive mappings).
        Falls back to a minimal inline set if whisper_tool is unavailable.
        """
        try:
            from integrations.service_tools.whisper_tool import populate_stt_catalog
            return populate_stt_catalog(self)
        except Exception as e:
            logger.debug(f"STT catalog population via whisper_tool skipped: {e}")

        # Minimal fallback (whisper_tool not yet importable at catalog init time)
        added = 0
        _fallback = [
            ('stt-whisper-base',   'Whisper Base (faster-whisper)',      0.2, 0.5,  0.75, 0.9),
            ('stt-whisper-medium', 'Whisper Medium (faster-whisper)',    1.5, 2.0,  0.85, 0.7),
            ('stt-whisper-large',  'Whisper Large v3 (faster-whisper)', 3.0, 4.0,  0.93, 0.5),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            # Claiming skip: an entry no populator claims is swept as
            # stale at the end of populate_from_subsystems.
            if self.already_registered(mid):
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.STT,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='torch', supports_gpu=vram > 0, supports_cpu=True,
                supports_cpu_offload=True, cpu_offload_method='torch_to_cpu',
                idle_timeout_s=300,
                capabilities={'realtime': True, 'diarization': False,
                              'multilingual': True},
                quality_score=quality, speed_score=speed,
                languages=['multilingual'],
                tags=['local', 'stt', 'cpu-friendly'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_llm_models(self) -> int:
        """Chat/LLM entries — the ladder every hardware-based recommendation reads.

        This catalog is the SINGLE SOURCE OF TRUTH for which chat models exist.
        Before this existed the LLM rung was missing (populate_from_subsystems
        seeded tts/stt/vlm/embodied/videogen/audiogen and skipped llm), so three
        ad-hoc lists grew to fill the gap and drifted apart: model_onboarding's
        MODEL_TIERS still named Qwen2.5 while agent_engine/model_registry.py had
        moved to Qwen3.5, and Nunba kept a fourth list of its own. Anything that
        needs "which chat model suits this box" reads THIS, and nothing else
        hardcodes a ladder.

        Sizing carries BOTH budgets on purpose. vram_gb gates the GPU path and
        ram_gb gates the CPU path, because a box with no GPU but plenty of RAM
        can still run a mid-size model -- the VRAM-only ladder this replaces
        collapsed every CPU-only machine to the smallest entry regardless of how
        much RAM it had.

        repo_id values are taken from core/hub_allowlist.py, so every entry here
        is already download-allowlisted; adding a model means adding it there
        too, and the allowlist stays the security boundary.

        Extending: append an entry. Selection is data-driven (budget vs
        vram_gb/ram_gb, ranked by priority) so no code changes to add a family.
        """
        # Rows are DOWNLOAD-COMPLETE on purpose: repo_id alone is not enough to
        # fetch a GGUF, so each carries the exact file name and, for the VL
        # models, the mmproj projector. mmproj has TWO names because the file is
        # usually published as mmproj-F16.gguf and must be stored under a
        # model-specific name locally or the second model overwrites the first.
        # Tiel-Coder is the exception: its upstream projector is BF16-only.
        #
        # Sourced from Nunba's llama/llama_installer.py MODEL_PRESETS, which is
        # what actually downloads today. NOT from core/hub_allowlist.py: that is
        # a security allowlist and lists transformers repos (google/gemma-2b-it)
        # that contain no GGUF at all, so seeding from it produced rows the
        # llama_cpp backend could never load. Gemma is therefore absent here
        # until a GGUF repo for it is allowlisted; a row that cannot download is
        # worse than no row.
        #
        # vram_gb/ram_gb are derived from weight size: GPU needs the weights
        # plus ~35% for KV cache and context, CPU needs roughly double the
        # weights to stay comfortable. Extending: add a row.
        MIN_BUILD_QWEN35 = 8148          # llama.cpp b8148+ required by Qwen3.5
        # (id, name, repo, gguf, mmproj|None, tier, prio, quality,
        #  speed, purposes, min_build)
        #
        # No size column. Weight sizes live in MODEL_WEIGHT_BYTES at the top of
        # this module, keyed by the gguf name already in each row — this table
        # used to restate them, and Nunba's MODEL_PRESETS restated them again.
        _llms = [
            ('llm-qwen3.5-0.8b', 'Qwen3.5 0.8B VL', 'unsloth/Qwen3.5-0.8B-GGUF',
             'Qwen3.5-0.8B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-0.8B-F16.gguf',
             'lite', 30, 0.45, 0.95, ['draft'], MIN_BUILD_QWEN35),
            ('llm-qwen3-2b-text', 'Qwen3 2B (text only)',
             'unsloth/Qwen3-2B-Instruct-GGUF', 'Qwen3-2B-Instruct-Q4_K_M.gguf',
             None, 'lite', 35, 0.50, 0.88, ['main'], None),
            ('llm-qwen3.5-2b', 'Qwen3.5 2B VL', 'unsloth/Qwen3.5-2B-GGUF',
             'Qwen3.5-2B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-2B-F16.gguf',
             'lite', 45, 0.55, 0.85, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-4b', 'Qwen3.5 4B VL', 'unsloth/Qwen3.5-4B-GGUF',
             'Qwen3.5-4B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-4B-F16.gguf',
             'standard', 60, 0.60, 0.70, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-9b', 'Qwen3.5 9B VL', 'unsloth/Qwen3.5-9B-GGUF',
             'Qwen3.5-9B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-9B-F16.gguf',
             'standard', 70, 0.72, 0.50, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-27b', 'Qwen3.5 27B VL', 'unsloth/Qwen3.5-27B-GGUF',
             'Qwen3.5-27B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-27B-F16.gguf',
             'full', 80, 0.85, 0.30, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-35b-a3b', 'Qwen3.5 35B-A3B MoE',
             'unsloth/Qwen3.5-35B-A3B-GGUF', 'Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf',
             'mmproj-Qwen3.5-35B-A3B-F16.gguf',
             'full', 85, 0.88, 0.35, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.6-35b-a3b', 'Qwen3.6 35B-A3B MoE',
             'unsloth/Qwen3.6-35B-A3B-GGUF', 'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf',
             'mmproj-Qwen3.6-35B-A3B-F16.gguf',
             'full', 85, 0.91, 0.36, ['main'], MIN_BUILD_QWEN35),
            ('llm-tiel-coder-35b-a3b', 'Tiel-Coder 35B-A3B MoE',
             'peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF',
             'Tiel-Coder-35B-A3B-UD-Q4_K_XL.gguf',
             'mmproj-Tiel-Coder-35B-A3B-BF16.gguf',
             'full', 90, 0.92, 0.34, ['main'], MIN_BUILD_QWEN35),
            # Same model, built with the multi-token-prediction head.  It is
            # a SEPARATE upstream repo (…-GGUF-MTP) with its own file names,
            # not a quant variant of the row above, so it needs its own row.
            #
            # VERIFIED ON DISK 2026-09-22, not inferred from the repo name:
            # the file carries blk.40.nextn.{eh_proj,enorm,hnorm,
            # shared_head_norm}.weight — the MTP head — among its 753
            # tensors. That block is the entire difference and the entire
            # reason to prefer it.
            #
            # It is only worth selecting where the SERVING build can use it:
            # llama.cpp exposes it as `--spec-type draft-mtp`, present in
            # build 10330 on this machine and absent from the 7909 binary
            # that also sits in .nunba/llama.cpp.  MIN_BUILD_QWEN35 (9180)
            # already gates the family; the MTP path additionally needs a
            # build carrying draft-mtp, which is NOT expressible in this
            # row.  It is decided at LAUNCH instead: every spawn asks
            # mtp_spec_args, which reads the head from the file and probes
            # the serving binary, so this row on an older build starts as a
            # plain MoE rather than failing to start.
            #
            # Priority 85, NOT the 90 of the row above, and the reason is an
            # invariant rather than a preference: test_llm_seed_priority_is_
            # monotonic_with_size requires priority to be non-decreasing when
            # the seeds are sorted by vram_gb/ram_gb, so that a small model
            # can never outrank a large one a big box could have run. This
            # row's MEASURED 21.19 GB puts it BELOW both 35B Qwens (85), so
            # anything above 85 here breaks the ladder.
            #
            # Which surfaces a real defect in the row above, left alone here
            # deliberately: its 22938 MiB is an ESTIMATE, and the file is
            # actually 22360478080 bytes = 20.82 GB — overstated by 1.58 GB.
            # Corrected, that row would sort BELOW the Qwens too and its own
            # priority 90 would break this same invariant. Its compliance
            # today rests on a wrong number. Re-ranking the ladder is a
            # bigger change than adding a row, so it is reported, not
            # smuggled in beside this.
            ('llm-tiel-coder-35b-a3b-mtp', 'Tiel-Coder 35B-A3B MoE (MTP)',
             'peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF-MTP',
             'Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf',
             'mmproj-Tiel-Coder-35B-A3B-MTP-BF16.gguf',
             'full', 85, 0.92, 0.34, ['main'], MIN_BUILD_QWEN35),
        ]
        # Rows seeded by an EARLIER version of this method that are now known to
        # be unloadable: google/gemma-*-it are transformers repos with no GGUF,
        # so the llama_cpp backend can never start them. Remove them rather than
        # leave a row the recommender might select. Scoped to these exact ids.
        for _bad in ('llm-gemma-2b-it', 'llm-gemma-7b-it'):
            if _bad in self._entries:
                self.unregister(_bad, persist=False)
                logger.info("Removed unloadable seeded LLM row %s (no GGUF in repo)", _bad)

        added = 0
        for (mid, name, repo, gguf, mmproj, tier, prio,
             quality, speed, purposes, min_build) in _llms:
            # ONE lookup, in bytes, then ONE division at the point of use.
            # A row whose gguf is absent from MODEL_WEIGHT_BYTES is a bug in
            # this file, not a runtime condition: skip it loudly rather than
            # seed a 0 GB entry that "fits" every compute budget there is.
            weight_bytes = model_weight_bytes(gguf)
            if not weight_bytes:
                logger.error(
                    "LLM row %s names %s, which has no entry in "
                    "MODEL_WEIGHT_BYTES - skipping rather than registering it "
                    "with a zero size that would pass every budget check",
                    mid, gguf)
                continue
            weights_gb = round(weight_bytes / (1024 ** 3), 2)
            files = {'model': gguf}
            if mmproj:
                # Local name is model-specific; source name is what the repo
                # publishes. Collapsing them overwrites across models.
                files['mmproj'] = mmproj
                files['mmproj_source'] = (
                    'mmproj-BF16.gguf' if mmproj.endswith('-BF16.gguf')
                    else 'mmproj-F16.gguf'
                )
            _vram_gb, _ram_gb = llama_gguf_compute_requirements(weights_gb)
            _definition = dict(
                name=name, model_type=ModelType.LLM,
                source='huggingface', repo_id=repo, files=files,
                vram_gb=_vram_gb,
                ram_gb=_ram_gb,
                disk_gb=weights_gb,
                min_capability_tier=tier,
                # 'llama.cpp' — the spelling in BACKENDS, in
                # TORCHLESS_BACKENDS and in ModelEntry._DOWNLOADED_BACKENDS.
                # This row said 'llama_cpp', which is in none of them, so
                # these entries were the only LLM rows in the catalogue that
                # (a) named a backend the registry does not define,
                # (b) answered TRUE to backend_requires_torch — the precise
                #     mis-provisioning TORCHLESS_BACKENDS exists to stop —
                # and (c) skipped validate()'s files['model'] requirement,
                # which is what makes an undownloadable row refusable.
                backend='llama.cpp',
                supports_gpu=True, supports_cpu=True,
                supports_cpu_offload=True, cpu_offload_method='restart_cpu',
                min_build=min_build,
                # weight_bytes, not size_mb: disk_gb is rounded to 2 decimals
                # for display, so anything reconstructing a size from it loses
                # ~5 MiB. Carrying the exact count makes the
                # entry -> preset -> entry round trip lossless.
                capabilities={'chat': True, 'vision': bool(mmproj),
                              'quant': 'Q4_K_M' if mmproj is None else 'UD-Q4_K_XL',
                              'weight_bytes': weight_bytes},
                quality_score=quality, speed_score=speed, priority=prio,
                purposes=list(purposes),
                tags=['local', 'chat', 'qwen'] + (['vision'] if mmproj else []),
            )
            if mid in self._entries:
                # UPDATE, do not skip. A seeded row is owned by this method, so a
                # corrected definition (a wrong file name, a missing mmproj, a
                # bumped min_build) has to reach boxes that already persisted the
                # old one -- "add if absent" silently pins the first version ever
                # written and makes the catalog uncorrectable. User-owned flags
                # (enabled / pinned / auto_load) and runtime state (downloaded /
                # loaded) are NOT in _definition, so they survive untouched.
                #
                # Nor does what this machine MEASURED: the residency record
                # lives in capabilities, which the seed replaces whole, so
                # every boot erased it (#110, measured). It is carried
                # forward; residency() already refuses a record taken
                # against a different weight file, so a re-pointed seed
                # cannot put a stale number into use.
                kept = (self._entries[mid].capabilities or {}).get('residency')
                if kept:
                    _definition['capabilities'] = {
                        **_definition['capabilities'], 'residency': kept}
                self.override(mid, persist=False, **_definition)
                continue
            self.register(ModelEntry(id=mid, **_definition), persist=False)
            added += 1
        return added

    def _populate_vlm_models(self) -> int:
        """VLM model entries — delegated to lightweight_backend.populate_vlm_catalog().

        lightweight_backend is the single source of truth for VLM backend names
        and hardware tier thresholds. Falls back to MiniCPM only if unavailable.
        """
        try:
            from integrations.vision.lightweight_backend import populate_vlm_catalog
            return populate_vlm_catalog(self)
        except Exception as e:
            logger.debug(f"VLM catalog population via lightweight_backend skipped: {e}")

        # Minimal fallback — MiniCPM only
        added = 0
        # Claiming question -- this method owns the id whether or not it
        # has to write it again, and the sweep removes what nobody claims.
        if not self.already_registered('vlm-minicpm-v2'):
            entry = ModelEntry(
                id='vlm-minicpm-v2', name='MiniCPM-V-2',  # 4GB VRAM → standard tier
                model_type=ModelType.VLM, source='huggingface',
                repo_id='openbmb/MiniCPM-V-2',
                vram_gb=4.0, ram_gb=4.0, disk_gb=4.0,
                min_capability_tier='standard',  # 4GB VRAM = standard, not full
                backend='sidecar', supports_gpu=True, supports_cpu=False,
                idle_timeout_s=900,
                capabilities={'image_input': True, 'video_input': False,
                              'description_loop': True},
                quality_score=0.8, speed_score=0.7,
                tags=['local', 'vision'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_embodied_models(self) -> int:
        """Embodied model entries — Qwen-RobotSuite (THREE foundation models).

        RobotSuite is three INDEPENDENT models that run inside HevolveAI (raw
        native intelligence; HARTOS has no ML): RobotManip (VLA manipulation),
        RobotWorld (language-conditioned video world model), and RobotNav
        (navigation). This bootstraps their METADATA exactly like an LLM — the
        catalog record that makes each discoverable to the admin UI + the
        orchestrator: its action vocabulary (RobotAction factories), the sensor
        modalities it consumes (SensorReading schema), and the shared
        WorldModelBridge endpoints. Errors dispatching to any of them propagate
        through the hive via WorldModelBridge._propagate_embodied_error.
        """
        # The bridge owns endpoint selection per-method — HevolveAI is
        # sensor-ingest-centric (actions + sensors → /v1/sensor/ingest, feedback
        # → /v1/stats), and that single source of truth lives in WorldModelBridge.
        # The catalog must NOT hardcode endpoint URLs (a prior copy advertised
        # /v1/actions, /v1/sensors/batch, /v1/feedback/latest that don't exist on
        # HevolveAI → catalog↔bridge drift; removed).
        bridge_caps = {
            'bridge': 'integrations.agent_engine.world_model_bridge.WorldModelBridge',
        }
        common = dict(
            model_type=ModelType.EMBODIED, source='pip', backend='in_process',
            supports_gpu=True, supports_cpu=True, supports_cpu_offload=True,
            cpu_offload_method='torch_to_cpu', idle_timeout_s=600,
            min_capability_tier='standard', tags=['local', 'embodied', 'robotics'],
        )
        specs = [
            dict(  # RobotManip — VLA manipulation: camera + language → low-level actions
                id='embodied-qwen-robotmanip',
                name='Qwen-RobotManip (VLA manipulation)',
                repo_id='QwenLM/Qwen-VLA',
                vram_gb=8.0, ram_gb=8.0, disk_gb=16.0,
                quality_score=0.78, speed_score=0.6,
                capabilities={
                    'action_verbs': ['vla_instruct', 'manip_action',
                                     'action_chunk', 'end_effector_delta'],
                    'inputs': ['camera', 'language'],
                    'sensor_modalities': ['camera', 'depth', 'force_torque', 'encoder'],
                    'language_conditioned': True, 'closed_loop': True, 'control_hz': 10,
                    # canonical 80-D masked state-action (2×29 per-arm + 22 reserved)
                    'action_space': '80d_masked', 'action_dims': 80,
                    'per_arm_dims': 29, 'reserved_dims': 22,
                    'per_arm_blocks': ['joint_positions', 'end_effector_pose',
                                       'gripper', 'dexterous_hand'],
                },
            ),
            dict(  # RobotWorld — language-conditioned video world model
                id='embodied-qwen-robotworld',
                name='Qwen-RobotWorld (language-conditioned video world model)',
                repo_id='Qwen/Qwen-RobotWorld',
                vram_gb=12.0, ram_gb=12.0, disk_gb=24.0,
                quality_score=0.75, speed_score=0.4,
                capabilities={
                    'action_verbs': ['world_model_rollout'],
                    'inputs': ['language', 'camera'],
                    'sensor_modalities': ['camera'],
                    'language_conditioned': True, 'world_model': True,
                    'output': 'predicted_video', 'default_horizon': 8,
                },
            ),
            dict(  # RobotNav — navigation → 8 (x, y, theta) waypoints
                id='embodied-qwen-robotnav',
                name='Qwen-RobotNav (navigation)',
                repo_id='QwenLM/Qwen-RobotNav',
                vram_gb=6.0, ram_gb=6.0, disk_gb=12.0,
                quality_score=0.74, speed_score=0.7,
                capabilities={
                    'action_verbs': ['navigate'],
                    'inputs': ['camera', 'language'],
                    'sensor_modalities': ['camera', 'depth', 'lidar', 'imu', 'gps'],
                    'output': 'waypoints_xytheta', 'num_waypoints': 8,
                },
            ),
        ]
        added = 0
        for spec in specs:
            # Claiming skip: an entry no populator claims is swept as
            # stale at the end of populate_from_subsystems.
            if self.already_registered(spec['id']):
                continue
            caps = {**bridge_caps, **spec.pop('capabilities')}
            self.register(ModelEntry(capabilities=caps, **common, **spec),
                          persist=False)
            added += 1
        return added

    def _populate_videogen_models(self) -> int:
        """Video generation model entries — delegated to media_agent.populate_videogen_catalog().

        media_agent is the single source of truth for video gen tool names
        and VRAM routing thresholds. Falls back to inline entries if unavailable.
        """
        try:
            from integrations.service_tools.media_agent import populate_videogen_catalog
            return populate_videogen_catalog(self)
        except Exception as e:
            logger.debug(f"Video gen catalog population via media_agent skipped: {e}")

        # Minimal fallback
        added = 0
        _fallback = [
            ('video_gen-wan2gp', 'Wan2GP',  8.0, 12.0, 0.88, 0.65),
            ('video_gen-ltx2',   'LTX2',    4.0,  8.0, 0.75, 0.80),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            # Claiming skip: an entry no populator claims is swept as
            # stale at the end of populate_from_subsystems.
            if self.already_registered(mid):
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.VIDEO_GEN,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='sidecar', supports_gpu=True,
                supports_cpu=(vram < 6),
                supports_cpu_offload=(vram < 6),
                idle_timeout_s=600,
                capabilities={'txt2vid': True, 'img2vid': False},
                quality_score=quality, speed_score=speed,
                tags=['local', 'video_gen'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_audiogen_models(self) -> int:
        """Audio/music generation entries — delegated to media_agent.populate_audiogen_catalog().

        media_agent is the single source of truth for audio gen tool names
        (ACE Step, DiffRhythm) and capability routing. Falls back to inline entries.
        Removes stale entries from previous catalog versions.
        """
        # Clean up stale entries with no capabilities (from old catalog JSON)
        for old_id in list(self._entries.keys()):
            if old_id.startswith('audio_gen-') and not self._entries[old_id].capabilities:
                del self._entries[old_id]

        try:
            from integrations.service_tools.media_agent import populate_audiogen_catalog
            return populate_audiogen_catalog(self)
        except Exception as e:
            logger.debug(f"Audio gen catalog population via media_agent skipped: {e}")

        # Minimal fallback
        added = 0
        _fallback = [
            ('audio_gen-acestep',    'ACE-Step 1.5',    6.0, 6.0, 0.85, 0.90),
            ('audio_gen-diffrhythm', 'DiffRhythm v1.2', 4.0, 4.0, 0.80, 0.75),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            # Claiming skip: an entry no populator claims is swept as
            # stale at the end of populate_from_subsystems.
            if self.already_registered(mid):
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.AUDIO_GEN,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='sidecar', supports_gpu=True,
                supports_cpu=(vram < 5),
                supports_cpu_offload=(vram < 5),
                idle_timeout_s=600,
                capabilities={'music_gen': 'acestep' in mid,
                              'singing': True, 'lyrics_input': True},
                quality_score=quality, speed_score=speed,
                tags=['local', 'audio_gen'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    # ── Capability tier helpers ────────────────────────────────────

    _TIER_RANK = {'embedded': 0, 'observer': 1, 'lite': 2, 'standard': 3,
                  'full': 4, 'compute_host': 5}

    def _get_capability_tier(self) -> str:
        """Get current node capability tier, or 'full' as fallback."""
        try:
            from security.system_requirements import get_tier_name, _capabilities
            tier_name = get_tier_name()
            if tier_name == 'embedded' and _capabilities is None:
                return 'full'
            return tier_name
        except ImportError:
            return 'full'

    @classmethod
    def _tier_sufficient(cls, current: str, required: str) -> bool:
        """Check if current capability tier meets the model's minimum requirement."""
        cur_rank = cls._TIER_RANK.get(current, 4)
        req_rank = cls._TIER_RANK.get(required, 0)
        return cur_rank >= req_rank

    # ── Persistence ───────────────────────────────────────────────

    def _load(self) -> None:
        """Load catalog from JSON file.

        On load, ALL entries have their ``loaded`` state cleared to False
        and ``device`` reset to 'unloaded'. This prevents stale
        "loaded" markers from a previous Nunba session from surviving
        across restarts — the old state claimed models were loaded even
        though the llama-server processes died with the previous session.
        ``ensure_loaded_async`` then trusted the stale state and skipped
        ``start_server()``, leaving the LLM down. See T21 #164.

        ``downloaded`` is NOT cleared — model files persist on disk
        across restarts and the catalog's downloaded flag is still valid.
        """
        if not self._path.exists():
            logger.info(f"No catalog at {self._path} — will auto-populate on first use")
            return
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _cleared = 0
            for d in data.get('models', []):
                try:
                    entry = ModelEntry.from_dict(d)
                    # Clear stale loaded state from previous session.
                    # Processes don't survive restart; loaded markers must
                    # not either. The eager-boot + ensure_loaded_async
                    # paths will re-mark as loaded once the server is
                    # actually alive and verified via /v1/models.
                    if entry.loaded:
                        entry.loaded = False
                        entry.device = 'unloaded'
                        entry.active_since = None
                        _cleared += 1
                    self._entries[entry.id] = entry
                except Exception as e:
                    logger.warning(f"Skipped malformed catalog entry: {e}")
            if _cleared:
                logger.info(
                    f"Loaded {len(self._entries)} models from catalog "
                    f"(cleared {_cleared} stale loaded markers)")
                self._save()  # Persist the cleared state
            else:
                logger.info(f"Loaded {len(self._entries)} models from catalog")
        except Exception as e:
            logger.error(f"Failed to load catalog: {e}")

    def _save(self) -> None:
        """Persist catalog to JSON."""
        with self._lock:
            data = {
                'version': 1,
                'updated_at': time.time(),
                'models': [e.to_dict() for e in self._entries.values()],
            }
        try:
            # Use unique temp file per save to prevent WinError 32 when
            # multiple populators call _save() concurrently.
            import tempfile
            fd, tmp_path = tempfile.mkstemp(
                suffix='.tmp', prefix='model_catalog_',
                dir=str(self._path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                Path(tmp_path).replace(self._path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("_save: swallowed OSError", exc_info=True)
                raise
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save catalog: {e}")

    def to_json(self) -> list:
        """Return all entries as JSON-safe list (for API responses)."""
        result = []
        for entry in self._entries.values():
            d = entry.to_dict()
            d['downloaded'] = entry.downloaded
            d['loaded'] = entry.loaded
            d['device'] = entry.device
            d['error'] = entry.error
            result.append(d)
        return result


# ── Singleton ─────────────────────────────────────────────────────
_catalog_instance: Optional[ModelCatalog] = None
_catalog_lock = threading.Lock()


def get_catalog() -> ModelCatalog:
    """Get or create the global ModelCatalog singleton."""
    global _catalog_instance
    if _catalog_instance is None:
        with _catalog_lock:
            if _catalog_instance is None:
                _catalog_instance = ModelCatalog()
                # Populate EVERY time, not only when the file is empty.
                # The old `if not list_all()` meant a node that had ever
                # written a catalogue never learned about a model shipped
                # afterwards: the owner's file was dated 2026-08-16 and was
                # missing six TTS engines that the English ladder ranks 2nd
                # through 7th.  That guard was also the only thing hiding a
                # destructive sweep -- with no refresh, the sweep never ran --
                # so it could not be removed until entries carried a claim
                # (0091a0500) and every populator made one.  Measured on a
                # copy of that live catalogue once both halves were in:
                # 40 -> 57 entries, nothing lost, user flags intact, and a
                # second run changes nothing.
                _catalog_instance.populate_from_subsystems()
    return _catalog_instance
