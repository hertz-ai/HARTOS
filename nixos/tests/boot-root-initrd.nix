# ═══════════════════════════════════════════════════════════════
# HART OS — Boot / root-mount / initrd nixosTest (USB-root enumeration)
# ═══════════════════════════════════════════════════════════════
#
# The behavioural proof for the "boot-root-initrd" hardware dimension: boot, mount
# root, and prove the two failure modes that BRICK a real-HW USB boot are guarded —
# never a silent black hang.
#
# FAILURE MODES this gates (the ones a virtio-root CI VM otherwise never sees):
#   1. initrd missing usb_storage / xhci / sd_mod  -> the USB root never enumerates
#      -> kernel panic "VFS: Unable to mount root fs on LABEL=HART_OS" / the
#      initramfs emergency shell (a black hang on a headless first boot). We prove
#      the hart.bootRootInitrd guard actually PACKS those modules into the BUILT
#      initrd (extracted + grep'd — not merely listed in a config value).
#   2. the duplicate-LABEL root race  -> hart-hartlog-create COMPLETING the live
#      boot medium's GPT makes BOTH the whole disk and partition 1 answer to
#      LABEL=HART_OS, so the by-label root device races per-boot udev order ("boots
#      once, panics next boot"). We prove the create unit NEVER carves the boot/root
#      disk: (a) the boot-time auto-detect run was a clean NOOP on the VM's internal
#      root disk (no carve, root still mounted), and (b) the test-seam BOOT-DISK
#      GUARD refuses to complete a stand-in boot medium's GPT.
#
# This test is BEHAVIOURAL (not grep-on-source): it boots a real node, confirms the
# root filesystem actually mounted, decompresses the node's REAL initrd and asserts
# the USB-root .ko modules are inside, then runs the ACTUAL hart-hartlog-create the
# module installs and asserts it never touched the boot/root disk.
#
# WHY [VM]-gated: it needs a real Linux block layer + a real built initrd — it
# cannot run on the Windows dev box. The VM boots from an internal virtio disk (NOT
# a USB), so the AUTO-DETECT USB-resolve link still needs a real USB boot to fully
# confirm; THIS test proves every link short of the physical stick (the initrd
# really carries the USB modules, and the create unit never completes the boot-disk
# GPT). The real-HW probe is the hart-boot-log "root / boot device + cmdline"
# section that records root-mount success onto the HARTLOG stick.
#
# #70 discipline preserved: built from `hartModules` alone via the shared `mkNode`.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
  lib = pkgs.lib;

  # ── The eval-time degrade-not-die TRIPWIRE proof (no VM boot needed) ──
  # The VM test below proves the POSITIVE case: with the guard on, the USB-root
  # modules are really PACKED into the built initrd. It does NOT (and cannot cheaply)
  # prove the NEGATIVE case: that the build FAILS LOUDLY if a future mkForce wipes
  # boot.initrd.availableKernelModules — which is the whole point of the guard's
  # assertion (a silent strip would otherwise ship a real-HW "VFS: Unable to mount
  # root fs" brick). This evaluates the REAL module in isolation under two scenarios
  # and asserts the guard's assertions stay quiet when the modules are present and
  # FIRE (naming the missing USB-root module) when they are stripped. It imports the
  # module file directly ON PURPOSE — the proof is that the guard fires on its OWN,
  # independent of the rest of the closure.
  evalGuard = strip: lib.evalModules {
    modules = [
      ../modules/hart-boot-root-initrd.nix
      ({ lib, ... }: {
        # Minimal stand-ins for the options the guard reads/writes, so the module can
        # be evaluated outside a full NixOS system (the boot test below is the
        # in-system proof; this is the isolated assertion proof).
        options.hart.enable = lib.mkEnableOption "hart";
        options.boot.initrd.availableKernelModules = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
        };
        options.assertions = lib.mkOption {
          type = lib.types.listOf (lib.types.submodule {
            options.assertion = lib.mkOption { type = lib.types.bool; };
            options.message = lib.mkOption { type = lib.types.str; };
          });
          default = [ ];
        };
        config._module.args.pkgs = pkgs;
        config.hart.enable = true;
        config.hart.bootRootInitrd.enable = true;
      })
    ] ++ lib.optional strip ({ lib, ... }: {
      # Simulate the brick: a profile/override wipes the removable-root module set.
      config.boot.initrd.availableKernelModules = lib.mkForce [ ];
    });
  };
  failedMsgs = ev: map (a: a.message)
    (lib.filter (a: ! a.assertion) ev.config.assertions);
  enabledFailed = failedMsgs (evalGuard false);
  strippedFailed = failedMsgs (evalGuard true);
  strippedNamesUsb = lib.any
    (m: lib.hasInfix "usb_storage" m || lib.hasInfix "VFS" m) strippedFailed;
