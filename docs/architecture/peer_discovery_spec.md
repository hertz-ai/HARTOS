# Peer Discovery — UDP 6780 Wire Format (Canonical)

> **One source of truth.** Both HARTOS (Python) and any client implementation (Android Kotlin, iOS Swift, Nunba Electron/pywebview) MUST encode and decode UDP-6780 packets exactly per this spec. Implementations that drift will silently fail to discover each other (this happened — Android originally framed JSON with `{magic: "HART_DISCOVERY"}` and never saw any HARTOS beacons that use a raw-bytes prefix).

## Scope

This document covers the **LAN auto-discovery layer** (UDP 6780) — the step that lets a phone or laptop on the same Wi-Fi find a HART node without DNS or out-of-band config. It does NOT cover the WebSocket session layer that follows; for that see `peer_link.md`.

## Wire format

Every packet on UDP 6780 is:

```
┌────────────────────────────┬─────────────────────────────────┐
│  MAGIC PREFIX (16 bytes)   │  JSON BODY (UTF-8, ≤ 2 KiB)     │
│  "HEVOLVE_DISCO_V1"        │  {…}                            │
└────────────────────────────┴─────────────────────────────────┘
```

- The magic prefix is **raw bytes**, not a JSON field. Length is exactly 16 bytes, ASCII `HEVOLVE_DISCO_V1` (no NUL terminator, no quotes).
- Anything that does not start with that prefix is dropped silently. This is how broadcast-port noise (Avahi, SSDP, random discovery from other apps on UDP 6780) is filtered without parsing.
- The JSON body follows the prefix immediately — no length field, no delimiter. The body length is whatever the UDP packet length minus 16 says it is.
- Max packet size is 2048 bytes. Bodies above this MUST NOT be sent.

### JSON body fields

| Field | Required | Type | Purpose |
|---|---|---|---|
| `type` | **yes** | string | Must equal `"hevolve-discovery"` for beacons + probes. Anything else → drop. |
| `node_id` | **yes** | string | Node identity. Ed25519 public key hex (first 16 chars) recommended. Used to dedupe + reject self-echoes. |
| `kind` | no | string | `"beacon"` (default, broadcaster announcing itself) or `"probe"` (listener asking who's out there). |
| `ws_port` | no | int | PeerLink WebSocket port for follow-on session. Default `5460`. |
| `backend_port` | no | int | HTTP backend port. Default `6777`. |
| `tier` | no | string | `"flat"` \| `"regional"` \| `"central"`. Default `"regional"`. |
| `platform` | no | string | `"linux"` \| `"darwin"` \| `"windows"` \| `"android"` \| `"ios"`. Informational. |
| `capabilities` | no | object | Free-form JSON, e.g. `{"gpu": true, "vram_mb": 24576}`. Consumed by the routing layer, not by discovery. |
| `signature` | no | base64 | Ed25519 signature over the canonicalized JSON (excluding the `signature` key itself). HARTOS will *require* this in `HEVOLVE_ENFORCEMENT_MODE=hard`. Clients that can't sign may omit it; they'll still get accepted in `soft` and `warn` modes. |
| `public_key` | no | base64 | Ed25519 public key matching `signature`. Same enforcement rules. |
| `guardrail_hash` | no | hex | Hive guardrails hash. HARTOS rejects mismatched peers when set. Clients without guardrails can omit it. |
| `code_hash` | no | hex | Release binary hash for HARTOS-to-HARTOS code-attestation. Clients can omit; servers compare against the release-hash registry in `hard` mode. |

## Behaviors

### Sender (broadcaster)

- Bind UDP port 6780 with `SO_BROADCAST` + `SO_REUSEADDR`.
- Every `HEVOLVE_DISCOVERY_INTERVAL` seconds (default 30), enumerate broadcast addresses (one per usable IPv4 NIC — not `255.255.255.255`; that picks one OS-chosen interface and misses multi-NIC hosts) and send a packet to each `<bcast>:6780`.
- Body MUST set `type: "hevolve-discovery"`, `kind: "beacon"` (optional, default).

### Receiver (listener)

- Bind UDP port 6780 with `SO_REUSEADDR` (so beacon broadcaster and listener can co-exist in the same process).
- Strip the leading 16-byte magic prefix. If it doesn't match `HEVOLVE_DISCO_V1` → drop.
- Parse the remaining bytes as UTF-8 JSON. If parsing fails → drop.
- Reject if `type != "hevolve-discovery"`.
- Reject if `node_id == own_node_id` (self-echo).
- In `HEVOLVE_ENFORCEMENT_MODE=hard`, also reject if:
  - `signature` is missing or fails to verify against `public_key`
  - `code_hash` not in the release-hash registry
  - `guardrail_hash` mismatches local `get_guardrail_hash()`

### Probe (listener-initiated)

- A client that wants discovery faster than `HEVOLVE_DISCOVERY_INTERVAL` MAY broadcast a `kind: "probe"` packet using the same wire format. HARTOS receivers SHOULD respond with an out-of-band unicast beacon to the probe's source address.
- Mobile clients SHOULD probe on app foreground if their last-known peer hasn't beaconed within 60 s.

## Reference implementations

| Repo | Path | Role |
|---|---|---|
| HARTOS (Python) | `integrations/social/peer_discovery.py` class `AutoDiscovery` | sender + receiver, signs + verifies |
| Hevolve_RN (Android Kotlin) | `android/app/src/main/java/com/hertzai/hevolve/peerlink/PeerLinkDiscovery.kt` | receiver + probe; no signature today |
| Nunba-Companion-iOS (Swift) | *not yet implemented* — see parity gap below | — |
| Nunba desktop (Python pywebview) | inherits HARTOS via bundled Python | sender + receiver |

## Parity status (2026-06-04)

| Implementation | Wire prefix | JSON `type` check | Signature verify | Probe support |
|---|---|---|---|---|
| HARTOS Python | ✅ `HEVOLVE_DISCO_V1` | ✅ `hevolve-discovery` | ✅ (enforced in `hard`) | ✅ |
| Android Kotlin | ✅ fixed today (was `HART_DISCOVERY` JSON field — incompatible) | ✅ fixed today | ⚠️ not implemented; accepted by HARTOS in `soft`/`warn` only | ✅ |
| iOS Swift | ❌ not implemented | — | — | — |

## When this spec changes

- Bump the magic prefix (`HEVOLVE_DISCO_V2`, …) — old implementations stop seeing new ones, which is the correct fail-mode for incompatible changes.
- Update ALL three reference implementations in the same PR. The drift this doc was written to prevent happened because changes landed in only one repo.
