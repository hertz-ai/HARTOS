# `/var/lib/hart/session-tier` — State-File Contract

> **Status:** SPEC ONLY (Phase 0). This file documents the contract; it has **no
> consumer yet**. The greeter-reader and the out-of-process supervisor that
> *writes* it are **Phase 1** (`hart-session-supervisor`, B4). Phase 0 lands the
> contract + the floor-lock harness; Phase 1 builds the mechanism this contract
> governs and VM-proves it via loop-kill fault injection.
>
> **Companions:** [`HART_OS_NATIVE_ARCHITECTURE.md`](HART_OS_NATIVE_ARCHITECTURE.md)
> §6 (Never-Fail Tiering) and [`../../compositor/ROADMAP.md`](https://github.com/hertz-ai/HARTOS/blob/main/compositor/ROADMAP.md)
> Phases 0–1. This contract is how a half-finished compositor is kept from bricking
> the box: the screen is never blank because the tier is a latched,
> operator-clearable file, not a guess.

---

## 1. Purpose

The never-fail tier ladder (§6 of the architecture) guarantees the screen always
paints. The mechanism is a single state file the **greeter reads** and the
**supervisor writes**:

```
/var/lib/hart/session-tier
```

It names which display tier the box boots into. On a crash-loop the supervisor
writes the **next-lower** tier and latches it across reboot, so a Smithay (Tier-1)
regression silently lands on today's audited cage floor (Tier-3) instead of a
black screen — and a transient bug can be cleared by an operator rather than
permanently masking as a downgrade.

This is the file `scripts/vm/boot-tier.sh --tier <t>` selects for a local QEMU
boot, and the file the Phase-1 `nixosTest` loop-kill fault injection asserts on.

---

## 2. Allowed values (the tier ladder)

The file contains **exactly one** of these tokens, lowercase, no trailing
whitespace beyond a single optional newline:

| Value | Tier | What boots | When the supervisor selects it |
|---|---|---|---|
| `hart-comp` | **1** | Smithay HART-comp (KMS/DRM + pixman software path), full AI-native WM IPC | Default once VM-proven (Phase 3+). **Not selectable until then.** |
| `sway` | **2** | sway (wlroots) single-output kiosk running the **same** glass shell as a layer-shell client; `tile`/`summon` shim onto `swaymsg` | HART-comp crash-loops but the GPU/Wayland stack is fine (Phase 1 wires it; reduced moat, present) |
| `cage` | **3** | The **EXACT** cage + WebKitGTK + forced-software-GL kiosk that ships today (`hart-liquid-ui.nix`, session id `hart-shell`) — the audited never-fail paint floor | Broken GPU / llvmpipe, OR everything above crash-looped. **The supervisor can never drop below this.** |

**Notes**
- The token `cage` maps to the registered wayland-session **`hart-shell`** (the
  `desktop.nix` `defaultSession`). The contract token is the *tier name*; the
  session id it resolves to is `hart-shell`. They are kept distinct so the ladder
  can name a tier independently of the .desktop basename.
- `hart-comp` and `sway` resolve to their own wayland-sessions once those modules
  land (`hart-comp.nix` Phase 3; sway Tier-2 Phase 1). Until a tier's session is
  built **and** VM-proven, the supervisor must refuse to select it (it is not in
  `providedSessions`), so the latch can only ever name a tier that paints.
- **GNOME is NOT a tier value.** GNOME stays **user-selectable at the greeter**
  (`desktop.nix` guarantee) as the ultimate human escape hatch, orthogonal to this
  latch. A latched `cage` never hides the greeter's GNOME entry.

---

## 3. Latch semantics

1. **Read on session start.** The greeter (Phase 1) reads the file before
   launching a session. **Missing or unreadable file ⇒ the floor `cage`** (fail to
   the audited floor, never to a higher unproven tier). An invalid/unknown token is
   treated as missing ⇒ `cage`.

2. **Monotonic downgrade only — the supervisor never raises the tier.** On a
   **crash-loop = 3 restarts within 5 minutes** of the *currently-latched* tier,
   the supervisor writes the **next-lower** tier (`hart-comp → sway → cage`) and
   restarts the greeter. The descent stops at `cage` (Tier-3 is the floor; the
   supervisor can never write a value below it). Only an **operator reset**
   (§4) raises the tier back up.

3. **Latch persists across reboot.** The file lives under `/var/lib/hart`
   (persistent state), so a Tier-1 regression that dropped to `cage` stays on
   `cage` after a power-cycle. This is deliberate: a flapping compositor must not
   re-enter its own crash-loop every boot. The cost — a box silently stuck on the
   floor after one bad boot — is bounded by the operator-reset + telemetry (§4).

4. **Atomic write.** The supervisor writes via a temp-file + `rename(2)` so a
   crash mid-write never leaves a torn/empty file (which would, by rule 1,
   correctly fall to `cage` anyway — fail-safe by construction). One writer only:
   the supervisor. The greeter and `boot-tier.sh` are **readers**; nothing else
   writes this file (one-writer-per-persisted-value, per the engineering gates).

5. **Out-of-process authority.** The writer is a systemd/greetd-level unit
   (`hart-session-supervisor`), **not** an in-process Python thread.
   `node_watchdog` is structurally a thread supervisor and **must not** own this
   file — at most it emits a "compositor unhealthy" signal the supervisor consumes
   (Phase 1). A thread supervisor cannot switch a display-manager session or latch
   the choice across a reboot.

---

## 4. Operator reset + observability

- **Reset to Tier-1:** `hartctl session reset-tier` clears the latch (writes
  `hart-comp`, or removes the file so rule-1's default is re-evaluated against the
  highest VM-proven tier). After reset, the next session attempts the top tier
  again. This guarantees a transient Tier-1 bug can never permanently mask as a
  downgrade with no recovery path.
- **Telemetry:** the current latched tier is surfaced (status endpoint / Conky /
  operator dashboard) so a user is never silently running on `cage` forever after
  one bad boot without knowing why.

---

## 5. Phase-0 freeze (what THIS file pins now)

- The **value domain** (`hart-comp | sway | cage`) is frozen here so every later
  phase (supervisor, greeter, boot-tier.sh, telemetry) agrees on the tokens.
- `cage` is pinned as the floor and the **current** `defaultSession`
  (`hart-shell`) — Phase 0 changes **no** runtime behavior; it only makes
  "never blank screen" a contract a later phase can implement and a test can
  assert.
- `scripts/vm/boot-tier.sh` already speaks this contract: `--tier <value>`
  validates against the same domain and records `hart.session_tier=<value>` on the
  kernel cmdline, so local QEMU iteration and the CI `nixosTest` exercise the same
  tokens.

The Phase-1 deliverable (B4) is the supervisor that *implements* §3–§4 and the
loop-kill `nixosTest` that proves: a crashing fake Tier-1 binary lands on `cage`
with the latch file = `cage`, and `hartctl session reset-tier` re-arms Tier-1 —
while GNOME stays greeter-selectable throughout.
