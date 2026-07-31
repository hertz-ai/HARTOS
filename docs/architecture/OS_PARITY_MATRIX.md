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
| Screen capture / portal | `xdg.portal` (`hart.portal`) | ❌ | gate exists; no `/api/shell` action |
| Remote desktop | RustDesk / Sunshine (`integrations/remote_desktop`) | ❌ | routes not on the shell API |
| Antivirus | ClamAV (`hart.security`) | ❌ | `hart-security` CLI only |
| Firewall | `networking.firewall` (`hart.firewall`) | ❌ | no live port-management route |

## Where HART is ahead of both

Local-first LLM/vision/TTS as an OS service (`hart.modelBus`), an agentic shell
composed by the local model (`hart.liquidUI`), peer compute (`hart.computeMesh`),
Android + Windows app subsystems natively (`hart.subsystems`), and a never-blank
tier-drop compositor ladder (`hart.sessionSupervisor`) with a cage floor.

## Honest gaps

1. **Five capabilities are declarative-only** (last five rows): the OS does the
   thing, but no agent can change it live. That is the second half of task #25
   and the concrete remaining parity work.
2. **`hart.devtools` does not fit the desktop image** — measured +3 GiB against
   ~2 GiB of slack (audits 30570492265 / 30573861911).
3. **Runtime verification** of the current tree is still owed: CI runner
   starvation (fixed in `c42f446a`) meant no nixosTest suite completed on main
   between 07-28 and 07-31.
