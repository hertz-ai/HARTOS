# ═══════════════════════════════════════════════════════════════
# HART OS — network-wifi degrade-not-die nixosTest (VM)
# ═══════════════════════════════════════════════════════════════
#
# PRINCIPLE (degrade-not-die): every hardware-dependent path must degrade
# gracefully on missing/faulting hardware — never brick, black, or hang. This is
# the network-wifi dimension's VM proof: the glass shell's wifi probe must, on a
# box with NO wifi chip, report "hardware not detected" HONESTLY (never a false
# "on"), tell a soft/hard rfkill block apart from no-hardware, and survive
# NetworkManager being up-but-deviceless — all WITHOUT crashing or hanging.
#
# Why a VM test on top of the unit tests:
#   tests/unit/test_wifi_probe_degrade.py drives _probe_wifi / _probe_rfkill_wifi
#   with a FAKED sysfs tree and a MOCKED nmcli — it proves the parser logic off
#   real hardware. THIS test runs the SAME real _ConnectivityCache code against a
#   LIVE Linux kernel rfkill subsystem + a LIVE NetworkManager/nmcli inside a
#   headless QEMU VM (which has no wifi chip), so it proves the integration the
#   unit test cannot: that real nmcli answering `radio wifi: enabled` does NOT
#   flip a definitive rfkill "absent" to available=True (the exact FM1 false-on),
#   and that the rfkill parser reads soft/hard/none/absent/unknown correctly off
#   a REAL on-disk sysfs-shaped tree (real os.listdir/open, not monkeypatched).
#
# It also proves the preemptive HARDWARE levers the shell depends on so a real
# radio can actually come up:
#   - hardware.enableRedistributableFirmware ships the iwlwifi/ath/brcm/rtw blobs
#     (FM1's "missing firmware" is a chip with no blob — shipping them is the
#     guard); the blobs must be in the system's firmware tree.
#   - the common wifi DRIVER modules (iwlwifi/ath*/brcm*/rtw*) are in the built
#     kernel module set, ready for udev to auto-load when a chip enumerates
#     (correctly NEVER force-loaded — degrade-not-die means no modprobe storms).
#
# `[VM]` — boots a real QEMU node; a live kernel rfkill subsystem + a real
# NetworkManager cannot be stood up on the Windows dev box, so this gates in CI
# (`nix flake check` / local QEMU), never inline. The on-a-real-radio read (a box
# WITH a wifi chip) still needs real HW — captured by the lspci/rfkill summary in
# hart-boot-log.nix's NETWORK section (the dimension's real-HW probe).
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix), NO ../configurations/X.nix installer-CD overlay. The wifi
# firmware + NetworkManager enables are set IN-TEST (the same way desktop.nix sets
# them in production; their presence in desktop.nix is guarded structurally by
# tests/unit/test_nixos_wifi.py).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # The SAME hart app package the node builds (mkNode does exactly this). Reused
  # only so the probe-runner drives the node's OWN _ConnectivityCache with the
  # node's OWN interpreter — no second python, no copy of the app. This is the
  # identical import path the running hart-liquid-ui service uses
  # (`from integrations.agent_engine.liquid_ui_service import ...`), proven to
  # import cleanly in the VM by the hart-desktop-boot test.
  hartApp = pkgs.callPackage ../packages/hart-app.nix {
    hartSrc = specialArgs.hartSrc;
  };

  # Drives the REAL wifi probe + the REAL rfkill parser and prints one JSON line
  # the testScript asserts on. Three layers:
  #   (1) the LIVE probe against the running kernel + nmcli (degrade-not-die: must
  #       return a well-formed dict, never raise/hang, on this no-wifi VM),
  #   (2) the rfkill PARSER against a REAL on-disk sysfs-shaped tree (real
  #       os.listdir/open) — soft/hard/none/absent/unknown,
  #   (3) the REAL _probe_wifi with a DETERMINISTIC rfkill verdict but the REAL
  #       nmcli, so "rfkill absent beats NM 'enabled'" and "soft-block stays
  #       available + distinguished" are proven against live NetworkManager.
  probeRunner = pkgs.writeText "hart-wifi-probe-runner.py" ''
    import json, os, sys, tempfile

    sys.path.insert(0, "${hartApp}")
    os.environ.setdefault("HEVOLVE_DATA_DIR", "/tmp/hart-wifi-probe")
    os.makedirs(os.environ["HEVOLVE_DATA_DIR"], exist_ok=True)

    # SAME module the hart-liquid-ui service imports (proven to load in the VM).
    from integrations.agent_engine.liquid_ui_service import _ConnectivityCache

    cache = _ConnectivityCache()
    out = {}

    # (1) LIVE probe against the running kernel rfkill + nmcli. A headless QEMU VM
    #     has NO wifi chip; whatever the verdict, this MUST return a well-formed
    #     dict and NEVER raise/hang (if it raised, python exits non-zero -> the
    #     testScript's succeed() fails).
    out["probe_wifi"] = cache._probe_wifi()
    out["rfkill_live"] = cache._probe_rfkill_wifi()

    # (2) The rfkill PARSER against a REAL on-disk sysfs-shaped tree (real
    #     os.listdir / open, NOT monkeypatched) — the soft-vs-hard-vs-no-hardware
    #     distinction on a real filesystem.
    def mk(entries):
        root = tempfile.mkdtemp(prefix="rfkill-")
        for name, typ, soft, hard in entries:
            d = os.path.join(root, name)
            os.makedirs(d)
            with open(os.path.join(d, "type"), "w") as f:
                f.write(typ + "\n")
            with open(os.path.join(d, "soft"), "w") as f:
                f.write(str(soft) + "\n")
            with open(os.path.join(d, "hard"), "w") as f:
                f.write(str(hard) + "\n")
        return root

    out["rf_none"]    = cache._probe_rfkill_wifi(mk([("rfkill0", "wlan", 0, 0)]))
    out["rf_soft"]    = cache._probe_rfkill_wifi(mk([("rfkill0", "wlan", 1, 0)]))
    out["rf_hard"]    = cache._probe_rfkill_wifi(mk([("rfkill0", "wlan", 0, 1)]))
    out["rf_absent"]  = cache._probe_rfkill_wifi(mk([("rfkill0", "bluetooth", 0, 0)]))
    out["rf_unknown"] = cache._probe_rfkill_wifi("/nonexistent-rfkill-dir")

    # (3) The REAL _probe_wifi with a DETERMINISTIC rfkill verdict but the LIVE
    #     nmcli. Done LAST so the live + parser reads above are untouched.
    #
    #   FM1 honesty: rfkill 'absent' (no wlan entry) MUST win over nmcli's
    #   `radio wifi: enabled` (the software switch answers enabled even with ZERO
    #   wifi devices) -> available=False, the honest "hardware not detected".
    cache._probe_rfkill_wifi = lambda *a, **k: "absent"
    out["probe_wifi_absent"] = cache._probe_wifi()

    #   FM2: a soft-blocked chip IS present hardware -> available=True, blocked
    #   'soft' (distinct from no-hardware), proven against live nmcli.
    cache._probe_rfkill_wifi = lambda *a, **k: "soft"
    out["probe_wifi_soft"] = cache._probe_wifi()

    print("HART_WIFI_PROBE_JSON " + json.dumps(out))
  '';
