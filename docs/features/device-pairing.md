# Device Discovery & Pairing

How HART OS devices find each other, establish trust, and communicate -- from headless embedded boards to screen-equipped desktops.

## How Devices Find Each Other

Three complementary mechanisms work together so that devices discover peers automatically, regardless of network topology.

### UDP Beacon Discovery

The `AutoDiscovery` service broadcasts a signed beacon every 30 seconds on **UDP port 6780**. Each beacon contains a magic header (`HEVOLVE_DISCO_V1`), the node's Ed25519 public key, its guardrail hash, and the HTTP URL where PeerLink can connect.

Beacons are signed with the node's Ed25519 private key. Receivers verify the signature before adding the peer to their local peer list. Unsigned or malformed beacons are silently dropped.

On HART OS (NixOS), the `hart-discovery` systemd service runs the beacon automatically with `CAP_NET_BROADCAST` capability and tight resource limits (48 MB RAM on edge devices, 128 MB on full nodes).

### Gossip Protocol Propagation

Once a node has at least one peer (from a seed list, UDP beacon, or manual entry), the `GossipProtocol` propagates peer lists across the network. Each gossip round, a node picks a random subset of known peers (the _fanout_) and exchanges peer lists.

Gossip bandwidth is tier-aware:

| Bandwidth Profile | Gossip Interval | Fanout | Payload | Used By |
|-------------------|-----------------|--------|---------|---------|
| **full** | 60 s | 3 peers | JSON (all fields) | Standard, full, compute_host, flat/regional/central |
| **constrained** | 300 s | 2 peers | JSON (compact -- essential fields only) | Observer, lite |
| **minimal** | 900 s | 1 peer | msgpack (~60% smaller) | Embedded devices |

Profile is auto-selected from the node's capability tier or can be overridden with `HEVOLVE_GOSSIP_BANDWIDTH`.

### NAT Traversal (5-Tier Strategy)

When two peers cannot reach each other directly, the `NATTraversal` class in `core/peer_link/nat.py` tries connection strategies in order, stopping at first success:

1. **LAN Direct** -- Same subnet: direct WebSocket to peer IP (`ws://192.168.1.x:6777/peer_link`)
2. **STUN** -- Get external IP via STUN server, try direct connection to peer's external IP
3. **WireGuard** -- Use the compute mesh WireGuard tunnel (`ws://10.99.x.x:6796/peer_link`)
4. **Peer Relay** -- Route through a mutual peer with a public IP (future)
5. **Crossbar Relay** -- Last resort legacy relay through the central WAMP broker

For same-user devices on the same LAN, strategy 1 always works. For cross-user WAN connections, strategies 2--5 are attempted based on the detected `NATType` (full_cone, restricted, symmetric, or public).

## Headless Device Boot (No Screen)

Embedded and headless devices (Raspberry Pi, IoT hubs, robot controllers) use `embedded_main.py` as their entry point. The boot sequence requires zero human intervention:

```
Step 1: System check -- hardware detection, tier classification
Step 2: Identity    -- Ed25519 keypair generation (or load existing)
Step 3: Guardrails  -- Verify guardrail integrity for federation
Step 4: Platform    -- EventBus, ServiceRegistry, MessageBus
Step 5: Database    -- SQLite for fleet commands and sync queue
Step 6: Main loop   -- Gossip heartbeat + fleet command handling
```

**What happens automatically on first boot:**

1. `get_or_create_keypair()` generates a fresh Ed25519 keypair at `agent_data/node_private_key.pem`
2. `run_system_check()` detects hardware (CPU cores, RAM, GPIO availability, serial ports) and assigns a capability tier (embedded, observer, lite, standard, full, compute_host)
3. `compute_code_hash()` fingerprints the running code for federation verification
4. The `hart-discovery` service starts broadcasting UDP beacons
5. Any HART node on the same LAN picks up the beacon, verifies its signature, and adds it to the gossip peer list
6. Within one gossip round (~30 s on full bandwidth), the new device is known to the local mesh

**No screen, no keyboard, no configuration file needed.** Plug in power and network, and the device joins the mesh.

