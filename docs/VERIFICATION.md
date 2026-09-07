# Verified and unverified

Every claim this project makes is in one of two tables. The top table has
evidence you can check today. The bottom table does not.

Two ways to help. Run an unverified row on hardware we do not have and move it
up. Or tell us where the design is wrong.

Most of the unverified rows need one thing: hardware that is not a developer
laptop. A Raspberry Pi, a board with no GPU, a machine with 4 GB. We do not have
enough of those, and CI has none.

**To claim a row:** open the linked issue, say which board you have, run the
steps, and paste the output. Pass or fail both help. A row that fails moves to
the top table as a verified negative, with the failure recorded, which is worth
as much as a pass.

## Design review

Better designs exist for most of what is here. The scheduler, the model bus, the
session supervisor, the peer transport, the tier ladder were all built by people
solving the problem in front of them.

If you see a better one, open an issue, name the file, make the argument. No
benchmark required.

Design compounds in an OS because everything above a choice inherits it. That
makes this worth more than another feature.

---

## Verified

| Claim | Evidence | Last checked |
|---|---|---|
| The compositor builds with Smithay linked, talking DRM and KMS | `nix-build-matrix.yml`, M9 gate `hart-comp`, `buildFeatures = ["smithay"]` | 2026-07-26, success |
| The desktop ISO builds and publishes in four parts | `release.yml` -> `build-iso (iso-desktop)` | 2026-07-27, success |
| The flake evaluates on every push to `nixos/**` | `nix-check.yml` | 2026-07-27, success |
| The co-pilot cannot escalate privilege or change the boot default | 19 tests, `tests/unit/test_copilot_daemon_boundary.py` | 2026-07-27, pass |
| Screen recording produces a real mp4 at the rate it captured | `hart desktop record`, measured -0.6% timeline drift over 4s | 2026-07-27 |
| Constitutional rules are enumerable without running anything | `security/hive_guardrails.py`, `CONSTITUTIONAL_RULES` frozen tuple, 13 guardrail classes | 2026-07-27 |
| The hive weights a Pi and a GPU rack equally at equal participation | `federated_aggregator.py:642`, `log1p(interactions)`, no tier multiplier | 2026-07-27 |
| The fleet binary cache serves signed HART system closures | `hart-nix-cache` on deepbox:8093, 5,575 signed paths / 12G, 57 `nixos-system-hart-node` toplevels, newest 11:37 | 2026-09-05, live |
| An OTA-applied generation would mount a raw-flashed disk | 28 of those 57 toplevels mount `/` by `hart-root` and `/boot` by `HART-ESP`; 0 carry per-rev labels. Read out of the cache: toplevel -> `-etc` -> `-etc-fstab`, zstd-decompressed | 2026-09-05, live |
| Both the ISO and the raw config reach the cache each run | The 57 arrive in 28 raw + 29 other pairs 1-2 min apart, which is `nix-build-matrix.yml`'s `cfgs="hart-desktop hart-desktop-raw"` loop | 2026-09-05, live |
| The raw generation registers its own boot entry | its `systemd-boot` builder carries `bootctl install/update` with `--graceful --no-variables` | 2026-09-05, live |
| A node can FETCH that generation from the substituter it ships with | `hart-base.nix` pins `http://etime.hertzai.com:8093`; that name resolves to 106.51.181.24 and returns HTTP 200 for `jzhhbf4d9hb2pdnis12cqlaz1myryyhj.narinfo`, the raw-bootable toplevel above, signed `hart-cache-1` which is the key pinned beside it | 2026-09-05, live |
| The stick carries the stable-label image, and it is THAT generation | Reflashed 2026-09-05 from the 2026-09-04 nightly. Flasher: stream sha256 == the published `.raw.sha256`, then a full device read-back == the stream. Its UKI's `.cmdline` names `jzhhbf4d9hb2pdnis12cqlaz1myryyhj`, the same toplevel verified above as mounting `hart-root`/`HART-ESP` | 2026-09-05, pass |
| **OTA applies, boots, and rolls back on hardware** | Full cycle on the Samsung box 2026-09-07. Fetched `yz36m179...` from `etime.hertzai.com:8093` in 50s (3 paths; rest shared), registered as generation 2, entries appeared in `/boot/loader/entries` with no manual work, rebooted in 148s onto generation 2, then `nix-env --rollback` + reboot in 63s back onto generation 1 | 2026-09-07, pass |
| A generation built from a DIFFERENT rev mounts the same disk | Same run: generation 2 booted with `/` on `/dev/disk/by-label/hart-root`. Under the per-rev labels this was unbootable by construction (stage 1 "must mount the root filesystem on /mnt-root"), which is the failure the cutover exists to fix | 2026-09-07, pass |
| First boot registers the rollback target and installs the bootloader | `[Profile] Registered generation 1 -> jzhhbf4d... (the rollback target OTA needs)` and `[Bootctl] systemd-boot installed/refreshed on the ESP`; `/boot/EFI/systemd/systemd-bootx64.efi` present. `/boot/loader/entries` was EMPTY before the first apply | 2026-09-07, pass |
| Two nodes form an ENCRYPTED PeerLink and messages cross it | `tests/standalone/peer_link_proof.py`, 11/11: upgrade in 1.92s, `encrypted=True`, server `active_links=1`, a gossip message reaches a handler on the far side | 2026-09-05, pass |
| A second node makes shard fan-out stop running single-node | same run, step 6: `hive_benchmark_prover._discover_nodes()` returns 2 nodes, 1 `type=peer_link` | 2026-09-05, pass |
| SAME_USER trust is grantable, so skill broadcast has a path | same run, steps 9 to 11: both sides grant `same_user`, and `federation.recipe_delta` (what `skill_exporter` publishes) arrives on the peer | 2026-09-05, pass |
| A node ACCEPTS a peer's signed announce and counts it | `tests/standalone/two_node_collaboration.py`: `accepted=True is_new=True`, B's own census reports 2 nodes with one `local=False` | 2026-09-05, pass |
| A learning delta produced on A is merged and counted by B | same run, step 4: delta accepted, read back FROM B rather than inferred from A's 200 | 2026-09-05, pass |
| A hive node bootstraps a newcomer better than a solo node | `tests/standalone/network_beats_solo_proof.py`: joiner inherits 2 community-validated heuristics pooled from 3 peers; the solo node hands over `{}` | 2026-09-05, pass |
| The experiment lifecycle advances on its own schedule without inventing outcomes | `tests/standalone/experiment_lifecycle_proof.py` 9/9, `decided` rows stay 0 | 2026-09-05, pass |
| No deployed shell route crashes on a plain request | `test_surface_drive_all.py` drives all 329 registered routes through a real `test_client`; every one returns a controlled status, zero 500s | 2026-09-05, pass |
| The shell surface works chapter by chapter with the OS boundary SEALED | `tests/integration/shell_surface/`, 449 pass: boot and first paint, session and personalize, system controls, network, apps and upgrades, events and sinks | 2026-09-05, pass |
| The earnings, health and delegation paths hold end to end | `test_compute_earnings_e2e.py`, `test_health_endpoints.py`, `test_task_delegation_bridge.py`, 22 pass | 2026-09-05, pass |
| An A2UI push wakes the SSE stream without waiting on a durable write | `test_liquid_ui_sse_event_driven.py` 4/4; the audit commit sat ahead of the wake and cost 5.8s to 11.5s per push, now 0.000s | 2026-09-05, pass |
| The native scene layer is not the frame-rate bottleneck | `scene_cost_fits_the_frame_budget` on an i7-7700K, release: a full relayout WITH real cosmic-text shaping is p99 219us, 1.3% of a 16.667ms frame; geometry alone is 2us | 2026-09-05, measured |
| The native compositor HOLDS as the live desktop | Cold boot on the Samsung box with the socket-wait fix: socket bound at 19.4s, first scanout 0.7s later, glass shell mapped as a Background layer surface, `layer.composited layers_painted=1`, and `current-tier=hart-comp` with `tier-degraded` absent | 2026-09-07, pass |
| Synthetic input reaches the compositor's seat on real hardware | A uinput device injected 2,228 pointer reports on 2026-09-07; hart-comp logged `New device "event10"` and `first seat input delivered - libinput/Seat path is LIVE`, so uinput -> evdev -> libinput -> seat is proven end to end | 2026-09-07, pass |
| Pointer motion over a static desktop costs ZERO frames | Same run: those 2,228 reports produced no repaint and no latency sample. Damage tracking working as designed, and the reason `hart-latency` prints nothing until input actually changes something | 2026-09-07, measured |
| The Tier-1 socket wait was what dropped the native tier | Three cold boots measured hart-comp binding at 14.6s / 22.5s / 19.4s against a 10s wrapper bound. The wrapper EXITS on timeout, so the glass shell was never launched at all and nothing could paint; the 120s watchdog then dropped a compositor that scans out 0.6s after binding | 2026-09-07, pass |
| hart-comp composites its own chrome on real hardware | Same boot: `orb.composed took_ms=2`, `bloom.composed width=1600 height=900 took_ms=66`, `native-chrome published chrome=bloom,orb` on GLES / Intel HD 4000 | 2026-09-07, measured |
| A steady desktop pays no layout at all | same run: a retained-tree hit is p50 and p99 both under 1us, and `rebuilds()` does not move on an unchanged key | 2026-09-05, measured |
| Windows / macOS capability parity is COMPUTED, not asserted | `OS_PARITY_MATRIX.md`, 30 rows, gated by `test_nixos_configs.py::TestParityMatrix`: 30 pass. 28 present, 2 deliberately partial (remote-desktop control and firewall writes are steward-gated ingress on purpose), 0 gaps | 2026-09-05, pass |
| The matrix cannot advertise a route that does not exist | same gate: every `/api/shell/...` the matrix cites is checked against the registered routes, and the honest-gap test parametrizes over the gap list, which is now EMPTY (hence its skip) | 2026-09-05, pass |
| The desktop's event channel opens immediately, not on the next keep-alive | `/api/notifications/stream` on the Samsung box, same probe before and after a one-line overlay: OPEN 15.024s (first frame `: hb`) -> OPEN 0.012s (first frame `: ok`), heartbeats unchanged at 15.012s / 30.013s. Werkzeug withholds the head until the first yield and the producer opened with a 15s CV wait, so every page load and reconnect paid a full heartbeat before `EventSource.onopen`. Fixed ebc84c1 | 2026-09-07, measured on hardware |
| HART-comp's window IPC works on real hardware | `com.hart.Compositor` on the Samsung box: `window.list` -> `ok:true`, an unknown method -> `code:unsupported`. The socket is `/run/user/1000/hart-comp.sock`, 0600, bound at compositor start under the DRM backend | 2026-09-07, measured on hardware |
| A banked window-layout recipe replays on the native tier | `replay_layout` through the new HART-comp transport, driven as the session user: `ok:true, replayed 3 of 3`, the workspace really switching to 2 and back. Before the transport existed the same recipe gave `available:true, replayed 0 of 3` with three reasonless `ok:false` | 2026-09-07, measured on hardware |
| The IPC relay carries HART-comp's protocol through the 0600 wall | the relay script extracted from `hart-layer-shell-host.nix`, run as root, driven the way systemd drives it (stdin/stdout ARE the client socket): a 71-byte framed reply came back intact. This is the script the unit execs, not a stand-in | 2026-09-07, measured on hardware |
| An OS app installs and LAUNCHES, under the service's own environment | the real `AppInstaller._install_nix`, unmocked, on the Samsung box, run with hart-liquid-ui.service's actual env (46 PATH entries, none of them `/run/current-system/sw/bin`; no `NIX_PATH`): the old code raised `FileNotFoundError` -> "nix-env not available", the new one installs, reports a real store path that exists, and the binary runs (`Hello, world!`). THREE stacked faults, each alone fatal: a channel-style argv on a node with no channels, `nix-env` unfindable on a unit PATH, and no `NIX_PATH` to resolve `<nixpkgs>`. All 176 installer tests fake the package manager, so none saw any of it | 2026-09-07, measured on hardware |
| The Nunba microfrontend is really proxied, not floored | on the box, `/social` through the shell answers `Server: hypercorn-h11` while `/` answers `Server: waitress`. So the UDS proxy to `hart-nunba` over `/run/hart/nunba.sock` is live and serving the daemon, not the bundled React fallback that stands in when the socket is unreachable | 2026-09-07, measured on hardware |
| The shell service reports its own features honestly | swept all 77 binaries the shell layer shells against hart-liquid-ui.service's REAL PATH on the box: 33 were installed and INVISIBLE to the unit (notify-send, xdg-open, xdg-mime, the clipboard tools, gsettings, wlr-randr, powerprofilesctl, git, gh, hart-self-build, sudo, and every nix binary), and ALL 29 other `hart-*` units had the same blindness. `run_probe` reads that as "optional tooling absent", so each degraded silently and the OS called its own working features unavailable. After the shared `system_search_path` fix, re-run under the unit's own PATH on the box: notify-send / xdg-open / gsettings / git / nix-env / wl-copy all REACHED, 6 of 6 | 2026-09-07, measured on hardware |
| Flatpak is wired correctly up to the download | on the box as the REAL service user (`hart`), under the unit's own PATH: `FLATPAK_USER_DIR` set, flatpak findable through the shared search path, `_ensure_flathub()` creates the remote, and `remote-info flathub org.gnome.Calculator` returns live metadata over the network (Download 1.8 MB, Runtime org.gnome.Platform/50). The DOWNLOAD is deliberately not exercised: no runtime is installed, so it would pull ~1GB onto a disk at 92% with 2.3G free, which collides with OTA headroom. That is a capacity decision, not a code question | 2026-09-07, measured on hardware |
| Daemon goals run end to end on a node | `/api/social/dashboard/health` on the box: 9 daemons registered with NodeWatchdog, all healthy and all INSIDE their configured threshold (`expected_interval` x the 10.0 frozen-multiplier). 7 heartbeat within seconds; `hive_benchmark_prover` sits at 9694s against a 43200s interval; `runtime_monitor` at 1347s against a 3000s threshold, and the watchdog had already restarted it twice (14:42, 15:37) when it did exceed, which is the mechanism working rather than failing. Watchdog's own check loop last ran 25.8s ago, fleet_halted false | 2026-09-07, measured on hardware |
| Synthetic input reaches the compositor over the REAL path | 104 Super+N workspace chords injected through /dev/uinput on the box, each one logged by hart-comp as `workspace.switched workspace=N restored=0`. That is uinput -> evdev -> libinput -> hart-comp's seat -> the Super+1..9 chord handler in comp_core.rs. The input half of input-to-photon needs no human | 2026-09-07, measured on hardware |
| The desktop burns a full core drawing what nobody sees | measured on the box 2026-09-07 with the native-chrome bridge FULLY engaged (verdict `bloom,orb`, shell logged `surface transparent`): `WebKitWebProcess` held **1383 CPU ticks in 14s, 98.8% of a core**, while `layer.composited` had fired ONCE the entire boot and the latency instrument recorded zero samples from 943 pointer plus 104 key events. The handoff stopped the canvas PAINTING and left the script DRAWING: rAF throttling keys off document visibility, not element visibility, so `visibility:hidden` never slowed the loop. `voiceOrbViz` is the only unconditional self-re-arming rAF loop in the shell after boot (every other is debounced one-shot or bounded; the splash lottie is explicitly destroyed). Fixed by handing the same verdict to script as well as CSS | 2026-09-07, defect measured on hardware; the CPU drop needs a page reload and is NOT yet measured there |

