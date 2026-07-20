"""Activation wire envelope — HARTOS side (parse + relay; NO torch).

Byte-for-byte mirror of section 2 of the frozen contract
(`hevolveai/docs/SHARD_RUNTIME_CONTRACT.md`):

    ENVELOPE = HEADER_JSON_LEN (uint32, little-endian)
             + HEADER_JSON     (utf-8)
             + PAYLOAD         (contiguous C-order tensor bytes, bf16/fp16;
                                or int32 for kind="token_ids")

    HEADER_JSON = {
      "v": 1,
      "model_id":    str,
      "request_id":  str,   # OWNED BY HART — the KV-cache key on the executor
      "order_index": int,   # producing shard's pipeline position
      "seq_pos":     int,   # absolute sequence position of the first payload row
      "dtype":       "bfloat16" | "float16",
      "shape":       [int, ...],
      "kind":        "activation" | "logits" | "token_ids"
    }

Why HARTOS only PARSES: HARTOS has no ML code (all model math lives in
hevolveai). The two HARTOS consumers of this format never reconstruct the
tensor:

  * the PeerLink relay reads ONLY the routing fields (`read_routing`) to move
    an opaque payload from one node to the next — it must not need torch;
  * the shard-orchestrator inspects the header to know pipeline position / kind
    and, on the first hop, builds the `token_ids` input frame (`token_ids_frame`)
    from plain Python ints.

The activation / logits payload is produced and consumed only by hevolveai's
ShardBackend on the two ends; HARTOS forwards `payload_view(raw)` unchanged.

This module depends on the stdlib only (`struct`, `json`) so it is safe to
import in any HARTOS/Nunba context, on any OS, with or without an ML stack.
"""

from __future__ import annotations

import json
import struct
from typing import Dict, List, Sequence, Tuple

# ─── Contract constants (must match hevolveai/envelope.py) ───

PROTOCOL_VERSION = 1

# uint32 little-endian header-length prefix.
_HEADER_LEN = struct.Struct('<I')

KINDS = ('activation', 'logits', 'token_ids')
DTYPES = ('bfloat16', 'float16')

# Every header the executor emits carries these. `read_routing` returns the
# subset a torch-free relay needs to move the frame.
REQUIRED_HEADER_FIELDS = (
    'v', 'model_id', 'request_id', 'order_index', 'seq_pos', 'dtype', 'shape', 'kind',
)
ROUTING_FIELDS = ('request_id', 'order_index', 'model_id', 'kind', 'seq_pos')


class EnvelopeError(ValueError):
    """A shard-runtime frame is malformed or violates the frozen contract."""


# ─── Encode (HART only ever builds the first-hop token_ids frame) ───

def frame(header: Dict, payload: bytes) -> bytes:
    """Assemble a wire frame from a header dict + raw payload bytes.

    Validates the header against the frozen contract (version, required fields,
    kind, dtype for tensor kinds) so a malformed producer fails loudly here
    rather than on the far node. The JSON encoding need not be byte-identical to
    hevolveai's — both sides `json.loads` the header to a dict — but the uint32
    length prefix and the field set must match exactly.
    """
    if not isinstance(header, dict):
        raise EnvelopeError(f"header must be a dict, got {type(header).__name__}")
    missing = [f for f in REQUIRED_HEADER_FIELDS if f not in header]
    if missing:
        raise EnvelopeError(f"header missing required fields: {missing}")
    if header['v'] != PROTOCOL_VERSION:
        raise EnvelopeError(
            f"unsupported envelope version {header['v']!r} (this build speaks v{PROTOCOL_VERSION})"
        )
    if header['kind'] not in KINDS:
        raise EnvelopeError(f"kind must be one of {KINDS}, got {header['kind']!r}")
    # dtype is ignored for token_ids (always int32) but required to be valid for tensors.
    if header['kind'] != 'token_ids' and header['dtype'] not in DTYPES:
        raise EnvelopeError(f"dtype must be one of {DTYPES} for a {header['kind']} frame, got {header['dtype']!r}")
    if not isinstance(payload, (bytes, bytearray)):
        raise EnvelopeError(f"payload must be bytes, got {type(payload).__name__}")

    header_bytes = json.dumps(header, separators=(',', ':')).encode('utf-8')
    return _HEADER_LEN.pack(len(header_bytes)) + header_bytes + bytes(payload)


