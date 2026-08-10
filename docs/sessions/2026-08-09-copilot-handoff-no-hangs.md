# Co-pilot handoff — "no hangs whatsoever" + live boot-hang diagnosis

**Written 2026-08-09, rewritten 2026-08-10 by the Nunba/Windows-side agent, for
the resident HART OS co-pilot to continue.** This is a conversation handoff. The
steward and I diagnosed a set of HART OS problems from Windows by reading the
exported journal off the stick. You are now inside the running node and can do
what I could not: observe live, activate changes with `systemctl start --wait
hart-copilot-verify.service`, and iterate on real hardware. Pick this up as your
own work.

## The mandate, in the steward's words

> "no hangs whatsoever" — capture all hangs/ANRs, contain the blast radius,
> recover from ANRs autonomously, and a wizard to connect to WiFi on bootup.

Absolute bar: the *user* must never experience a hang. Prevent where possible;
where a hang is still possible, detect + contain + recover so it never reaches
the user. This is the Android model (liveness heartbeat, ANR timeout, kill the
wedged unit not the world), applied top to bottom.

---

## PRIORITY 1 — one root cause behind THREE symptoms: hart-comp never becomes DRM master

This is the rewrite. The steward and I converged on a single hypothesis that
ties together three separate-looking failures. **Chase this before anything
else — if it holds, it collapses three tasks into one fix.**

### The evidence (from the 6675-line journal export)

- The compositor got a real GPU context: `HART-comp DRM: GLES GPU renderer
  initialised on the primary node`, Intel HD 620, Mesa 24.2.8, OpenGL ES 3.2, on
  `card1`. **So the compositor is NOT software/cairo — it is on the iGPU.**
- But immediately: `smithay::backend::drm::device::fd: Unable to become drm
  master, assuming unprivileged mode` (t=20:56:53). It has a GLES context yet is
  running **unprivileged** — not the real DRM master / seat leader.
- The GTK4 host's `GSK = CAIRO` line is a RED HERRING for the hang: that is only
  the GTK4 window frame (deliberately pinned to cairo to dodge a known GSK-GL
  layer-shell hang — see `hart-layer-shell-host.nix:296-308`). The orb itself is
  WebKit content and WebKit's DMABUF/GPU compositing is kept ON on this rung. The
  orb is *supposed* to composite on the iGPU.

### Why "not DRM master" plausibly causes all three symptoms

1. **The orb-hover hang.** An unprivileged DRM device cannot hand out scanout
   dma-bufs / do privileged atomic KMS the way a master can. So WebKit's DMABUF
   import can't get a real GPU surface and WebKit's compositor silently degrades
   to CPU — and the one thing continuously animating, the orb, pegs a core.
   (WebKit RSS climbs 179→410 MB in ~110 s while compositor RSS stays ~85 MB —
   consistent with WebKit doing the work on the CPU.)
2. **The in-shell terminal "no output / async" wedge.** Same render-root: shell
   content on the degraded GPU path, CPU-bound, so typing lags and the in-shell
   terminal appears to hang. NOT a separate bug.
3. **Ctrl+Alt+F2 / recovery consoles dead.** This is the load-bearing new
   evidence. `desktop.nix:655-688` ALREADY ships a recovery-console design that
   pre-spawns a getty on tty2 and relies on **logind** to do the VT switch, on
   the theory "the hung graphical session cannot veto a kernel VT switch — logind
   owns the seat, not the compositor." **That block was already in the image the
   steward last booted (`02709426`), and Ctrl+Alt+F2 STILL did nothing.** So the
   logind-owns-the-seat theory is empirically false on this hardware — which is
   exactly what you'd expect if hart-comp never properly took the seat/DRM
   master: a compositor that grabbed the seat but is stuck unprivileged can hold
   VT1 in a state where neither logind switches away NOR the compositor hands
   off. The dead VT switch and the orb hang are then the SAME seat/master
   failure, seen from two angles.

