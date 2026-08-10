# HART OS — Parity Closure Plan

**What this is.** The durable, sequenced plan for closing "HART OS has parity
with a proper closed OS (Windows/macOS), all functionalities tested & verified,
nothing left to guess." It complements [OS_PARITY_MATRIX.md](OS_PARITY_MATRIX.md)
(capability status, guarded by `TestParityMatrix`): the matrix says *what exists*;
this plan says *what "done" means, what is verified, and what remains* — so every
work cycle checks against a plan instead of re-deriving priorities.

**How to use it.** Before starting parity work, read the "Verified" and
"Remaining" sections. When an item moves, update its row here in the SAME change
(never let status live only in chat/tasks). Task numbers (`#N`) reference the
session task list.

**Closure criteria — what "parity done" means.** A capability is CLOSED only when:
1. it is provided by an existing NixOS/systemd option (never a HART reimpl),
2. it is reachable by an agent (a `/api/shell/*` route OR the LLM tool registry),
3. it is **executed and passing** in a test that actually exercises it — a
   nixosTest on a booted VM for anything hardware/boot/OS-level, a behavioural
   unit test for pure logic — not merely eval-clean, and
4. any latency budget it carries is asserted (`core.constants.LATENCY_BUDGETS`).

---

## ✅ Verified (executed & passing) — evidence attached

**OS-level, on booted VMs — nixosTests run `31347690532` (cache recovered, ran
cold-then-cached; these passed deterministically):**

| Capability | Test | Evidence |
|---|---|---|
| Driver binding (USB xhci/usbhid, HD-audio, e1000 NIC, virtio blk/balloon) | `hart-driver-matrix` | booted desktop VM, sysfs bind checks |
| **BIOS compatibility** — UEFI (Hyper-V Gen-2) **and** legacy SeaBIOS (Hyper-V Gen-1) | `hart-firmware-boot-matrix` | both firmware paths booted; `/sys/firmware/efi` present on one, absent on the other |
| NVMe + SATA/AHCI controller binding | `hart-driver-matrix-storage` | added this session; null-co disks, `nvme`/`ahci` bind |
| **Latency enforced on a booted node** | `hart-boot-latency` | kernel/userspace budgets read from the canonical `LATENCY_BUDGETS` table |
| USB-root VFS-panic guard (initrd packs usb_storage/uas/sd_mod/xhci) | `hart-boot-root-initrd` | initrd extracted + module presence proven |
| Cross-OS filesystems (ntfs/exfat/vfat/ext4/btrfs/xfs/f2fs r/w) | `hart-storage-filesystems` | real mkfs+mount+rw round-trips |
| Compositor (hart-comp) | Nix build + Rust `cargo test` | passed both VM runs |

**Latency budgets:** all 17 in `LATENCY_BUDGETS` are asserted — 15 by python
tests (`latency_budget('key')`), 2 boot budgets by `boot-latency.nix` — guarded
by `TestNoOrphanBudget` (a budget with no test now fails CI).

**Security — 8 exploitable bugs found (ultracode rounds 1–3 + MCP) and fixed,
each reviewed + re-run green here, atomic commits on `main`:** unauth OS
rebuild/rollback (`cdab4fc7`), SSRF-to-cloud-metadata (`f61cfe6c`),
`/bots/register` account-takeover (`431c3966`), goal `target_file` command
injection (`c3e6a0b2`), fail-open remote-exec (`319b8ab8`), unauth `/channels/send`
relay (`8a7c4b7e`), WS path-traversal write (`9fd786f0`), MCP fail-open + non-200
exfil (`bd57dfbe`).

**Availability bugs fixed + VM-validated:** `#40` wifi-starvation on state-persist
(`7e5ed76f`), `#41` hart-discovery not restarting after backend crash (`40599414`).

**Nothing-lost audits:** every `nixos/modules/*.nix` is imported somewhere (orphan
audit clean); each variant config imports its profile (`TestNothingLostImport
Invariant`); Hyper-V guest storage (`hv_storvsc`) rides `hartModules`; the
re.match `^...$` trailing-newline validator class swept (7 input-gates, `#36`).

**Decided:** `#35` file-explorer READ routes = full-disk browse parity (mutations
sandboxed) — recorded + guarded.

---

## 🔧 Remaining real bugs (mine to fix)