Read the sealed rows for what they are. `shell_surface/conftest.py` guarantees a
poweroff/format/nmcli test can never touch the host, and chapter 00 asserts that
seal. So those 449 exercise the REAL handlers with the OS calls faked. They are
not hardware, and a row here never becomes one; what they rule out is a whole
class of crash and regression before hardware is involved.

## Unverified

Ordered by how much a contributor with modest hardware can settle in an evening.

| # | Claim | What would settle it | Hardware |
|---|---|---|---|
| 1 | The Pi 4 image boots | Flash `hart-edge`, boot, paste `journalctl -b` | Pi 4, any RAM |
| 2 | GPIO actually toggles from the agent | `integrations/channels/hardware/gpio_adapter.py` against a real pin and an LED | Pi 4 + LED |
| 3 | The CPU-only path is usable, not just present | tokens/sec for the 2B on a Pi 4 8 GB, no GPU | Pi 4, 8 GB |
| 4 | A 4 GB board degrades honestly | Confirm it lands in LITE and says so rather than failing to start a model | Pi 4, 4 GB |
| 5 | Serial adapter drives real hardware | `serial_adapter.py` to any USB-serial device | any board |
| 6 | The ROS 2 bridge talks to a real node | `ros_bridge.py` publish/subscribe against a running ROS 2 graph | ROS 2 install |
| 7 | Two nodes find each other and borrow compute | Two machines, `hart hive connect`, one borrows from the other | 2 machines |
| 8 | A borrow settles what it owes | End-to-end through `compute_borrowing.py` and `revenue_aggregator.py` | 2 machines |
| 9 | The 19 nixosTests pass | `nixos-vm-tests.yml` is manual-dispatch and has no passing run | KVM host |
| 10 | The co-pilot completes one loop on a node | Task picked up, config activated, PR opened. Its verification step was broken until `21acfecb` | installed node |
| 11 | PinePhone boots | `nixos/hardware/pinephone.nix` | PinePhone |
| 12 | RISC-V boots | `nixos/hardware/riscv-generic.nix` | RISC-V board |
| 13 | A Reachy Mini runs an agent locally | Drive it through `gpio_adapter.py` / `serial_adapter.py` / `ros_bridge.py`, whichever its stack exposes | Reachy Mini |
| 14 | **Intelligence compounds between two robots** | Robot A learns a skill by doing. Robot B performs it without having done it. Measure B before and after | 2 Reachy Minis |
| 15 | A co-pilot's work on one node improves another | Seed a goal on node A only. Show node B starting ahead of where it was | 2 nodes |
| 16 | Three nodes beat one node | Same benchmark, one node then three, same models. A floor test: error minimisation, not a capability claim | 3 machines |
| ~~17~~ | SETTLED 2026-09-07, see the Verified table. Full cycle run on the box: fetch from the cache, apply, boot generation 2, roll back to generation 1 | done | 1 reflashed node |
| 18 | An app installs through the SHIPPED SERVICE and launches | The nix platform is now verified above, driven as the session user. Not yet driven through `hart-liquid-ui`'s own routes as `hart` (its profile and nix-daemon access are untested), and the flatpak / appimage / android platforms remain faked in tests only | installed node |
| 19 | Input-to-photon meets the per-surface budgets | NOT a human problem, and not a broken instrument. Established on the box 2026-09-07: the instrument IS compiled into the running binary (the `hart-latency` string is present in it), synthetic input DOES reach the compositor (104 chords, each logged `workspace.switched`), and it emitted zero samples anyway. Cause: T_photon never occurs. Every switch logged `restored=0` because there are no toplevel windows, so it changed no pixels; `layer.composited` has fired ONCE this boot. latency.rs pairs an input with the NEXT page flip, so if the display were flipping at all, all 104 chords and the earlier 2228 pointer moves would have produced samples. Zero samples is proof the screen is static. What is needed is an input that causes a REPAINT: a native toplevel to arrange (app-launch is the uid wall, DROPPED by owner) or a hover that makes the WebKit shell damage its surface | a node with a native toplevel, or a hover target that repaints |
| 20 | A drop line never appears on a healthy box | Same journal. `hart-latency dropped ... verdict=SUSPECT` means vblanks stopped being reaped or frames stopped being queued, and the numbers beside it cannot be trusted | node with a GPU |
| 21 | A recipe replays THROUGH THE SHIPPED SERVICE | The replay itself is now verified above, driven as the session user. What is NOT verified is the same thing from `hart-liquid-ui`, which runs as `hart` and reaches the compositor only through the `hart-comp-ipc` relay + `HART_COMP_SOCK` added in 94294ef. Those units parse and are unit-tested but have never run: this node cannot build a generation locally and OTA needs a pushed commit | a node running a build that carries 94294ef |

