# ═══════════════════════════════════════════════════════════════
# HART OS - All-OS app installation VERIFICATION nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Boots a minimal NixOS node, makes the REAL unified AppInstaller importable on
# the node's actual Python env (the same minimal pythonEnv hart-app.nix ships,
# NOT the dev box's), and proves EVERY platform handler reaches its POSITIVE
# runtime confirmation step:
#
#   nix        -> nix-env exit 0
#   flatpak    -> flatpak install exit 0
#   appimage   -> file copied + present in the install dir
#   windows    -> wine present + exit 0
#   android    -> waydroid app install + the package id appears in
#                 `waydroid app list` (exit 0 alone is NOT proof)
#   browser_ext-> the extension id is written into the managed policy AND read
#                 back off disk
#   snap       -> honest "unsupported" refusal (never a crash / nix misroute)
#
# A real download is infeasible on an offline VM, so the PACKAGE SOURCE is
# mocked: the driver drops tiny fake `nix-env` / `flatpak` / `wine64` /
# `waydroid` / `aapt` / `chromium` shims into a temp dir and prepends it to PATH,
# so the handlers' real `subprocess.run` + `shutil.which` + on-disk policy writes
# all execute against a REAL (faked) tool surface. This is the runtime twin of
# tests/unit/test_app_install_handlers.py (which mocks the boundary on the dev
# box); here the WHOLE module actually loads + runs on the shipped Python env.
#
# `[VM]` - boots a QEMU node; gates in CI (`nix flake check`) / local QEMU.
# CANNOT run on the Windows dev box (mirrors native-subsystems.nix). The
# behavioural per-handler contract is unit-tested on the dev box; this proves it
# survives the real NixOS Python closure + a real subprocess/PATH/disk surface.
#
# #70 discipline: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix), NO ../configurations/X.nix installer-CD overlay. Server variant
# (the lightest; the installer is variant-neutral).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
  hartSrc = specialArgs.hartSrc;
  # The SAME packaged source + minimal Python env the image ships (DRY: reuse
  # hart-app.nix, do not re-derive a parallel env). ${hartApp} is the source
  # root on PYTHONPATH; pythonEnv is the interpreter with the shipped deps.
  hartApp = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };
  pyEnv = hartApp.pythonEnv;

  # The driver is authored FLUSH-LEFT and uses only double-quoted Python strings
  # + `\n`-encoded shell bodies, so the Nix indented-string minimal-indent strip
  # cannot mangle Python indentation and no literal `''` / `${` appears (the
  # `python -c` in a Nix '' string pitfall from the 2026-06-23 boot-loop).
  driver = pkgs.writeText "hart-app-install-verify-driver.py" ''
import json
import os
import sys
import tempfile

from integrations.agent_engine.app_installer import (
    AppInstaller, InstallRequest,
)

fails = []

def record(label, ok):
    print("RESULT " + label + " " + ("verified" if ok else "FAILED"))
    if not ok:
        fails.append(label)

# Mock the package source: fake CLI shims on a prepended PATH so the real
# handlers run their real subprocess/which against a tool surface that exists.
fakebin = tempfile.mkdtemp(prefix="hart_fakebin_")

def tool(name, body):
    path = os.path.join(fakebin, name)
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n")
        handle.write(body)
    os.chmod(path, 0o755)

tool("nix-env", "exit 0\n")
tool("flatpak", "exit 0\n")
tool("wine64", "exit 0\n")
tool("wine", "exit 0\n")
tool("chromium", "exit 0\n")
tool("aapt", "if [ \"$1\" = \"dump\" ]; then echo \"package: name='com.hart.testapp' versionCode='1'\"; fi\nexit 0\n")
tool("waydroid", "case \"$1\" in status) echo RUNNING; exit 0;; app) case \"$2\" in install) exit 0;; list) echo com.hart.testapp; exit 0;; *) exit 0;; esac;; *) exit 0;; esac\n")

os.environ["PATH"] = fakebin + os.pathsep + os.environ.get("PATH", "")

work = tempfile.mkdtemp(prefix="hart_appinstall_")
inst = AppInstaller()
inst._install_dir = os.path.join(work, "apps")
os.makedirs(inst._install_dir, exist_ok=True)

# nix
res = inst._install_nix(InstallRequest(source="nixpkgs.hello"))
record("nix", res.success and res.verified)

# flatpak
res = inst._install_flatpak(InstallRequest(source="flathub:org.test.App"))
record("flatpak", res.success and res.verified)