in
{
  network-wifi = pkgs.testers.runNixOSTest {
    name = "hart-network-wifi";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME (same false "Name not defined" as the
    # boot-log / supervisor tests). Skip both static passes; the VM still boots
    # and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.wifi = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      # The two production wifi levers (set in desktop.nix; their presence there
      # is guarded structurally by tests/unit/test_nixos_wifi.py). Set them
      # IN-TEST on the #70-minimal node so this VM exercises the same stack:
      #   - NetworkManager owns wifi + provides the nmcli the probe execs.
      #   - redistributable firmware ships the iwlwifi/ath/brcm/rtw blobs.
      networking.networkmanager.enable = true;
      hardware.enableRedistributableFirmware = true;
      # A plain unprivileged user with NO session — the negative control for the
      # #149 polkit grant below. A sessionless non-hart caller MUST hit the
      # NetworkManager polkit default (auth_admin -> denied non-interactively), so
      # `nmcli connection add` fails as netprobe but SUCCEEDS as the hart shell user.
      # Pinning a uid keeps it deterministic. (Same shape as power-actions.nix's
      # powerprobe control.)
      users.users.netprobe = {
        isNormalUser = true;
        uid = 4322;
      };
    };

    testScript = ''
      import json

      # The driver keys the single machine global by HOSTNAME — mkNode forces it
      # to the variant ("desktop"), NOT the nodes.wifi key. Bind from machines[0].
      wifi = machines[0]
      wifi.start()
      wifi.wait_for_unit("multi-user.target")

      # ── 1. NetworkManager is up so the probe exercises REAL nmcli ──
      with subtest("NetworkManager is up and nmcli answers"):
          wifi.wait_for_unit("NetworkManager.service")
          radio = wifi.succeed("nmcli radio wifi").strip()
          # The SOFTWARE radio switch answers 'enabled' even with ZERO wifi
          # devices — which is exactly why rfkill (NOT this) must decide presence.
          assert radio in ("enabled", "disabled"), f"unexpected nmcli radio: {radio!r}"
          # This VM has no wifi chip. The OLD assertion demanded rc!=0 from
          # `nmcli device wifi` — but NetworkManager on the pinned nixpkgs
          # exits 0 with an EMPTY list when no wifi device exists, so the
          # test failed against a CORRECT stack on every run ("unexpectedly
          # succeeded", run 30485906966). Assert the hardware reality itself:
          # zero wifi scan results, however nmcli spells it.
          scan = wifi.execute("nmcli -t -f ACTIVE,SSID,SIGNAL device wifi")[1].strip()
          assert scan == "" or "No Wi-Fi device" in scan, \
              f"VM has no wifi chip yet nmcli returned scan rows: {scan!r}"

      # ── 2. The REAL probe degrades + the rfkill parser reads a real fs tree ──
      with subtest("the real wifi probe degrades (no crash/hang) + rfkill parses a real fs"):
          raw = wifi.succeed("${hartApp.python}/bin/python ${probeRunner}")
          lines = [l for l in raw.splitlines() if l.startswith("HART_WIFI_PROBE_JSON ")]
          assert lines, f"probe runner produced no JSON marker line; got: {raw!r}"
          data = json.loads(lines[0][len("HART_WIFI_PROBE_JSON "):])

          # (a) LIVE probe: well-formed, never raised (succeed() already proves no
          #     crash/hang), and the contract shape is intact.
          w = data["probe_wifi"]
          assert set(w) == {"available", "enabled", "connected",
                            "ssid", "signal", "blocked"}, \
              f"live probe returned a malformed dict: {w!r}"
          assert isinstance(w["available"], bool)
          assert w["blocked"] in (None, "soft", "hard"), \
              f"blocked must be None/soft/hard, got {w['blocked']!r}"

          # (b) rfkill PARSER on a REAL on-disk sysfs tree (real os calls): the
          #     soft-vs-hard-vs-no-hardware distinction, off real HW.
          assert data["rf_none"] == "none", data
          assert data["rf_soft"] == "soft", data
          assert data["rf_hard"] == "hard", data
          assert data["rf_absent"] == "absent", data   # bt/wwan only, no wlan == no chip
          assert data["rf_unknown"] == "unknown", data  # no /sys/class/rfkill at all

          # (c) FM1 honesty WITH live nmcli: rfkill 'absent' beats NM 'enabled'.
          wa = data["probe_wifi_absent"]
          assert wa["available"] is False, \
              "no wlan rfkill entry must NOT report available even when nmcli " \
              f"radio wifi answers 'enabled' (false-on regression): {wa!r}"
          assert wa["blocked"] is None, wa

          # (d) FM2: a soft-blocked chip is present hardware, distinguished from
          #     no-hardware — proven against live NetworkManager.
          ws = data["probe_wifi_soft"]
          assert ws["available"] is True, \
              f"a soft-blocked chip IS present hardware: {ws!r}"
          assert ws["blocked"] == "soft", ws

      # ── 3. Firmware lever: the iwlwifi/ath/brcm/rtw blobs are shipped ──
      with subtest("redistributable wifi firmware blobs are shipped (iwlwifi/ath/brcm/rtw)"):
          # enableRedistributableFirmware merges linux-firmware into the kernel's
          # firmware search path. FM1's "missing firmware" is a chip with no blob;
          # shipping these is the preemptive guard so a real radio can clear
          # soft-rfkill + come up. Discover the firmware root (path can vary by
          # NixOS rev) rather than hard-coding it.
          fw_root = wifi.succeed(
              "for d in /run/current-system/firmware /run/booted-system/firmware "
              "/lib/firmware; do [ -d \"$d\" ] && echo \"$d\" && break; done").strip()
          assert fw_root, "no firmware directory found (enableRedistributableFirmware off?)"
          # RECURSIVE, and follow symlinks. -maxdepth 1 only ever worked by
          # accident: it matched families whose top-level entry happens to be
          # named after the family (ath10k/, brcm/, rtw88/ are directories).
          # iwlwifi ships as loose *.ucode FILES whose layout upstream has
          # moved, so depth-1 reported "family missing" on a node that sets
          # enableRedistributableFirmware=true (verified: network-wifi.nix
          # sets it on THIS node) — i.e. the probe was wrong, not the OS.
          # -L matters because /run/current-system/firmware is a symlink tree
          # into the store.
          for fam in ["iwlwifi", "ath", "brcm", "rtw"]:
              n = wifi.succeed(
                  "find -L " + fw_root + " -iname '" + fam
                  + "*' 2>/dev/null | wc -l").strip()
              assert int(n) > 0, (
                  "no " + fam + " firmware blobs shipped (family missing).\n"
                  # Dump the actual layout: a bare "missing" told us nothing
                  # last time and cost a full CI round to re-ask.
                  "--- " + fw_root + " (top level) ---\n"
                  + wifi.succeed("ls -1 " + fw_root + " | head -40 || true")
                  + "--- entries matching '" + fam + "' at ANY depth ---\n"
                  + wifi.succeed("find -L " + fw_root + " -iname '*" + fam
                                 + "*' 2>/dev/null | head -10 || true"))

      # ── 4. Driver lever: the wifi driver modules are in the kernel module set ──
      with subtest("common wifi DRIVER modules are available (iwlwifi/ath/brcm/rtw)"):
          # The drivers ride the stock kernel + udev auto-load (correctly NEVER
          # force-loaded — degrade-not-die means no modprobe storms). modinfo
          # resolves a module WITHOUT loading it, proving it is in the built tree
          # ready to bind when a chip enumerates.
          fams = [
              ("Intel iwlwifi",     ["iwlwifi"]),
              ("Atheros/Qualcomm",  ["ath9k", "ath10k_pci", "ath11k_pci", "ath11k"]),
              ("Broadcom",          ["brcmfmac", "brcmsmac"]),
              ("Realtek rtw",       ["rtw88_pci", "rtw89_pci", "rtw88_core", "rtw_pci"]),
          ]
          for label, mods in fams:
              cmd = " ; ".join(
                  "modinfo " + m + " >/dev/null 2>&1 && echo FOUND-" + m
                  for m in mods)
              # `|| true`, NOT `; true`. The test driver runs commands under
              # errexit, so a failing subshell aborts BEFORE a following
              # `; true` can run — the rescue never fired. `||` is a
              # condition, so errexit ignores the left side and the whole
              # line exits 0.
              #
              # Why it matters: the subshell's status is its LAST command's.
              # With `;` chaining, a family whose FINAL probe misses fails the
              # assertion even when the family is present. Real 2026-08-06
              # failure — FOUND-rtw88_pci, FOUND-rtw89_pci and FOUND-rtw88_core
              # were all printed, then `modinfo rtw_pci` (absent in this
              # kernel) made the whole thing exit 1. The intent is "at least
              # one of these resolves", which the `assert "FOUND-" in res`
              # below already expresses correctly — the command just has to
              # let it be reached.
              res = wifi.succeed("( " + cmd + " ) || true")
              assert "FOUND-" in res, \
                  label + ": none of " + str(mods) + " resolved via modinfo"

      # ── 5. #149 polkit grant: the hart shell user CAN change network settings ──
      # The steward's real-HW bug: "entering a wifi password -> Not authorised to
      # change network settings". The shell SERVER execs `nmcli` as the sessionless
      # `hart` service user; saving a Wi-Fi profile needs polkit auth for
      # org.freedesktop.NetworkManager.settings.modify.system, which the polkit
      # default DENIES for a sessionless daemon. The hart-base polkit rule (#149)
      # must flip that to YES. We prove it BEHAVIOURALLY + NON-DESTRUCTIVELY: adding
      # a saved Wi-Fi connection profile (NOT activating it — no device needed)
      # exercises settings.modify.system end-to-end through real polkit. This is the
      # nmcli VM twin of the mocked shell-route tests; a regression here (drop the
      # rule, break the subject.user check, mis-name the action prefix) passes 100%
      # of the Python suite while the real box says "Not authorised" again.
      with subtest("the hart shell user is GRANTED network settings (nmcli con add succeeds, not 'Not authorised')"):
          # runuser -u hart runs nmcli AS the sessionless shell-server uid. The add
          # creates a saved profile only (no ifname bind / no activation), so it is
          # non-destructive on this chipless VM yet still hits settings.modify.system.
          wifi.succeed(
              "runuser -u hart -- nmcli connection add type wifi "
              "con-name hart-polkit-probe ssid HARTTESTSSID "
              "wifi-sec.key-mgmt wpa-psk wifi-sec.psk 'hartpolkitpw123'")
          # Clean up the probe profile so the node state is unchanged.
          wifi.succeed("runuser -u hart -- nmcli connection delete hart-polkit-probe")

      with subtest("the grant is SCOPED: a plain sessionless user is NOT authorised (rule did not widen authority)"):
          # The same add as a sessionless non-hart user MUST be denied (the polkit
          # default for settings.modify.system), proving the #149 rule grants ONLY
          # the hart daemon (and active local seats), not everyone. nmcli exits
          # non-zero with a "not authorized" error -> fail() asserts the denial.
          out = wifi.fail(
              "runuser -u netprobe -- nmcli connection add type wifi "
              "con-name netprobe-denied ssid NOPE "
              "wifi-sec.key-mgmt wpa-psk wifi-sec.psk 'nopenopenope12' 2>&1")
          # NetworkManager's denial wording changed across releases: older NM
          # surfaces polkit's "not authorized", newer NM reports the same
          # polkit denial as "Insufficient privileges". Both are the SCOPED
          # denial this subtest exists to prove; accept either vocabulary.
          _denied = out.lower()
          assert ("not authorized" in _denied or "not authorised" in _denied
                  or "insufficient privileges" in _denied), \
              ("sessionless netprobe nmcli add failed for a NON-polkit reason; "
               "expected a polkit authorization denial, got:\n" + out)
          # Defensive: if NM somehow created it despite the expected denial, remove it.
          wifi.succeed(
              "runuser -u hart -- nmcli connection delete netprobe-denied "
              "2>/dev/null || true")
    '';
  };
}
