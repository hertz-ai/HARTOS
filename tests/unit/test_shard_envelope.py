"""Cross-implementation parity tests for the shard-runtime activation envelope.

These prove the HARTOS parser (`core/shard_runtime/envelope.py`) reads the byte
layout frozen in `hevolveai/docs/SHARD_RUNTIME_CONTRACT.md` §2. The producer side
here is an INDEPENDENT encoder (`_contract_encode`) that follows the contract
spec literally, NOT HARTOS's own `frame()`, so a drift between the two
implementations fails the test rather than silently agreeing with itself.

Also asserts the `distributed-shard` model_registry backend is gated behind
HART_SHARD_RUNTIME (present only when the flag is set).
"""
import json
import struct

import pytest

from core.shard_runtime.envelope import (
    PROTOCOL_VERSION,
    ROUTING_FIELDS,
    EnvelopeError,
    frame,
    token_ids_frame,
    parse_header,
    read_routing,
    payload_view,
    decode_token_ids,
)


# ─── Independent encoder: the far (hevolveai) side, per contract §2 verbatim ───

def _contract_encode(header: dict, payload: bytes) -> bytes:
    """uint32-LE header length + utf-8 JSON header + payload. No HART code."""
    hb = json.dumps(header).encode('utf-8')
    return struct.pack('<I', len(hb)) + hb + payload


def _activation_header(**over):
    h = {
        'v': PROTOCOL_VERSION,
        'model_id': 'qwen3-70b',
        'request_id': 'req-abc-123',
        'order_index': 2,
        'seq_pos': 41,
        'dtype': 'bfloat16',
        'shape': [1, 7, 8192],
        'kind': 'activation',
    }
    h.update(over)
    return h


# ─── Parse a frame produced by the independent encoder ───

def test_parse_activation_frame_matches_contract_layout():
    header = _activation_header()
    payload = b'\x01\x02\x03\x04' * 16  # opaque bf16 bytes; HART never decodes these
    raw = _contract_encode(header, payload)

    parsed, offset = parse_header(raw)
    assert parsed == header
    # offset must land exactly at the payload start: 4-byte prefix + header bytes.
    assert offset == 4 + len(json.dumps(header).encode('utf-8'))


def test_read_routing_returns_only_routing_fields():
    raw = _contract_encode(_activation_header(), b'\x00' * 8)
    routing = read_routing(raw)
    assert set(routing.keys()) == set(ROUTING_FIELDS)
    assert routing['request_id'] == 'req-abc-123'
    assert routing['order_index'] == 2
    assert routing['model_id'] == 'qwen3-70b'
    assert routing['kind'] == 'activation'
    assert routing['seq_pos'] == 41


def test_payload_view_returns_exact_opaque_bytes():
    payload = bytes(range(64))
    raw = _contract_encode(_activation_header(kind='logits'), payload)
    assert payload_view(raw) == payload


def test_logits_frame_parses_like_any_other():
    raw = _contract_encode(_activation_header(kind='logits', shape=[1, 152064]), b'\xaa\xbb')
    parsed, _ = parse_header(raw)
    assert parsed['kind'] == 'logits'


# ─── The one frame HART produces: token_ids (int32 LE) ───

def test_token_ids_frame_is_int32_le_and_roundtrips():
    ids = [1, 2, 3, 128000, 42]
    raw = token_ids_frame('qwen3-70b', 'req-xyz', ids, seq_pos=0)

    # HART's own decoder round-trips.
    assert decode_token_ids(raw) == ids

    # Independent decode: strip the header, read int32 little-endian.
    header, offset = parse_header(raw)
    assert header['kind'] == 'token_ids'
    assert header['request_id'] == 'req-xyz'
    body = raw[offset:]
    assert len(body) == 4 * len(ids)
    assert list(struct.unpack('<%di' % len(ids), body)) == ids


def test_token_ids_frame_rejects_non_int():
    with pytest.raises(EnvelopeError):
        token_ids_frame('m', 'r', [1, 2, 'three'])