| 22 | A copilot task writes real files | BLOCKED AT THE CREDENTIAL, and now precisely. Proven present on the box: `hart-copilot-daemon` alive and polling every 5 min, correctly logging `no task assigned by the hive`; the hive session live on :6777 with `can_edit_files`/`can_run_commands`; the `claude` binary on the daemon's own PATH. Missing: any Anthropic credential. No API key in the unit (only `ANTHROPIC_MODEL=claude-sonnet-5`), no credential file for the daemon user, no EnvironmentFile. `claude -p` then HANGS rather than failing: 0 bytes on stdout AND stderr, killed at 45s. The daemon bounds it with TASK_TIMEOUT_S so it reports a timeout, but it can never succeed. Note `gh` is also unauthenticated, so the push/PR step cannot reach GitHub either | a node with an Anthropic credential provisioned |
| 23 | Screen capture works under Tier-1 | hart-comp does NOT implement `wlr-screencopy-unstable-v1`: `grim` refuses and `/api/shell/screenshot` returns 403 while the native tier is live. The parity matrix's screen-capture row is green for the sway/cage tiers only | node with a GPU |

Row 7's first half is now settled: two nodes DO find each other, see the PeerLink
and announce rows above. What is still open there is the compute borrow itself.

### What "the hive learns" means today, precisely

