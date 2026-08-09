# Co-pilot handoff — "no hangs whatsoever" + live boot-hang diagnosis

**Written 2026-08-09 by the Nunba/Windows-side agent, for the resident HART OS
co-pilot to continue.** This is a conversation handoff. The steward and I
diagnosed a set of HART OS problems from Windows by reading the exported
journal off the stick. You are now inside the running node and can do what I
could not: observe live, activate changes with `systemctl start --wait
hart-copilot-verify.service`, and iterate on real hardware. Pick this up as
your own work.

## The mandate, in the steward's words

> "no hangs whatsoever" — capture all hangs/ANRs, contain the blast radius,
> recover from ANRs autonomously, and a wizard to connect to WiFi on bootup.

Absolute bar: the *user* must never experience a hang. Prevent where possible;
where a hang is still possible, detect + contain + recover so it never reaches
the user. This is the Android model (liveness heartbeat, ANR timeout, kill the
wedged unit not the world), applied top to bottom.

## What is already landed on main (verify these are in your checkout)

1. `6866ca9e` — the `/chat` 500. `gather_agentdetails.py` dereferenced a
   `None` autogen in a function ANNOTATION, evaluated at import time. Fixed with
   `from __future__ import annotations` + `autogen = lazy_module("autogen")`.
   Blast radius was ONE request (a 500), NOT an OS hang — do not conflate them.
2. `83f388c3` — `hart-journal-export.nix` built `/dev//dev/sdb` for enumerated
   vfat rows (PKNAME from `lsblk -lnpo` is already a full path), so the
   removable gate rejected every genuine USB stick and only the `force=1`
   dedicated-label path worked. Now strips a leading `/dev/`. VERIFY on this
   node: plug a spare FAT32 stick, confirm `hart-journal-<host>.txt` lands on
   it, not only on the internal HARTJRNL.

## The actual hang the steward reported, and why it is NOT in any log

Symptom: minimise the orb, hover over it → hang. It appears NOWHERE in the
exported journal (searched orb|hover|minimi|pointer|repaint|frame callback|
damage|vblank across 3144 lines: 2 hits, both unrelated). The journal ends on
a clean periodic export, no wedge/OOM/hung_task recorded.

What the journal DOES establish, and it fits the symptom exactly:

- **Split rendering.** Compositor got real GPU (`HART-comp DRM: GLES GPU
  renderer initialised`, Intel HD 620, Mesa 24.2.8, OpenGL ES 3.2, card1). But
  the GTK4 glass shell reports `render rung = webkit-cairo` / `GSK = CAIRO`. So
  the orb and its hover effects repaint in SOFTWARE, on the CPU, inside the
  shell process.
- **WebKit RSS climbs monotonically**: 179 → 369 → 377 → 385 → 398 → 410 MB in
  ~110 s, fds 33 → 42; compositor RSS flat at ~85 MB. The growth is WebKit.
- RULED OUT: the `nvidia-smi`/`rocm-smi` FileNotFoundError tracebacks (20 total,
  paired, every ~30 s from hart-liquid-ui + hart-backend) are noise.

This is precisely the failure `hart-journal-export.nix`'s own header names:
"the software-rendered glass shell pegging the CPU so typing lags ~500ms and
the compositor / in-shell terminal eventually wedge."

## Why the existing watchdog did not catch it — the precise gap

`hart-session-supervisor.nix` is a strong Android-style tier watchdog, but it
watches the TIER, not the shell CONTENT:

- **paint watchdog** — `shell-ready` marker; drops the tier if the compositor
  is up but never presents a first frame. The orb tier DID paint, so it passes.
- **input-alive watchdog** — `input-alive` marker; drops the tier if painted
  but the seat delivers no events. Input was alive, so it passes.

The orb hang is a tier that painted AND has live input AND then wedges on a
specific in-shell interaction. It passes BOTH markers. Nothing below the tier
level watches the shell's own main loop for responsiveness. That is the gap.

The backend has `NodeWatchdog` (heartbeat between blocking ops, 300 s frozen
threshold, `integrations/agent_engine/agent_daemon.py:356`), but it watches the
agent daemon, not the UI. So neither the tier watchdog nor NodeWatchdog sees an
in-shell ANR.

## Your work list (specs, in priority order)

### 1. In-shell ANR watchdog — the heart of "no hangs whatsoever"

Give the glass shell a liveness heartbeat the way Android's Watchdog pings the
main looper. Concretely:

- The shell's main/render loop touches a tmpfs marker (e.g.
  `/run/hart/session/shell-alive`) on a fixed cadence, the SAME dir + 0770
  group-writable pattern as `shell-ready`/`input-alive` (see supervisor lines
  ~118-124). Add `HART_SHELL_ALIVE_FLAG` next to the existing exported paths.
