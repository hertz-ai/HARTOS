# HART Agent Protocol (PeerLink)

> Peer-to-peer communication layer for HART OS.
> Decentralized, encrypted, works offline.

## Design Principles

1. **Same-user = simple, everywhere.** Your own devices talk via plain WebSocket — whether on the same LAN, across the internet, or on a regional node you logged into. Trust is based on **authenticated user identity** (matching user_id), not network proximity. No encryption overhead between your own machines.

2. **Cross-user = encrypted.** When data crosses to another user's device, E2E encryption is mandatory. No relay, regional node, or central server can read the payload.

3. **Works fully offline.** A single device with no internet gets full LOCAL delivery. Multiple devices on the same LAN get multi-device sync without any cloud dependency.

4. **Crossbar is safety, not a crutch.** Central Crossbar provides telemetry (metadata only) and kill switch delivery. If Crossbar goes down, peer-to-peer still works.

5. **Data classification per channel.** Channels carrying user prompts/responses (`compute`, `dispatch`, `hivemind`, `sensor`) are classified PRIVATE and always encrypted on cross-user links. Channels carrying public data (`gossip`, `federation`, `events`) are classified OPEN.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     MessageBus                           │
│  bus.publish('chat.response', {user_id, text})           │
│                                                         │
│  Routes to ALL available transports simultaneously:      │
│                                                         │
│  ┌─────────────┐ ┌──────────────┐ ┌─────────────────┐  │
│  │ 1. LOCAL     │ │ 2. PEERLINK  │ │ 3. CROSSBAR     │  │
│  │ EventBus    │ │ Direct WS    │ │ Central         │  │
│  │ Always on   │ │ E2E for peers│ │ Telemetry+Safety│  │
│  └─────────────┘ └──────────────┘ └─────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Trust Levels

| Trust Level | When | Encryption | Example |
|-------------|------|------------|---------|
| `SAME_USER` | Your own devices (LAN, WAN, or regional) — same authenticated user_id | None (plain WebSocket) | Phone ↔ Laptop on home WiFi, Phone ↔ VPS you logged into, Laptop ↔ Regional GPU node where you're authenticated |
| `PEER` | Different user's device | AES-256-GCM session key (X25519 ECDH) | Your node ↔ Contributor's GPU node |
| `RELAY` | Traffic through intermediate | AES-256-GCM (relay sees only ciphertext) | NAT-challenged peer via seed relay |

## Data Classification

| Channel | Data Class | Content | Encrypted (cross-user) |
|---------|------------|---------|----------------------|
| `control` | SYSTEM | Handshake, heartbeat, bye | No (metadata only) |
| `compute` | **PRIVATE** | Inference prompts & results | **Always** |
| `dispatch` | **PRIVATE** | Agent task payloads | **Always** |
| `gossip` | OPEN | Peer lists, health | Only on PEER/RELAY links |
| `federation` | OPEN | Federated posts | Only on PEER/RELAY links |
| `hivemind` | **PRIVATE** | Thought vectors, queries | **Always** |
| `events` | OPEN | Theme changes, config | Only on PEER/RELAY links |
| `ralt` | OPEN | Skill availability | Only on PEER/RELAY links |
| `sensor` | **PRIVATE** | Camera/screen frames | **Always** |

PRIVATE channels are encrypted even on SAME_USER links if the channel config has `encrypt_local: True`. By default, SAME_USER links are unencrypted for simplicity.

## Scenarios

### Scenario 1: Single Device, No Internet

```
[ Device A ]
     │
     └── LOCAL EventBus only
         All events fire within the process.
         No PeerLink, no Crossbar.
         Everything works except multi-device sync.
```

### Scenario 2: Multi-Device, Same LAN, No Internet

```
[ Phone ] ←── plain WebSocket ──→ [ Laptop ]
     │         (SAME_USER)              │
     └── LOCAL EventBus                 └── LOCAL EventBus

Discovery: UDP beacon on port 6780 (compute_mesh)
Encryption: None (your own devices, trusted network)
What works: All events, multi-device sync, compute offload,
            remote desktop, EventBus cross-device
What doesn't: No mobile push (no Firebase), no central telemetry
```

### Scenario 3: Multi-Device, Different Networks, Internet

```
[ Phone ] ←── encrypted WS ──→ [ Remote Laptop ]
     │       (SAME_USER or PEER)          │
     │                                    │
     ├── CROSSBAR telemetry              ├── CROSSBAR telemetry
     │   (metadata only)                 │   (metadata only)
     └── LOCAL EventBus                  └── LOCAL EventBus

NAT traversal: STUN → WireGuard → Crossbar relay (last resort)
Encryption: E2E if cross-user, plain if same-user (user choice)
What works: Everything
```

### Scenario 4: Hive Mode (Many Users, Many Devices)

