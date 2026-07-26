# ═══════════════════════════════════════════════════════════════
# HART OS — Local LLM reachability + first-boot model provision nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the A4 compat shim (P0b) on the OS side: the local LLM is BINDABLE and
# PROVISIONED, so an agent call from the Nunba UI resolves to on-device llama,
# never a remote proxy.
#
# What it asserts (behaviour, on a booted VM):
#   1. PROVISION: with hart.llm.modelUrl pointed at a node-local file server, the
#      hart-llm-provision oneshot fetches the GGUF and ATOMICALLY publishes it to
#      modelPath — REUSING the legacy first-boot download contract (curl +
#      HART_DEFAULT_MODEL_URL), not a new downloader. No leftover .part file.
#   2. IDEMPOTENT: a second run is a no-op (model already present, never clobbered).
#   3. BINDABLE: the hart-llm unit carries CAP_NET_BIND_SERVICE so llama-server (as
#      the unprivileged hart user) can bind the privileged OS-mode port (<1024) —
#      without it the LLM crash-loops on bind and is never reachable.
#   4. ORDERING: the provisioner is non-boot-critical — it declares its own
#      network-online wait and orders only BEFORE hart-llm, never before the
#      backend/shell, so a slow model download cannot stall the desktop.
#
# Honest-hardware-limit: a fake GGUF cannot actually be *loaded* by llama-server,
# so this proves the provision + atomic publish + the cap/ordering WIRING (the
# parts that ship), not a real token. Real inference on the privileged port is a
# real-HW check. `[VM]` — cannot run on the Windows dev box; gates in CI / local
# QEMU. Needs flake.nix / the test workflow to register it (held-file follow-up,
# like notify.nix); it is self-contained so it evaluates standalone.
#
# #70 discipline: built from `hartModules` via the shared `mkNode` (./lib.nix) and
# imports ../modules/hart-llm.nix directly so it runs whether or not flake.nix has
# registered the module yet.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # A directory served over HTTP carrying a stand-in GGUF the provisioner fetches.
  fakeModelDir = pkgs.writeTextDir "default.gguf" "FAKE-GGUF-BYTES-not-a-real-model";
  modelServerPort = 8099;
in
{
  hart-llm-provision = pkgs.testers.runNixOSTest {
    name = "hart-llm-provision";
    # Same runtime-injected node-global false positives the other hart tests
    # document; the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    # "server" variant: hart.llm.enable defaults true and there is no GUI to bring
    # up — keeps the test focused on the LLM units.
    nodes.llmnode = mkNode "server" {
      imports = [ ../modules/hart-llm.nix ];

      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };

      hart.llm.enable = true;
      # Point the provisioner at the node-local file server (no real internet in
      # the VM). This is the SAME modelUrl option a steward would override.
      hart.llm.modelUrl = "http://127.0.0.1:${toString modelServerPort}/default.gguf";

      # Serve the stand-in model so the provisioner's curl has a target.
      systemd.services.fake-model-server = {
        description = "stand-in GGUF file server (test only)";
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          ExecStart = "${pkgs.python3}/bin/python -m http.server ${toString modelServerPort} --directory ${fakeModelDir}";
          Restart = "on-failure";
        };
      };
    };

    testScript = ''
      llmnode = machines[0]
      llmnode.start()
      llmnode.wait_for_unit("multi-user.target")
      llmnode.wait_for_unit("fake-model-server.service")
      # the file server is listening before we drive the provisioner
      llmnode.wait_until_succeeds(
          "curl -fsS http://127.0.0.1:${toString modelServerPort}/default.gguf >/dev/null")

      model = "/var/lib/hart/models/default.gguf"

      with subtest("1. provisioner fetches + atomically publishes the model"):
          # Run the REAL provisioner unit (manual start is robust against
          # network-online timing in the VM; the unit + script are unchanged).
          llmnode.succeed("systemctl start hart-llm-provision.service")
          llmnode.wait_for_unit("hart-llm-provision.service")
          llmnode.succeed(f"test -s {model}")
          llmnode.succeed(f"grep -q FAKE-GGUF-BYTES {model}")
          # Atomic publish: the .part temp must be gone.
          llmnode.fail(f"test -e {model}.part")

      with subtest("2. idempotent — a re-run never clobbers an existing model"):
          llmnode.succeed(f"echo USER-MODEL > {model}")
          llmnode.succeed("systemctl restart hart-llm-provision.service || true")
          # ConditionPathExists=!model means the unit is skipped; the file is intact.
          out = llmnode.succeed(f"cat {model}")
          assert "USER-MODEL" in out, f"provisioner clobbered an existing model: {out!r}"

      with subtest("3. llama unit is bindable on a privileged port (CAP_NET_BIND_SERVICE)"):
          unit = llmnode.succeed("systemctl cat hart-llm.service")
          assert "CAP_NET_BIND_SERVICE" in unit, \
              "hart-llm must grant CAP_NET_BIND_SERVICE to bind the <1024 OS port:\n" + unit

      with subtest("4. provisioner is non-boot-critical (orders only before hart-llm)"):
          before = llmnode.succeed("systemctl show -p Before hart-llm-provision.service")
          assert "hart-llm.service" in before, before
          # It must NOT gate the realtime backend or the shell.
          assert "hart-backend.service" not in before, before
          assert "hart-liquid-ui.service" not in before, before
          wants = llmnode.succeed("systemctl show -p Wants hart-llm-provision.service")
          assert "network-online.target" in wants, wants
    '';
  };
}
