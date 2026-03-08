# HART OS Hardening & Distro Excellence Plan

## Scope
Systematic review of all HART OS distro changes with security hardening for regional deployment, Nunba packaging awareness, deployment mode coverage, language optimization, and comprehensive test coverage.

**Principle**: Preserve ALL existing functionality. No breaking changes.

---

## PART A — CRITICAL SECURITY FIXES (6 items)

### A1. TFTP Path Traversal Hardening
**File**: `deploy/distro/pxe/hart-pxe-server.py:73-77`
**Problem**: `replace('..', '').lstrip('/')` can be bypassed with `....//` (nested traversal).
**Fix**: Use `os.path.normpath()` + verify result stays under `serve_dir` via `os.path.commonpath()`. Add explicit reject if resolved path escapes.
```python
filename = os.path.normpath(filename.replace('..', '').lstrip('/'))
filepath = os.path.join(self.server.serve_dir, filename)
# Fail-closed: verify path stays under serve_dir
if not os.path.commonpath([filepath, self.server.serve_dir]) == self.server.serve_dir:
    self._send_error(sock, 2, "Access denied")
    return
```
**Tests**: `test_pxe_server.py` — add `test_nested_traversal`, `test_encoded_traversal`, `test_commonpath_guard`

### A2. Mandatory Update Signature Verification
**File**: `deploy/distro/update/hart-update-service.py:199-211`
**Problem**: Ed25519 signature check is optional — if `.sig` missing, update proceeds unsigned.
**Fix**: Add `HART_UPDATE_REQUIRE_SIGNATURE` env var (default `true` in production). When true, reject unsigned bundles.
```python
if require_sig:
    raise ValueError("Unsigned update rejected (HART_UPDATE_REQUIRE_SIGNATURE=true)")
else:
    logger.warning("No .sig file found — signature verification skipped (dev mode)")
```
**Tests**: `test_ota_update.py` — add `test_mandatory_signature_rejects_unsigned`, `test_dev_mode_allows_unsigned`

### A3. Model Download Checksum Verification
**File**: `deploy/distro/first-boot/hart-first-boot.sh:147-164`
**Problem**: Downloads model over HTTPS without checksum. MITM → backdoored LLM.
**Fix**: Add embedded SHA-256 hash, verify after download, reject on mismatch.
```bash
EXPECTED_HASH="abc123..."  # Pinned in hart.env or this script
ACTUAL_HASH=$(sha256sum "$MODEL_PATH" | cut -d' ' -f1)
if [[ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]]; then
    echo "[HART OS] MODEL VERIFICATION FAILED" >> /var/log/hart/first-boot.log
    rm -f "$MODEL_PATH"
fi
```
**Tests**: `test_distro_configs.py` — add `test_first_boot_has_model_hash_check`

### A4. Network Provisioner Input Validation
**File**: `integrations/agent_engine/network_provisioner.py:225-250`
**Problem**: `target_host`, `ssh_user` passed to shell commands without validation. Command injection possible.
**Fix**: Add `_validate_provisioning_params()` — regex-validate hostname (FQDN/IPv4), username (alphanumeric+underscore), port (int 1-65535).
```python
def _validate_provisioning_params(target_host, ssh_user, backend_port):
    if not re.match(r'^[a-zA-Z0-9._-]+$', target_host):
        raise ValueError(f"Invalid target_host: {target_host}")
    if not re.match(r'^[a-zA-Z0-9_-]+$', ssh_user):
        raise ValueError(f"Invalid ssh_user: {ssh_user}")
    if not (1 <= int(backend_port) <= 65535):
        raise ValueError(f"Invalid port: {backend_port}")
```
**Tests**: `tests/test_network_provisioner.py` — add `test_command_injection_hostname`, `test_command_injection_user`, `test_invalid_port`

### A5. SSH Host Key Policy Hardening
**File**: `integrations/agent_engine/network_provisioner.py:68-75`
**Problem**: `WarningPolicy()` accepts unknown SSH host keys silently. MITM possible on first connection.
**Fix**: Use `AutoAddPolicy()` with explicit logging + user-facing warning. Add `strict_host_key` parameter that uses `RejectPolicy()` for production.
```python
if strict_host_key:
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
else:
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    logger.warning("Auto-adding SSH host key for %s (first connect)", target_host)
```
**Tests**: `tests/test_network_provisioner.py` — add `test_strict_host_key_rejects_unknown`, `test_auto_add_logs_warning`

