"""Shard-orchestrator — drives one distributed forward across a mesh cluster.

The HARTOS half of the frozen SHARD_RUNTIME_CONTRACT. It runs NO model math and
owns NO second wire format or transport: it seeds the first frame from token ids,
relays the activation down an ordered ring of already-paired peers via the proven
``ComputeMeshService.relay_shard`` (the same cross-machine transport verified
live .165<->.9), and returns the final frame. Each hop's actual layer-slice
forward is executed by that node's ShardBackend (hevolveai) when present, or the
node's contract-valid stand-in until then, so the SAME relay path serves real and
stand-in with zero forks.

Cluster FORMATION (capacity-aware plan_shard on candidates -> load_shard to
assign contiguous layer ranges -> the ordered peer list) is a separate concern
that reuses ComputeMesh peer selection; this module is the DRIVE over a formed
cluster. Decode-phase drive is a follow-up (needs the backend's per-request KV
cache); the prefill drive below is the proven core.
"""
from __future__ import annotations

import uuid
from typing import List, Optional, Sequence

from core.shard_runtime.envelope import token_ids_frame, parse_header


class ShardOrchestrator:
    """Drive a distributed forward over a ComputeMeshService's paired peers."""

    def __init__(self, mesh):
        # mesh: a ComputeMeshService already paired with the cluster peers. One
        # transport, no fork — the orchestrator only calls mesh.relay_shard.
        self.mesh = mesh

    def run(self, model_id: str, token_ids: Sequence[int], peer_ids: List[str],
            request_id: Optional[str] = None) -> bytes:
        """Drive one forward across ``peer_ids`` ordered first..last.

        token_ids -> first peer -> activation -> next peer -> ... -> last peer.
        Returns the final frame bytes (logits on the last shard once a real
        ShardBackend is present; a contract-valid activation stand-in until then).

        Raises on an empty cluster or a hop failure — the caller re-plans; the
        orchestrator never silently drops a request (fail loud, not wrong).
        """
        if not peer_ids:
            raise ValueError("shard cluster is empty (no peer_ids to drive)")
        rid = request_id or str(uuid.uuid4())
        frame = token_ids_frame(model_id, rid, list(token_ids), order_index=0)
        for hop, peer_id in enumerate(peer_ids):
            frame = self.mesh.relay_shard(peer_id, frame)   # real relay; node executes
            if not frame:
                raise RuntimeError(f"shard hop {hop} (peer {peer_id}) returned no frame")
        return frame

    @staticmethod
    def final_header(frame_bytes: bytes) -> dict:
        """Parse the final frame's header (kind='logits' on a real last shard)."""
        header, _ = parse_header(frame_bytes)
        return header
