{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Boot / root-mount / initrd hardening (USB-root enumeration)
# ═══════════════════════════════════════════════════════════════
#
# THE failure mode this guards (the "boot-root-initrd" dimension):
#   HART OS boots from a USB stick. For the kernel to find + mount the live root,
#   the INITRD must carry the modules that enumerate a USB block device BEFORE the
#   root pivot:
#     - usb_storage / uas : the stick presented as a SCSI/USB Mass Storage device
#     - sd_mod            : the /dev/sdX block node the stick surfaces as
#     - xhci_pci/xhci_hcd : the USB3 host controller (the port the stick is in)
#     - ehci_pci/ehci_hcd : the USB2 host controller (older ports / hubs)
#     - usbhid/hid_generic: a USB keyboard in the initrd (so a recovery prompt is
#                           typeable if the pivot fails)
#   If ANY of usb_storage / xhci / sd_mod is missing from the initrd, the USB root
#   never enumerates: the kernel panics "VFS: Unable to mount root fs on
#   LABEL=HART_OS / unknown-block(0,0)" or drops to the initramfs emergency shell —
#   a BLACK HANG on a headless first boot, with no recovery path. This is the
#   classic "works in the VM (virtio root), bricks on the real USB stick" gap that
#   CI never sees, because a nixosTest VM boots from an internal virtio disk and
#   never exercises the USB-storage path.
#
# THE guard (degrade-not-die, applied to the boot floor itself):
#   1. ENSURE the USB-root module set is in `boot.initrd.availableKernelModules`
#      (udev auto-loads them on hardware match during early boot — the idiomatic
#      removable-root path, same set the nixpkgs all-hardware profile ships). This
#      is DEFENSE IN DEPTH: the desktop ISO already inherits these from the
#      installer-CD profile, but a future profile/override change must NOT be able
#      to silently drop them and brick the USB boot.
#   2. ASSERT, at eval time, that the critical subset (usb_storage, a UAS path, an
#      xhci host-controller module, and sd_mod) actually survived into the merged
#      `boot.initrd.availableKernelModules` — so a `mkForce` elsewhere that wiped
#      the list is a BUILD-TIME failure, never a silent real-HW brick.
#
# It is a PURE EVAL/CLOSURE guard — it adds initrd modules + an assertion, NOTHING
# at runtime. It can never block, slow, or fail a boot; the worst it can do is fail
# the BUILD (loudly, in CI) if the USB-root module set was stripped.
#
# Opt-in (hart.bootRootInitrd.enable = false default) -> a pure no-op for every
# variant + every #70-minimal nixosTest node (which boot a virtio root and must NOT
# inherit a USB-root assertion). desktop.nix (the real USB-boot ISO) turns it on.
# tests/boot-root-initrd.nix is the behavioural proof: it boots a node with this
# guard ON, confirms root actually mounted, and EXTRACTS the built initrd to prove
# usb_storage / xhci / sd_mod were really PACKED (not just listed) — then re-proves
# the hart-hartlog-create boot-disk-GPT guard that protects the root device from
# the duplicate-LABEL race.

let
  cfg = config.hart;
  bri = config.hart.bootRootInitrd;

  # The module set a USB / removable root MUST have available in the initrd to
  # enumerate + mount the stick before the root pivot. Underscored names match the
  # nixpkgs `profiles/all-hardware.nix` convention (modprobe normalises _/-, but
  # the strings here are what land in availableKernelModules).
  usbRootModules = [
    "usb_storage"   # USB Mass Storage — the stick as a block device
    "uas"           # USB Attached SCSI — the faster USB3 mass-storage transport
    "sd_mod"        # SCSI disk — the /dev/sdX the stick presents as
    "xhci_pci"      # USB3 host controller (PCI glue)
    "xhci_hcd"      # USB3 host controller core
    "ehci_pci"      # USB2 host controller (PCI glue) — older ports / hubs
    "ehci_hcd"      # USB2 host controller core
    "ohci_pci"      # USB1.1 host controller (PCI glue) — legacy hubs
    "ohci_hcd"      # USB1.1 host controller core
    "usbhid"        # USB keyboard/mouse in the initrd (recovery typing)
    "hid_generic"   # generic HID fallback for the recovery prompt
  ];

  # The CRITICAL subset the assertion enforces survived the merge. Dropping ANY of
  # these is what bricks a USB root, so these are the tripwire (the rest are belt).
  # xhci is satisfied by EITHER xhci_pci or xhci_hcd (a kernel may build one =y).
  criticalModules = [ "usb_storage" "uas" "sd_mod" ];
  haveXhci = lib.any (m: lib.elem m config.boot.initrd.availableKernelModules)
    [ "xhci_pci" "xhci_hcd" ];
  missingCritical = lib.filter
    (m: ! lib.elem m config.boot.initrd.availableKernelModules) criticalModules;
in
{
  options.hart.bootRootInitrd.enable = lib.mkEnableOption ''
    Boot / root-mount / initrd hardening for a USB-booted HART OS. Ensures the
    initrd carries the USB-storage host-controller + mass-storage + sd_mod module
    set required to enumerate + mount the live USB root before the pivot (so the
    real-HW USB boot can never hit "VFS: Unable to mount root fs" / the initramfs
    emergency shell because a profile change silently dropped usb_storage / xhci /
    sd_mod), and ASSERTS at eval time that the critical subset survived into
    boot.initrd.availableKernelModules. A pure eval/closure guard — it adds initrd
    modules + a build-time assertion and does NOTHING at runtime, so it can never
    block, slow, or fail a boot (only fail the BUILD, loudly, if the USB-root
    module set was stripped). Opt-in: a no-op for every variant + every minimal
    test node until enabled (desktop.nix turns it on for the USB-boot ISO)'';

  config = lib.mkIf (cfg.enable && bri.enable) {
    # 1. ENSURE the USB-root module set is available in the initrd. availableKernel-
    #    Modules (NOT kernelModules): udev loads them on hardware match during early
    #    boot — never force-loaded, so a box without USB3 simply never loads xhci.
    boot.initrd.availableKernelModules = usbRootModules;

    # NOTE (2026-08-13): a rootdelay was added here on the theory that a slow USB
    # controller was failing stage 1. That was WRONG and has been removed. The
    # real console output showed stage 1 succeeding completely:
    #     stage-1-init: checking /dev/disk/by-label/nixos...
    #     nixos: clean, 473146/1818624 files, 4970844/7253243 blocks
    #     EXT4-fs (sdc2): mounted filesystem 7981aaf4-... r/w
    #     Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000100
    #     CPU: 5 UID: 0 PID: 1 Comm: switch_root
    # Root was found, fsck'd and mounted. The failure is in switch_root: stage 2's
    # init exits 1 immediately, i.e. the UKI's baked init path does not work against
    # the rootfs shipped in the SAME image. Do not add boot-timing workarounds for
    # this class of failure -- read the console first.

    # 2. ASSERT the critical subset actually survived into the merged list. If a
    #    mkForce elsewhere wiped it, this fails the BUILD (CI) — never a silent
    #    real-HW brick. (We read the merged config value, so the assertion sees
    #    contributions from the installer-CD profile + this module + anything else.)
    assertions = [
      {
        assertion = missingCritical == [ ];
        message = ''
          hart.bootRootInitrd: the USB-root initrd module(s) ${toString missingCritical}
          are MISSING from boot.initrd.availableKernelModules. Without them a USB
          boot cannot enumerate the stick and the kernel panics "VFS: Unable to
          mount root fs". Something (likely a mkForce on
          boot.initrd.availableKernelModules) stripped the removable-root module
          set. Restore usb_storage/uas/sd_mod (and an xhci host-controller module).
        '';
      }
      {
        assertion = haveXhci;
        message = ''
          hart.bootRootInitrd: no xhci host-controller module (xhci_pci / xhci_hcd)
          is in boot.initrd.availableKernelModules. A USB3 port cannot enumerate the
          live stick without it, so the USB root never appears. Restore the xhci
          modules to the initrd.
        '';
      }
    ];
  };
}