**Active now (Phase 1).** `federated_aggregator.py` runs four channels: embedding
deltas (`embedding_delta.py` + `gradient_service.py`), model lifecycle deltas,
resonance tuning, and recipe sharing, which the code calls "trained task
intelligence". `world_model_bridge.py` turns agent interactions into training
data and distributes skills by gossip and RALT ingestion. This is real learning
at the retrieval, skill and routing layer, and it is what compounds today.

**Not active (Phase 2).** `federated_gradient_protocol.py` says so in its first
line: "Phase 2 stubs for LoRA gradient sync". Byzantine-resilient aggregation,
differential privacy, gradient compression, all interface definitions and
placeholders.

**Where the learning itself lives.** Not in this repo. Hebbian, Bayesian and
gradient work is in HevolveAI, a private sibling, loaded at runtime as a
signature-verified binary with a stub fallback. The seam is
`security/native_hive_loader.py`, and the README says so under a heading that
calls it the part we are not comfortable with.

So the accurate split is: HevolveAI learns, this repo carries what it derives
between nodes over four channels, and node-to-node weight sync is Phase 2.

### Row 16 is a floor, not a threshold

`hive_benchmark_prover.py` carries a seven-stage convergence ladder, from one
node at roughly 62% MMLU to a hundred thousand nodes past any single model.
Stage 2, three nodes ensembling, is where it claims the sum first exceeds the
single.