| # | Item | Status / next step |
|---|---|---|
| `#42` | VM suite has FLAKY timing tests (`hart-server-boot` port-bind, `hart-session-supervisor-reboot-latch`) — different reds each run, blocks all-green | **ROOT-CAUSED + FIXED (verifying, run `31364337720`).** Two causes: (a) one-shot `curl` after `wait_for_open_port` raced socket-listening-before-Flask-serving → bounded `wait_until_succeeds` at all 4 sites (`8ef180d7`); (b) the reboot-latch subtest asserted a broken Tier-1 (`/bin/false`) FREEZES as `hart-comp` across a reboot — invalid: `greetd.service` is a NixOS-managed unit, so boot-time `/etc` regeneration undoes the runtime `systemctl mask`, greetd re-runs the selector and correctly re-drops. Now asserts the reboot READS+HONORS the durable re-arm via the supervisor's append-only journal (`f7eb7325`). Close when the re-run shows both absent from `--log-failed`. |
| `#12` | `hart-layer-shell-host-paint` GTK4 SIGABRT on software GL (vulkan/GSK hang) | Known, deep, multi-session; GPU proven-good — the fix is root-causing the GSK-GL/layer-shell path, not another workaround |
| `#29` | Python CI fleet red — harness inherits host machine identity (D-Bus/sysfs/abs paths) | Hermetic-harness fix; separate from the OS-parity VMs |
| `#19` | Edge cgroup caps below the measured import floor | Edge backend would crash-loop on real edge HW; align caps to the import floor |
| `#3` | Shell hang cluster: mic-freeze on cage FLOOR + false-healthy paint signals | Live; part of the shell-stability track |

## 🧭 Steward decisions (yours — not mine to default)

| # | Decision |
|---|---|
| `#38` | WebSocket handshake auth posture. The path-traversal *write* is fixed; authenticating the *handshake* changes the web-chat **client contract** (the UI's WS must send a token). Options: (a) token/JWT on connect + reject unauthenticated, (b) leave read-only + rely on local-only network, (c) same-origin + CSRF. |
| Remote-desktop **control** | `/api/shell/remote-desktop/status` (read-only, credentials redacted) shipped. start/stop **control** stays a steward decision like firewall-write — expose from the local API, or agent-tool-only? |

## 🗓️ Multi-session closure (scope, not a single turn)

| # | Item | Closure criterion |
|---|---|---|
| `#26` | Full Windows/macOS parity | Every OS_PARITY_MATRIX row ✅ (per the criteria at the top), all nixosTests green in one run |
| `#27` | Whole-repo test coverage | A measured repo-wide coverage number exists (the ~4h suite is the blocker; only slices measured so far — `security/tls_config` 0→100%, parity shell surface ~63%) and the security/OS-critical surfaces are ≥ target |
| `#23` | Flake-check eval gate resilience | The monolithic `nix flake check` job stops getting canceled/preempted — make the GATING verdict per-check (the `diagnose` job already evals per-check, non-gating) |

---

## Session log (2026-08-09/10) — how the verified state was reached

1. Coverage-hardening (3 ultracode workflows, 39 agents, 0 errors): 15 real bugs
   found (8 security-critical), ~490 behavioural tests added across 24 modules,
   all reviewed + re-run green here, DRY (extended existing test files).
2. That got the nixosTests **executing** in CI (the Determinate magic-nix-cache
   outage that blocked them for ~6h recovered; `#23` monolithic-eval flakiness is
   separate and orthogonal).
3. Run `31347690532` VERIFIED the driver/BIOS/latency matrix on booted VMs and
   surfaced 3 real reds → `#40`/`#41` fixed + validated (`31354663901`), `#12`
   still open, `#42` flakiness identified.
4. `#42` root-caused from the ACTUAL failure line (run `31354663901`,
   `--log-failed`), not a guess: the reboot-latch red was the invalid
   "broken Tier-1 freezes as hart-comp across a reboot" assert — greetd is
   NixOS-managed, so the runtime mask cannot survive a boot and the supervisor
   correctly re-drops. Fixed to assert the honored-read via the append-only
   supervisor journal (`f7eb7325`); the one-shot-curl races bounded-retried at
   all 4 sites (`8ef180d7`). Re-run `31364337720` dispatched to confirm both are
   gone from `--log-failed`.
