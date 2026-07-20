"""HART OS shard-runtime — the HARTOS half of WAN distributed model-inference sharding.

This package holds the HARTOS side of the boundary frozen in
`hevolveai/docs/SHARD_RUNTIME_CONTRACT.md` (mirrored here as
`docs/architecture/SHARD_RUNTIME_HARTOS_SIDE.md`):

  * `envelope` — the self-describing activation wire format. HARTOS only ever
    PARSES the header for routing (it has no ML code / no torch); PeerLink
    relays the payload bytes opaquely, a different machine/backend reconstructs
    the tensor.

The orchestration (ComputeMesh shard planner + PeerLink relay + the
`distributed` model_registry backend) is documented in the side doc and built
against this envelope. hevolveai owns the layer executor behind the Model Bus
`/v1/shard/*` verbs; HARTOS never runs a forward pass itself.
"""

from core.shard_runtime.envelope import (
    PROTOCOL_VERSION,
    KINDS,
    DTYPES,
    REQUIRED_HEADER_FIELDS,
    ROUTING_FIELDS,
    EnvelopeError,
    frame,
    token_ids_frame,
    parse_header,
    read_routing,
    payload_view,
)

__all__ = [
    'PROTOCOL_VERSION',
    'KINDS',
    'DTYPES',
    'REQUIRED_HEADER_FIELDS',
    'ROUTING_FIELDS',
    'EnvelopeError',
    'frame',
    'token_ids_frame',
    'parse_header',
    'read_routing',
    'payload_view',
]