That stage measures error minimisation. Three models voting reduce variance and
catch each other on a given answer, which is well established and says nothing
about capability. If three nodes do not beat one, something is broken. If they
do, ensembling works, which nobody disputes.

The capability claim lives in mechanisms stage 2 does not exercise: expert
routing across models with different blind spots, generate-review-test where
verification is separable from generation, and learning that compounds so the
next node starts ahead. Rows 14 and 15 test those. Row 16 only shows the floor
exists.

**Every number in that ladder is a projection.** Not one has been measured, and
the source now says so.

Three laptops answer it. No robot, no Pi, no GPU rack.

**How the node count gets there:** crowdsourced compute, from users and
developers lending what is idle. `compute_borrowing.py` advertises and settles,
`revenue_aggregator.py` splits 90/9/1 and tracks payouts. That is the bootstrap
path, and it only starts paying for itself past this row.

### Rows 14 and 15 are the thesis

Rows 1 to 13 are plumbing. These two are the claim, and neither is demonstrated.

The mechanism is written. `integrations/agent_engine/world_model_bridge.py`:
"Every agent interaction becomes training data for continuous learning. Skills
distribute via gossip notification + local RALT ingestion." Experiences pass
`ConstitutionalFilter` before storage, RALT export is rate-limited and witnessed
by `WorldModelSafetyBounds`, and skill packets go through `ConstructiveFilter`.
`federated_aggregator.extract_local_delta()` pulls learning stats from the bridge
and signs them with the node identity.

