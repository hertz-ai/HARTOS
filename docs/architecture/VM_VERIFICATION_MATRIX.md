# VM Verification Matrix — one row per open task

**Every open task names the VM test that will prove it, or says plainly that
none exists yet and why.** A task with no named verification is a task that
will be closed on somebody's judgement instead of on a booted machine.

Guarded by `tests/unit/test_vm_verification_matrix.py`, which fails if a row
names a `nixos/tests/*.nix` file or a check that does not exist. That guard is
the whole point: a matrix nobody checks drifts into fiction within a week.

## How to read the STATUS column

| status | meaning |
|---|---|
| `EXISTS` | the named test is written, imported by `nixos/flake.nix`, AND merged into `checks` — all three, because a file that never reaches `checks` is invisible to the dynamic gate and has never run. Guarded by `TestNoOrphanedNixosTests`. |
| `EXISTS-RED` | the test exists and currently FAILS — the failure is the task's next step, not a reason to distrust the row |
| `TO WRITE` | no VM test yet; the row names what it must assert |
| `NOT VM` | the task genuinely cannot be settled in a VM, with the reason stated |
| `BLOCKED` | a VM test is possible, but writing it is NOT what is missing — a decision or an upstream fix is. The blocker is named. |

`NOT VM` is deliberately rare and must justify itself. "Hard to test" is not a
reason; "needs physical hardware" or "needs a human to look at it" is.

`BLOCKED` is not a softer `TO WRITE`. It means the honest next action belongs
to someone else — usually the steward — and writing a test first would either
be red on purpose or test a component that is about to be deleted. A row that
sits BLOCKED for long is a decision going stale, which is worth seeing.

---

## The matrix