# appimage (confirmation = the copied file exists on disk)
appimg = os.path.join(work, "Demo.AppImage")
with open(appimg, "wb") as f:
    f.write(b"\x7fELF" + b"\x00" * 64)
res = inst._install_appimage(InstallRequest(source=appimg))
record("appimage", res.success and res.verified and os.path.isfile(res.install_path))

# windows (wine present + exit 0)
winexe = os.path.join(work, "Setup.exe")
with open(winexe, "wb") as f:
    f.write(b"MZ" + b"\x00" * 64)
res = inst._install_windows(InstallRequest(source=winexe))
record("windows", res.success and res.verified)

# android (confirmed by `waydroid app list`)
apk = os.path.join(work, "App.apk")
with open(apk, "wb") as f:
    f.write(b"PK" + b"\x00" * 64)
res = inst._install_android(InstallRequest(source=apk))
record("android", res.success and res.verified and res.app_id == "com.hart.testapp")

# browser_ext (.crx force-install, confirmed by reading the policy back)
crx = os.path.join(work, "ext.crx")
with open(crx, "wb") as f:
    f.write(b"Cr24" + b"\x00" * 64)
policy_dir = os.path.join(work, "chromium-policy")
res = inst._install_browser_ext(InstallRequest(
    source=crx, options={"id": "abcdefghijklmnop", "policy_dir": policy_dir}))
on_disk = False
if res.install_path and os.path.isfile(res.install_path):
    with open(res.install_path) as f:
        pol = json.load(f)
    ids = [e.split(";")[0] for e in pol.get("ExtensionInstallForcelist", [])]
    on_disk = "abcdefghijklmnop" in ids
record("browser_ext", res.success and res.verified and on_disk)

# snap (honest refusal, no crash, not verified)
res = inst._install_snap(InstallRequest(source="snap:firefox"))
snap_ok = (not res.success) and (not res.verified) and ("not supported" in res.error.lower())
print("RESULT snap " + ("refused" if snap_ok else "FAILED"))
if not snap_ok:
    fails.append("snap")

# missing tool: empty PATH -> graceful honest failure, never an exception
saved = os.environ["PATH"]
os.environ["PATH"] = ""
try:
    res = inst._install_nix(InstallRequest(source="nixpkgs.hello"))
finally:
    os.environ["PATH"] = saved
graceful = (not res.success) and ("not available" in res.error.lower())
print("RESULT missing-tool " + ("graceful" if graceful else "FAILED"))
if not graceful:
    fails.append("missing-tool")

if fails:
    print("FAILS " + ",".join(fails))
    sys.exit(1)
print("ALL-OK")
'';
in
{
  hart-app-install-verify = pkgs.testers.runNixOSTest {
    name = "hart-app-install-verify";
    # Same runtime-injected Machine-global false positives the other hart tests
    # document (the static passes flag `host.succeed(...)` as undefined though
    # the node IS bound at runtime). Skip them; the VM still asserts.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.host = mkNode "server" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      # The shipped Python env on PATH so `python3` resolves to the interpreter
      # the installer actually runs under in production.
      environment.systemPackages = [ pyEnv ];
    };

    testScript = ''
      # Bind the runtime Machine global (mkNode forces hostname to the variant).
      host = machines[0]
      host.start()
      host.wait_for_unit("multi-user.target")

      with subtest("The unified installer module imports on the shipped Python env"):
          # Proves the WHOLE import chain (integrations -> core guard -> stdlib)
          # loads on the minimal pythonEnv, not just the dev box.
          host.succeed(
              "PYTHONPATH=${hartApp} ${pyEnv}/bin/python3 -c "
              "'import integrations.agent_engine.app_installer as m; "
              "print(m.InstallerPlatform.NIX.value)'")

      with subtest("Every platform handler reaches its positive confirmation step"):
          out = host.succeed(
              "PYTHONPATH=${hartApp} ${pyEnv}/bin/python3 ${driver}")
          # The driver exits non-zero on any failed assertion (so host.succeed
          # already gates it); these per-platform markers make a regression
          # legible in the test log.
          for marker in (
              "RESULT nix verified",
              "RESULT flatpak verified",
              "RESULT appimage verified",
              "RESULT windows verified",
              "RESULT android verified",
              "RESULT browser_ext verified",
              "RESULT snap refused",
              "RESULT missing-tool graceful",
              "ALL-OK",
          ):
              assert marker in out, f"missing {marker!r} in driver output:\n{out}"
    '';
  };
}
