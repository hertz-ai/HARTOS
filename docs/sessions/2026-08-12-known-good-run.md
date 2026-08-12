# HART OS: the known-good, hang-free run — reproduce + verify

Derived from a real-hardware session on a **Samsung NP550P5C** (i7-3630QM, Intel HD 4000)
booting HART OS 1.0.0 from a USB2 stick. Every step below was executed and **measured**; the
verification commands are the ones that actually distinguished a working run from a broken
one. Companion RCA (including the wrong turns): `2026-08-12-real-hw-freeze-rca.md`.

The permanent versions of all of this live on `build/seatfix-raw`. This runbook is what to do
when you have a **running box** and want the good state now, or when you need to prove a
freshly-flashed image is actually correct.

---

## 0. Diagnose from OUTSIDE the session, never inside it

SSH survives the freeze; the compositor does not. Get networked first, then debug over SSH.

```bash
# in a text console (boot with systemd.unit=multi-user.target if the GUI wedges)
nmcli device wifi connect "<SSID>" password "<PSK>"    # persists across reboots
ip -4 addr ; systemctl is-active sshd
```

**Do NOT put `journalctl` or `dmesg` in a diagnostic loop on this class of box.** They peg a
core at ~99% on a large journal. Twice in this session that heated the machine being measured
and was misread as a system fault. Read `/sys` and sleep; use `journalctl -n N` (bounded) or
`--since`, never an unbounded `-b`.

---

## 1. The compositor must OWN the display (this is the hang)

**Symptom:** desktop renders, then freezes instantly on interaction (hovering the orb, opening
Wi-Fi). The mouse keeps moving. No process is spinning. The CPU widget looks normal (~12%,
because one saturated core averaged over 8 threads is invisible).

**Cause:** `systemd-logind` holds DRM master and never yields it, so hart-comp's `drmSetMaster`
returns `EACCES` forever. Every page-flip then fails and retries — a repaint can never land.

**Two things are required, and fixing only the first breaks the second.**

```bash
PE=$(ls /nix/store/*patchelf*/bin/patchelf | head -1)
GLVND=$(ls -d /nix/store/*libglvnd-*/lib | head -1)     # must be the NON -dev output
STORE=$(ls /nix/store/*hart-comp-0.1.0/bin/hart-comp | head -1)

systemctl stop greetd; sleep 2
cp -f "$STORE" /tmp/hc-egl && chmod +w /tmp/hc-egl

# (a) DT_RUNPATH — MUST come first. setcap sets AT_SECURE=1, and glibc then IGNORES
#     LD_LIBRARY_PATH, which is exactly how the launcher resolves the runtime
#     dlopen("libEGL.so.1"). Without this the compositor takes the display but loses
#     the GPU: "Failed to load LibEGL" -> pixman software floor -> maximum CPU heat.
"$PE" --add-rpath "$GLVND:/run/opengl-driver/lib" /tmp/hc-egl

# (b) the capability that lets it take master from logind
setcap cap_sys_admin+ep /tmp/hc-egl
mount --bind /tmp/hc-egl "$STORE"          # live only; the image does this properly
```

### Verify (all four must hold)

```bash
P=$(pgrep -x hart-comp | head -1)
grep -cE 'libEGL|libGLESv2' /proc/$P/maps          # expect >0  (measured: 11). 0 = EGL FAILED
journalctl -u greetd -n 120 --no-pager | grep -aiE 'acquired DRM master|STILL refused'
ps -eo pcpu,comm --sort=-pcpu --no-headers | head -3
cat /run/hart/session/current-tier                 # expect: hart-comp
```

| | broken | good |
|---|---|---|
| EGL libs mapped | `0` + `Failed to load LibEGL` | **11** |
| master | `drmSetMaster STILL refused` | **`acquired DRM master`** |
| WebKitWebProcess | **146%** (software rendering) | **~1%** |
| tier | `sway` / `cage` | **`hart-comp`** |

`146% -> ~1%` is the single clearest signal that the iGPU is doing the work.

---

## 2. Force the top tier (a latched degrade is sticky)

The supervisor latches a degraded tier, so it will keep launching sway/cage even once
Tier-1 is fixed.

```bash
rm -f /run/hart/session/tier-degraded
echo hart-comp > /var/lib/hart/session-tier
chown hart-admin:hart /var/lib/hart/session-tier
systemctl start greetd
```

