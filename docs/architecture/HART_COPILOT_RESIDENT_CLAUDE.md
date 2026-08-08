# HART Co-Pilot: Claude Code resident inside HART OS

**Status:** shipped into an installed image. `hart.copilot.enable` **and**
`hart.copilot.daemon.enable` are both `true` in `nixos/profiles/desktop.nix`, and the
raw-desktop image built at `3ed0fc2b` (CI run `31213146492`) carries the module. That
image was verified byte-perfect against its published `.raw.xz.sha256` and written to
USB on 2026-08-08. §6.1's blocker is therefore CLOSED: the writable root persists the
OAuth credential across reboots.
**Date:** 2026-07-26/27; status refreshed 2026-08-08.

**One manual step remains by design.** No API key ships in the image (§5), so on a
freshly flashed node the daemon starts with no credential until a human runs `claude`
once and completes `/login`. Unattended residency begins after that, not at first boot.
**Steward's ask:** *"claude code shd be the co-pilot of HARTOS fixing and
bootstrapping it"*, living in the node's own Linux/Nix terminal, *"achieving all
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
| Acts fluently as a person | It edits, tests, and **commits under the steward's identity**. The repo rule is already *no `Co-Authored-By: Claude`* (`feedback_no_coauthor.md`). Delegation, not deception. |
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
| **The 71 seeded goals** | `integrations/agent_engine/goal_seeding.py:18` (consumed `:2169`) | `SEED_BOOTSTRAP_GOALS`, **71 entries**, matching the "~70 recipes fleet-wide" objective in `CLAUDE.md`. Already sharded deterministically across peers by `sha256(slug) % N`. |
| **The guardrails** | `security/hive_guardrails.py` | The constitutional layer every action passes. |
| **The one pipeline** | `POST /chat`, `dispatch.py::dispatch_goal` | Per the Hive Collab Bootstrap: agentic work has exactly one execution path. |
| **Idle-compute loop** | `integrations/coding_agent/coding_daemon.py` | Existing background worker. |

### Gaps found

| Gap | Status |
|---|---|
| `claude-code` not packaged for the node | **CLOSED** by this work (see §3) |
| `hart hive connect` CLI | **CLOSED.** `hart_cli.py` now has a `hive` group: connect, status, tasks, scope, pause, resume, disconnect. Thin client over the routes `claude_hive_session.get_blueprint()` already served. Wired but not yet exercised against a live dispatcher. |
| Persistent credential on the node | **BLOCKED on the installed image** (see §6) |

---

## 3. What was built

### `nixos/modules/hart-copilot.nix` (new)

Adds two commands to the node:

```
claude          # the co-pilot itself, in the terminal
hart-copilot    # opens it BOUNDED: writable checkout, fresh branch, boundary printed
```

**Packaging: no packaging work was needed.** `claude-code` is absent from the
pinned 24.11 nixpkgs but present in **25.05**, the input this flake *already*
threads through for Rust:

- flake inputs: `nixpkgs` = `50ab793` (24.11), `nixpkgs-rust` = `ac62194c…` (25.05)
- `claude-code` **v1.0.85** at `pkgs/by-name/cl/claude-code/package.nix` in that rev
  (`buildNpmPackage`, `mainProgram = "claude"`, `license = unfree`)
- the flake already sets `allowUnfree = true` (`flake.nix:363`)

It is instantiated with the **same pattern as `hart-comp`'s `rust_1_88`**, one
way this repo reaches the newer nixpkgs, no third nixpkgs, nothing vendored.

**Boundary made mechanical, not advisory.** `hart-copilot`:

1. clones/updates a **writable** checkout (`~/HARTOS`). The nix store is read-only,
   so the co-pilot *structurally cannot* mutate the running system's source in place;
   its output ships back the normal way (branch → human merge → OTA).
2. **checks out a fresh branch** (`copilot/<timestamp>`) before handing over, so it can
   never be sitting on `main`.
3. prints the boundary contract, then `exec`s `claude` in that directory.

**Wiring:** registered in `flake.nix` `hartModules`; `hart.copilot.enable = true` in
`configurations/desktop.nix`. Defaults **OFF**, so a normal build is byte-identical
and carries none of the closure.

**Robustness:** `claudePkg` is `lib.optionals`-guarded. If the upstream attribute
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
claude                # then /login  (OAuth, and NO API key is baked into the image)
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

## 6. Known caveats

1. **The login does not survive a reboot on the live ISO.** The ISO's home is
   **tmpfs**. Persistence requires the **installed writable-root image**, which
   makes the raw-desktop / systemd-repart work (already committed and eval-green) a
   hard prerequisite for an unattended resident co-pilot, not just a nicety.
2. **Closure weight.** This adds node + `claude-code` to `iso-desktop`. The flake
   eval is green; the **full ISO build is the real gate** for the size/time ceiling.
   Enabled as exactly one feature at a time, per the "everything-on sweep broke
   iso-desktop ×4" lesson.
3. **The node is an 8 GB potato.** A resident co-pilot competes with the OS for RAM.
   If that bites, the alternative topology is Claude Code on the steward's box
   driving the node over OTA. Same loop, with no credential sitting on the
   device and nothing competing for its RAM.

---

## 7. Remaining work

