# HART OS ↔ Windows / macOS parity matrix

**What this is:** the capability list a person expects from a finished desktop
OS, each row saying whether HART has it, *which existing NixOS/systemd option
provides it* (never a HART reimplementation), and whether an agent can act on
it. Guarded by `tests/unit/test_nixos_configs.py::TestParityMatrix` so a row
cannot claim something the tree does not have.

**Why it exists:** "does HART have parity" was a judgement call answered from
memory. Every row below was machine-checked against the tree, and the check
runs in CI, so the answer is computed.

**How to read a row.** *Nix* = the declarative half (the option that makes it
true on a booted system). *Agent* = the live half (`/api/shell/...` so an agent
can change it without a rebuild) — the two halves of the rule in task #25.
`n/a` means the capability has no meaningful runtime action (you cannot toggle
CPU microcode at runtime).

| Capability | Nix (declarative) | Agent (live) | Notes |
|---|---|---|---|
| Users & groups | `users.users.*` | ✅ | |
| Wi-Fi / networking | `networking.networkmanager` | ✅ | privacy-first: never auto-joins unknown SSIDs |
| Display / resolution | `services.xserver`, `displayManager` | ✅ | |
| Audio devices | `services.pipewire` | ✅ | + `hart.audio.bootUnmute` (never boot silent) |
| Printing | `services.printing` + avahi | ✅ | |
| Scanning | `hardware.sane` (`hart.scanner`) | ✅ | eSCL/AirScan wireless |
| Bluetooth | `hardware.bluetooth` | ✅ | |
| Power / battery | `services.upower`, `tlp`, `thermald` | ✅ | |
| Disks / mount / format | `services.udisks2` (`hart.storage`) | ✅ | |
| Defrag / TRIM / chkdsk | `hart.storage` tooling | ✅ | `/storage/{defrag,trim,fsck}`; defrag correctly returns *nothing* for f2fs/ntfs/vfat/exfat — Windows offers it anyway |
| Device Manager (tree) | kernel + udev | ✅ | `/api/shell/drivers` = `lspci -mm -k` + `lsusb`, reporting **driver binding + `unclaimed`** (the yellow-bang: driver available but not attached ⇒ firmware missing) and an honest `truncated` flag instead of the old silent 50-cap |
| Task Manager (processes) | psutil + systemd | ✅ | `/api/shell/tasks/{processes,kill,priority,resources}` — per-process CPU/mem/threads, kill behind a protected-name guard, renice, live resource totals |
| Process isolation / containment | systemd cgroups (`CPUQuota`, `MemoryMax`/`MemoryHigh`, `TasksMax`) + `systemd.oomd` via `hart.memory.oomProtect` | n/a | Android's model, not just a lower priority: background agents are HARD-bounded on cpu, memory and task count, so a wedged agent degrades itself rather than the node. `Nice`/`CPUWeight` alone were the trap — they only bite under contention |
| App install / store | flatpak, appimage, `hart.apps` | ✅ | offline catalog |
| Updates + rollback | `hart.ota` → `nixos-rebuild` generations | ✅ | atomic; rollback is a generation switch |
| Accessibility | `at-spi2`, `hart.accessibility` | ✅ | |
| Input methods (IME) | `i18n.inputMethod` (`hart.ime`) | ✅ | CJK + Indic |
| Night light | `hart.nightlight` | ✅ | |
| Time / locale / RTC | `time.hardwareClockInLocalTime` (installer-written) | ✅ | **the dual-boot clock fix, task #24** |
| Device firmware | `hardware.enableRedistributableFirmware` | n/a | all variants (was desktop-only) |
| CPU microcode | `hardware.cpu.{intel,amd}.updateMicrocode` | n/a | |
| Hypervisor guest | `hypervGuest`, `qemuGuest`, `spice-vdagentd`, `vmware.guest` | n/a | Hyper-V / KVM / SPICE / VMware |
| BIOS + UEFI boot | `isoImage.makeBiosBootable`; systemd-boot / GRUB by probe | n/a | Hyper-V **Gen 1** boots |
| Disk encryption | `hart.luks` | ❌ | **gap: no live agent route** |
| Screen capture / portal | `xdg.portal` (`hart.portal`) | ✅ | `/api/shell/screenshot` + `/api/shell/recording/{start,stop}` (`shell_os_apis.py`) — this row previously read ❌ from a name-only search that missed all three |
| Remote desktop | RustDesk / Sunshine (`integrations/remote_desktop`) | ❌ | routes not on the shell API |
| Antivirus | ClamAV (`hart.security`) | ❌ | `hart-security` CLI only |
| Firewall | `networking.firewall` (`hart.firewall`) | 🟡 | `/api/shell/firewall` reads live backend + open ports; changing ports stays declarative on purpose |

## Where HART is ahead of both

Local-first LLM/vision/TTS as an OS service (`hart.modelBus`), an agentic shell
composed by the local model (`hart.liquidUI`), peer compute (`hart.computeMesh`),
Android + Windows app subsystems natively (`hart.subsystems`), and a never-blank
tier-drop compositor ladder (`hart.sessionSupervisor`) with a cage floor.

## Honest gaps

1. **Three capabilities are declarative-only** (disk encryption, remote
   desktop, antivirus): the OS does the thing, but no agent can see or
   change it live. That is the second half of task #25 and the concrete
   remaining parity work. Each was re-checked against real `@app.route`
   registrations on 2026-07-31, not a name search — screen capture had been
   listed here wrongly and is in fact fully routed. Firewall is 🟡 — readable but deliberately not
   writable, since opening a port from an unauthenticated local HTTP API is a
   security decision rather than a convenience; the declarative side stays the
   source of truth.
2. **`hart.devtools` does not fit the desktop image** — measured +3 GiB against
   ~2 GiB of slack (audits 30570492265 / 30573861911).
3. **Runtime verification** of the current tree is still owed: CI runner
   starvation (fixed in `c42f446a`) meant no nixosTest suite completed on main
   between 07-28 and 07-31.
