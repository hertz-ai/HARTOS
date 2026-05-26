"""Bandwidth model — mesh vs SFU per-peer cost crossover by call kind.

This module is the SINGLE SOURCE OF TRUTH for per-kind mesh thresholds.
``api_calls._DEFAULT_KIND_THRESHOLDS`` is an import-alias of
``OPERATIONAL_THRESHOLDS`` defined here — there is no duplicate dict.

Real-world measurement (Task #276 live phase) is expected to validate
or refine these constants — until then they are the *theoretical*
crossover from first principles, not an arbitrary choice of 4.

Topology cost model (per peer, sustained bitrate):

    Mesh:  upload   = (N - 1) × bitrate_per_track    (encode once, send N-1 copies — *)
           download = (N - 1) × bitrate_per_track    (one stream per other peer)

    SFU:   upload   = 1 × bitrate_per_track          (one stream up to SFU)
           download = (N - 1) × bitrate_per_track    (SFU forwards each peer's stream)

(* `react-native-webrtc` and the LiveKit web SDK both emit a single
   encoded copy per outgoing track and the OS-level networking layer
   does the duplicate sends — so the *encode* cost is 1×, but the
   *upload bandwidth* cost is (N-1)×.)

For each call kind, the SFU starts winning on **per-peer upload** the
moment (N - 1) > 1, i.e. at N ≥ 3.  But that's not the whole story:

  - Mesh has a smaller signaling round-trip on the happy path
    (no SFU media transit).
  - Mesh is the only mode that actually preserves end-to-end media
    encryption between users (SFU sees the media frames).
  - Mesh keeps working when central / regional HARTOS is down.
  - SFU adds operational complexity (binary lifecycle, port mgmt,
    Redis for clusters) — only worth it when the bandwidth pain
    actually shows up.

So we use a different *practical* crossover per kind:

  * voice (32 kbps Opus):
      mesh upload at N=4 = 3 × 32 = 96 kbps — fine on every uplink.
      mesh upload at N=5 = 4 × 32 = 128 kbps — still fine.
      Default threshold = 4 (promote at N=5+).  Adoptable up to ~6
      if measured.

  * video (500 kbps default VP8):
      mesh upload at N=3 = 2 × 500 = 1.0 Mbps — borderline on
        residential ADSL up / shared 4G uplink.
      mesh upload at N=4 = 3 × 500 = 1.5 Mbps — actively painful
        for many residential connections.
      Default threshold = 3 (promote at N=4+).

  * screen_share, mixed:
      Always SFU regardless of N.  Multi-track re-negotiation (camera +
      screen + audio) on a mesh re-shuffles SDP for every peer; SFU
      handles it once.  Threshold = 1 (promote at N=2+).

These are *defaults*.  Operators can override per-kind via env vars
(see api_calls._mesh_threshold).  Live measurement (Task #276) will
validate.
"""

from __future__ import annotations

from typing import Dict, NamedTuple


# Approximate sustained bitrates by media kind (kbps per outgoing track).
KIND_BITRATE_KBPS = {
    'voice': 32,         # Opus narrowband sweet spot
    'video': 500,        # VP8 default, no simulcast
    'video_simulcast': 1500,  # VP8 simulcast 3 layers (worst case)
    'av1_video': 300,    # AV1 — 30-40% cheaper than VP8
    'screen_share': 800, # screen 1080p25 typical
}


class CostPoint(NamedTuple):
    n: int
    mesh_up_kbps: float
    mesh_down_kbps: float
    sfu_up_kbps: float
    sfu_down_kbps: float


def crossover_table(kind: str = 'voice', max_n: int = 8) -> Dict[int, CostPoint]:
    """Return per-peer bandwidth cost table for a given kind, N=2..max_n."""
    bitrate = KIND_BITRATE_KBPS.get(kind, 500)
    table: Dict[int, CostPoint] = {}
    for n in range(2, max_n + 1):
        peers = n - 1
        table[n] = CostPoint(
            n=n,
            mesh_up_kbps=peers * bitrate,
            mesh_down_kbps=peers * bitrate,
            sfu_up_kbps=bitrate,
            sfu_down_kbps=peers * bitrate,
        )
    return table


