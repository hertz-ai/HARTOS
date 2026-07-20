# ═══════════════════════════════════════════════════════════════
# HART OS — robot Model-Bus capability probe nixosTest (embodied twin of
#           hart-compat-smoketest: MEASURE a robot's reach, don't claim it)
# ═══════════════════════════════════════════════════════════════
#
# Proves hart-robot-probe.nix ships a REAL post-boot oneshot that actually
# REACHES the Model Bus for each core intelligence a robot needs and writes an
# HONEST per-capability verdict to /run/hart/robot-capability-status — the same
# discipline hart-compat-smoketest uses for foreign-OS runtimes, now for the
# embodied brain.
#
# What it asserts:
#   1. The probe oneshot RAN and SUCCEEDED (oneshot + RemainAfterExit + the python
#      always exits 0) — a measurement can never fail/block the boot (degrade-not-
#      die), and it is NOT ordered before greetd (runs in parallel with the desktop).
#   2. It wrote /run/hart/robot-capability-status under the tmpfs /run (re-derived
#      every boot) with one honest `key=value` line per capability, and every
#      verdict is one of the documented honest values (ok / no-model / ready /
#      no-backend / down / unavailable) — never a fabricated "ready".
#   3. Degrade-honest: with no live LLM/vision backend the probe still writes the
#      file and still succeeds — one dead capability records its fail-state and the
#      others are still measured.
#   4. Best-effort positive: once the Model Bus HTTP port is actually up, a re-run
#      records model_bus=ok — proving the probe reaches a REAL bus, not a stub.
#
# Honest-hardware-limit: a headless VM has no GPU model loaded, so llm/vision may
# honestly read no-model/down; that is the point — the probe MEASURES, it does not
# claim. `[VM]` — cannot run on the Windows dev box; gates in CI (`nix flake check`)
# / local QEMU.
#
# #70 discipline: built from `hartModules` alone via the shared `mkNode` (./lib.nix),
# and self-contained — it imports ../modules/hart-robot-probe.nix directly so it runs
# whether or not flake.nix has registered the module yet (the held-file follow-up).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-robot-probe = pkgs.testers.runNixOSTest {
    name = "hart-robot-probe";
    # Same runtime-injected-node-global false positives the floor-lock / notify
    # tests document (the driver injects `node`/`machines` at runtime); skip the
    # static passes — the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.robotnode = mkNode "desktop" {
      # Self-contained: import the probe module directly so the test does not depend
      # on flake.nix having registered it yet (held-file follow-up). hart-model-bus
      # is already carried by hartModules; importing the probe twice is idempotent.
      imports = [ ../modules/hart-robot-probe.nix ];

      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };

      # The bus the probe reaches. Enabling it also default-enables the probe
      # (hart.robotics.probe.enable defaults to hart.modelBus.enable); both are set
      # explicit for clarity / regression safety.
      hart.modelBus.enable = true;
      hart.robotics.probe.enable = true;
    };

    testScript = ''
      import re

      # The driver keys the single machine global by HOSTNAME — mkNode forces it to
      # the variant ("desktop"), not the nodes.robotnode key. Bind from the machines
      # list (single-node test => element 0), the floor-lock.nix / notify.nix lesson.
      robotnode = machines[0]
      robotnode.start()
      robotnode.wait_for_unit("multi-user.target")

      STATUS = "/run/hart/robot-capability-status"
      # The full honest verdict vocabulary from integrations/robotics/model_bus_probe.py.
      ALLOWED = {
          "model_bus": {"ok", "down"},
          "llm": {"ok", "no-model", "down"},
          "vision": {"ready", "no-model", "down"},
          "vla": {"ready", "no-backend", "no-model"},
          "intelligence_api": {"ok", "unavailable"},
      }

      def read_status():
          raw = robotnode.succeed("cat " + STATUS)
          kv = {}
          for line in raw.splitlines():
              line = line.strip()
              if not line or "=" not in line:
                  continue
              k, v = line.split("=", 1)
              kv[k] = v
          return raw, kv

      # ── 1. The probe oneshot ran + SUCCEEDED (never-fail measurement) ──
      with subtest("robot capability probe ran and the oneshot succeeded (degrade-not-die)"):
          # oneshot + RemainAfterExit => "active" iff ExecStart exited 0. The python
          # always returns 0, so a wedged backend can never fail the unit.
          robotnode.wait_for_unit("hart-robot-probe.service", timeout=240)
          state = robotnode.succeed(
              "systemctl show -p Result --value hart-robot-probe.service").strip()
          assert state == "success", \
              f"probe unit must succeed even when backends are down, got Result={state!r}"

      # ── 2 + 3. Honest per-capability status file (tmpfs, one key=value per line) ──
      with subtest("probe wrote honest per-capability verdicts to the tmpfs status file"):
          robotnode.wait_until_succeeds("test -f " + STATUS, timeout=60)
          raw, kv = read_status()
          for key, allowed in ALLOWED.items():
              assert key in kv, f"{key} verdict missing from status file:\n{raw}"
              assert kv[key] in allowed, \
                  f"{key}={kv[key]!r} is not an honest verdict {allowed}:\n{raw}"
          # robots is a plain integer count (context, not a capability verdict).
          assert re.fullmatch(r"\d+", kv.get("robots", "")), \
              f"robots must be an integer count, got {kv.get('robots')!r}:\n{raw}"

      # ── 4. Best-effort positive: a live bus makes the re-run read model_bus=ok ──
      # If the heavy Model Bus HTTP transport comes up, a manual re-run must observe
      # it (proving the probe reaches a REAL bus). If the bus can't boot in this VM,
      # the honest degrade path above already stands — so this stays best-effort and
      # never fails the test on a bus-boot limitation.
      with subtest("best-effort: a reachable Model Bus yields model_bus=ok on re-run"):
          try:
              robotnode.wait_for_open_port(6790, timeout=180)
              bus_up = True
          except Exception as e:
              print("[robot-probe test] Model Bus port did not open, "
                    "keeping the degrade-honest assertions only:", e)
              bus_up = False
          if bus_up:
              # restartIfChanged=false only guards nixos-rebuild; an explicit restart
              # re-runs ExecStart now that the bus is up.
              robotnode.succeed("systemctl restart hart-robot-probe.service")
              robotnode.wait_for_unit("hart-robot-probe.service", timeout=120)
              _, kv2 = read_status()
              assert kv2.get("model_bus") == "ok", \
                  f"a reachable bus must read model_bus=ok, got {kv2.get('model_bus')!r}"
    '';
  };
}