Also ensure the fallbacks can start at all — with `seatd` disabled, wlroots probes the seatd
backend FIRST and both sway and cage die in <40ms (`Failed to load session backend`), which is
a BLACK SCREEN instead of a graceful degrade:

```bash
# permanent in 017500c (environment.variables.LIBSEAT_BACKEND); live equivalent:
mkdir -p /run/systemd/system/greetd.service.d
printf '[Service]\nEnvironment=LIBSEAT_BACKEND=logind\n' \
  > /run/systemd/system/greetd.service.d/99-libseat.conf
systemctl daemon-reload
```

---

## 3. Audio: check the layer BELOW PipeWire

**Symptom:** no sound, while `wpctl` reports a healthy unmuted sink.

PipeWire does not un-mute ALSA elements it did not mute, so a codec-default or
alsactl-restored element mute survives every `wpctl set-mute 0`.

```bash
amixer -c0 get Headphone            # was [off] on this laptop -> total silence
for c in Master Speaker Headphone PCM Front; do amixer -c0 set "$c" unmute 80%; done
amixer -c0 set "Auto-Mute Mode" Disabled     # false jack-detect mutes speakers permanently
```

### Verify with real output, not with a status field

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u hart-admin)
speaker-test -t sine -f 440 -l 1 -c 2                     # a tone
espeak-ng -s 140 -a 200 "HART OS audio is working"        # intelligible speech
```

Both were confirmed audible on the built-in speakers. `wpctl get-volume` showed a perfect sink
throughout the silent period — **do not trust it as proof of audio.**

---

## 4. Rule out the measurement before believing a fault

Three findings this session were artefacts or misreadings. Check these before theorising:

* **A pegged core is invisible in an averaged CPU widget.** One core of 8 reads ~12%. Check
  per-process (`ps --sort=-pcpu`), not the dashboard.
* **A 100%-CPU process is worth ~18C on this chassis** (measured 94C -> 76C when killed). On a
  box already near its limit, `intel_powerclamp` then force-idles the WHOLE package — not the
  offending core — so one runaway process can stall every core. Confirm with
  `/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count` (a rising delta) rather
  than temperature alone.
* **Temperature alone does not prove the freeze.** This box ran the sway tier **fully
  responsive at 94C**. That single observation is what disproved the thermal root-cause theory.
  If one tier is fine and another freezes at the SAME temperature, the fault is in the tier.

---

## 5. Everything above is live-only

`mount --bind`, a stopped timer, `amixer`, and the tier latch **all die on reboot.** The
permanent fixes are on `build/seatfix-raw`:

| Commit | Fix |
|---|---|
| `211a6a1` | disable seatd (it fought logind for seat0) |
| `15411db` | `security.wrappers.hart-comp` — `cap_sys_admin+ep` for DRM master |
| `7f2a012` | `DT_RUNPATH` stamp (EGL under AT_SECURE) + GPU-probe storm + thermal reporting |
| `b67480f` | journal-export tail-bounded + `Nice=19`/idle (hygiene, NOT the root cause) |
| `017500c` | global `LIBSEAT_BACKEND=logind` — fallbacks stop going black |
| `a391425` | clock leading-zero flicker |
| `1482b3e` | ALSA-element unmute + `alsa-utils` in the unit PATH |

### The one check that validates a freshly-flashed image

```bash
P=$(pgrep -x hart-comp | head -1)
grep -cE 'libEGL|libGLESv2' /proc/$P/maps          # >0
journalctl -u greetd -n 80 --no-pager | grep -ai 'acquired DRM master'
```

Both present = the display and the GPU are correct, and the freeze is gone. Either missing =
stop and fix that before looking anywhere else.

---

## Still open (do not assume these are fixed)

* hart-comp still **degrades to sway on its own** after running a while — root cause unknown.
* `/chat` returns 500: `No module named 'autogen'` (deliberately unpackaged,
  `hart-app.nix:108`). This is the "retry connecting" on the agent cards.
* Numpad — never committed.
* `database is locked` (sqlite contention) in `hart-liquid-ui`; `clamav-daemon` failed.
* The `DT_RUNPATH` fix is proven by live bind-mount but has **never run from a flashed image**.
