# HART Co-Pilot — Claude Code resident inside HART OS

**Status:** module built, flake-eval green (`03cd1cbb`), ISO build pending.
**Date:** 2026-07-26/27.
**Steward's ask:** *"claude code shd be the co-pilot of HARTOS fixing and
bootstrapping it"* — living in the node's own Linux/Nix terminal, *"achieving all
the seeded goals as its own"*, *"via the guardrails HARTOS has"*.

---

## 1. The governing idea: trust is a boundary

The steward's framing, which drives every design decision below:

> *"trust is a boundary which makes it human-like... the goal for AI is not just
> maximising the only goal but to make it the only outcome people care about in
> their life to be a rich civilization. if capitalism is what we want to win we let
> it win via people democratically."*

and:

> *"doesn't mean HARTOS cannot be used to impersonate human, it just means where
> important doesn't change the outcome."*

Translated into architecture:

| Principle | Implementation |
|---|---|
| Not a maximizer | The co-pilot does **not** choose its own objective. Its queue is the **seeded goals** the fleet already agreed on. |
| Acts fluently as a person | It edits, tests, and **commits under the steward's identity** — the repo rule is already *no `Co-Authored-By: Claude`* (`feedback_no_coauthor.md`). Delegation, not deception. |
| Where important, the outcome is unchanged | **Merge, OTA publish, and master-key signing stay human/democratic.** The agent proposes; a human disposes. |
| Value flows to people | Work is attributed through the existing Spark / 90-9-1 rails, not captured by the daemon. |

**The line we hold:** an agent acting *for* a person is delegation and is fine. An
agent presenting itself as a *specific real third party in order to deceive them* is
fraud, and is not. Everything here is the former.

**Net effect:** full autonomy *inside* the work, zero authority *at* the boundaries.
The worst case of an unattended run is **a branch nobody merges.**

---

## 2. What already existed (verified, not rebuilt)

Per the standing *leverage-existing / no-rebuild* rule, this was a wiring job, not a
build-from-scratch. Verified this session:

| Piece | Where | Note |
|---|---|---|
| **`ClaudeHiveSession`** | `integrations/coding_agent/claude_hive_session.py:81` | Claude Code joining the hive as a worker is **already a protocol**: connect → receive task → execute → report → earn Spark, over PeerLink, shard-engine privacy filtering, **master-key verification on task origin**. Its own docstring already states the boundary: *"All code changes require user approval before commit."* |
| **The 71 seeded goals** | `integrations/agent_engine/goal_seeding.py:18` (consumed `:2169`) | `SEED_BOOTSTRAP_GOALS`, **71 entries** — matches the "~70 recipes fleet-wide" objective in `CLAUDE.md`. Already sharded deterministically across peers by `sha256(slug) % N`. |
| **The guardrails** | `security/hive_guardrails.py` | The constitutional layer every action passes. |
| **The one pipeline** | `POST /chat`, `dispatch.py::dispatch_goal` | Per the Hive Collab Bootstrap: agentic work has exactly one execution path. |
| **Idle-compute loop** | `integrations/coding_agent/coding_daemon.py` | Existing background worker. |

### Gaps found

| Gap | Status |
|---|---|
| `claude-code` not packaged for the node | **CLOSED** by this work (see §3) |
| `hart hive connect` CLI | **STILL OPEN** — `claude_hive_session.py`'s docstring advertises it; `hart_cli.py` has no `hive` command. This is the one wire between the co-pilot and its shard of the 71 goals. |
| Persistent credential on the node | **BLOCKED on the installed image** (see §6) |

---

## 3. What was built

### `nixos/modules/hart-copilot.nix` (new)

Adds two commands to the node:

```
claude          # the co-pilot itself, in the terminal
hart-copilot    # opens it BOUNDED: writable checkout, fresh branch, boundary printed
```

**Packaging — no packaging work was needed.** `claude-code` is absent from the
pinned 24.11 nixpkgs but present in **25.05** — the input this flake *already*
threads through for Rust:

- flake inputs: `nixpkgs` = `50ab793` (24.11), `nixpkgs-rust` = `ac62194c…` (25.05)
- `claude-code` **v1.0.85** at `pkgs/by-name/cl/claude-code/package.nix` in that rev
  (`buildNpmPackage`, `mainProgram = "claude"`, `license = unfree`)