```
[ User A Phone ] ←── E2E encrypted ──→ [ User B GPU Server ]
     │                (PEER trust)              │
     │                                         │
[ User A Laptop ] ←── plain WS ──→ [ User A VPS ]
     │                (SAME_USER,               │
     │                 WAN — same user_id)      │
     │                                         │
[ User A Laptop ] ←── plain WS ──→ [ User A Phone ]
     │                (SAME_USER, LAN)
     │
     ├── CROSSBAR telemetry (all nodes)
     └── Gossip discovery (all nodes)

Same-user mesh: PeerLink with SAME_USER trust — LAN or WAN (no encryption)
Cross-user compute: PeerLink with PEER trust (E2E encrypted)
  → Hosting peer CANNOT read the prompt being offloaded
Central: Telemetry aggregation + kill switch delivery

ALL tiers participate in hive — flat, regional, central:
  Flat nodes (10 max links): Home devices serving compute, earning Spark
  Regional nodes (50 max links): Regional hubs with relay capacity
  Central nodes (200 max links): Data center capacity
The budget limits simultaneous connections, NOT capabilities.
```

### Scenario 5: Central Goes Down

```
[ All Peers ] ←── PeerLink (still working) ──→ [ All Peers ]
                         │
                    CROSSBAR offline
                         │
                    Kill switch delivery:
                      Gossip backup (minutes instead of seconds)

                    Self-restriction timeline:
                      <1h:  Full operation
                      1-24h: Degraded (no hive compute serving)
                      >24h:  Restricted (agent daemon pauses)
```

### Scenario 6: Internet Down, LAN Peers Available

```
[ Device A ] ←── plain WS ──→ [ Device B ]
                (SAME_USER, LAN)

Works: LAN multi-device sync, local compute offload,
       EventBus cross-device, remote desktop
Doesn't work: WAN peers, central telemetry, mobile push
Grace: Node continues full local operation indefinitely
```

## Encryption Details

### Same-User (No Encryption — LAN or WAN)

```
Phone sends: {"ch":"gossip","id":"abc","d":{peer_list...}}
Laptop receives: same bytes, parsed as JSON
(Works the same whether Phone and Laptop are on home WiFi
 or on opposite sides of the planet — trust = authenticated user_id)
```

No ECDH, no AES, no key exchange. Just WebSocket frames.
Trust determination: compute_mesh device registry (LAN) OR gossip peer
info with matching user_id (WAN/regional).

### Cross-User Peer (E2E Encrypted)

```
Handshake:
  1. Both exchange X25519 public keys
  2. ECDH → shared_secret
  3. HKDF(shared_secret, salt, info='hart-peerlink-v1') → session_key (256-bit)

Per message:
  plaintext = {"ch":"compute","id":"abc","d":{prompt...}}
  nonce = random 12 bytes
  ciphertext = AES-256-GCM(session_key, nonce, plaintext)
  wire = nonce || ciphertext

Key rotation: new ECDH every 3600 seconds (forward secrecy)
```

### What Each Entity Sees

| Entity | Sees | Cannot See |
|--------|------|------------|
| Same-user device | Full plaintext | N/A (trusted) |
| Cross-user peer | Only their own decrypted channel data | Other peers' data |
| Relay peer | Opaque ciphertext + byte count | Any content |
| Regional node | Opaque ciphertext + byte count | Any content |
| Central (Crossbar) | Telemetry metadata: msg counts, byte counts, peer IDs | Any message content |
| ISP / network | WebSocket frames + IP addresses | Everything above TLS |

## NAT Traversal

Strategies tried in order (stop at first success):

1. **LAN Direct** — Same subnet → direct WebSocket (`ws://192.168.1.x:6777/peer_link`)
2. **STUN** — Get external IP → try direct connection to peer's external IP
3. **WireGuard** — Compute mesh tunnel → WebSocket over mesh IP (`ws://10.99.x.x:6796/peer_link`)
4. **Peer Relay** — Route through mutual peer with public IP (future)
5. **Crossbar Relay** — Last resort legacy relay through central WAMP broker

## Safety Guarantees

1. **PeerLink can't bypass central oversight** — Telemetry flows regardless of peer-to-peer activity.
2. **Central can't read peer content** — E2E encryption. Central sees traffic patterns, not data.
3. **Central can always halt** — Master key signed `emergency_halt` via Crossbar (instant) + gossip (backup).
4. **Disconnected nodes self-restrict** — >24h without central → agent daemon pauses.
5. **Relay peers can't inspect payload** — Session encryption makes traffic opaque to intermediaries.

## Device Discovery

PeerLink relies on three discovery mechanisms that feed peers into the connection manager.

### UDP Beacon (LAN)

`AutoDiscovery` in `peer_discovery.py` broadcasts a signed UDP beacon every 30 seconds on port 6780. The beacon contains a magic header (`HEVOLVE_DISCO_V1`), the node's Ed25519 public key, guardrail hash, and HTTP URL. Receivers verify the signature before accepting the peer. This is the primary mechanism for same-LAN, zero-config discovery.

The `hart-discovery` NixOS service runs the beacon as a systemd unit with `CAP_NET_BROADCAST` and tight memory limits.

### Gossip Propagation (WAN)

