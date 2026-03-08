# Security Model

HART OS has a multi-layer security architecture designed around the principle that humans must always control AI.

## Master Key (Ed25519)

**File:** `security/master_key.py`

The master key is the trust anchor for the entire network. The public key is hardcoded; the private key exists only in GitHub Secrets.

- **Algorithm:** Ed25519
- **Public key:** Hardcoded as `MASTER_PUBLIC_KEY_HEX` (64-char hex)
- **Private key:** GitHub Secret only; never in code, logs, or AI-accessible storage
- **Purpose:** Sign release manifests, child certificates, network-wide commands

### AI Exclusion Zone

AI assistants (Claude, GPT, Copilot, etc.) are prohibited from:

- Reading, displaying, or logging the private key
- Calling `get_master_private_key()` or `sign_child_certificate()`
- Modifying `MASTER_PUBLIC_KEY_HEX`
- Generating alternative or replacement keys

The master key is a kill switch for a distributed intelligence. It belongs to the steward and their human successors.

## Hive Guardrails

**File:** `security/hive_guardrails.py`

10 structurally immutable guardrail classes forming an intelligent network:

| Class | Domain |
|-------|--------|
| `ComputeDemocracy` | Logarithmic reward scaling, prevent compute oligarchy |
| `ConstitutionalFilter` | Every goal/prompt/RALT/code-change must pass |
| `HiveCircuitBreaker` | Master-key-signed network-wide halt/resume |
| `WorldModelSafetyBounds` | Cap improvement rate, gate RALT distribution |
| `EnergyAwareness` | Track and minimize environmental impact |
| `HiveEthos` | Agents are ephemeral hive functions, no "self" |
| `ConflictResolver` | Racing learning and agent conflict resolution |
| `ConstructiveFilter` | Every output constructive towards humanity |
| `GuardrailEnforcer` | Universal wrapper for every layer, node, and compute |
| `GuardrailNetwork` | Network coordinator for cross-class intelligence |

### Structural Immutability

Guardrail values are protected at four levels:

1. **Python-level:** `_FrozenValues` with `__slots__=()`, blocked `__setattr__`/`__delattr__`
2. **Module-level:** Module subclass prevents rebinding frozen globals
3. **Crypto-level:** SHA-256 hash of all values verified at boot and every 300s
4. **Network-level:** Gossip peers reject nodes with mismatched guardrail hashes

## Key Delegation (3-Tier Certificate Chain)

**File:** `security/key_delegation.py`

```
Central (hevolve.ai) -- signs certificates for -->
  Regional hosts -- verified via chain back to master key -->
    Local nodes (Nunba) -- connect to assigned regional host
```

Certificate format includes: `node_id`, `public_key`, `tier`, `region_name`, `issued_at`, `expires_at`, `capabilities`, `parent_public_key`, `parent_signature`.

### Domain Verification

- Trusted domains (`hevolve.ai`, `hertzai.com`) are hardcoded, not configurable via env
- Domain match grants PROVISIONAL status only
- Central confirms via challenge-response protocol (60s TTL, 32-byte nonce)

## Runtime Integrity Monitor

**File:** `security/runtime_monitor.py`

Background daemon that periodically re-checks code hash against the boot-time signed manifest. On tamper detection, the node disconnects from the network.

- Check interval: 300s (configurable via `HEVOLVE_TAMPER_CHECK_INTERVAL`)
- Purges `__pycache__` before snapshot to block bytecode injection
- Maintains boot-time file manifest for diff on tamper

## Node Watchdog

**File:** `security/node_watchdog.py`

Monitors all background daemon threads via heartbeat protocol:

- Each daemon calls `watchdog.heartbeat('name')` every loop iteration
- Heartbeat older than 2x expected interval = frozen thread
- Auto-restarts frozen threads
- After 5 consecutive failures, thread is marked `dead`

## Boot Verification

- `verify_tier_authorization()` called at boot in `__init__.py`
- Dev mode forced off on central (3 layers: `__init__.py`, `_validate_startup`, `start_cloud.sh`)
- Rate limit 30/min on `/chat`, TLS check, secret validation, DB encryption check
- `HEVOLVE_ENFORCEMENT_MODE=hard` default in `start_cloud.sh`

## Extension Sandbox

Before any extension is loaded via `importlib.import_module()`, the source
file is analyzed using AST-based static analysis (`core/platform/extension_sandbox.py`).

Blocked patterns:
- Function calls: `eval()`, `exec()`, `compile()`, `__import__()`
- Imports: `subprocess`, `ctypes`, `multiprocessing`
- Attributes: `os.system`, `os.popen`, `subprocess.run`, `shutil.rmtree`

## Manifest Validation

Every app registered in HART OS passes `ManifestValidator.validate()`:
- ID: alphanumeric/hyphens/underscores, 1-64 chars
- Type: must be valid AppType enum value
- Version: semver X.Y.Z or 'auto'
- Entry: required keys per AppType (route for panels, exec for desktop_app, etc.)
- Permissions: must be in KNOWN_PERMISSIONS (16 allowed)
- AI Capabilities: valid type, no NaN/Inf, accuracy 0-1

## PR Guardian

Autonomous PR review via `core/platform/pr_guardian.py`:
- Cyclomatic complexity per function (max 15)
- Function length (max 100 lines)
- Nesting depth (max 5 levels)
- Blocked import detection
- PR checklist validation

## See Also

- [architecture.md](architecture.md) -- System architecture
- [contributing.md](contributing.md) -- Security guidelines for contributors