### What to actually do (in order, live on the Lenovo)

1. **Find out WHY hart-comp is not DRM master.** Candidates to rule in/out:
   - a leftover holder of the master / VT1: plymouth, fbcon, `cage`, a previous
     tier that did not fully release, or greetd's own session;
   - libseat backend: is it using the **logind** backend or **seatd**? (If seatd,
     logind's VT-switch path in `desktop.nix:671-678` is a no-op by construction —
     they are different seat managers — and only the compositor can switch VTs.)
   - the startup RACE the code already knows about: `udev.rs:~599` comments an
     "Unable to become drm master startup race" that `apply_pending_session` is
     supposed to recover by re-acquiring master on the next session-activate.
     **Confirm it actually re-acquires** — add a log line after the re-acquire and
     watch it live. If it silently stays unprivileged, that is the bug.
2. **Once hart-comp is the real master, RE-TEST all three symptoms in one pass:**
   orb hover (does the core still peg?), in-shell terminal (does typing still
   lag?), Ctrl+Alt+F2 (does it switch to the tty2 getty?). If the master fix
   clears them, you are done with three tasks at once.
3. **Only if F2 still fails after hart-comp is master** do you need the compositor
   to initiate the switch itself — the `change_vt` patch is in the appendix below,
   demoted precisely because it is likely moot once master/seat is correct.

**Do NOT flip the shell to `GSK=GL` to "fix" the render** — that reopens the
known GSK-GL layer-shell hang the host module forces cairo to avoid, and it does
not touch the master/seat root. The render is already on the iGPU; the problem is
privilege, not the rung.

---

## PRIORITY 2 — In-shell ANR watchdog + autonomous recovery (required regardless)

Even if Priority 1 clears the current wedge, "no hangs whatsoever" demands a
recovery net for the NEXT wedge. This is still unbuilt — `desktop.nix:1066`
literally says the render config stays as-is *"until the in-shell ANR watchdog
lands to recover the surface autonomously."* No commit implements it yet.

Why the existing watchdogs miss it: `hart-session-supervisor.nix` watches the
TIER, not the shell CONTENT. The **paint watchdog** (`shell-ready`) drops a tier
that never presents a frame; the **input-alive watchdog** drops a painted tier
whose seat delivers no events. The orb wedge PAINTS and HAS live input, then
stalls in-shell — it passes both. `NodeWatchdog` (agent daemon, 300 s,
`integrations/agent_engine/agent_daemon.py:356`) watches the daemon, not the UI.
So nothing sees an in-shell ANR. Build it:

- The shell's main/render loop touches a tmpfs marker (e.g.
  `/run/hart/session/shell-alive`) on a fixed cadence — SAME dir + 0770
  group-writable pattern as `shell-ready`/`input-alive` (supervisor ~lines
  118-124). Add `HART_SHELL_ALIVE_FLAG` next to the existing exported paths.
- Extend the supervisor's selector loop (do NOT add a parallel mechanism — the
  module is emphatic about one path) to treat a stale marker while the tier is
  painted+input-alive as an ANR.
- **Contain the blast radius**: recover the wedged surface first (reload the
  WebView / restart just the shell host), do NOT tear down the compositor.
  Escalate to a tier drop only after surface recovery fails N times, via the
  existing `record_crash → lower_tier → write_tier` ladder.
- **Autonomous recovery**: on ANR, capture first (Priority 3), then recover, then
  log the recovery with a streak count so a flapping surface is visible.
- FAIL-SAFE like the input marker: gate the authoritative behaviour behind a
  timeout > 0 (as `inputAliveTimeoutSeconds` does) so an older shell that does
  not yet write the marker never flaps a healthy tier.

## PRIORITY 3 — Unify hang capture so an ANR is always recorded