def first_n_where_mesh_upload_exceeds(kind: str, ceiling_kbps: float) -> int:
    """Return the smallest N where mesh upload per peer > ceiling_kbps.

    Used to compute a 'promote to SFU' threshold given an uplink
    budget.  E.g. if your residential ADSL uplink is 1000 kbps and the
    call kind is video (500 kbps), this returns 3 (mesh upload at
    N=3 = 2 × 500 = 1000 kbps which equals — at N=4 it exceeds).
    """
    bitrate = KIND_BITRATE_KBPS.get(kind, 500)
    n = 2
    while n <= 64:  # safety cap
        if (n - 1) * bitrate > ceiling_kbps:
            return n
        n += 1
    return 64


# Bandwidth ceilings — conservative residential numbers used as
# inputs to the model.
RESIDENTIAL_UPLINK_KBPS = 1500   # 1.5 Mbps — ADSL / shared 4G floor
MOBILE_4G_UPLINK_KBPS = 5000     # decent 4G — comfortable video uplink


# ── Operational vs theoretical thresholds ─────────────────────────────
#
# The pure-bandwidth model alone produces *generous* thresholds:
#   voice on 1500 kbps uplink: mesh ok through N=46  (32 kbps × 45)
#   video on 1500 kbps uplink: mesh ok through N=4   (500 kbps × 3 = 1500)
#
# But mesh has costs the bandwidth model ignores:
#   1. CPU encode is 1× regardless of N (the encoder emits one stream
#      and the OS networking layer fans it out), BUT codec parameters
#      become harder to negotiate as N grows — each additional peer's
#      ICE/DTLS/SDP exchange happens on the call's hot path.
#   2. Signaling round-trips: mesh has O(N²) signaling messages on
#      join.  Past N=4 this starts adding visible "joining" latency.
#   3. Subjective UX research (e.g., Hangouts / Meet older designs)
#      consistently puts the "too noisy" boundary at 4-5 simultaneous
#      audio sources without any active speaker detection.
#
# So the *operationally-chosen* defaults are tighter than the pure
# bandwidth crossover.  Encoded in OPERATIONAL_THRESHOLDS below — this
# is the canonical home; api_calls imports it as
# _DEFAULT_KIND_THRESHOLDS:
#
#   voice → 4   (mesh mode for ≤ 4 participants; SFU at 5+)
#   video → 3   (mesh for ≤ 3; SFU at 4+ to leave headroom under 1500)
#   screen_share → 1  (always SFU — multi-track re-negotiation is hard)
#   mixed → 1         (always SFU — same reason)
#
# Live measurement (Task #276) is expected to validate these.  When it
# does, update OPERATIONAL_THRESHOLDS below — api_calls picks it up
# automatically via the import alias.

OPERATIONAL_THRESHOLDS = {
    'voice': 4,
    'video': 3,
    'screen_share': 1,
    'mixed': 1,
}

# Pure-bandwidth crossover (informational; not used as defaults).  These
# are the largest N where mesh upload still fits within the residential
# uplink ceiling.  Useful for operators on faster uplinks who want to
# raise the threshold above the operational default.
BANDWIDTH_ONLY_CEILING_AT_RESIDENTIAL = {
    'voice': first_n_where_mesh_upload_exceeds(
        'voice', RESIDENTIAL_UPLINK_KBPS) - 1,
    'video': first_n_where_mesh_upload_exceeds(
        'video', RESIDENTIAL_UPLINK_KBPS) - 1,
    'screen_share': first_n_where_mesh_upload_exceeds(
        'screen_share', RESIDENTIAL_UPLINK_KBPS) - 1,
}


__all__ = [
    'KIND_BITRATE_KBPS',
    'CostPoint',
    'crossover_table',
    'first_n_where_mesh_upload_exceeds',
    'RESIDENTIAL_UPLINK_KBPS',
    'MOBILE_4G_UPLINK_KBPS',
    'OPERATIONAL_THRESHOLDS',
    'BANDWIDTH_ONLY_CEILING_AT_RESIDENTIAL',
]