# ─── Fail-closed parsing (a relay must never forward an unroutable frame) ───

def test_truncated_length_prefix_raises():
    with pytest.raises(EnvelopeError):
        parse_header(b'\x01\x02')  # < 4 bytes


def test_header_longer_than_frame_raises():
    # Claim a 999-byte header but supply almost nothing.
    raw = struct.pack('<I', 999) + b'{}'
    with pytest.raises(EnvelopeError):
        parse_header(raw)


def test_version_mismatch_raises():
    raw = _contract_encode(_activation_header(v=2), b'')
    with pytest.raises(EnvelopeError):
        parse_header(raw)


def test_non_json_header_raises():
    bad = b'not json at all'
    raw = struct.pack('<I', len(bad)) + bad
    with pytest.raises(EnvelopeError):
        parse_header(raw)


def test_missing_routing_field_raises():
    # Valid v1 header but drop a routing field (request_id).
    h = _activation_header()
    del h['request_id']
    h['v'] = PROTOCOL_VERSION  # still v1 so parse_header passes; read_routing must catch it
    raw = struct.pack('<I', len(json.dumps(h).encode())) + json.dumps(h).encode() + b''
    with pytest.raises(EnvelopeError):
        read_routing(raw)


# ─── frame() producer guards (loud failure at the producer, not the far node) ───

def test_frame_rejects_unknown_kind():
    with pytest.raises(EnvelopeError):
        frame(_activation_header(kind='embeddings'), b'')


def test_frame_rejects_bad_dtype_for_tensor():
    with pytest.raises(EnvelopeError):
        frame(_activation_header(dtype='float64'), b'')


def test_frame_requires_all_header_fields():
    h = _activation_header()
    del h['seq_pos']
    with pytest.raises(EnvelopeError):
        frame(h, b'')


def test_frame_roundtrips_through_parser():
    raw = frame(_activation_header(), b'\x09' * 4)
    parsed, _ = parse_header(raw)
    assert parsed['request_id'] == 'req-abc-123'
    assert payload_view(raw) == b'\x09' * 4


# ─── distributed-shard backend is flag-gated ───

def test_distributed_backend_registers_only_with_flag(monkeypatch):
    mr = pytest.importorskip('integrations.agent_engine.model_registry')

    # Flag OFF: entry must be absent (clear any prior state first).
    monkeypatch.delenv('HART_SHARD_RUNTIME', raising=False)
    mr.model_registry.unregister('distributed-shard')
    mr._register_defaults()
    assert mr.model_registry.get_model('distributed-shard') is None

    # Flag ON: entry present, EXPERT tier, distributed, non-endpoint base_url.
    monkeypatch.setenv('HART_SHARD_RUNTIME', '1')
    mr._register_defaults()
    backend = mr.model_registry.get_model('distributed-shard')
    assert backend is not None
    assert backend.tier == mr.ModelTier.EXPERT
    assert backend.is_local is False
    assert backend.config_list_entry['base_url'] == 'shard://cluster'


def test_expert_selector_skips_distributed_shard_placeholder(monkeypatch):
    """distributed-shard is ADVERTISED but must never be auto-dialled until the
    orchestrator lands to intercept it (the 'Selection guard'). Regression: the
    0.0-cost / 0.92-accuracy EXPERT entry would otherwise win get_expert_model on
    a keyless decentralized node and the dispatcher would dial shard://cluster."""
    mr = pytest.importorskip('integrations.agent_engine.model_registry')
    monkeypatch.setenv('HART_SHARD_RUNTIME', '1')
    mr.model_registry.unregister('distributed-shard')
    mr._register_defaults()
    backend = mr.model_registry.get_model('distributed-shard')
    assert backend is not None                      # registered / advertised
    assert backend.is_dispatchable() is False       # single-source guard
    picked = mr.model_registry.get_expert_model()   # keyless: may be None
    assert picked is None or picked.model_id != 'distributed-shard'