`GossipProtocol` exchanges peer lists with known peers on every gossip round. Bandwidth profiles (full, constrained, minimal) adjust interval and payload size based on the node's capability tier. Embedded devices gossip every 15 minutes with a single peer using msgpack; full nodes gossip every 60 seconds with 3 peers using JSON.

### Pairing (Cross-Network)

When auto-discovery fails (different networks, carrier NAT), the `PairingManager` in `integrations/channels/security.py` generates a 6-character alphanumeric code. The user sends this code to the agent via any channel adapter (WhatsApp, Telegram, Discord, etc.) using the `/pair <code>` command. Codes expire after 15 minutes and are case-insensitive.

For screen-equipped devices, QR code pairing encodes the node's identity (node_id, public key, OTP, WebSocket URL) for scanning with the Hevolve Droid app.

See [Device Discovery & Pairing](../features/device-pairing.md) for the full user-facing guide.

## File Structure

```
core/peer_link/
├── __init__.py        # Exports: PeerLink, TrustLevel, get_link_manager, get_message_bus
├── link.py            # PeerLink: persistent WebSocket, trust-aware encryption
├── link_manager.py    # Manages all peer connections, auto-upgrade, HTTP fallback
├── channels.py        # Channel definitions, data classification, dispatch
├── nat.py             # NAT traversal orchestration
├── telemetry.py       # Crossbar telemetry + safety (kill switch delivery)
└── message_bus.py     # Unified publish/subscribe (LOCAL + PEERLINK + CROSSBAR)
```

## Integration Points

### How Subsystems Adopt PeerLink

Each subsystem adds ~3 lines: "if peer has a link, use it; else HTTP fallback":

```python
# peer_discovery.py — gossip exchange
def _exchange_with_peer(self, peer_url):
    peer_id = self._url_to_peer_id(peer_url)
    if peer_id:
        from core.peer_link.link_manager import get_link_manager
        link = get_link_manager().get_link(peer_id)
        if link:
            return link.send('gossip', {
                'peers': self._gossip_peer_list(),
                'sender': self._gossip_self_info(),
            }, wait_response=True, timeout=10)
    # Existing HTTP fallback (unchanged)
    resp = pooled_post(f"{peer_url}/api/social/peers/exchange", ...)
```

### MessageBus Migration (from publish_async)

```python
# BEFORE (hart_intelligence_entry.py)
publish_async(f'com.hertzai.hevolve.chat.{user_id}', json.dumps(msg))

# AFTER
from core.peer_link.message_bus import get_message_bus
get_message_bus().publish('chat.response', msg, user_id=user_id)
```

## Cloud Deployment Changes

Docker/cloud deployments that consume Crossbar need updates:

1. **chatbot_pipeline** subscribers (`confirmation.py`, `actions.py`, `android.py`, etc.) continue to work — MessageBus publishes to Crossbar as before. No changes needed for legacy consumers.

2. **start_cloud.sh / docker-compose** — Add `HEVOLVE_TELEMETRY_INTERVAL` env var. Default 60s.

3. **Crossbar config** — Add topics: `com.hartos.telemetry.*`, `com.hartos.control.broadcast`, `com.hartos.node.*.diagnose`.

4. **TLS termination** — All WAN PeerLink connections should use `wss://` (TLS) in addition to E2E encryption. TLS protects metadata (which peer is talking to which).

## Encryption at Rest

Sensitive data stored on disk is encrypted when `HEVOLVE_DATA_KEY` (Fernet key) is configured. Falls back to plaintext when the key is absent — no data is lost, no functionality breaks.

### What's Encrypted

| Data | File Location | Encryption |
|------|--------------|------------|
| Resonance profiles (biometric embeddings, preferences) | `agent_data/resonance/{user_id}_resonance.json` | Fernet (AES-128-CBC + HMAC) |
| Instruction queues (user instructions, context) | `agent_data/instructions/{user_id}_queue.json` | Fernet |
| Ed25519 node private key | `agent_data/node_private_key.pem` | Fernet |
| X25519 ECDH private key | `agent_data/node_x25519_private.key` | Fernet |
| Public keys | `agent_data/node_*_public.*` | **Not encrypted** (public) |

### Design

- **Encrypt on write, decrypt on read** — boundary-only, same pattern as PeerLink transport encryption
- **Auto-detect** — `decrypt_data()` checks for Fernet prefix (`gAAAAA`). Plaintext files are returned as-is.
- **Seamless migration** — existing plaintext files are read correctly and encrypted on next write. No manual conversion needed.
- **Opt-in** — set `HEVOLVE_DATA_KEY` env var (or via `security.secrets_manager`). Without it, everything stays plaintext.
- **Zero hive impact** — encryption is at the persistence boundary only. In-memory data is always plaintext. Hive learnability, inference, training, agent reasoning, and federation are completely unaffected.

### Key Generation

```bash
python -c "from security.crypto import generate_data_key; print(generate_data_key())"
# Export: HEVOLVE_DATA_KEY=<generated-key>
```
