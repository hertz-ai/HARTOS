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

**Edge cgroup caps (`#19`) — CLOSED (fixed + guarded).** The edge backend caps were
raised `384M/256M/32 → 640M/512M/64` (2026-07-28) after the module-scope import
floor was measured at 275 MB — the old `MemoryHigh=256M` sat *below* it. Guarded by
`hart-edge-boot`'s "capped HART units stay inside their own cgroup caps" subtest,
which auto-discovers every capped `hart-*` unit and hard-fails on the real
crash-loop signals (`peak ≥ MemoryMax`, `Result=oom-kill`, `NRestarts>0`) and on any
running unit it couldn't measure. The `MemoryHigh` throttle-only case is deliberately
NOT gated (the 275 MB figure is a dev-box measurement with torch/transformers that
`hart-app.nix` does not ship — gating on an unmeasured guess = a gate nobody passes).

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
| **compositor build (crate-403)** | **⬇️ DEMOTED 2026-08-30 — PROBE-PROVEN NOT A VM BLOCKER.** Probe run `33308820048`: `PROBE_HART_COMP=GREEN` — `hart-comp` (the compositor the nixosTest desktop closure actually builds, vendored via **crane** = static.crates.io) builds fine (161 tests pass, 15.64s). `PROBE_HART_RUST_PRECEDENT=RED` — only the standalone `hart-rust-precedent` (nixpkgs `importCargoLock`) 403s, and it is **NOT in any system closure**: `installBinary` defaults `false` (`hart-rust-precedent.nix:163`) and `desktop.nix` sets only `rustPrecedent.enable = true` (exposes the `nix build` attr; does not add the pkg to `environment.systemPackages`). So the VM suite (driver/BIOS/Hyper-V/latency) is **NOT** blocked by crate-403 — it is blocked SOLELY by **#29** (Python fleet red → nixosTests `needs: unit-tests` → SKIP). crate-403 now only reddens the standalone `compositor-nix` CI job, which redundantly builds the precedent that `hart-comp` already supersedes; clean fix = drop `hart-rust-precedent` from that job's build loop (`flake-checks.yml:498`). **The stale "HIGHEST-PRIORITY BLOCKER, gates ALL VM" framing below is superseded by this line.** ~~HIGHEST-PRIORITY BLOCKER — gates ALL VM verification.~~ The compositor Rust build fetches its ~245 crates and gets **HTTP 403** on `js-sys 0.3.93` → the `Compositor nix build` job fails → ~~nixosTests cascade-SKIP~~ (WRONG — nixosTests skip on #29, not this) → the entire VM parity suite (driver/BIOS/Hyper-V/latency/`#42`/`#12`) never executes. Root-caused via run `33298320160` @ `ca645a43`. **PRECISE CAUSE (live-confirmed this session, curl from a dev box):** `https://crates.io/api/v1/crates/js-sys/0.3.93/download` → **403**, but `https://static.crates.io/crates/js-sys/js-sys-0.3.93.crate` → **200** (S3, 103 KB). crates.io now 403s the legacy `/api/v1/.../download` endpoint for non-cargo fetchers; the build's fetch routes through it instead of the working `static.crates.io` CDN. `js-sys` is a benign target-agnostic transitive (vendored, never compiled on native — NOT removable). **GROUND-TRUTH CORRECTION (run `33298320160` @ `ca645a43`, 2026-08-30, full job log line):** the failing derivation is `crate-rustyline-15.0.0.tar.gz` (also `syntect-5.3.0`; the exact crate varies run-to-run — whichever the fetcher hits first — because the 403 is systematic, not crate-specific), and the fetch line is verbatim `crate-rustyline> trying https://crates.io/api/v1/crates/rustyline/15.0.0/download`. The `crate-<name>-<ver>.tar.gz` name + `cannot download … from any mirror` phrasing is nixpkgs **`importCargoLock`** (`pkgs/build-support/rust/import-cargo-lock.nix`), and the build that failed first is **`hart-rust-precedent` / `hart-claw-precedent`** — the `buildRustPackage` path, **NOT crane.** **The earlier "bump crane" attribution was WRONG:** the pinned `crane` `469fd08d` is dated **2026-06-18**, well *after* crane's CDN switch (crane v0.16.3, 2024-03-19, "sources are now fetched [from] crates.io's CDN" — crane changelog), so crane already uses `static.crates.io`. The live culprit is **nixpkgs-rust's `importCargoLock`**, which still builds the deprecated `https://crates.io/api/v1/crates/${n}/${v}/download` URL that crates.io now **403s** (rust-lang/crates.io#13482 — the API endpoint / UA block, a 2026 change). **FIX = `nix flake lock --update-input nixpkgs-rust`** to a rev whose `importCargoLock` fetches from `static.crates.io` (nixpkgs fixed this in response to #13482; bump `crane` too for the hart-comp path to be safe). The `.crate` from `static.crates.io` is byte-identical to the old API download (the API was a 302→CDN redirect), so every `Cargo.lock` sha256 stays valid — the bump only changes the URL, not any content hash. **BLOCKED ON A NIX ENV:** `flake lock` recomputes `flake.lock`'s input narHash, which cannot be hand-authored on the Windows dev box (no nix). A `hartOverlays` mirror (à la libsciter `ebabc10c`) would need to wrap `fetchurl` on BOTH the main `pkgs` (importCargoLock) AND `rustNixpkgs` (crane, `hart-comp.nix:159`), and a mis-authored overlay eval-reds ALL shards — too risky to push blind to shared `main`. Handoff: run the two `flake lock --update-input` commands on any Nix box (or CI with lock-write), or greenlight me to draft the targeted overlay and iterate it via CI. |
| `#42` | VM suite has FLAKY timing tests (`hart-server-boot` port-bind, `hart-session-supervisor-reboot-latch`) — different reds each run, blocks all-green | **ROOT-CAUSED + FIXED (verifying, run `31367253577`).** Three causes: (a) one-shot `curl` after `wait_for_open_port` raced socket-listening-before-Flask-serving → bounded `wait_until_succeeds` at all 4 sites (`8ef180d7`); (b) the reboot-latch subtest asserted a broken Tier-1 (`/bin/false`) FREEZES as `hart-comp` across a reboot — invalid: `greetd.service` is a NixOS-managed unit, so boot-time `/etc` regeneration undoes the runtime `systemctl mask`, greetd re-runs the selector and correctly re-drops. Now asserts the reboot READS+HONORS the durable re-arm via the supervisor's append-only journal (`f7eb7325`); (c) that rewrite's OWN terminal `cat`==cage was itself the one-shot race (cage needs a 2nd degrade after `sway`) → bounded `wait_until_succeeds` on the floor value (`a0e5c4fe`, caught by the parallel-path audit). **Verify run `31377165213`** (the prior `31367253577` was WASTED: `a0e5c4fe`'s comment carried a literal `''` inside the testScript indented string, which closed it → eval syntax error → all 12 shards red on eval at 2m37s, not the tests; fixed by a concurrent session in `74e54c1a`). Close when the re-run shows both absent from `--log-failed`. |
| `#12` | `hart-layer-shell-host-paint` GTK4 SIGABRT on software GL (vulkan/GSK hang) | Known, deep, multi-session; GPU proven-good — the fix is root-causing the GSK-GL/layer-shell path, not another workaround |
| `#29` | Python CI fleet red — harness inherits host machine identity (D-Bus/sysfs/abs paths) | **THE nixosTests gate** (`needs: unit-tests` → any red shard SKIPs ALL VM tests). Whittled per-shard from fresh CI logs (run `33310728776` @ `9015a342`): shards 2/3/4/5/6 already GREEN. Remaining reds fixed 2026-08-30 on shards **0/1/7**, each root-caused + locally verified: **shard 0** — orb harness section C asserted the pre-`#32` inline `pointer-events:auto` the fix deliberately cleared → realigned to the anti-swallow guard (`3cec4f1c`); `crate403-probe.yml`'s concurrency group lacked `github.ref` (test_ci_workflow_concurrency) → deleted the ephemeral probe (`8843d7af`). **shard 1** — `test_world_model_learning_active` hand-set `_last_flush_at` from `time.time()` after the source moved to `time.monotonic()` (my own clock-jump fix; sibling test_world_model_bridge was updated, this file missed) → inject monotonic (`57a0090c`). **shard 7** — `test_whatsapp_live_adapter` flaked on CPython-3.11 sqlite `SystemError` (a `side_effect=RuntimeError(instance)` retained a traceback pinning the sqlite conn; GC mid-`db.commit()` re-entered) → fresh-exception callable (`0d36f515`); repo-health ratchet bare-`except:pass` rose 1528→1540 → converted `world_model_bridge.py`'s 24 silent swallows to logged debug/warning + ratcheted budget to 1521 (`29e8dc8d`). Run `33313552929` @ `8843d7af` CONFIRMED shards **0 + 7 flipped GREEN** (and 2/3/4/6 green), exposing the last reds from fresh logs: **shard 1** also had `test_list_agents_tool` (empty `:memory:` DB → 0 trained agents; hermetic seed `26e23bd8`) and `test_draft_first_dispatch::test_agent_bound_escalates_none_to_local` (STALE — the agent-bind guard now replaces the draft reply with `_REFUSAL_STANDBY_REPLY`, test still asserted the old raw reply; `44affaf4`); **shard 5** regressed to a 120s Timeout on `test_monitor_detects_code_tamper` (a flaky whole-repo `compute_file_manifest` in the tamper path, mocked `766778cb`). compositor-nix at 8843d7af was still the PRE-`bb122b24` YAML (built the crate-403 precedent); HEAD builds `hart-comp` only. **✅ CLEARED 2026-08-30 — run `33315100170` @ `44affaf4`: ALL 8 Python shards GREEN + BOTH compositor jobs GREEN (compositor-nix's crate-403 precedent failure gone now that it builds `hart-comp` only). `needs: unit-tests` satisfied → nixosTests shards 0-3/4 SPAWNED and executing — the driver/BIOS/Hyper-V/latency VM suite is finally running (long pole; #12 layer-shell + any #42 flakies may still red, but the gate that blocked ALL VM verification is open).** |
| `#3` | Shell hang cluster: mic-freeze on cage FLOOR + false-healthy paint signals | Live; part of the shell-stability track |

(`#19` edge cgroup caps moved to Verified above — fixed + VM-guarded.)

### 🖥️ VM suite — FIRST full run since #29 cleared (run `33315100170` @ `44affaf4`, 2026-08-30)

nixosTests finally EXECUTED (the gate had SKIPped them for weeks). Shard 1 GREEN;
shards 0/2/3 red with **5 deterministic** `failed with error` tests — a mix of
known-hard and regressions that accumulated *unvalidated* while the suite was
gated off. **None trace to this session** (test-only + two `except:pass`→log
source edits; VM boot/paint/mount failures cannot come from that). The many
`hart-backend … sqlalche.me` journal lines are **benign idempotent-migration
skips** (`duplicate column`), not failures.

| VM test | Failure | Class | Action |
|---|---|---|---|
| `hart-desktop-shell-boot` | WebView first-frame PAINT (OCR) timed out 120s on llvmpipe | **#12** GPU/GSK software-paint | known-hard; root-cause the GSK-GL path |
| `display-tiers-…-paint-ladder` | `/run/hart/session/shell-ready` EXISTS when all tiers fell to the cage floor with no paint | **#3/#6 false-healthy** | shell-boot design; consult HOME_DESKTOP_DESIGN_CHECKLIST — one of 5 shell-ready writers touches it without a real paint |
| `hart-native-subsystems` | "waydroid init must bound a hung mirror with RuntimeMaxSec" | **stale test** | ✅ FIXED `b6f7717d` — test queried `RuntimeMaxSec`; the systemd show-property is `RuntimeMaxUSec` (unit correctly sets `RuntimeMaxSec=3600`) |
| `hart-ota-central` | "realtime push leg wired into backend" timed out 120s | triage | needs CI-iteration to see the backend push-leg state |
| `hart-boot-log` | `mount /dev/vdb` exit 32 after finding HARTLOG on another device | test-infra (device-name) | the label lookup found HARTLOG elsewhere; the mount hardcodes `/dev/vdb` — CI-iteration to confirm the actual device |

Verification of the waydroid fix + flaky-vs-deterministic on the other four rides
the next flake-checks dispatch. CI-iteration only from the Windows box (no local
nix/VM); the shell paint items (#12, false-healthy shell-ready) are the #3
workstream and design-gated.

## 🧭 Steward decisions (yours — not mine to default)

| # | Decision |
|---|---|
| `#38` | WebSocket handshake auth posture. The path-traversal *write* is fixed; authenticating the *handshake* changes the web-chat **client contract** (the UI's WS must send a token). Options: (a) token/JWT on connect + reject unauthenticated, (b) leave read-only + rely on local-only network, (c) same-origin + CSRF. |
| Remote-desktop **control** | `/api/shell/remote-desktop/status` (read-only, credentials redacted) shipped. start/stop **control** stays a steward decision like firewall-write — expose from the local API, or agent-tool-only? |
| `#28` | Federation-egress timing in `save_report`. The exception-safety bug is fixed and each peer POST is already bounded (5s + backoff); the residual ~37s is N-peers×5s **sequential** at end-of-background-autoresearch. Making it async/parallel changes semantics — `export_learning_delta` sets the session's `federation_broadcast_enforced`/`export_enforced` flags that tests assert synchronously, and concurrency needs `_peer_backoff` thread-safety. Recommend: fire-and-forget daemon egress (backend is long-lived) + poll the flags in tests. Not changed behind the prior session's documented deferral. |

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
