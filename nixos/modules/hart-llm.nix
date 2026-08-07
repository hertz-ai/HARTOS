{ config, lib, pkgs, ... }:

# HART OS Local LLM Module
# llama.cpp server for local inference with GPU support
# Ported from deploy/linux/systemd/hart-llm.service
# Only starts when a model file exists

let
  cfg = config.hart;

  # GPU-accelerated llama.cpp. Vulkan is the UNIVERSAL GPU backend -- Intel iGPU (mesa
  # ANV), AMD (RADV), NVIDIA (its Vulkan ICD) -- so ONE build offloads on any GPU and
  # cleanly falls back to CPU. The old flake `.default` was CPU-only, so EVERY layer ran
  # on the CPU = the "assistant keeps thinking" slowness. The Vulkan RUNTIME is already
  # present (desktop.nix `hardware.graphics.enable` -> mesa ICDs in /run/opengl-driver).
  # This is the NixOS analogue of the Nunba companion's "CPU prebundled, GPU when present"
  # bootstrap: one universal binary, GPU-or-CPU decided at launch, no runtime download.
  llama-server = pkgs.llama-cpp.override { vulkanSupport = true; };

  # GPU-aware launcher. A Vulkan-built llama-server with --n-gpu-layers>0 but NO Vulkan
  # device ERRORS out at load (and would crash-loop under Restart=on-failure), so gate
  # -ngl on a real render node + a Vulkan ICD actually being present; otherwise pure-CPU.
  llamaLauncher = pkgs.writeShellScriptBin "hart-llm-server" ''
    set -eu
    # CPU-thread budget. 0 = AUTO = leave one core for the rest of the OS (nproc - 1, min
    # 1) so CPU-FALLBACK inference never saturates every core and starves the interactive
    # desktop/shell. (The systemd CPUWeight + Nice below keep it a background citizen on the
    # SHARED cores too -- the desktop always wins under contention, spare CPU is still used
    # when it is idle.)
    THREADS="${toString config.hart.llm.threads}"
    if [ "$THREADS" -le 0 ]; then
      THREADS=$(( $(${pkgs.coreutils}/bin/nproc) - 1 ))
      [ "$THREADS" -lt 1 ] && THREADS=1
    fi
    NGL=""
    if [ -e /dev/dri/renderD128 ] && ls /run/opengl-driver/share/vulkan/icd.d/*.json >/dev/null 2>&1; then
      NGL="--n-gpu-layers ${toString config.hart.llm.gpuLayers}"
      echo "hart-llm: Vulkan GPU present -> offloading ${toString config.hart.llm.gpuLayers} layers ($THREADS CPU threads)" >&2
    else
      echo "hart-llm: no Vulkan GPU -> CPU inference ($THREADS threads, one core left for the OS)" >&2
    fi
    exec ${llama-server}/bin/llama-server \
      --model "${config.hart.llm.modelPath}" \
      --port "${toString cfg.ports.llm}" \
      --ctx-size "${toString config.hart.llm.contextSize}" \
      --threads "$THREADS" \
      $NGL
  '';
in
{
  options.hart.llm = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.variant != "edge";
      description = "Enable local LLM inference (llama.cpp)";
    };

    modelPath = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.dataDir}/models/default.gguf";
      description = "Path to GGUF model file";
    };

    contextSize = lib.mkOption {
      type = lib.types.int;
      # 12288, NOT 4096 — this MUST equal core/constants.py::LLAMA_CTX_SIZE_DEFAULT.
      # That constant is the wire-trim layer's budget ceiling (llm_outbound_logger.py
      # left-trims to `(n_ctx / slots) - max_tokens - safety`), and its own comment
      # already says it "must match the --ctx-size cmdline". At 4096 the two DISAGREED
      # by 3x: the trim layer believed it had 12288, trimmed to a target the server
      # could not accept, and llama-server rejected the request — so the
      # "zero-tolerance context overflow" guard was computing against a ceiling that
      # did not exist.
      #
      # Measured on 1,407 real requests (~/Documents/Nunba/logs/llm_outbound.jsonl,
      # 2026-08-07): 78.7% overflow at 4096, 2.1% at 12288, 0% at 16384. The overflow
      # is NOT runaway history (~95 tok) — it is a ~2,229-tok system prompt plus a
      # ~2,029-tok task, i.e. a two-message request born over a 4096 limit with
      # nothing to trim. Raising to 12288 is not a new number; it is ending a drift
      # against the value the Python side already assumes.
      #
      # The residual 2.1% are the requests carrying the 67-tool schema block
      # (~10,713 tok), which _trim_body_for_ctx does not count at all because it only
      # walks body['messages'] — tracked separately; do NOT paper over it by pushing
      # this to 16384, since n_ctx costs KV-cache RAM on the CPU-only potato floor.
      default = 12288;
      description = ''
        Context window size (n_ctx) passed to llama-server as --ctx-size.
        MUST equal core/constants.py::LLAMA_CTX_SIZE_DEFAULT — the wire-layer trim
        budget is derived from that constant, so a mismatch silently reintroduces
        context-overflow rejections. Guarded by
        tests/unit/test_source_guard_llama_ctx_size_agrees.py.
      '';
    };

    threads = lib.mkOption {
      type = lib.types.int;
      default = 0;
      description =
        "CPU threads for inference. 0 = AUTO: leave one core for the rest of the OS "
        + "(nproc - 1, min 1) so CPU-fallback inference never starves the interactive "
        + "desktop/shell. Set a positive value to pin it.";
    };

    gpuLayers = lib.mkOption {
      type = lib.types.int;
      default = 999;
      description =
        "Layers to offload to the (Vulkan) GPU when one is present. 999 = offload ALL "
        + "(llama.cpp clamps to the model's real layer count). The launcher only passes "
        + "this when a Vulkan device actually exists, else it runs pure-CPU.";
    };

    autoProvision = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description =
        "On first boot, download the default GGUF to `modelPath` if no model is present, "
        + "so the local LLM is usable out of the box (an agent call from the Nunba UI hits "
        + "the LOCAL llama, never a remote proxy). This REUSES the legacy first-boot model "
        + "mechanism (deploy/distro/first-boot/hart-first-boot.sh: curl the default GGUF via "
        + "`HART_DEFAULT_MODEL_URL`) — it does NOT reinvent the download. Best-effort + "
        + "network-gated + non-boot-critical: on an offline live USB the fetch fails fast and "
        + "the LLM simply stays gated (the shell degrades to a static UI), it never stalls "
        + "boot. Set false for offline / bring-your-own-model nodes.";
    };

    modelUrl = lib.mkOption {
      type = lib.types.str;
      default =
        "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/"
        + "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf";
      description =
        "Default GGUF URL the first-boot provisioner fetches when `autoProvision` is on and no "
        + "model exists. Same model + same contract as the legacy hart-first-boot.sh; override "
        + "per-node with the `HART_DEFAULT_MODEL_URL` env in /etc/hart/hart.env (the env wins, "
        + "matching the legacy script) so a steward can point at any OpenAI-compatible GGUF.";
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.llm.enable) {

    # NVIDIA GPU support (declarative — the NixOS way)
    hardware.nvidia = lib.mkIf (builtins.pathExists "/dev/nvidia0") {
      open = true;  # Use open-source kernel modules (Turing+)
    };

    # ── First-boot model provisioner ──
    # Closes the "no model -> hart-llm never starts (ConditionPathExists)" gap so
    # the local LLM is usable out of the box. REUSES the legacy first-boot model
    # mechanism (deploy/distro/first-boot/hart-first-boot.sh fetched the default
    # GGUF via `HART_DEFAULT_MODEL_URL`); the NixOS first-boot port had dropped
    # that step. This is NOT a new downloader — same model, same env contract,
    # same curl-with-retry shape the nunba launcher + first-boot script use.
    #
    # Boot-safety: it is `before` hart-llm (so the model has landed before
    # llama-server's condition is checked) but it is NOT ordered before anything
    # the shell needs — hart.target only Wants= it, so a slow download delays only
    # hart-llm, never the desktop. Offline live-USB: curl fails fast (bounded by
    # --connect-timeout / --speed-limit), the unit exits 0 (offline is not a
    # fault), and hart-llm stays gracefully gated until a model is provided.
    systemd.services.hart-llm-provision = lib.mkIf config.hart.llm.autoProvision {
      description = "HART OS Local LLM model provisioner (first boot)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      # Needs the network for the fetch — declare the network-online wait LOCALLY
      # (per hart-base's rule) so it never leaks into the boot-critical path.
      wants = [ "network-online.target" ];
      after = [ "network-online.target" "hart-first-boot.service" ];
      before = [ "hart-llm.service" ];
      wantedBy = [ "hart.target" ];

      # Idempotent across reboots: only run when the model is absent. Once it has
      # landed, this is skipped (condition = success) and hart-llm starts normally.
      unitConfig = {
        ConditionPathExists = "!${config.hart.llm.modelPath}";
      };

      # curl is NOT on the unit's minimal PATH.
      path = with pkgs; [ curl coreutils ];

      environment = {
        # Default URL; the EnvironmentFile below can override HART_DEFAULT_MODEL_URL
        # (legacy contract: the env wins over the baked default).
        HART_DEFAULT_MODEL_URL = config.hart.llm.modelUrl;
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "hart";
        Group = "hart";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        ExecStart = pkgs.writeShellScript "hart-llm-provision" ''
          set -euo pipefail
          MODEL_PATH="${config.hart.llm.modelPath}"
          MODEL_DIR="$(dirname "$MODEL_PATH")"
          URL="''${HART_DEFAULT_MODEL_URL:-${config.hart.llm.modelUrl}}"

          mkdir -p "$MODEL_DIR"

          # Belt-and-suspenders with ConditionPathExists: never clobber an existing
          # (user- or previously-provisioned) model.
          if [ -s "$MODEL_PATH" ]; then
            echo "[HART OS LLM] Model already present: $MODEL_PATH"
            exit 0
          fi

          echo "[HART OS LLM] Provisioning default model from $URL"
          TMP="$MODEL_PATH.part"
          rm -f "$TMP"

          # Same curl contract as the legacy first-boot script + the nunba launcher:
          # follow redirects, retry, bounded connect, and abort a stalled transfer
          # (< 1 KiB/s for 60s) so a dead link never hangs the unit.
          if curl -fL --retry 3 --connect-timeout 30 --speed-time 60 --speed-limit 1024 \
                 -o "$TMP" "$URL"; then
            mv -f "$TMP" "$MODEL_PATH"   # atomic publish — hart-llm never sees a partial file
            echo "[HART OS LLM] Model provisioned: $MODEL_PATH"
          else
            rm -f "$TMP"
            # Offline / fetch failure is NOT a unit failure — the LLM stays gated and
            # the next boot re-attempts (ConditionPathExists is re-evaluated). Exit 0
            # so the journal does not show a spurious failed unit on every offline boot.
            echo "[HART OS LLM] Model download failed (offline?) — LLM stays gated until a model is provided." >&2
            exit 0
          fi
        '';

        # A large model on a slow link can take minutes; the curl speed-limit
        # guards a wedged transfer, so cap generously rather than the 90s default.
        TimeoutStartSec = "30min";

        # Security hardening (mirrors hart-llm's posture; writes ONLY the model dir).
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "${cfg.dataDir}/models" ];
        PrivateTmp = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        SystemCallFilter = [ "@system-service" ];
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-llm-provision";
      };
    };

    systemd.services.hart-llm = {
      description = "HART OS Local LLM (llama.cpp)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      # `after` the provisioner so that, on the very first boot, llama-server only
      # starts once the default model has landed (its ConditionPathExists below
      # gates it on the model file). The provisioner is gated on autoProvision —
      # when it is off (or already a no-op because the model exists) this After=
      # is satisfied immediately, so hart-llm starts normally. Referencing a unit
      # that may not exist is harmless: systemd ignores an absent After= dep.
      after = [ "network.target" "hart-first-boot.service" "hart-llm-provision.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      unitConfig = {
        # Only start if a model file exists
        ConditionPathExists = config.hart.llm.modelPath;
      };

      environment = {
        HART_LLM_PORT = toString cfg.ports.llm;
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        # The GPU-aware launcher (see `llamaLauncher` in the `let` above): offloads to the
        # Vulkan GPU when one is present, pure-CPU otherwise. Replaces the old bare
        # llama-server call that passed NO --n-gpu-layers, so every layer ran on the CPU.
        ExecStart = "${llamaLauncher}/bin/hart-llm-server";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 10;
        TimeoutStartSec = 60;

        # GPU access
        SupplementaryGroups = [ "video" "render" ];

        # Bind the local LLM port. In OS mode cfg.ports.llm is 808 — a PRIVILEGED
        # port (<1024) — but llama-server runs as the unprivileged `hart` user, so
        # without this it dies "bind: permission denied" and crash-loops under
        # Restart=on-failure (the local LLM is then never reachable, every agent
        # call from the Nunba UI fails). Grant exactly CAP_NET_BIND_SERVICE so it
        # can bind <1024 and nothing else; the bounding set tightens hardening
        # (the unit had none before, inheriting all caps). Harmless when a variant
        # sets a >=1024 port — llama needs no cap there and never uses this one.
        # Same pattern as hart-discovery.nix / hart-compute-mesh.nix. Ambient caps
        # coexist with NoNewPrivileges=true (systemd raises them for the exec).
        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
        CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "${cfg.dataDir}/models" ];
        PrivateTmp = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = false;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;

        # Resource limits. The LLM is the heaviest service, but it must NOT starve the
        # interactive desktop/shell when it falls back to CPU-only inference. So it is a
        # deliberate BACKGROUND citizen: CPUWeight 50 (BELOW the default 100 the UI services
        # run at, so they win the shared cores under contention) + Nice 10, paired with the
        # launcher's "leave one core for the OS" thread budget. Spare CPU is still used when
        # the desktop is idle; the desktop just always wins when it needs it. (Was
        # CPUWeight 150 -- ABOVE the UI -- which would let CPU-fallback inference stall the
        # whole desktop, the very thing the steward flagged.)
        MemoryMax = "8G";
        CPUWeight = 50;
        TasksMax = 64;
        IOWeight = 50;
        Nice = 10;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-llm";
      };
    };
  };
}
