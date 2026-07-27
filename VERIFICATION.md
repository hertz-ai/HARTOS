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
| 13 | A Reachy Mini runs an agent locally | Drive it through `gpio_adapter.py` / `serial_adapter.py` / `ros_bridge.py`, whichever its stack exposes | Reachy Mini |
| 14 | **Intelligence compounds between two robots** | Robot A learns a skill by doing. Robot B performs it without having done it. Measure B before and after | 2 Reachy Minis |
| 15 | A co-pilot's work on one node improves another | Seed a goal on node A only. Show node B starting ahead of where it was | 2 nodes |

| 16 | **Three nodes beat one node** | Same benchmark, one node then three, same models. The crossover the whole design rests on | 3 machines |

### What "the hive learns" means today, precisely

This matters more than any single row, because getting it wrong in public is
unrecoverable. Someone will open the file and read the first line.

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

### How to describe the closed core in public

A rule worth keeping, because getting it wrong costs more here than anywhere
else in the project.

HevolveAI is closed. Every property anyone claims about it is unverifiable by
construction. So a sentence like "plastic, continual, grounded, encoder-free,
Hebbian forward-pass learning with causal attribution and epistemic awareness"
is a dozen assertions about a binary nobody can open. Every word can be true and
it still reads as invented, because there is no way for a reader to find out.
That is the shape people call slop, and it is what got parodied in the thread.

The README already does the opposite and it is the better model:

> "The learning is not open. Hebbian, Bayesian and gradient code lives in a
> private repo called HevolveAI, and this runtime loads it as a signed binary
> and falls back to a stub when it is missing. You can see the seam in
> `security/native_hive_loader.py`."

Name the mechanism, admit the discomfort, point at the one thing that IS
checkable: the seam. A reader who opens `native_hive_loader.py` and finds a real
signature check and a real stub fallback will extend more credit than any list
of properties earns.

**Describe open parts by what they do and where the file is. Describe the closed
part by its boundary.** Reaching for "epistemic awareness" about a blob hands a
thread its best line.

### Row 16 is the threshold

`hive_benchmark_prover.py` carries a seven-stage convergence ladder, from one
node at roughly 62% MMLU to a hundred thousand nodes past any single model.
Stage 2, three nodes ensembling, is where it claims the sum first exceeds the
single. That is the threshold: below it the hive is a nice idea, above it the
approach works and the rest is scale.

**Every number in that ladder is a projection.** Not one has been measured. They
are now marked as such in the source, because a percentage that looks like a
result and is not is the fastest way to lose a reader who checks.

Three laptops answer this. It does not need a robot, a Pi or a GPU rack, and it
is the single cheapest experiment that could refute the project.

**How the node count gets there:** crowdsourced compute, from users and
developers lending what is idle. `compute_borrowing.py` advertises and settles,
`revenue_aggregator.py` splits 90/9/1 and tracks payouts. That is the bootstrap
path, and it only starts paying for itself past this row.

### Rows 14 and 15 are the thesis

Everything else here is plumbing. These two are the claim the project exists to
make, and neither has been demonstrated.

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