def token_ids_frame(
    model_id: str,
    request_id: str,
    token_ids: Sequence[int],
    *,
    seq_pos: int = 0,
    order_index: int = 0,
) -> bytes:
    """Build the first-hop input frame (kind="token_ids", int32 little-endian C-order).

    This is the ONE frame HARTOS produces itself: the orchestrator hands the
    prompt's token ids to the `role="first"` shard. No numpy/torch — the ids are
    packed as explicit little-endian int32 so any backend reconstructs them from
    the header alone.
    """
    ids = list(token_ids)
    for t in ids:
        if not isinstance(t, int):
            raise EnvelopeError(f"token id must be int, got {type(t).__name__}: {t!r}")
    payload = struct.pack('<%di' % len(ids), *ids)
    header = {
        'v': PROTOCOL_VERSION,
        'model_id': model_id,
        'request_id': request_id,
        'order_index': order_index,
        'seq_pos': seq_pos,
        'dtype': 'int32',   # ignored for token_ids (frame() exempts this kind)
        'shape': [1, len(ids)],
        'kind': 'token_ids',
    }
    # frame() already exempts kind="token_ids" from the DTYPES check, so it is the
    # single canonical assembler here too (no separate length-prefix packing).
    return frame(header, payload)


# ─── Parse / relay (the hot path for HARTOS: routing without torch) ───

def parse_header(raw: bytes) -> Tuple[Dict, int]:
    """Read the header from a wire frame.

    Returns `(header_dict, payload_offset)`. Pure bytes → dict; never touches the
    payload tensor. Raises `EnvelopeError` on a truncated frame, a bad length
    prefix, invalid JSON, or a version mismatch — the relay/orchestrator must fail
    closed rather than forward a frame it cannot route.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise EnvelopeError(f"frame must be bytes, got {type(raw).__name__}")
    if len(raw) < _HEADER_LEN.size:
        raise EnvelopeError(f"frame too short for a length prefix ({len(raw)} bytes)")
    (hlen,) = _HEADER_LEN.unpack_from(raw, 0)
    start = _HEADER_LEN.size
    end = start + hlen
    if len(raw) < end:
        raise EnvelopeError(f"frame truncated: header claims {hlen} bytes, only {len(raw) - start} present")
    try:
        header = json.loads(bytes(raw[start:end]).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EnvelopeError(f"header is not valid utf-8 JSON: {e}") from e
    if not isinstance(header, dict):
        raise EnvelopeError(f"header must decode to an object, got {type(header).__name__}")
    v = header.get('v')
    if v != PROTOCOL_VERSION:
        raise EnvelopeError(f"unsupported envelope version {v!r} (this build speaks v{PROTOCOL_VERSION})")
    return header, end


def read_routing(raw: bytes) -> Dict:
    """Return only the fields PeerLink needs to relay a frame (no tensor touch).

    This is what the relay calls per hop: it learns the destination request /
    pipeline position / kind without decoding or reconstructing the payload.
    Missing routing fields raise — an unroutable frame is not silently forwarded.
    """
    header, _ = parse_header(raw)
    missing = [f for f in ROUTING_FIELDS if f not in header]
    if missing:
        raise EnvelopeError(f"frame missing routing fields: {missing}")
    return {f: header[f] for f in ROUTING_FIELDS}


def payload_view(raw: bytes) -> bytes:
    """Return the opaque payload bytes (everything after the header).

    PeerLink forwards these unchanged; only hevolveai's ShardBackend on the far
    end reconstructs the tensor from `parse_header` + these bytes.
    """
    _, offset = parse_header(raw)
    return bytes(raw[offset:])


def decode_token_ids(raw: bytes) -> List[int]:
    """Decode a kind="token_ids" frame back to a list of ints (test / first-hop echo).

    Provided so HARTOS can round-trip the one frame it produces. Tensor frames
    (activation/logits) are NOT decodable here by design — that needs hevolveai.
    """
    header, offset = parse_header(raw)
    if header.get('kind') != 'token_ids':
        raise EnvelopeError(f"decode_token_ids only handles token_ids frames, got kind={header.get('kind')!r}")
    body = bytes(raw[offset:])
    if len(body) % 4 != 0:
        raise EnvelopeError(f"token_ids payload not a whole number of int32 ({len(body)} bytes)")
    n = len(body) // 4
    return list(struct.unpack('<%di' % n, body))
