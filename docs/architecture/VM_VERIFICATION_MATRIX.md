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

`NOT VM` is deliberately rare and must justify itself. "Hard to test" is not a
reason; "needs physical hardware" or "needs a human to look at it" is.

---

## The matrix

| # | Task | VM verification | Status |
|---|---|---|---|
| 3 | Shell hang + false-healthy signals | `session-supervisor.nix` (`hart-session-supervisor-unhealthy-flag`, `-paint-watchdog`, `-tier-drop`) + `display-tiers-neverblack.nix` + `vm-tests.nix` (`hart-edge-boot` asserts `/status` reports learning_active=false, keeps learning_mode, and names the reason) | EXISTS for the learning_active + response-shape half; the mic-on-cage-floor assertion is still TO WRITE |
| 4 | Onboarding onto Nunba LightYourHART | `desktop-boot.nix` — first-run surface reached with `hart.nunba.enable` on | TO WRITE — and BLOCKED: enabling nunba does not evaluate (`hypercorn-0.16.0 not supported for interpreter python3.10`, run 30785511463) |
| 7 | Honest first-run / offline UI | `hart-app-install-verify.nix` (shipped python speaks a number) + `desktop-boot.nix` (espeak-ng SYNTHESISES a WAV with no network) | EXISTS |
| 8 | Event-driven shell | `desktop-boot.nix` boot-to-paint; the bounded-`curl` fix needs a node where the backend accepts TCP and never answers | TO WRITE |
| 9 | Shell ↔ Nunba drift | `layer-shell-host.nix` paint + a theme round-trip across both surfaces | TO WRITE |
| 10 | Backend forks (TTS / draft-LLM / constants) | `hart-app-install-verify.nix` for the shipped-python half; the ENGINE fork needs a listening test | NOT VM for the voice-identity half — a human must hear which engine spoke |
| 11 | Right-click "Ask <Product>" | `desktop-boot.nix` — context menu entry present and wired | TO WRITE |
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
| 35 | PXE server is Ubuntu-era | a netboot node served by the hart-pxe-server-go **package** (a flake package, not a check — it has no nixosTest at all) | TO WRITE — and possibly never: the component may be deleted instead |
| 36 | nouveau blacklist is machine-specific | `driver-matrix.nix` with an NVIDIA-class device — the display path must still bind a KMS driver | TO WRITE |
| 37 | 3 pre-existing unit failures | none — these are unit tests | NOT VM — they are `tests/unit/*`, and the gate that must report them is `flake-checks.yml`'s sharded python job |

---

## What this matrix is honest about

**Six rows are `TO WRITE`** (was nine; #7, #24 and #31's unit half landed). That is the real state of "100% VM verification":
the parity program's own matrices exist and run, and the older feature tasks
mostly do not have a VM assertion yet. Writing those nine is the remaining work
of the goal, and naming them is what makes it finite rather than a feeling.

**Five rows are `NOT VM`, each with a reason** — a closure diff, a coverage
percentage, an option rename, a CI-runner memory death, and unit tests. Forcing
those through a VM would be ceremony, not verification.

**One row is `NOT VM` for a human reason** (#10's voice identity): which TTS
engine speaks is a thing a person has to hear. Automating a claim nobody can
check is worse than admitting the check is manual.