| # | Item | Why |
|---|---|---|
| 1 | ~~**`hart hive connect` CLI**~~ **DONE** | Built. `hart_cli.py` gained a `hive` group (connect, status, tasks, scope, pause, resume, disconnect) that calls the routes `claude_hive_session.get_blueprint()` already served. What remains is not code: run it against a live dispatcher and confirm a task actually arrives, executes and reports. |
| 2 | ~~**Installed writable-root image**~~ **DONE** | raw-desktop built at `3ed0fc2b`, hash-verified, flashed 2026-08-08. The root is writable, so the OAuth credential and all state persist. |
| 3 | **Full `iso-desktop` build green** | Still open, but no longer gates this: the raw/installed image is the delivery path now (`raw_image_installed_system_pivot_2026-07-16`), and updates arrive OTA rather than by re-flash. |
| 4 | ~~*(optional)* systemd daemon mode~~ **DONE** | `hart-copilot-daemon.service` exists and `copilot.daemon.enable = true` is set in the profile. Bounded: 1 core / 2 G, `Restart=on-failure` + `RestartSec=60`, and activation goes through a ROOT path unit whose `ExecStart` hardcodes `test` — the daemon passes no arguments, so an agent that ignores every instruction in its prompt still cannot activate an arbitrary config. |
| 5 | **First `/login` on the node** | The one remaining human step. Until it happens the daemon has no credential (§5 — no key in the image, by design). |
| 7 | **MCP self-configuration on the node — task #48** | **The resident co-pilot currently has NO connection to the agent stack.** See §9. |
| 6 | **Prove a task actually lands** | Carried over from item 1: `hart hive` was driven end to end against a stood-up blueprint, but a LIVE dispatcher sending this session a real task has still never been run. |

## 9. The Nunba integration page belongs to THIS plan (steward, 2026-08-08)

> *"whatever Nunba page offers is to wire into `HART_COPILOT_RESIDENT_CLAUDE.md`"*

Nunba already ships the other half of this capability: `/admin/integrations/claude-code`
(commit `1f876118`, routed at `MainRoute.js:684`, backed by `main.py:4890`
`GET /api/admin/mcp/token` + `:4918` rotate). It hands a **human** the MCP endpoint,
the bearer token, and a copy-paste `mcpServers.hartos` snippet.

**These are one capability at two locations, and they are not connected.** Verified:
`nixos/modules/hart-copilot.nix` contains no MCP wiring at all (the only `.claude` hit
is a comment about the Windows dev box), and `grep -ri mcp nixos/` returns **zero
files**. So the resident co-pilot boots as a **bare** Claude Code: it can edit and test
in `~/HARTOS`, but it cannot `list_goals`, `agent_status`, `create_goal`,
`dispatch_goal` or `steer_goal` — the exact loop the Nunba page advertises is the one
the node cannot run. A human would have to open a browser and paste a snippet into the
filesystem of a machine that already knows its own endpoint and already owns its token.

The hook needs no new mechanism: `integrations/mcp/mcp_http_bridge.py:82-94` already
resolves `HARTOS_MCP_TOKEN` → `HARTOS_MCP_TOKEN_FILE` → `~/.nunba/mcp.token`. The node
mints the token and serves the bridge; `hart-copilot` just has to write the config
before it `exec`s.

**DRY constraint:** the snippet shape must come from the ONE generator Nunba already
exposes as `config_snippet` — never re-templated in Nix. The last drift of this exact
shape (stdio → http + bearer, `f5b99d8`) produced silent 403s and is *why* the Nunba
page had to be written.

This does not touch the boundary in §1/§5: no key enters the image, the checkout stays
writable-only, the branch stays fresh, and merge / OTA / master-key signing stay human.
It grants the resident agent the same verbs the steward already grants their own Claude
Code, under the same audit actor and the same guardrail gate.

Full scope, rotation handling and artifact-level acceptance: **task #48**.

---

### Why `enable` alone was not enough (2026-07-30 incident)

The 30 July flash had `copilot.enable = true` and the resident co-pilot did nothing.
`enable` installs only the `hart-copilot` launcher; the bounded Claude Code worker is a
SECOND, separate opt-in (`copilot.daemon.enable`) and no consumer had set it. Both are
now set in `nixos/profiles/desktop.nix`. Recorded here because the symptom — an
enabled feature that is inert on the node — reads as a broken module rather than an
unset second switch.

---

## 8. Validation performed

- `hart-copilot` launcher body: `bash -n` clean.
- Nix brace/paren balance checked on all three touched files.
- `tests/unit/test_nix_embedded_python_parses.py`: 16 passed.
- **CI flake evaluation: GREEN** (`03cd1cbb`), the authoritative structural gate,
  since local Nix cannot evaluate on the Windows dev box.
- `hart hive` group: driven end to end against a real server. A minimal Flask
  app serving only `claude_hive_session.get_blueprint()` was stood up on
  :6799, and every subcommand was run against it. connect returned a session
  id, status reported `idle` with the scope that was asked for, scope changed
  it, pause moved the session to `paused` and resume back to `idle`, tasks
  reported none, disconnect returned it to `disconnected`, an invalid scope
  was rejected by click before any request left the machine, and with the
  server stopped the CLI printed `Cannot connect` and exited 1.

  That run caught a real bug. The first version of `hive status` branched on a
  `connected` boolean and read `tasks_completed` from the top level. The
  payload has neither: `status` is one of disconnected / connecting / idle /
  working / paused, and the counters are nested under `stats`. So status would
  have printed "Not connected" immediately after a successful connect. It
  parsed, it ran, it gave a clean error against no server, and it was wrong.

  Still not done: a live dispatcher actually sending this session a task. That
  test has not been run.
- Full `iso-desktop` build: **pending**.