So the path from one robot's experience to another robot's competence exists as
code, end to end. Nobody has watched it happen.

**What would settle row 14.** Not two robots taking turns answering, which is
only failover and proves nothing about learning. Robot A acquires something by
doing it. Robot B, which never did it, is then measurably better at it than it
was before. The before-and-after is the whole test: without a baseline it is a
story.

**What would settle row 15.** Same shape, one layer up. The co-pilot daemon works
a seeded goal on node A. Node B, given a related goal, starts from a better place
than it would have. This is what "the hive bootstraps itself" means, and it is
either measurable or it is a slogan.

A negative result here is worth more than any passing row above. If joint
experience does not compound, the architecture needs to change and everyone
should know.

### The honest state of these

Rows 1 to 6 are written and never confirmed on the metal. The code exists; that
is a different claim from the code working, and today produced a reminder: the
co-pilot's `nixos-rebuild` step had never once run on any machine because the
unit could not use `sudo`, and nothing caught it because nothing ran it.

Row 9 is worse than unverified. Four shards fail on every push to `main`, and
have long enough that the red is read as noise.

Rows 7 and 8 are the ones that decide whether the whole design works. The hive
is supposed to bootstrap itself: a device too small to think borrows compute,
the lender is paid, payment makes lending worth doing. Every part of that exists
in code. None of it has been demonstrated between two machines owned by
different people.

That is the experiment. It needs more than one person's hardware, which is why
it is written down here rather than claimed in the README.

## Rules for this file

- A row moves up only with output pasted in the issue. Not a description of
  output.
- A failure moves up too, recorded as a failure. A file that only ever gains
  passes is not being read honestly.
- If a claim in the README is not in either table, it should not be in the
  README.