| # | Task | VM verification | Status |
|---|---|---|---|
| 3 | Shell hang + false-healthy signals | `session-supervisor.nix` (`hart-session-supervisor-unhealthy-flag`, `-paint-watchdog`, `-tier-drop`) + `display-tiers-neverblack.nix` + `vm-tests.nix` (`/status` truth) + `desktop-boot.nix` (cage-floor mic: GST_PLUGIN_SYSTEM_PATH_1_0 resolves to real capture plugins) | EXISTS — one deliberate gap: `/status` still returns HTTP 200 when degraded. `/ready` already 503s honestly, and flipping /status's code has three consumers, so that is its own decision. |
| 4 | Onboarding onto Nunba LightYourHART | `desktop-boot.nix` — first-run surface reached with `hart.nunba.enable` on | TO WRITE — and BLOCKED: enabling nunba does not evaluate (`hypercorn-0.16.0 not supported for interpreter python3.10`, run 30785511463) |
| 7 | Honest first-run / offline UI | `hart-app-install-verify.nix` (shipped python speaks a number) + `desktop-boot.nix` (espeak-ng SYNTHESISES a WAV with no network) | EXISTS |
| 8 | Event-driven shell | `desktop-boot.nix` — the SHIPPED health probe's own flags, exercised against a real half-up backend (listen without accept), must return bounded | EXISTS for 2.2 (boot health-wait); 2.1 (SSE producer) landed earlier with a behavioural test; 2.3 (connectivity double-poll) is TO WRITE |
| 9 | Shell ↔ Nunba drift | `layer-shell-host.nix` paint + a theme round-trip across both surfaces | TO WRITE |
| 10 | Backend forks (TTS / draft-LLM / constants) | `hart-app-install-verify.nix` for the shipped-python half; the ENGINE fork needs a listening test | NOT VM for the voice-identity half — a human must hear which engine spoke |
| 11 | Right-click "Ask <Product>" | `desktop-boot.nix` — the shell must REFERENCE hartAskMenu.js, the server must SERVE it non-empty, and the served copy must still compute its label from `product()` | EXISTS for the desktop half; Android + iOS are separate surfaces with no VM |
| 12 | Layer-shell vulkan/GSK hang | `layer-shell-host.nix` (`hart-layer-shell-host-paint`) — GTK4 first frame on llvmpipe, OCR'd off the framebuffer | EXISTS-RED |
| 13 | Collapse the two image-build paths | `firmware-boot-matrix.nix` — the raw image must BOOT, not merely build | EXISTS for the boot path; the repart-vs-generators decision is TO WRITE |
| 14 | 22 GiB desktop closure | `.github/workflows/closure-audit.yml` — differential build, both directions | NOT VM — a closure diff is a build measurement, not a boot; measurements already recorded (gaming 2.2 GiB, devtools 2.8 GiB) |
| 15 | nixosTests premise | the whole `flake-checks.yml` shard set — this task IS the gate | EXISTS-RED |
| 17 | Hardware-agnostic installer | `hart-installer.nix` (`hart-installer-dualboot`) | EXISTS |
| 21 | Installed-desktop parity | `desktop-boot.nix` + `hart-app-install-verify.nix` — the installed system must carry what the image carries | EXISTS |
| 23 | Flake eval dies mid-evaluation | `nix-check.yml`'s memory sampler + dmesg OOM verdict | NOT VM — this is a CI-runner failure, not an OS one. Instrumented; the curve is now captured (1.5 GB -> 8.9 GB in 60 s) |
| 24 | RTC/clock jumps backwards | `vm-tests.nix` (`hart-edge-boot`) — steps the guest clock back by the exact 19800s and asserts the OS answers, multi-user stays active, and hart-backend/hart-discovery neither die nor restart | EXISTS |
| 25 | Package every common OS feature | `native-subsystems.nix`, `power-actions.nix`, `storage-filesystems.nix`, `network-wifi.nix`, `notify.nix`, `portal-screencast.nix` | EXISTS — the surface is broad; the gap is which features are still unrepresented |
| 27 | Driver compatibility matrix | `driver-matrix.nix` (`hart-driver-matrix`) | EXISTS-RED |
| 28 | BIOS/firmware boot matrix | `firmware-boot-matrix.nix` (`hart-firmware-boot-matrix`) | EXISTS |
| 29 | Latency SLAs in the VM | `boot-latency.nix` (`hart-boot-latency`) — budgets PARSED from `core/constants.py` at build time | EXISTS |
| 30 | Coverage baseline | `.github/workflows/coverage-baseline.yml` — 4 shards + combine | NOT VM — measuring python coverage does not need a booted OS |
| 31 | Degraded-mode review | `tests/unit/test_degraded_mode_inventory.py` ratchets + `vm-tests.nix` (`hart-edge-boot` SIGKILLs each critical unit and proves it recovers, with NRestarts and a new MainPID as evidence) | EXISTS for the inventory and the unit failure paths; per-route behavioural tests continue in python |
| 33 | num2words missing from hart-app.nix | `hart-app-install-verify.nix` — the SHIPPED python must expand "Rs.200" to words | EXISTS (the file was an ORPHAN until 2026-08-04 — flake.nix never imported it, so this assertion had never run) |
| 34 | Rename `hart.devtools` | flake eval must stay green through the rename | NOT VM — an option rename is settled by evaluation, not by booting |
| 35 | PXE server is Ubuntu-era | a netboot node served by the hart-pxe-server-go **package** (a flake package, not a check — it has no nixosTest at all) | BLOCKED (decision) — the component extracts Ubuntu `casper/` paths and writes a Subiquity `autoinstall` boot line, so it cannot provision a NixOS machine at ALL. A test today would assert failure, and the open question is delete-or-rewrite, not test-or-not. |
| 36 | nouveau blacklist is machine-specific | none possible for the hazard itself | NOT VM — qemu emulates no NVIDIA GPU, and the hazard IS "an NVIDIA-only machine gets no display because nouveau is blacklisted". That needs real hardware. The separable half — image and installed system must AGREE on the blacklist — is an eval/source parity check, not a boot. |
| 37 | 3 pre-existing unit failures | none — these are unit tests | NOT VM — they are `tests/unit/*`, and the gate that must report them is `flake-checks.yml`'s sharded python job |

---

## What this matrix is honest about

**One row is `TO WRITE`** (was nine). #7, #24, #31's unit half, #3's mic half, #8's boot-wait and #11's desktop half were written; #35 and #36 were RECLASSIFIED after looking at them properly — see below. That is the real state of "100% VM verification":
the parity program's own matrices exist and run, and the older feature tasks
mostly do not have a VM assertion yet. Writing those nine is the remaining work
of the goal, and naming them is what makes it finite rather than a feeling.

**Five rows are `NOT VM`, each with a reason** — a closure diff, a coverage
percentage, an option rename, a CI-runner memory death, and unit tests. Forcing
those through a VM would be ceremony, not verification.

**One row is `NOT VM` for a human reason** (#10's voice identity): which TTS
engine speaks is a thing a person has to hear. Automating a claim nobody can
check is worse than admitting the check is manual.