### A6. OEM SSH Host Key Wipe
**File**: `deploy/distro/oem/hart-oem-config.sh:21-33`
**Problem**: OEM --prepare wipes HART keys but NOT `/etc/ssh/ssh_host_*`. All shipped devices share same SSH fingerprint.
**Fix**: Add SSH host key removal in --prepare, regeneration in --user-setup.
```bash
# In --prepare:
rm -f /etc/ssh/ssh_host_*_key*
# In --user-setup:
ssh-keygen -A  # Regenerate all host key types
```
**Tests**: `test_distro_configs.py` — add `test_oem_prepare_removes_ssh_keys`, `test_oem_user_setup_regenerates_ssh_keys`

---

## PART B — HIGH-PRIORITY HARDENING (10 items)

### B1. AppArmor Profiles for All Services
**New files**: `deploy/linux/apparmor/` directory
- `hart-backend` — allow: /opt/hart/**, /var/lib/hart/**, /var/log/hart/**; deny: /etc/shadow, /root/**
- `hart-agent-daemon` — same as backend + deny network listen (only localhost)
- `hart-discovery` — allow: network broadcast, /var/lib/hart/node_*.key; deny: /opt/hart/agent_data write
- `hart-vision` — allow: /opt/hart/models/**, /dev/video*; deny: network listen except 9891+5460
- `hart-llm` — allow: /opt/hart/models/**, network listen 8080; deny: /var/lib/hart/node_private.key

**Install**: install.sh copies to `/etc/apparmor.d/`, runs `apparmor_parser -r`
**Tests**: `test_distro_configs.py` — add TestAppArmorProfiles class (5 profiles × exists + syntax + deny rules)

### B2. Systemd Service Isolation
**Files**: All 6 `.service` files in `deploy/linux/systemd/`
**Add to each service**:
```ini
PrivateUsers=yes
ProtectClock=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
MemoryDenyWriteExecute=yes
```
Move agent_data to `/var/lib/hart/agent_data` (not under `/opt/hart/`).
**Tests**: `test_distro_configs.py` — add `test_service_has_private_users`, `test_service_has_syscall_filter`, etc.

### B3. Journald Configuration
**New file**: `deploy/distro/journald/hart-journald.conf`
```ini
[Journal]
SystemMaxUse=2G
MaxRetentionSec=30day
MaxFileSec=7day
Compress=yes
ForwardToSyslog=yes
```
**Install**: Copy to `/etc/systemd/journald.conf.d/hart.conf`
**Tests**: `test_distro_configs.py` — add TestJournaldConfig

### B4. Fail2ban for SSH
**New file**: `deploy/linux/fail2ban/hart-sshd.conf`
```ini
[sshd]
enabled = true
port = ssh
maxretry = 5
bantime = 3600
findtime = 600
```
**Install**: install.sh copies to `/etc/fail2ban/jail.d/`, restarts fail2ban
**Tests**: `test_distro_configs.py` — add TestFail2banConfig

### B5. Unattended Security Updates
**New file**: `deploy/distro/apt/50hart-unattended-upgrades`
```
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Mail "root";
```
**Install**: first-boot.sh copies to `/etc/apt/apt.conf.d/`, enables unattended-upgrades
**Tests**: `test_distro_configs.py` — add TestUnattendedUpgrades

### B6. Auditd Rules for Security Events
**New file**: `deploy/linux/audit/hart-audit.rules`
```
-w /var/lib/hart/node_private.key -p rwa -k hart_key_access
-w /etc/hart/hart.env -p wa -k hart_config_change
-w /opt/hart/ -p wa -k hart_code_change
-a always,exit -F arch=b64 -S execve -F uid=hart -k hart_exec
```
**Install**: install.sh copies to `/etc/audit/rules.d/`, runs augenrules --load
**Tests**: `test_distro_configs.py` — add TestAuditRules (rules exist, watch paths correct, key names)

### B7. Private Key Immutable Flag
**File**: `deploy/distro/first-boot/hart-first-boot.sh:57-60`
**Add after key generation**:
```bash
chattr +i "$DATA_DIR/node_private.key"  # Immutable — even root can't modify
```
**Recovery must**: `chattr -i` before rm in hart-recovery.sh
**Tests**: `test_distro_configs.py` — add `test_first_boot_sets_immutable`, `test_recovery_clears_immutable`

### B8. DNS Hardening
**New file**: `deploy/distro/network/hart-resolved.conf`
```ini
[Resolve]
DNSSEC=yes
DNSOverTLS=opportunistic
LLMNR=no
MulticastDNS=no
```
**Install**: Copy to `/etc/systemd/resolved.conf.d/hart.conf`
**Tests**: `test_distro_configs.py` — add TestDNSHardening

### B9. Update Service Run as hart User
**File**: `deploy/distro/update/hart-update.service`
**Change**: `User=root` → `User=hart`
**Add**: sudoers rule for hart to run `systemctl restart hart.target`
**New file**: `deploy/linux/sudoers/hart-update`
```
hart ALL=(root) NOPASSWD: /bin/systemctl restart hart.target
hart ALL=(root) NOPASSWD: /bin/systemctl daemon-reload
```
**Tests**: `test_distro_configs.py` — update `test_update_service_user`, add TestSudoersConfig

### B10. Firewall Inter-Service Rules
**File**: `deploy/linux/firewall/hart-ufw.profile` (extend)
**Add**: Rules restricting internal service ports to localhost only:
```
# Internal only — not exposed
ports=9891/tcp|5460/tcp|8080/tcp
rule=deny  # External access denied; localhost allowed via systemd binding to 127.0.0.1
```
**Tests**: `test_distro_configs.py` — add `test_internal_ports_localhost_only`

---

## PART C — DEPLOYMENT MODE AWARENESS (5 items)

### C1. Deployment Mode Manifest
**New file**: `deploy/DEPLOYMENT_MODES.json`
A machine-readable manifest declaring all 5 deployment modes with their properties:
```json
{
  "modes": {
    "standalone": {"entry": "langchain_gpt_api.py", "db": "sqlite", "cert": "flat", ...},
    "bundled":    {"entry": "Nunba.exe", "db": "sqlite", "cert": "flat", ...},
    "headless":   {"entry": "embedded_main.py", "db": "sqlite", "cert": "flat", ...},
    "regional":   {"entry": "install.sh", "db": "sqlite|postgres", "cert": "regional", ...},
    "central":    {"entry": "install.sh", "db": "postgres", "cert": "central", ...}
  },
  "tiers": { ... },
  "services": { ... }
}
```
**Nunba packaging**: `setup_freeze_nunba.py` can read this to know what to include/exclude per mode.
**Tests**: `test_distro_configs.py` — add TestDeploymentManifest (valid JSON, all 5 modes, all 6 tiers)

### C2. Nunba Build Awareness of Deploy Files
**File**: Backend `pyproject.toml` — add optional extras:
```toml
[project.optional-dependencies]
distro = ["paramiko>=3.0"]  # For network provisioner
desktop = ["pystray>=0.19", "dbus-python"]  # For Linux desktop
```
**No deploy/ files in pip install** (correct — they're Linux-specific).
**Document**: Nunba bundles core + integrations + security. Deploy files are for HART OS ISO/systemd only.

### C3. Config Loading Chain Documentation
**File**: `core/config_cache.py` — add clear docstring explaining:
1. Encrypted vault (SecretsManager) → highest priority
2. `config.json` (standalone) or `langchain_config.json` (bundled) → fallback
3. Environment variables → always override config file values
4. AIKeyVault in Nunba → loads encrypted keys into env vars BEFORE config_cache runs

### C4. Tier Detection Unification
**File**: `security/system_requirements.py` — ensure `run_system_check()` works identically in:
- Standalone Python (`python langchain_gpt_api.py`)
- Bundled Nunba (`sys.frozen=True`)
- HART OS systemd (`/opt/hart/venv/bin/python`)
- Headless embedded (`embedded_main.py`)
Current status: Already works. Just add test coverage for frozen mode mock.
**Tests**: `tests/test_system_requirements.py` — add `test_frozen_mode_detection`, `test_headless_mode_detection`

### C5. Variant-Aware Service Gating
**File**: `deploy/distro/first-boot/hart-first-boot.sh`
Currently tier-based. Add variant awareness:
```bash
# Read variant from /etc/hart/variant (set during ISO build)
VARIANT=$(cat /etc/hart/variant 2>/dev/null || echo "server")
if [[ "$VARIANT" == "edge" ]]; then
    # Force disable services regardless of tier
    systemctl disable hart-agent-daemon.service hart-vision.service hart-llm.service
fi
```
**Tests**: `test_distro_configs.py` — add `test_first_boot_reads_variant`

---

## PART D — GO REWRITES FOR DISTRO TOOLS (2 items)

### D1. Rewrite hart-cli in Go
**Current**: `deploy/linux/hart-cli.py` (330 lines Python, 150ms startup)
**New**: `deploy/linux/hart-cli/` Go module
**Why**: Operators run `hart status` dozens of times daily. 150ms Python startup is noticeable. Go gives <3ms.

**Go CLI structure**:
```
deploy/linux/hart-cli/
├── main.go           # Entry point, cobra/flag command parsing
├── commands.go       # status, start, stop, restart, logs, health, join, provision, update, node-id, version
├── api.go            # api_get(), api_post() HTTP helpers (net/http)
├── systemctl.go      # exec.Command wrappers for systemctl, journalctl
├── go.mod            # module github.com/hevolve/hart-cli
└── go.sum
```

**Port mapping (Python → Go)**:
| Python function | Go equivalent |
|-----------------|---------------|
| `get_backend_port()` | Read `/etc/hart/hart.env`, parse `HARTOS_BACKEND_PORT` |
| `api_get(path)` | `http.Get(fmt.Sprintf("http://localhost:%d%s", port, path))` |
| `api_post(path, data)` | `http.Post(...)` with `json.Marshal` |
| `run_cmd(args)` | `exec.Command(args[0], args[1:]...).CombinedOutput()` |
| `cmd_status()` | Print formatted table from `/status` JSON |
| `cmd_health()` | Call `/api/social/dashboard/health`, format output |
| `cmd_logs()` | `exec.Command("journalctl", "-u", service, "-f")` with os.Stdout pipe |
| `cmd_join(peer)` | POST to `/api/social/peers/announce` |
| `cmd_provision(host, user)` | POST to `/api/provision/deploy` |
| `argparse` | `flag` stdlib or `cobra` library |

**Build**: `go build -o hart-cli -ldflags="-s -w" .` → single ~3MB binary, zero dependencies.
**Install**: `install.sh` copies `hart-cli` binary to `/usr/local/bin/hart` (replaces Python script).
**Backward compat**: Same CLI interface, same exit codes, same output format.
**Tests**: `tests/test_distro_tools.py` — TestHartCLI tests stay (test the Go binary output via subprocess).
**Go tests**: `deploy/linux/hart-cli/main_test.go` — unit tests for api helpers, port parsing, systemctl wrappers.

### D2. Rewrite hart-pxe-server in Go
**Current**: `deploy/distro/pxe/hart-pxe-server.py` (389 lines Python, GIL-limited TFTP)
**New**: `deploy/distro/pxe/hart-pxe-server/` Go module
**Why**: Network boot serving hundreds of nodes. GIL serializes UDP packet handling. Go goroutines handle 1000s concurrent.

**Go PXE server structure**:
```
deploy/distro/pxe/hart-pxe-server/
├── main.go           # Entry point, flag parsing, start servers
├── tftp.go           # RFC 1350 TFTP server (goroutine per client)
├── http.go           # HTTP file server (net/http.FileServer)
├── extract.go        # ISO extraction (exec mount/umount/cp)
├── pxeconfig.go      # PXE config generation (pxelinux.cfg/default)
├── security.go       # Path traversal prevention (filepath.Rel + commonpath check)
├── go.mod
└── go.sum
```

**Port mapping (Python → Go)**:
| Python class/function | Go equivalent |
|-----------------------|---------------|
| `TFTPHandler` | `func handleTFTP(conn *net.UDPConn, addr *net.UDPAddr, data []byte)` |
| `TFTPServer` | `net.ListenUDP("udp", addr)` + goroutine dispatch |
| `PXEHTTPHandler` | `http.FileServer(http.Dir(serveDir))` |
| `extract_iso()` | `exec.Command("mount", ...)` + `filepath.Walk` + copy |
| `setup_pxe_config()` | `os.WriteFile` with template string |
| Path traversal check | `filepath.Rel(serveDir, requested)` — reject if starts with `..` |

**Key improvements**:
- Goroutine per TFTP client (no GIL, true concurrent serving)
- `filepath.Rel()` for path traversal (built-in, battle-tested)
- `net/http.FileServer` for HTTP (production-grade, handles range requests)
- Single binary ~5MB, zero Python dependency on PXE boot server

**Build**: `go build -o hart-pxe-server -ldflags="-s -w" .`
**Install**: Makefile target `pxe-server` builds and copies to `/usr/local/bin/hart-pxe-server`.
**Tests**: `tests/test_pxe_server.py` — existing tests call the Go binary via subprocess (black-box).
**Go tests**: `deploy/distro/pxe/hart-pxe-server/tftp_test.go` — unit tests for packet parsing, path security, config generation.

**Python fallback**: Keep `hart-pxe-server.py` as `hart-pxe-server-py` for environments without Go. Install script prefers Go binary if present.

---

## PART E — DISTRO EXCELLENCE (8 items)

### E1. Tamper-Evident Boot Log
**New file**: `deploy/distro/first-boot/hart-boot-audit.sh`
After first-boot completes, append a signed entry to `/var/lib/hart/boot_audit.log`:
```
TIMESTAMP | NODE_ID | TIER | SERVICES_ENABLED | CODE_HASH | GUARDRAIL_HASH | Ed25519_SIGNATURE
```
This creates an immutable audit trail — if a node's code is tampered, the boot audit signature won't match.
**Tests**: `test_distro_configs.py` — add TestBootAudit (script exists, has signing logic)

### E2. Backup Automation
**New file**: `deploy/distro/backup/hart-backup.sh`
Daily cron/timer: backup node keys + database + agent_data to `/var/backups/hart/` with 7-day retention.
**New file**: `deploy/distro/backup/hart-backup.timer` (systemd timer, daily)
**New file**: `deploy/distro/backup/hart-backup.service` (systemd oneshot)
**Tests**: `test_distro_configs.py` — add TestBackupConfig (timer exists, service exists, script has retention logic)

### E3. Health Dashboard Endpoint Enhancement
**File**: `integrations/social/api_dashboard.py`
Add `/api/social/dashboard/system` endpoint that returns:
- Tier, variant, deployment mode
- CPU/RAM/Disk/GPU usage (live)
- Service statuses (via systemd D-Bus or subprocess)
- Boot audit hash
- Update status (current version, available update)
**Tests**: `tests/test_social_api.py` — add `test_system_dashboard_endpoint`

### E4. Network Topology Visualization Data
**File**: `integrations/social/api_dashboard.py`
Add `/api/social/dashboard/topology` endpoint:
- Returns peer graph (node_id, tier, region, latency, trust_score)
- Used by Nunba UI to render live network map
**Tests**: `tests/test_social_api.py` — add `test_topology_endpoint`

### E5. Fleet OTA Coordination
**File**: `deploy/distro/update/hart-update-service.py`
Add fleet-aware update: Before applying, check with central/regional if version is approved.
```python
def _check_fleet_approval(self, version):
    """Ask regional host if this version is approved for rollout."""
    try:
        resp = urllib.request.urlopen(f"http://localhost:6777/api/social/fleet/update-approved?v={version}")
        return json.loads(resp.read()).get('approved', False)
    except:
        return True  # Standalone: always approved
```
**Tests**: `test_ota_update.py` — add `test_fleet_approval_check`, `test_standalone_auto_approves`

### E6. Secure PXE Boot (HTTPS)
**File**: `deploy/distro/pxe/hart-pxe-server.py`
Add optional HTTPS mode for autoinstall delivery (TFTP stays UDP, but HTTP content served via TLS).
Add `--tls-cert` and `--tls-key` arguments. Generate self-signed cert if not provided.
**Tests**: `test_pxe_server.py` — add `test_tls_argument_parsing`, `test_self_signed_cert_generation`

### E7. Distro Variant File (`/etc/hart/variant`)
**Files**: `deploy/distro/build-iso.sh`, `deploy/distro/first-boot/hart-first-boot.sh`
During ISO build, write variant name to `/etc/hart/variant`. First-boot reads this for service gating.
Nunba should also write `bundled` to this file (or equivalent env var).
**Tests**: `test_distro_configs.py` — add `test_build_iso_writes_variant`

### E8. Plymouth Boot Animation Polish
**File**: `deploy/distro/branding/plymouth/hart-theme/hart-theme.script`
Add progress bar tied to systemd boot progress (hart-first-boot.service sends progress updates).
Current: Logo + pulse only. Enhanced: Logo + pulse + "Initializing node..." text + progress %.
**Tests**: `test_distro_configs.py` — existing TestBranding covers file existence; add `test_plymouth_has_progress_text`

---

## PART F — COMPREHENSIVE TEST PLAN

### F1. New Test Files
| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_security_hardening_distro.py` | ~40 | AppArmor, auditd, fail2ban, journald, DNS, firewall, immutable keys |
| Extensions to `tests/test_distro_configs.py` | ~35 | New configs: backup, sudoers, variant, unattended-upgrades |
| Extensions to `tests/test_pxe_server.py` | ~8 | Nested traversal, TLS args, commonpath guard |
| Extensions to `tests/test_ota_update.py` | ~6 | Mandatory signatures, fleet approval, dev mode |
| Extensions to `tests/test_network_provisioner.py` | ~6 | Input validation, strict host key, command injection |
| Extensions to `tests/test_distro_tools.py` | ~4 | CLI lazy imports, frozen mode |
| `tests/test_deployment_modes.py` | ~20 | Manifest validation, tier×mode matrix, feature gating correctness |

**Total new tests: ~119**

### F2. Regression Pack Updates
**File**: `scripts/run_regression.bat` — add new group:
```
Group 11: Distro Security Hardening
  test_security_hardening_distro.py
  test_deployment_modes.py
```
**File**: `conftest.py` — no changes needed (new tests are standard pytest).

### F3. Test Execution Order
1. Run existing 296 distro tests first (verify no regressions)
2. Run new security hardening tests (~40)
3. Run new deployment mode tests (~20)
4. Run extended existing tests (~59 new assertions across 5 files)
5. Full regression: `pytest tests/ -s` to verify total count increases to ~2580+

---

## IMPLEMENTATION ORDER

| Phase | Items | Est. Tests | Scope |
|-------|-------|-----------|-------|
| **1. Critical Security** | A1-A6 | 20 | Fix vulnerabilities that could be exploited |
| **2. Service Hardening** | B1-B4, B7 | 35 | AppArmor, systemd, journald, fail2ban, immutable keys |
| **3. Infrastructure** | B5-B6, B8-B10 | 20 | Unattended updates, audit, DNS, sudoers, firewall |
| **4. Deployment Modes** | C1-C5 | 25 | Manifest, awareness, config chain, tier detection |
| **5. Distro Excellence** | E1-E8 | 15 | Boot audit, backup, dashboard, fleet OTA, PXE TLS |
| **6. Go Rewrites** | D1-D2 | 10 | CLI in Go (~330 lines), PXE server in Go (~389 lines) |
| **Total** | **31 items** | **~125 new tests** | |

---

## NON-GOALS (Explicitly Out of Scope)
- Kubernetes/Docker orchestration (separate initiative)
- UEFI Secure Boot signing (requires real signing key infrastructure)
- HSM integration (skeleton exists, real HSM is hardware-dependent)
- Commercial billing integration (API exists, real Stripe integration is separate)
- Full disk encryption setup (LUKS2 is hardware/user-choice dependent)
