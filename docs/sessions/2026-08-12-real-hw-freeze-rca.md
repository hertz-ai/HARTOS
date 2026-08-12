# Real-hardware RCA: the "instant freeze with the mouse still moving"

> **CORRECTION (same session, after the fact).** The chain below is **partly wrong** and is
> kept only because the measurements in it are real. Two later findings contradict it:
>
> 1. **The unbounded capture is NOT slow.** Timed on the affected box:
>    `journalctl -b --no-pager | head -c 5000000` completes in **1 second** (the `head`
>    closes the pipe and journalctl exits early). So "it cannot finish inside
>    TimeoutStartSec=90s" is not the mechanism. A `journalctl -b` at 99% CPU for 33s+ WAS
>    observed under this unit, and killing it DID drop 94C -> 76C, but the reason that
>    particular invocation was slow is unexplained -- most likely it was already being
>    thermally throttled, or blocked writing to the slow USB target, which inverts the
>    cause and effect asserted below.
> 2. **The machine returned to 94C with that timer still stopped**, so `hart-journal-export`
>    is not the sole heat source. `WebKitWebProcess` was separately observed at **98.3% CPU**
>    and is the better suspect.
>
> `b67480f` (tail-bound + `Nice=19`/idle scheduling) is still worth keeping as hygiene -- a
> background diagnostic must never out-prioritise the desktop -- but it should NOT be cited
> as the root cause. **The freeze is not solved.**
>
> What DOES stand on its own evidence: the seatd conflict (`211a6a1`) and the DRM-master
> denial (`15411db`, confirmed by `acquired DRM master via drmSetMaster` plus real page-flip
> vblanks). With DRM master held, every page-flip lands instead of returning EACCES into a
> retry loop, and orb-hover stopped freezing the live machine.

**Date:** 2026-08-12
**Hardware:** Samsung NP550P5C-S05IN (550P5C/550P7C), i7-3630QM, Intel HD 4000, booting
HART OS 1.0.0 from a USB2 stick.
**Branch:** `build/seatfix-raw`
**Symptom as reported:** the desktop renders and is usable, then freezes — instantly, not
gradually. The mouse cursor keeps moving. The CPU/RAM widget shows nothing unusual (~12%).
Hovering the orb, or clicking Wi-Fi, triggers it reliably. Windows on the same laptop runs
continuously without issue.

---

## The chain, end to end

```
hart-journal-export.timer (every 15s)
  -> journalctl -b --no-pager       (formats the ENTIRE boot, cannot finish in 90s)
  -> killed at TimeoutStartSec=90s, timer refires 15s later, FOREVER
  -> one core pinned at 99% permanently, unit stuck ActiveState=activating
  -> +18C of pure waste heat        (MEASURED: 94C with it, 76C the instant it stopped)
  -> die idles ~10C below Tjmax     (94C vs ~105C) = no thermal headroom left
  -> any interaction (orb hover / Wi-Fi click) spikes the die past the limit in
     MILLISECONDS (die thermal mass is tiny; the HEATSINK is what is slow)
  -> intel_powerclamp injects idle across the WHOLE PACKAGE, not the offending core
  -> every core stalls, so the compositor stalls
  -> instant system-wide freeze, cursor still moving, no userspace metric abnormal
```

**Why it was invisible:** one saturated core averaged across 8 threads reads ~12% on the
desktop widget. The widget was correct and useless. `NRestarts=0`, so nothing ever appeared
"failed" either.

**Why one pegged core froze an 8-thread machine** (it should not, and does not directly):
`intel_powerclamp` force-idles the entire package. On a laptop with cooling headroom the same
pegged core would have been harmless.

### Evidence

| Measurement | Value |
|---|---|
| `thermal_zone0/1` (acpitz) | 94C |
| `coretemp Package id 0` | 94-95C (both sensors agree — not a bad sensor) |
| `cpu0 scaling_cur_freq` | 1200000 (idle, lowest P-state) |
| C7 residency | dominant (deep sleep working correctly) |
| governor / driver | `schedutil` / `intel_cpufreq` (correct) |
| Fan cooling_device | `cur=1/max=1` (already maxed) |
| `core_throttle_count` | 1.2M / 843K / 1.5M |
| `intel_powerclamp` idle injections | 124 in one boot |
| After stopping the hog | **76C, zero new throttle events over 60s, load 0.49** |

---

## Fixes committed