- the flake already sets `allowUnfree = true` (`flake.nix:363`)

So it is instantiated with the **same pattern as `hart-comp`'s `rust_1_88`** — one
way this repo reaches the newer nixpkgs, no third nixpkgs, nothing vendored.

**Boundary made mechanical, not advisory** — `hart-copilot`:

1. clones/updates a **writable** checkout (`~/HARTOS`). The nix store is read-only,
   so the co-pilot *structurally cannot* mutate the running system's source in place;
   its output ships back the normal way (branch → human merge → OTA).
2. **checks out a fresh branch** (`copilot/<timestamp>`) before handing over — it can
   never be sitting on `main`.
3. prints the boundary contract, then `exec`s `claude` in that directory.

**Wiring:** registered in `flake.nix` `hartModules`; `hart.copilot.enable = true` in
`configurations/desktop.nix`. Defaults **OFF**, so a normal build is byte-identical
and carries none of the closure.

**Robustness:** `claudePkg` is `lib.optionals`-guarded — if the upstream attribute
ever disappears, the failure is a readable assertion instead of an eval crash on
`lib.getExe null`.

**Env knobs:** `HART_COPILOT_REPO`, `HART_COPILOT_ORIGIN`, `HART_COPILOT_BRANCH`,
`HART_COPILOT_BACKEND` (defaults to this node's own `http://127.0.0.1:6777`, so
"debug the OS from within" drives the *live local* runtime).

---

## 4. How to use it on the node

```bash
hart-copilot          # bounded: writable clone, fresh branch, boundary printed
# first run only:
claude                # then /login  (OAuth — NO API key is baked into the image)
```

Then the co-pilot works normally: read the live journal, reproduce, fix, run tests,
commit to its branch. Everything agentic it dispatches goes through `POST /chat` /
`dispatch_goal`, so the constitution applies to an AI-initiated fix exactly as it
does to a human one.

---

## 5. Security posture

- **No API key in the image.** Interactive OAuth only; the credential lands in the
  user's home, never the nix store, never a release artifact.
- **Master-key signing is AI-EXCLUDED by construction** (`security/master_key.py`).
  This module neither needs nor touches it. The steward signs releases.
- **No new ingress.** The co-pilot is a local terminal program; it opens no port.
- **Commits are branch-scoped.** No path in this module force-pushes or touches
  `main`.

---

## 6. Known caveats (honest)

1. **The login does not survive a reboot on the live ISO.** The ISO's home is
   **tmpfs**. Persistence requires the **installed writable-root image** — which
   makes the raw-desktop / systemd-repart work (already committed and eval-green) a
   hard prerequisite for an unattended resident co-pilot, not just a nicety.
2. **Closure weight.** This adds node + `claude-code` to `iso-desktop`. The flake
   eval is green; the **full ISO build is the real gate** for the size/time ceiling.
   Enabled as exactly one feature at a time, per the "everything-on sweep broke
   iso-desktop ×4" lesson.
3. **The node is an 8 GB potato.** A resident co-pilot competes with the OS for RAM.
   If that bites, the alternative topology is Claude Code on the steward's box
   driving the node over OTA — same loop, no on-device credential, no RAM contention.

---

## 7. Remaining work

| # | Item | Why it matters |
|---|---|---|
| 1 | **`hart hive connect` CLI** | The single missing wire. Everything it would call (`ClaudeHiveSession`, the deterministic shard, `dispatch_goal`) already exists. This is what turns "Claude in the terminal" into "achieving the seeded goals as its own". |
| 2 | **Installed writable-root image** | Makes the credential (and any state) persist — prerequisite for unattended operation. |
| 3 | **Full `iso-desktop` build green** | Confirms the closure fits. |
| 4 | *(optional)* systemd daemon mode | Only after 1–3, and only with the branch-only boundary preserved. |

---

## 8. Validation performed

- `hart-copilot` launcher body: `bash -n` clean.
- Nix brace/paren balance checked on all three touched files.
- `tests/unit/test_nix_embedded_python_parses.py`: 16 passed.
- **CI flake evaluation: GREEN** (`03cd1cbb`) — the authoritative structural gate,
  since local Nix cannot evaluate on the Windows dev box.
- Full `iso-desktop` build: **pending**.