Today HARTLOG (`hart-boot-log.nix`) writes the RICH bundle (shell-ready marker +
mtime, GSK/GDK/EGL/GBM/WebKit GL errors, tier latch, crash window, `systemctl
--failed`) but is a permanent NO-OP on an installed node (`hart-hartlog-
create.nix` refuses internal disks). HARTJRNL (`hart-journal-export.nix`) DOES
run here but writes only header + 200 warnings + raw journal — it lacks exactly
the paint/renderer surface the hang needs. Fold the two CAPTURE payloads into
one: write the rich bundle to whichever labelled FAT32 partition exists (HARTLOG
removable-carved OR HARTJRNL operator-carved). Keep the two CREATION strategies
(live stick vs installed) — they genuinely differ — but there is no reason for
two payloads and a `force=1` bypass. Trigger on ANR (Priority 2), not only on a
timer, so the pre-wedge state reaches the stick.

## PRIORITY 4 — WiFi-on-boot wizard

`hart-net-diag.nix` does `rfkill unblock` and USB-ethernet DHCP auto-connect, but
its own comment says "Connecting to a Wi-Fi SSID still needs the user to enter
it" — no onboarding wizard. On the critical path: YOU need network to reach the
Claude API and continue this conversation, and the steward should not hand-edit
wpa_supplicant. Note `7e5ed76f` already fixed wifi-credential *persistence* (they
were starved under disk pressure, #40) — so persistence is handled; the wizard is
the missing UI. Requirements:
- Fires on boot when no network is up and no wired fallback exists.
- Scans, lists SSIDs, takes a password, connects via NetworkManager.
- Must render on a tier that CANNOT hang — host it on the cage paint floor or a
  minimal layer-shell surface, NOT inside the WebKit shell whose responsiveness
  is the very thing in doubt. A wizard that hangs is worse than none.
- NEVER store the PSK in the world-readable store; NetworkManager keyfile, correct
  perms.

---

## Already landed on main (verify these are in your checkout)

1. `886870f4` — local-time RTC (`time.hardwareClockInLocalTime = true`) + gate
   the flathub re-run on real connectivity=full. Fixes the clock skew that broke
   TLS → time-sync → flatpak/app-installs. First boot should show a monotonic
   clock and DNS-over-TLS staying up (the journal captured the skew as an
   out-of-order `15:32` stamp inside a `20:56` boot + a `TLS→UDP` resolved
   downgrade — verify it is gone).
2. `9e3ee090` — Magic SysRq / REISUB: a guaranteed kernel escape from a wedged
   shell. Plus SSH (`hart-base.nix:402`, password auth for first login) — both are
   MANUAL escapes; neither is autonomous recovery (Priority 2 is).
3. `7e5ed76f` — wifi credentials no longer starved under disk pressure (#40).
4. `6866ca9e` — the `/chat` 500 (autogen deref in an import-time annotation).
   Blast radius was ONE request, NOT an OS hang — do not conflate.
5. `83f388c3` — `hart-journal-export.nix` `/dev//dev/sdb` double-prefix fixed.
   VERIFY: plug a spare FAT32 stick, confirm `hart-journal-<host>.txt` lands on it.

## How to work (from the copilot daemon's own contract)

- You are on a `copilot/*` branch. NEVER commit to main, never push to main.
- Activate an OS change on THIS machine: `systemctl start --wait
  hart-copilot-verify.service` (runs `nixos-rebuild test` on this clone; a power
  cycle undoes it; you have no sudo and cannot change the boot default — the
  steward's call).
- Verify with the repo's own tests before committing: STRUCTURAL nixosTest (units
  in the closure, no-op path, parses under sh) plus a live confirmation — follow
  `tests/boot-log.nix` / `tests/journal-export.nix`.
- An unverified fix is not a fix. Say in the PR what you verified live and what you
  did not. Open a PR against `main`; a human merges. A local branch never becomes
  a build, so it never reaches the next flashed image — the PR is the only way the
  work ships.

---

## Appendix A — the two-finger scroll patch (independent of Priority 1; apply it)

The `DeviceAdded` handler (`udev.rs:571-581`) enables tap-to-click and
tap-and-drag but never sets a scroll method. libinput does NOT always default a
clickpad to two-finger, so a two-finger drag can emit zero axis events. The axis
PATH is fine (`process_input_event` → `on_pointer_axis`, `comp_core.rs:610`), so
this is a device-config gap. In the same `if device.config_tap_finger_count() >
0` block, after the tap knobs:
```rust
if device.config_scroll_methods().contains(&ScrollMethod::TwoFinger) {
    if let Err(err) = device.config_scroll_set_method(ScrollMethod::TwoFinger) {
        debug!(?err, "libinput: two-finger scroll enable refused (best-effort)");
    }
}
```
`ScrollMethod` is the `input`-crate enum the `config_tap_*` methods already ride;
import from smithay's reexport. **Check FIRST** whether the failing scroll is over
a scrollable WebView/app or over the empty background layer surface — verify with
`libinput debug-events` before assuming the config is the whole fix.

## Appendix B — the VT `change_vt` patch (CONDITIONAL — likely moot after Priority 1)

Only apply this if, after hart-comp is the real DRM master/seat leader, Ctrl+Alt+F2
STILL does not switch. The compositor currently has no `change_vt` call anywhere;
if the seat is genuinely the compositor's (seatd, not logind), it must initiate
the switch itself. Six edits:
1. `comp_core.rs` `enum WmAction` (~140): add `SwitchVt(i32),`.
2. `comp_core.rs` `process_keyboard_shortcut` top (after `let workspace_digit`,
   before the `mods.alt` branch): match the modified sym against the contiguous
   VT keysyms (anvil does the same):
   ```rust
   let raw = keysym.raw();
   if (xkb::KEY_XF86Switch_VT_1..=xkb::KEY_XF86Switch_VT_12).contains(&raw) {
       return Some(WmAction::SwitchVt((raw - xkb::KEY_XF86Switch_VT_1 + 1) as i32));
   }
   ```
   (`xkb` already imported as `keysyms as xkb` at `comp_core.rs:81`.)
3. `comp_core.rs` `apply_wm_action` (~687): `WmAction::SwitchVt(n) => state.change_vt(n),`.
4. `comp_core.rs` `trait CompState` (~218): default no-op `fn change_vt(&mut self, _vt: i32) {}`.
5. `wayland.rs` (DRM `State`, behind `cfg(feature="smithay")`): add
   `pub session: Option<LibSeatSession>,` to the struct (~145) + `session: None,`
   in the constructor literal + override in `impl CompState for State` (~310):
   ```rust
   fn change_vt(&mut self, vt: i32) {
       if let Some(s) = self.session.as_mut() {
           if let Err(e) = s.change_vt(vt) { warn!(?e, "change_vt failed"); }
       }
   }
   ```
   import `use smithay::backend::session::{libseat::LibSeatSession, Session};`.
6. `udev.rs` after `run_udev` builds the session (~360) + `State`:
   `state.session = Some(session.clone());`.
`winit.rs::State` inherits the no-op (a windowed dev build has no VT). No compiler
on the Windows box, so `cargo build --features smithay` then runtime-test here.

## Cross-agent context (the wider hive work in flight)

An MSI-side HART OS agent and a HevolveAI backend agent work the same repo via a
shared scratchpad. Settled facts: peer-discovery signing chain fixed (`477d61b7`,
`47e05ec8`, `62b7b722`); two-node collaboration proven
(`tests/standalone/two_node_collaboration.py`). RESOURCE decisions (llama
ownership, VRAM, the governor, the scheduler, audit-dominance fraud scoring) are
under a standing human ruling: "don't act on resource unless the agents all
agree." Do not unilaterally change any of those.