Environment variables for headless mode:

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEVOLVE_HEADLESS` | `true` | Required. Enables headless mode. |
| `HEVOLVE_CODE_HASH_PRECOMPUTED` | -- | Skip code hash computation (ROM/SD card) |
| `HEVOLVE_FORCE_TIER` | auto-detected | Force capability tier (e.g. `embedded`) |
| `HEVOLVE_GOSSIP_BANDWIDTH` | auto from tier | Override gossip bandwidth profile |

## Pairing Code (When Auto-Discovery Fails)

When devices cannot see each other on the LAN (different networks, firewalled, behind carrier NAT), users can pair manually using a short alphanumeric code.

### How It Works

1. The `PairingManager` in `integrations/channels/security.py` generates a 6-character alphanumeric code (excluding ambiguous characters like `0`, `O`, `1`, `I`).
2. The code is HMAC-signed for tamper resistance.
3. The user sends the code to the agent via **any channel** -- WhatsApp, Telegram, Discord, Slack, SMS, or any of the 30+ supported channel adapters.
4. The `/pair <code>` built-in command verifies the code and creates a `PairedSession` linking the channel identity to the agent user.

### Code Properties

| Property | Value |
|----------|-------|
| Length | 6 characters |
| Alphabet | `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no ambiguous chars) |
| Expiration | 15 minutes (configurable via `code_expiry_minutes`) |
| Case sensitivity | Case-insensitive (codes are uppercased before comparison) |
| Security | HMAC-signed with `PAIRING_SECRET_KEY` |

### Example Flow

```
1. User runs: hart pair --user-id 123 --prompt-id 456
   -> Code generated: "HK7W3M"

2. User opens WhatsApp chat with their HART agent
   -> Sends: /pair HK7W3M

3. PairingManager.verify_pairing("whatsapp", "user_phone", "HK7W3M")
   -> Returns PairedSession with user_id=123

4. From now on, all messages from that WhatsApp account
   are routed to user 123's agent context.
```

### Persistence

Pairing state is persisted to `agent_data/pairing_data.json`. Sessions survive server restarts.

## QR Code Pairing (Screen-Equipped Devices)

For devices with a screen (desktops, tablets, smart displays), QR code pairing provides a faster alternative to typing codes.

### What the QR Contains

The QR code encodes a JSON payload with the device's identity:

| Field | Purpose |
|-------|---------|
| `node_id` | Ed25519 public key hex prefix (device identifier) |
| `public_key` | Full Ed25519 public key for signature verification |
| `otp` | One-time pairing token (expires with the QR) |
| `ws_url` | WebSocket URL for PeerLink connection |

### Pairing Flow

1. Device displays QR code on its screen (or on a connected display)
2. User scans with the Hevolve Droid app (React Native) or a web camera connected to another HART node
3. The scanning device verifies the Ed25519 public key and OTP
4. PeerLink connection established with SAME_USER trust
5. The agent guides the user: _"I see your Kitchen Hub. What should I call it?"_

### When QR Is Preferred

- First-time setup of a desktop or tablet when the mobile app is already configured
- Adding a smart display or TV to an existing device mesh
- Environments where typing a 6-character code is inconvenient (e.g., TV remote)

## Firewall & Network Negotiation

### HART Firewall (NixOS)

The `hart-firewall.nix` module provides nftables-based firewall management with four zones:

| Zone | Purpose | Access |
|------|---------|--------|
| **internal** | LAN devices on trusted interfaces | Full access |
| **mesh** | Compute mesh peers | Restricted to mesh ports |
| **external** | Internet traffic | Minimal ingress |
| **management** | SSH + API administration | Locked to trusted IPs |

**Default open ports:**

- **TCP 6777** -- Backend API (Flask/Waitress)
- **TCP 22** -- SSH
- **UDP 6780** -- Peer discovery beacon

Rate limiting is enabled by default (25 new TCP connections/second, burst 50) for SYN flood protection.

### CLI Tool

```bash
hart-firewall status      # Show active rules and default zone
hart-firewall ports        # Show listening ports
hart-firewall block <IP>   # Block an IP address
hart-firewall unblock <IP> # Unblock an IP address
```

### NAT-PMP / UPnP (Planned)

Automatic port mapping for home routers is planned but not yet implemented. Currently, users behind carrier-grade NAT rely on the WireGuard mesh or Crossbar relay strategies.

## Device Control After Pairing

Once paired, any channel can send commands to any of the user's devices through the `device_control()` agent tool.

