# Verified and unverified

Every claim this project makes is in one of two tables. The top table has
evidence you can check today. The bottom table does not, and moving a row from
the bottom to the top is the most useful contribution available here.

Most of the unverified rows need one thing: hardware that is not a developer
laptop. A Raspberry Pi, a board with no GPU, a machine with 4 GB. We do not have
enough of those, and CI has none.

**To claim a row:** open the linked issue, say which board you have, run the
steps, and paste the output. Pass or fail both help. A row that fails moves to
the top table as a verified negative, with the failure recorded, which is worth
as much as a pass.

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