| Commit | Fix |
|---|---|
| `211a6a1` | `services.seatd.enable = lib.mkForce false` — seatd and logind both managed seat0, so `drmSetMaster` returned EACCES and the session collapsed hart-comp -> sway -> cage. Tests updated (the `seat` group no longer exists). |
| `15411db` | `security.wrappers.hart-comp` with `cap_sys_admin+ep` — logind holds DRM master and never yields it, so the compositor's existing retry looped EACCES forever and sat on the pixman software floor. Live-proven: `acquired DRM master via drmSetMaster` + a real page-flip vblank. |
| `7f2a012` | Three things: (a) `hart-thermal-health` module — the OS knew it was throttling and said nothing; now it reports max-across-zones + the kernel's own throttle delta and states plainly that a freeze is thermal; (b) `vram_manager.detect_gpu` — `refresh_gpu_info` nulled the cache every TTL so three services re-forked `nvidia-smi`/`rocm-smi` forever (**160 swallowed FileNotFoundErrors in 10 minutes**), now latched via `shutil.which` and logged once; (c) **`DT_RUNPATH` stamp on the capability binary** — see the regression note below. |
| `b67480f` | **The root cause.** `journalctl -b -n 20000` (tail-bounded, seeks from the end) instead of formatting the whole boot, plus `Nice=19` + `CPUSchedulingPolicy=idle` + `IOSchedulingClass=idle` so a background diagnostic can never again out-prioritise the desktop it exists to diagnose. |

### A regression this session introduced and caught

Adding the file capability sets `AT_SECURE=1`, and glibc then **ignores `LD_LIBRARY_PATH`** —
which is exactly how the launcher resolves the compositor's runtime
`dlopen("libEGL.so.1")`. Live-confirmed: `Failed to load LibEGL: libEGL.so.1: cannot open
shared object file`. The compositor would take DRM master but **lose GPU rendering**, i.e.
maximum CPU heat on the machine least able to afford it. Fixed by stamping the same two
directories into the binary's `DT_RUNPATH` (honoured under `AT_SECURE`), with the build
**failing** if the stamp is missing rather than shipping a silently GPU-less compositor.

---

## Live-only state (DIES ON REBOOT)

The machine was left working via two temporary changes that the commits above make permanent:

1. `systemctl stop hart-journal-export.timer` + `pkill journalctl` — the CPU hog.
2. A `mount --bind` of a `setcap cap_sys_admin+ep` copy of `hart-comp` over its store path.

Also reset during recovery: the tier latch (`/var/lib/hart/session-tier` -> `hart-comp`) after
a failed experiment dropped the session to cage, where cage itself failed with
`Failed to load session backend` — which is what killed input on the login screen.

**A reboot without flashing a fixed image brings the freeze back.**

---

## Still open

* **The iGPU is still not rendering.** DRM master is fixed and proven; the EGL half
  (`DT_RUNPATH`) is committed but has never run on hardware — it needs a fresh image
  (`patchelf` is a build-time dependency). Until then the compositor is on the pixman
  software floor and the shell on its Cairo rung, which is why the cards render flat.
  This is the single biggest remaining heat source.
* **`WebKitWebProcess` at 98.3% CPU** observed in the degraded cage tier — a second hog,
  independent of the journal-export one, not yet investigated.
* **`/chat` returns 500**: `ModuleNotFoundError: No module named 'autogen'` /
  `Agent creation requires the 'pyautogen' package`. This is the "retry connecting" on the
  agent cards.
* **`database is locked`** (sqlite contention) in `hart-liquid-ui`.
* **Login-screen clock flickers** between `5:24` and `05:24` (inconsistent zero-padding).
* **The hover -> throttle link is inferred, not measured.** The hog, the +18C, the throttle
  counts and the fix are all proven; a temperature+throttle sample captured *at the instant of
  a hover-freeze* would close the last gap.
* `clamav-daemon.service` failed; `nixosTests` shards 0/2 fail on `main` too (pre-existing).

## Method notes (for whoever picks this up)

* **Do not use `journalctl` or `dmesg` in probes on this box.** They peg a core at 99% on a
  large journal — twice during this session that heated the machine being measured and
  corrupted the readings, and it was initially misattributed to a system fault.
* Read `/sys` directly and sleep. A health probe that changes what it measures is a bug.
* SSH survives the freeze; the compositor does not. Diagnose from SSH, never from inside the
  session under test.
