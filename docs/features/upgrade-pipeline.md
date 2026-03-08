# Autonomous Upgrade Pipeline

HART OS includes a fully autonomous, 7-stage upgrade pipeline that detects new versions, validates them through multiple safety gates, deploys to a canary population, and rolls out to the full network — all without human intervention.

## Architecture Overview

```
Version Detection (code hash change)
         │
         ▼
┌─────────────────────────────────────────────┐
│  7-Stage Pipeline (UpgradeOrchestrator)     │
│                                             │
│  1. BUILD    ─ Compute code hash            │
│  2. TEST     ─ Run regression suite (≥95%)  │
│  3. AUDIT    ─ ConstitutionalFilter check   │
│  4. BENCHMARK─ Compare vs previous version  │
│  5. SIGN     ─ Ed25519 release manifest     │
│  6. CANARY   ─ 10% of nodes, 30 min        │
│  7. DEPLOY   ─ Gossip broadcast to all      │
│                                             │
│  State: agent_data/upgrade_state.json       │
└─────────────────────────────────────────────┘
         │
         ▼
   Node Auto-Update
   (signature verified, pinned commit, graceful restart)
```

## Components

### Upgrade Orchestrator

**File:** `integrations/agent_engine/upgrade_orchestrator.py`

The central pipeline controller. Singleton with thread-safe state management and crash-recovery via persistent JSON state.

| Stage | Gate Condition | On Failure |
|-------|---------------|------------|
| **BUILD** | `compute_code_hash()` succeeds | Pipeline fails |
| **TEST** | ≥95% regression test pass rate | Pipeline fails |
| **AUDIT** | `ConstitutionalFilter` self-test passes | Pipeline fails |
| **BENCHMARK** | All benchmarks match or improve vs previous | Rollback |
| **SIGN** | `scripts/sign_release.py` produces valid Ed25519 signature | Pipeline fails |
| **CANARY** | 10% of nodes healthy for 30 min (exception rate <50% increase) | Rollback to all |
| **DEPLOY** | Gossip broadcast accepted by peers | Logged |

**Stage States:**

```python
class UpgradeStage(enum.Enum):
    IDLE = 'idle'
    BUILDING = 'building'
    TESTING = 'testing'
    AUDITING = 'auditing'
    BENCHMARKING = 'benchmarking'
    SIGNING = 'signing'
    CANARY = 'canary'
    DEPLOYING = 'deploying'
    COMPLETED = 'completed'
    ROLLED_BACK = 'rolled_back'
    FAILED = 'failed'
```

### Version Detection

```python
# upgrade_orchestrator.py
def check_for_new_version() -> dict:
    """Compare current code hash vs stored hash.
    Returns: {new_version_detected: bool, version: str, code_hash: str}
    """
```

Version is extracted from `git describe --tags` or falls back to `auto-{timestamp}`.

### Canary Deployment

The canary stage selects 10% of active `PeerNode` records with `master_key_verified=True`, deploys the update, and monitors 5 health criteria:

1. Exception rate increase (<50% threshold)
2. World model health check
3. Node responsiveness (heartbeat)
4. Guardrail integrity hash match
5. Benchmark regression detection

If any criterion fails, `_broadcast_rollback()` reverts all canary nodes.

**Configuration:**

| Environment Variable | Default | Purpose |
|---------------------|---------|---------|
| `HEVOLVE_CANARY_DURATION_SECONDS` | `1800` (30 min) | How long canary runs |
| `HEVOLVE_CANARY_PCT` | `0.10` (10%) | Fraction of nodes in canary |

### Auto-Deploy Service

**File:** `integrations/agent_engine/auto_deploy_service.py`

Triggered when a PR is merged to main:

```
on_pr_merged(repo_url, merge_sha)
  1. git pull origin main
  2. Run full regression test suite (gate: ≥95% pass)
  3. Capture benchmark snapshot
  4. Check is_upgrade_safe() via BenchmarkRegistry
  5. Sign release manifest (REQUIRED — unsigned = blocked)
  6. notify_nodes() via gossip to all PeerNode records
  7. Each node runs auto_update_node()
```

### Node Auto-Update Flow

```
auto_update_node(version, manifest)
  1. VERIFY manifest.signature via verify_release_manifest()
     → Unsigned manifest = REJECTED (no bypass)
  2. Compare code_hash → if already up-to-date, return
  3. git fetch origin main + git checkout manifest.merge_sha
  4. Request graceful restart via NodeWatchdog
  5. Return {updated: true, old_version, new_version}
```

### OTA Update Service (System-Level)

**File:** `deploy/distro/update/hart-update-service.py`

Runs as a systemd service (`hart-update.service`) triggered daily by `hart-update.timer`.

