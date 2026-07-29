# HART Installer — the Union Plan

**One sentence:** every way HART lands on hardware composes the *union* — NixOS's
hardware layer (`nixos-generate-config`, dual-boot, partitioning) **plus** HART's
OS layer (`hartModules` + the variant profile) — and never ships stock NixOS.

Origin: steward, 2026-07-28, four refinements in sequence:

> "I'll shrink and create a partition? ... without impacting the MBR etc"
> "are you saying all the nice features of nixos like dual boot etc we gotta reinvent?"
> "nothing shd be shipped nix only, there is no point of that without our os customisations" / "union of features"
> "an os for anything which has compute ... embedded devices controllers running robots"
> "reuse and extend when need be if we can rebrand properly if it's open rather than reiventing"

Tracked as session task #17. This document is the durable home.

## Design principles

1. **UNION, never either/or.** NixOS supplies hardware detection, dual-boot,
   partitioning; HART supplies the OS. An install path whose output is stock
   NixOS is worth nothing here, however little code it costs.
2. **Reuse + extend + rebrand open components** (repo-wide default). Calamares is
   GPL and is the rebrand-me installer behind Manjaro/EndeavourOS/NixOS itself;
   its `calamares-nixos-extensions` config module is the intended seam — extend
   what it *writes*, rebrand what the user *sees*. A bespoke installer UI would
   be the reinvention. Bespoke is justified only where HART's architecture
   demands semantics no open component can express (hart-comp, recipe pipeline,
   guardrails).
3. **The unit of composition is the variant profile + the `mkHartSystem` recipe —
   never the bare module list.** `nixosModules.hart` alone yields a dormant HART:
   `hart.package` has no default, `hart.enable` defaults false, the features live
   in the profile, and the modules need `mkSpecialArgs`. (This is the same defect
   class that kept 25 nixosTests red — task #15's mkNode.)
4. **OTA stays the only update path.** The `/etc/nixos` flake copy an installer
   leaves behind is install-time bootstrap + offline rebuild capability, one
   writer, documented — never a second update channel.

## The plan, with live status (updated 2026-07-29)

| # | step | status |
|---|------|--------|
| 1 | Extract `nixos/profiles/{desktop,server,edge,phone}.nix` — verbatim moves, one canonical home consumed by images, tests, installer | ✅ `5975c519` — byte-identity proven against git HEAD; full build matrix green |
| 2 | Export `lib.mkHartSystem`; re-point `mkSystem`/`mkRepartSystem` through it so the flake is the recipe's first consumer and it cannot drift | ✅ `72f66070` — matrix green. Plus `hart-desktop-installed` (`0ad04b53`): the installer's exact composition, eval-gated on every push, doubling as its template |
| 3 | Re-base test nodes on the shipped config | ⚠️ **AMENDED in execution**: importing the full profile into every VM node would drag the ~20 GiB desktop closure into shards and conflict with tests that enable deliberate subsets. Actual approach: per-test enablement of exactly what each test asserts (2 tests shard-confirmed fixed; desktop-boot enablement landed, awaiting a clean shard). The deep #15 investigation this triggered found and killed the Resource Governor RLIMIT_AS bug (`cae47a12`) |
| 4 | `hartImageKind = "installed"` semantics + `hart-installer.nix` (ISO-gated) + thin scriptable `hart-install` CLI — orchestrator over `nixos-generate-config` + `nixos-install`, NOT a reinvented installer. Headless/fleet path for robots and embedded | ⬜ imageKind value + composition exist (step 2); the module + CLI do not |
| 5 | **Rebranded Calamares** as the graphical path: HART branding descriptor (product name, logo, slideshow, QSS) + a HART extension of `calamares-nixos-extensions` whose config module writes the `mkHartSystem` union config. GUI and CLI call the SAME generator — one writer for the installed configuration | ⬜ not started |
| 6 | nixosTest: install into a VM disk carrying a fake Windows ESP entry; assert the entry **survives** and both OSes boot. The dual-boot promise, tested | ⬜ not started |

## Load-bearing technical facts (verified, with sources)

- **The raw image can never do a partition install**: it is a whole-disk image
  (GPT at LBA 0 + ESP + root, `nixos/modules/hart-repart-image.nix`); `dd` writes
  from byte 0 of the *device*. Whole-disk flash and partition install are
  different products; the installer path is the answer for dual-boot.
- **Bootloader contract inverts between image kinds**: the portable raw image
  sets `canTouchEfiVariables = false` (removable-media path; must not write
  NVRAM); an installed dual-boot system sets **true** and registers its own NVRAM
  entry *beside* Windows Boot Manager. Overwriting `EFI/BOOT/BOOTX64.EFI`
  (Windows' fallback loader) is the one thing it must never do. Encoded in
  `hart-desktop-installed`.
- **No BIOS change needed for Intel RST/RAID SATA**: `drivers/ata/ahci.c` (v6.15)
  matches Intel controllers by PCI class (`PCI_CLASS_STORAGE_RAID << 8`,
  `PCI_ANY_ID`) plus explicit IDs incl. `0x282a`. The real RST blocker is NVMe
  remapping — a different case. Solve by class, never per machine.
- **BIOS (non-UEFI) targets** (old industrial controllers): bootloader chosen by
  firmware probe — systemd-boot on EFI, GRUB + `useOSProber` on BIOS.
- **What already exists — do not rebuild**: substrates x86_64 / aarch64 / riscv64
  across server/desktop/edge/phone/rpi; formats iso, raw-efi, qcow2, vmware,
  vbox, docker, sd-*. Substrate breadth is not the gap; the install path is.

## Reference machine (steward's Lenovo, measured 2026-07-28)

| disk | layout |
|------|--------|
| 0 — CT500BX500SSD1 465.8 GB GPT | ESP + C: 465.1 + Recovery — boot + system |
| 1 — ST1000LM035 931.5 GB GPT | Recovery, own ESP, D: 442.2, E: 253.9, F: 233.9, G: 0.5 (HARTJRNL) — full |
| 2 — SanDisk Cruzer Blade 28.7 GiB | flash stick (raw image target; 26G root fits as of `4f2dd86d`) |

Both internal disks are GPT — there is no MBR to preserve; the things to preserve
are the GPT tables and Disk 0's ESP entries. Storage controller: PCI `8086:282A`
class 0104, handled generically by `ahci`.