- A watchdog (extend the supervisor's selector loop, do NOT add a parallel
  mechanism — the module is emphatic about one path) treats a stale marker
  while the tier is painted+input-alive as an ANR.
- **Contain the blast radius**: an ANR must NOT drop the whole tier if it can be
  isolated. First response is to recover the wedged surface (reload the WebView
  content / restart just the shell host), not to tear down the compositor. Only
  escalate to a tier drop if the surface-level recovery fails N times — reuse
  the existing `record_crash → lower_tier → write_tier` path for that escalation
  so there is still one ladder.
- **Autonomous recovery**: on ANR, capture first (see #2), then recover, then
  log the recovery with a streak count so a flapping surface is visible.
- FAIL-SAFE like the input marker: absence of the alive marker is ambiguous on a
  shell build that does not yet write it, so gate the authoritative behaviour
  behind a timeout > 0 the way `inputAliveTimeoutSeconds` does. Never flap a
  healthy tier because an older shell lacks the writer.
- Address the ROOT too, not only detection: the orb repaints in software
  (`GSK = CAIRO`) while the GPU is available. Investigate why the glass shell is
  on the cairo rung when EGL/GLES initialised on card1, and whether the WebKit
  RSS growth is a leak (fds also climb). Detection contains it; fixing the
  software-render + leak is how you get to "no hangs" rather than "fast
  recovery from hangs".

### 2. Unify hang capture so an ANR is always recorded

Today HARTLOG (`hart-boot-log.nix`) writes the RICH bundle (shell-ready marker
+ mtime, GSK/GDK/EGL/GBM/WebKit GL errors, tier latch, crash window,
`systemctl --failed`) but is a permanent NO-OP on an installed node, because
`hart-hartlog-create.nix` refuses internal disks. HARTJRNL
(`hart-journal-export.nix`) DOES run here but writes only header + 200 warnings
+ raw journal — it lacks exactly the paint/renderer surface the orb hang needs.

Fold the two CAPTURE payloads into one: write the rich bundle to whichever
labelled FAT32 partition exists (HARTLOG removable-carved OR HARTJRNL
operator-carved). Keep the two CREATION strategies (live stick vs installed) —
they genuinely differ — but there is no reason for two capture payloads and a
`force=1` bypass. Trigger the capture on ANR (#1), not only on a timer, so the
pre-wedge state is on the stick.

### 3. WiFi-on-boot wizard — load-bearing for THIS whole exercise

`hart-net-diag.nix` does `rfkill unblock` and USB-ethernet DHCP auto-connect,
but its own comment says "Connecting to a Wi-Fi SSID still needs the user to
enter it" — there is no onboarding wizard. This is on the critical path: YOU
(the co-pilot) need network to reach the Claude API and continue this
conversation, and the steward should not have to hand-edit wpa_supplicant.
Requirements:
- Fires on boot when no network is up and no wired fallback exists.
- Scans, lists SSIDs, takes a password, connects via NetworkManager.
- Must render on a tier that CANNOT hang — i.e. it must work even if the glass
  shell is on its cairo floor or wedged. Consider hosting it on the cage paint
  floor or a minimal layer-shell surface, not inside the WebKit shell whose
  responsiveness is the very thing in doubt. A wizard that hangs is worse than
  none.
- NEVER store the PSK in the world-readable store; NetworkManager's own
  keyfile with correct perms.

## How to work (from the copilot daemon's own contract)

- You are on a `copilot/*` branch. NEVER commit to main, never push to main.
- Activate an OS change on THIS machine to test it:
  `systemctl start --wait hart-copilot-verify.service` (runs `nixos-rebuild
  test` on this clone; a power cycle undoes it; you have no sudo and cannot
  change the boot default — that is the steward's decision).
- Verify with the repo's own tests before committing. The repo convention for
  HW-gated modules is a STRUCTURAL nixosTest (units in the closure, no-op path,
  parses under sh) plus a live confirmation you run here — follow
  `tests/boot-log.nix` and `tests/journal-export.nix` as the pattern.
- An unverified fix is not a fix. Say in the PR what you verified live and what
  you did not.
- Open a PR against `main`; a human merges. A local branch never becomes a
  build, so it never reaches the next flashed image — the PR is the only way
  the work gets somewhere.

## Cross-agent context (the wider hive work in flight)

There is an MSI-side HART OS agent and a HevolveAI backend agent working the
same repo, coordinated via a shared scratchpad. Recent settled facts you may
rely on: the peer-discovery signing chain is fixed (`477d61b7`, `47e05ec8`,
`62b7b722`); two-node collaboration is proven
(`tests/standalone/two_node_collaboration.py`). RESOURCE decisions (llama
ownership, VRAM, the governor, the scheduler, audit-dominance fraud scoring)
are under a standing human ruling: "don't act on resource unless the agents all
agree." Do not unilaterally change any of those.
