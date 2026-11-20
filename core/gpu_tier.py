"""
core.gpu_tier — single source of truth for GPU capability tiers.

WHY THIS EXISTS
───────────────
The 24 / 10 / 4 GB tier thresholds were duplicated in TWO places:
  - main.py `/backend/health` (Python, server-side classification).
  - landing-page/src/components/chat/GpuTierBadge.jsx (JavaScript,
    client-side label generation).

When commit 2acf21a raised the speculative-decoding gate from 8 GB to
10 GB, the backend was updated but the frontend label still said "10GB+"
because it was hard-coded — they happened to match by accident.  Two
months later when product wanted to test 12 GB as the new break-point,
ONE side would have shipped and the other wouldn't, producing tier
labels that disagreed with the server's actual behaviour.

This module is the canonical table.  The frontend now FETCHES it via
`GET /api/v1/system/tiers` so the labels and thresholds shipped to the
user are always derived from the same source the backend classifier
uses.  Future threshold tweaks happen here ONCE.

WHO CONSUMES IT
───────────────
- main.py /backend/health → classify(vram_gb, cuda_available)
- main.py /api/v1/system/tiers → tier_table() (frontend bootstrap)
- landing-page/src/components/chat/GpuTierBadge.jsx → fetches /tiers

THRESHOLD RATIONALE
───────────────────
ULTRA   ≥ 24 GB  → 70B-class model viable (Llama-70B Q4 ≈ 38GB; with
                   offload + KV-cache budget, 24GB is the practical floor
                   where ULTRA tier actually unlocks behaviour the lower
                   tiers can't access).
FULL    ≥ 10 GB  → main + draft speculative decoding fits with 4GB TTS
                   headroom (raised from 8GB in commit 2acf21a).
STANDARD 4-10 GB → main-only (no speculation; ~1.3-2.0s slower per reply
                   on chat).
NONE    < 4 GB / no CUDA → CPU fallback.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List


class GpuTier(str, Enum):
    """GPU capability tier — string-valued Enum so JSON serialization is
    transparent and the wire format matches the Python value."""
    ULTRA = 'ultra'
    FULL = 'full'
    STANDARD = 'standard'
    NONE = 'none'


# Single source of truth.  Keys are tier names; values are the MINIMUM
# VRAM (GB) required for that tier.  Order matters for `classify` — it
# walks descending so the first match wins.  Keep this descending.
TIER_THRESHOLDS: Dict[GpuTier, float] = {
    GpuTier.ULTRA: 24.0,
    GpuTier.FULL: 10.0,
    GpuTier.STANDARD: 4.0,
    GpuTier.NONE: 0.0,
}


# Operator-readable tier descriptions.  The frontend pulls these directly
# so the label + tooltip text in GpuTierBadge.jsx never drifts from what
# the backend documents.  Keep these <120 chars; the chip tooltip wraps
# but the badge label itself is rendered as the short form.
TIER_DESCRIPTIONS: Dict[GpuTier, Dict[str, str]] = {
    GpuTier.ULTRA: {
        'label': 'Ultra GPU',
        'short': 'Ultra',
        'description': (
            '24GB+ VRAM. 70B-class models viable with speculative '
            'decoding + full TTS headroom.'
        ),
    },
    GpuTier.FULL: {
        'label': 'Full GPU',
        'short': 'Full',
        'description': (
            '10GB+ VRAM. Draft + main speculative decoding active. '
            'Replies ~40% faster than Standard.'
        ),
    },
    GpuTier.STANDARD: {
        'label': 'Standard GPU',
        'short': 'Standard',
        'description': (
            'Heavy model only. Upgrade to 10GB+ VRAM for ~40% faster '
            'replies (speculative decoding unlocks at 10GB to leave room for voice).'
        ),
    },
    GpuTier.NONE: {
        'label': 'CPU',
        'short': 'CPU',
        'description': (
            'No CUDA GPU detected (or under 4GB VRAM). Chat runs on CPU. '
            'A 10GB+ NVIDIA GPU unlocks speculative decoding.'
        ),
    },
}


# Seconds the draft-first dispatcher waits for the draft/classifier call.  With
# no draft server of its own the draft URL is the MAIN llama-server, and a box
# with no GPU cannot answer a completion in 15 s: measured on a no-CUDA box,
# main-model completions took a median of 55 to 71 s (min 5 s), and 18 of 18
# draft-first calls were cut at 30 s (15 s plus pooled_post's one re-send) with
# an empty reply.  NONE therefore gets the read budget core.http_pool names for
# a local completion (LLM_COMPLETION_TIMEOUT, guarded by a test); the tiers with
# a GPU keep the 15 s they always had (not measured, so not changed).
DEFAULT_DRAFT_CALL_TIMEOUT_S = 15
DRAFT_CALL_TIMEOUT_S: Dict[GpuTier, int] = {
    GpuTier.ULTRA: DEFAULT_DRAFT_CALL_TIMEOUT_S,
    GpuTier.FULL: DEFAULT_DRAFT_CALL_TIMEOUT_S,
    GpuTier.STANDARD: DEFAULT_DRAFT_CALL_TIMEOUT_S,
    GpuTier.NONE: 120,
}


def classify(vram_gb: float, cuda_available: bool) -> GpuTier:
    """Return the GPU tier for the given VRAM size and CUDA availability.

    No CUDA → always NONE (regardless of VRAM size — an integrated GPU
    can have lots of "shared" RAM but no CUDA path).

    Args:
        vram_gb: Total VRAM in gigabytes (NOT free — total).  Read from
            `vram_manager.get_total_vram()`.
        cuda_available: True iff `torch.cuda.is_available()` returned True
            at probe time (via vram_manager.detect_gpu).

    Returns:
        The most-capable tier whose threshold is ≤ vram_gb, or NONE if no
        CUDA / vram_gb < STANDARD threshold.
    """
    if not cuda_available:
        return GpuTier.NONE
    # Walk the table in declared (descending) order; first match wins.
    for tier, threshold in TIER_THRESHOLDS.items():
        if tier is GpuTier.NONE:
            # Don't classify a CUDA-available system with vram > 0 as NONE
            # via the threshold ladder; that case is "under STANDARD" which
            # we treat as NONE below.
            break
        if vram_gb >= threshold:
            return tier
    return GpuTier.NONE


def draft_call_timeout(tier: GpuTier) -> int:
    """Seconds the draft-first dispatcher waits for its draft call on ``tier``."""
    return DRAFT_CALL_TIMEOUT_S[tier]


def tier_table() -> List[Dict[str, object]]:
    """Return the canonical tier table for the /api/v1/system/tiers endpoint.

    Format chosen to be consumed directly by the React component without
    re-keying — the frontend just renders the array."""
    return [
        {
            'name': tier.value,
            'min_vram_gb': TIER_THRESHOLDS[tier],
            'label': TIER_DESCRIPTIONS[tier]['label'],
            'short': TIER_DESCRIPTIONS[tier]['short'],
            'description': TIER_DESCRIPTIONS[tier]['description'],
        }
        for tier in TIER_THRESHOLDS
    ]


__all__ = [
    'GpuTier',
    'TIER_THRESHOLDS',
    'TIER_DESCRIPTIONS',
    'DEFAULT_DRAFT_CALL_TIMEOUT_S',
    'DRAFT_CALL_TIMEOUT_S',
    'classify',
    'draft_call_timeout',
    'tier_table',
]