### How It Works

```
User (via WhatsApp): "Turn on the living room light"
    |
    v
Channel Adapter -> /chat endpoint -> Agent LLM
    |
    v
device_control(action="turn on light", device_hint="iot hub")
    |
    v
DeviceRoutingService.pick_device(db, user_id, capability)
    |
    v
PeerLink dispatch channel -> Target device executes locally
    |
    v
FleetCommandService.execute_command('device_control', params)
```

### Trust Enforcement

Device control commands are only accepted from **SAME_USER** trust links. If a PEER or RELAY link attempts to send a `device_control` message, the request is rejected with a warning:

```python
if link.trust != TrustLevel.SAME_USER:
    return {'success': False, 'message': 'Only SAME_USER devices can send control commands'}
```

### Supported Actions

The `device_control` tool routes actions through `FleetCommandService`, which supports:

- **GPIO**: Pin read/write for sensors and actuators
- **Serial**: Send/receive data on serial ports
- **Shell commands**: Execute commands on the target device
- **Sensor config**: Adjust polling intervals and pin assignments
- **TTS/Audio**: Stream speech to a device with speaker capability

### Device Routing

`DeviceRoutingService` selects the best target device based on form factor and capability:

| Priority | Form Factor | Typical Use |
|----------|-------------|-------------|
| 1 | phone | TTS, notifications, mobile sensors |
| 2 | desktop | Compute, display, full agent |
| 3 | tablet | Display, touch interaction |
| 4 | tv | Media display, ambient |
| 5 | embedded | GPIO, sensors, actuators |
| 6 | robot | Physical actions, embodied AI |

### FleetCommand Fallback

If PeerLink cannot reach the device (offline, network issue), the command is queued in the `FleetCommand` database table. When the device comes back online, it drains pending commands on its next gossip round. Commands are signed with the issuer's certificate and verified before execution.

## Trust Chain

Every layer of the discovery and pairing system is secured by cryptographic verification.

### Ed25519 Signatures on Beacons

UDP beacons include an Ed25519 signature over the beacon payload. Receivers verify against the sender's public key before accepting the peer. This prevents beacon spoofing on shared networks.

### Guardrail Hash Verification

During gossip exchange, peers compare guardrail hashes (SHA-256 of the 33 constitutional rules). Peers with mismatched guardrail hashes are rejected from federation -- they are running modified code that may not enforce the same safety guarantees.

### Code Hash Verification

Each node computes a hash of its running code via `compute_code_hash()`. This hash is included in gossip messages and checked against the release registry. Nodes running unsigned or unrecognized code versions are flagged.

### PeerLink SAME_USER Trust

Between a user's own devices, PeerLink establishes SAME_USER trust based on authenticated user identity. Traffic is unencrypted (no overhead) because both endpoints are controlled by the same person. Cross-user connections use PEER trust with full AES-256-GCM encryption.

### Origin Attestation (Federation)

For federation with remote hives, `security/origin_attestation.py` provides cryptographic proof of origin. The attestation includes the HART OS identity, master public key, license, and a SHA-256 fingerprint. Federation handshakes require a signed attestation -- forks without the master key cannot join.

## Source Files

| File | Purpose |
|------|---------|
| `integrations/social/peer_discovery.py` | GossipProtocol, AutoDiscovery (UDP beacon) |
| `core/peer_link/nat.py` | NATTraversal (5-tier strategy) |
| `core/peer_link/link.py` | PeerLink, TrustLevel |
| `core/peer_link/link_manager.py` | Connection management, trust upgrade |
| `integrations/channels/security.py` | PairingManager, PairingCode, PairedSession |
| `integrations/channels/commands/builtin.py` | `/pair`, `/unpair` commands |
| `integrations/social/device_routing_service.py` | DeviceRoutingService |
| `integrations/social/fleet_command.py` | FleetCommandService (queen bee dispatch) |
| `core/agent_tools.py` | `device_control()` tool |
| `embedded_main.py` | Headless boot sequence |
| `nixos/modules/hart-discovery.nix` | Systemd service for UDP beacon |
| `nixos/modules/hart-firewall.nix` | nftables firewall zones |
| `security/origin_attestation.py` | Cryptographic origin proof |
| `security/node_integrity.py` | Ed25519 keypair, code hash |