in
{
  hart-boot-root-initrd = pkgs.testers.runNixOSTest {
    name = "hart-boot-root-initrd";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME (same false "Name not defined" as the
    # sibling boot tests — the node IS bound at runtime). Skip both static passes;
    # the VM still boots and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.br = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A spare raw disk standing in for "a USB stick" so the BOOT-DISK GUARD seam
        # can be exercised (the VM's real boot disk is the internal virtio root).
        emptyDiskImages = [ 256 ];
      };
      # The guard under test: ensure + assert the USB-root initrd module set.
      hart.bootRootInitrd.enable = true;
      # The create unit runs at boot — we prove it never carves the internal root.
      hart.hartlogCreate.enable = true;
      # Decompressors + cpio for the initrd extraction, GPT tools for the seam.
      environment.systemPackages = [
        pkgs.zstd pkgs.gzip pkgs.xz pkgs.lz4 pkgs.cpio
        pkgs.gptfdisk pkgs.util-linux pkgs.gnugrep
      ];
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME — mkNode forces it to
      # the variant ("desktop"), NOT the nodes.br key. Bind from machines[0].
      br = machines[0]
      br.start()
      br.wait_for_unit("multi-user.target")

      # ── 1. BOOT + ROOT MOUNT succeeded (the baseline of the whole dimension) ──
      with subtest("the root filesystem actually mounted (boot + root-mount succeeded)"):
          # Reaching multi-user.target already implies root mounted; assert it
          # explicitly + record the source device + the kernel root= param so the
          # "kernel cmdline root= mismatch" failure mode is observable here too.
          rootsrc = br.succeed("findmnt -n -o SOURCE / ").strip()
          assert rootsrc, "root (/) has no backing source — root never mounted"
          br.log(f"root mounted from: {rootsrc}")
          # NO root= cmdline assertion here: the nixos-test driver boots its
          # VMs with a host-shared store root and NO root= param BY FRAMEWORK
          # DESIGN — the old assert failed every run against a correctly
          # booted VM (run 30485906966). The "kernel root= matches the boot
          # medium" link is real-HW-only, probed by hart-boot-log's
          # root/boot-device+cmdline section exactly as this file's header
          # already documents. Log it for the record instead.
          cmdline = br.succeed("cat /proc/cmdline").strip()
          br.log(f"kernel cmdline (driver-booted, root= absent by design): {cmdline}")

      # ── 2. The BUILT initrd carries usb_storage / xhci / sd_mod (PACKED, not just
      #       listed). This is the link a virtio-root VM never exercises — the guard
      #       must put the USB-enumeration modules INTO the initrd so a real USB boot
      #       can find the stick before the root pivot. ──
      with subtest("the initrd really PACKS usb_storage + xhci + sd_mod (USB-root enumeration)"):
          initrd = br.succeed("readlink -f /run/current-system/initrd").strip()
          assert initrd, "could not resolve /run/current-system/initrd"

          def initrd_has(pattern):
              # Decompress the initrd (try each compressor — recent NixOS defaults to
              # zstd) and look for the module's .ko path. cpio filenames are plaintext
              # in the decompressed stream, so grep -a on the stream is sufficient;
              # we also try `cpio -t` for a clean listing. The module FILE name uses
              # '-' (usb-storage.ko) while the module NAME uses '_' (usb_storage), so
              # the pattern uses '.' to match either.
              script = (
                  f'i="{initrd}"; '
                  'for dc in "zstd -dc" "gzip -dc" "xz -dc" "lz4 -dc" "cat"; do '
                  f'  if $dc "$i" 2>/dev/null | cpio -t 2>/dev/null | grep -Eq "{pattern}"; then echo HIT; exit 0; fi; '
                  f'  if $dc "$i" 2>/dev/null | grep -aEq "{pattern}"; then echo HIT; exit 0; fi; '
                  'done; echo MISS'
              )
              return "HIT" in br.succeed(script)

          assert initrd_has("usb.storage"), \
              "initrd does NOT carry usb_storage — a USB root cannot enumerate (VFS panic)"
          assert initrd_has("xhci"), \
              "initrd does NOT carry an xhci host-controller module — a USB3 port can't enumerate the stick"
          assert initrd_has("sd_mod"), \
              "initrd does NOT carry sd_mod — the stick's /dev/sdX block node never appears"
          # uas (USB Attached SCSI) is the faster USB3 mass-storage transport — also
          # part of the guarded set; informational (some kernels fold it into
          # usb_storage), so log rather than hard-fail.
          br.log(f"initrd carries uas: {initrd_has('uas')}")

      # ── 3. The boot-time create run NEVER carved the internal root disk ──
      # hart-hartlog-create ran at boot (wantedBy multi-user.target). On this VM the
      # live root is an INTERNAL virtio disk, not a resolvable USB, so the run MUST be
      # a clean NOOP that never repartitions the boot/root disk — the never-touch-the-
      # root-device invariant proven on a REAL boot (not a hand-run).
      with subtest("the boot-time hartlog-create run was a clean NOOP on the internal root disk"):
          br.wait_for_unit("hart-hartlog-create.service")
          status = br.succeed("cat /run/hart/hartlog-create.status")
          assert "DECISION=NOOP" in status, \
              f"boot-time create must NOOP on the internal root disk, got: {status!r}"
          assert "DECISION=CREATED" not in status, \
              f"boot-time create must NOT have carved the root disk, got: {status!r}"
          # No HARTLOG was carved anywhere on this non-USB boot.
          br.fail("blkid -L HARTLOG")
          # Root is STILL mounted + healthy after the create unit ran (the carve, had
          # it wrongly run on the boot disk, is exactly what races the root mount).
          br.succeed("findmnt -n / ")

      # ── 4. The BOOT-DISK GUARD: the create unit refuses to complete a boot
      #       medium's GPT (the duplicate-LABEL root race) ──
      # Build a stand-in stick on the spare disk (GPT + a small ISO part + free tail),
      # then drive the REAL create script telling it THIS disk IS the live boot medium
      # (HART_HARTLOG_TEST_BOOT_DISK). It MUST refuse: NOOP, no relocation, no appended
      # partition, no HARTLOG — closing the race that panics "VFS: Unable to mount root
      # fs on LABEL=HART_OS" once the boot-disk GPT is completed.
      with subtest("the create unit NEVER completes a boot medium's GPT (duplicate-LABEL race guard)"):
          disk = br.succeed(
              "for d in /dev/vdb /dev/sdb; do [ -b \"$d\" ] && echo \"$d\" && break; done"
          ).strip()
          assert disk, "no spare disk surfaced for the boot-disk-guard seam"
          br.succeed(f"sgdisk --zap-all {disk}")
          br.succeed(f"sgdisk --new=1:2048:+64M --change-name=1:ISO {disk}")
          br.succeed("udevadm settle || true")
          n_before = br.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          lu_before = int(br.succeed(f"sgdisk -E {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")

          out = br.succeed(
              f"HART_HARTLOG_TEST_DISK={disk} HART_HARTLOG_TEST_BOOT_DISK={disk} "
              f"hart-hartlog-create 2>&1; echo RC=$?"
          )
          assert "RC=0" in out, f"boot-disk guard must exit 0, got: {out!r}"
          assert "DECISION=NOOP" in out, \
              f"the boot medium must be a NOOP (never carved), got: {out!r}"
          assert "boot medium" in out, \
              f"the no-op must name the boot-medium guard reason, got: {out!r}"
          # No partition appended, no backup-GPT relocation (last_usable unchanged).
          n_after = br.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          assert n_after == n_before, \
              f"the boot medium must gain NO partition ({n_before} -> {n_after})"
          lu_after = int(br.succeed(f"sgdisk -E {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")
          assert lu_after == lu_before, \
              f"the boot medium's GPT must NOT be completed — last_usable {lu_before} -> {lu_after}"
          # And no HARTLOG was carved on the (stand-in) boot medium.
          br.fail("blkid -L HARTLOG")
    '';
  };

  # The eval-time tripwire proof (the negative case the VM boot can't show): the
  # guard's build-time assertion must STAY QUIET with the USB-root modules present
  # and FIRE (naming the missing module) when they are stripped. Computed at eval
  # time (above), checked in the build phase so evaluation never throws (keeps
  # `nix flake show` working) — the BUILD fails loudly if the invariant breaks.
  # Auto-wired: flake.nix merges this whole attrset via `// bootRootInitrd`.
  hart-boot-root-initrd-guard-eval = pkgs.runCommand "hart-boot-root-initrd-guard-eval"
    {
      enabledFailedCount = toString (builtins.length enabledFailed);
      strippedFailedCount = toString (builtins.length strippedFailed);
      strippedNamesUsb = if strippedNamesUsb then "yes" else "no";
    }
    ''
      echo "enabled (usb_storage/uas/sd_mod/xhci present): $enabledFailedCount failed assertion(s)  [EXPECT 0]"
      echo "stripped (mkForce [] on availableKernelModules): $strippedFailedCount failed assertion(s)  [EXPECT >= 1]"
      echo "the fired assertion names the missing USB-root module: $strippedNamesUsb  [EXPECT yes]"
      fail=0
      if [ "$enabledFailedCount" != "0" ]; then
        echo "FAIL: the hart.bootRootInitrd guard wrongly fired while the USB-root modules were present"
        fail=1
      fi
      if [ "$strippedFailedCount" = "0" ]; then
        echo "FAIL: the degrade-not-die tripwire did NOT fire when usb_storage/xhci/sd_mod were stripped (mkForce []) - a silent real-HW 'VFS: Unable to mount root fs' brick would ship undetected"
        fail=1
      fi
      if [ "$strippedNamesUsb" != "yes" ]; then
        echo "FAIL: the fired assertion message does not name the missing USB-root module (usb_storage / VFS)"
        fail=1
      fi
      [ "$fail" = "0" ] || exit 1
      echo "OK: the USB-root initrd guard passes when present and fails loudly when stripped"
      touch "$out"
    '';
}