```
check_for_updates() → GitHub Releases API
  → check_fleet_approval(version) → regional host gates
  → download_update(url, checksum_url) → SHA-256 + Ed25519
  → _run_orchestrated_upgrade(version, bundle_path) → 7-stage pipeline
  → apply_update(bundle_path) → backup, extract, pip install, migrate, restart
  → rollback(backup_dir) → if apply fails
```

**Safety gates:** SHA-256 checksum, Ed25519 signature against `MASTER_PUBLIC_KEY_HEX`, fleet approval from regional host.

## Upgrade Agent (Autonomous)

### Goal Type Registration

```python
# goal_manager.py
register_goal_type('upgrade', _build_upgrade_prompt, tool_tags=['upgrade'])
```

The upgrade prompt instructs the agent to:

1. Check for new versions with `check_upgrade_status`
2. Capture pre-upgrade benchmarks with `capture_benchmark`
3. Start 7-stage pipeline with `start_upgrade`
4. Advance each stage with `advance_upgrade_pipeline`
5. Monitor canary health with `check_canary_health`
6. Rollback if ANY degradation with `rollback_upgrade`
7. Compare benchmarks with `compare_benchmarks`

**Safety rule:** ALL benchmarks must improve or match. Any regression = rollback. Zero tolerance.

### Bootstrap Goal (Continuous Monitor)

```python
# goal_seeding.py → bootstrap_upgrade_monitor
goal_type: 'upgrade'
title: 'Continuous Version Upgrade Monitor'
config: {mode: 'monitor', continuous: True}
spark_budget: 200
```

Seeded at boot, runs continuously to detect and apply upgrades.

### Upgrade Tools (10 AutoGen Functions)

**File:** `integrations/agent_engine/upgrade_tools.py`

| Tool | Purpose |
|------|---------|
| `check_upgrade_status()` | Poll orchestrator + detect new versions |
| `capture_benchmark(version, tier)` | Snapshot (fast/heavy/all) |
| `compare_benchmarks(old, new)` | Report regressions |
| `start_upgrade(version)` | Initiate 7-stage pipeline |
| `advance_upgrade_pipeline()` | Execute next stage |
| `check_canary_health()` | Monitor canary nodes |
| `rollback_upgrade(reason)` | Safe rollback at any stage |
| `get_benchmark_history()` | List all snapshots |
| `register_benchmark(repo_url, name)` | Install from git repo |
| `list_benchmarks()` | List registered adapters |

## CI/CD Integration

### Release Signing (GitHub Actions)

**File:** `.github/workflows/release-sign.yml`

Triggered on push tags matching `v*`:

1. Compute code hash via `compute_code_hash()`
2. Compute file manifest hash
3. Sign release manifest with master key
4. Verify signature (belt-and-suspenders)
5. Upload `release_manifest.json` to GitHub Release

### Release Gate (Manual Trigger)

**File:** `.github/workflows/release.yml`

1. Run 6 test files (test gate)
2. Validate semver format
3. Check for duplicate tags
4. Build package
5. Upload to GitHub Release

### Security Scan (On Push/PR)

**File:** `.github/workflows/security-scan.yml`

Three parallel gates:

- **Bandit** (SAST) — static security analysis
- **pip-audit** — dependency vulnerability scan
- **flake8** — code quality

## State Persistence

Upgrade state is persisted to `agent_data/upgrade_state.json`:

```json
{
  "stage": "building",
  "version": "v2.0",
  "git_sha": "",
  "started_at": 1771864669.14,
  "stage_history": [
    {"stage": "building", "at": 1771864669.14}
  ]
}
```

Path resolution supports standalone, Nunba bundled (`~/Documents/Nunba/data/agent_data/`), and DB-path-relative modes.

## Deployment Manifest

**File:** `deploy/deployment-manifest.json`

Defines 5 deployment modes:

| Mode | Tier | Database | Use Case |
|------|------|----------|----------|
| `standalone` | flat | SQLite | Developer machine |
| `bundled` | flat | SQLite | Nunba desktop app |
| `headless` | flat | SQLite | Embedded/headless |
| `regional` | regional | SQLite/Postgres | Systemd services |
| `central` | central | PostgreSQL | Authority node |

## Test Coverage

| Test File | Coverage |
|-----------|----------|
| `tests/unit/test_upgrade_pipeline.py` | Orchestrator stages, state persistence |
| `tests/unit/test_federation_upgrade.py` | BenchmarkRegistry + upgrade safety |
| `tests/unit/test_ota_update.py` | HartUpdateService, signature verification |
| `tests/unit/test_deployment_scenarios.py` | Deployment mode tier tests |
