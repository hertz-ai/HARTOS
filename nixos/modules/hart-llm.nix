{ config, lib, pkgs, ... }:

# HART OS Local LLM Module
# llama.cpp server for local inference with GPU support
# Ported from deploy/linux/systemd/hart-llm.service
# Only starts when a model file exists

let
  cfg = config.hart;

  # ── llama.cpp: keep the fleet baseline, ADD a floor for pre-Haswell nodes ────
  # nixpkgs' stock llama-cpp is a DELIBERATE avx2 baseline, not an accident of
  # whichever machine built it: the derivation passes `-DGGML_NATIVE:BOOL=FALSE`
  # (verified with `nix derivation show`), and in ggml's CMakeLists NATIVE=OFF
  # while NATIVE_DEFAULT=ON sets `INS_ENB=ON`, which turns GGML_AVX2 and GGML_FMA
  # ON (AVX512 stays off). For most of the fleet -- Haswell (2013) and newer --
  # those kernels are a large speedup and MUST be kept.
  #
  # They are also fatal on anything older. The HART box is an i7-3630QM (Ivy
  # Bridge, 2012): avx + f16c + sse4.2, but NO avx2/fma/bmi2. Its libggml-cpu.so
  # carried 1028 avx2/fma instructions, so llama-server took an illegal
  # instruction (SIGILL, status 4/ILL) and core-dumped at startup, before binding
  # cfg.ports.llm. Under Restart=on-failure that was a crash loop: 6762 restarts
  # over ~19h (2026-08-24), the node had no local inference, and every goal agent
  # stalled.
  #
  # The "Ivy Bridge Vulkan incomplete" journal lines sitting above each dump are
  # only a PARTIAL red herring, and the difference is worth stating because it
  # was measured rather than assumed. Building the Vulkan variant WITH this ISA
  # fix and running it on the box (2026-08-24) separates the two:
  #   * `llama-server --version`, which used to core-dump, now prints normally
  #     -- so the STARTUP crash was the avx2 libggml-cpu.so, not Vulkan;
  #   * but that same binary still cannot SERVE here: it fails model load and
  #     dies with "vkDestroyFence: Invalid device", because the HD 4000's Mesa
  #     ANV driver is incomplete on Ivy Bridge.
  # So there are TWO independent faults on this hardware and neither explanation
  # covers the other. Fixing the ISA alone would still leave this node unable to
  # serve; dropping Vulkan alone would still leave it SIGILLing.
  #
  # llama.cpp b4154 has no runtime CPU dispatch (GGML_CPU_ALL_VARIANTS and
  # GGML_BACKEND_DL do not exist in this source tree -- checked), so ONE binary
  # cannot serve both CPUs. Hence two variants and a per-boot choice. Building
  # only the portable one would fix this 2012 laptop by taking avx2/fma AND GPU
  # offload away from every capable node in the fleet, which is a far bigger
  # regression than the bug.
  llamaFast = pkgs.llama-cpp.override { vulkanSupport = true; };

  # The floor: exactly the configuration PROVEN to run on the box (avx/f16c only,
  # no Vulkan). Vulkan is dropped HERE ONLY, and by experiment rather than by
  # assumption -- see above: with the ISA fix applied, a Vulkan build STILL fails
  # model load on this hardware ("vkDestroyFence: Invalid device"). Capable nodes
  # keep Vulkan via llamaFast above; this variant is the one that has to boot on
  # a machine whose GPU driver cannot be trusted.
  llamaPortable = (pkgs.llama-cpp.override { vulkanSupport = false; }).overrideAttrs (o: {
    cmakeFlags = (o.cmakeFlags or [ ]) ++ [
      "-DGGML_NATIVE=OFF"
      "-DGGML_AVX2=OFF"
      "-DGGML_FMA=OFF"
      "-DGGML_BMI2=OFF"
      "-DGGML_AVX512=OFF"
      "-DGGML_AVX=ON"
      "-DGGML_F16C=ON"
    ];
  });

  # THE dispatcher, and it is deliberately named `llama-server`: HARTOS's own
  # ModelLifecycleManager._find_llama_server_binary() falls back to
  # shutil.which('llama-server'), so putting this on PATH means every existing
  # consumer (vision/lightweight_backend's captioner, model_onboarding, the G3
  # direct-launch supervisor, mcp_server's model switch) transparently gets a
  # binary that is correct for the CPU it is running on. One decision point,
  # every caller, no per-node configuration and no second copy of the choice.
  #
  # Selection is on the CPU's ACTUAL advertised flags rather than a model name or
  # a build-time guess, so a fleet image boots correctly on hardware nobody has
  # tested yet. Adding a further tier later (e.g. pre-AVX) is one more branch.
  llamaDispatch = pkgs.writeShellScriptBin "llama-server" ''
    set -eu
    if ${pkgs.gnugrep}/bin/grep -qw avx2 /proc/cpuinfo \
       && ${pkgs.gnugrep}/bin/grep -qw fma /proc/cpuinfo; then
      exec ${llamaFast}/bin/llama-server "$@"
    fi
    exec ${llamaPortable}/bin/llama-server "$@"
  '';

  # Service launcher: supplies only what the OS owns (model path, declared port,
  # the ctx-size pinned to core/constants.py::LLAMA_CTX_SIZE_DEFAULT) and defers
  # the binary choice to the dispatcher. THREADS = nproc - 1 by default (0 = AUTO)
  # so inference leaves one core for the OS; with the systemd CPUWeight/Nice below
  # it stays a background citizen and never starves the interactive desktop.
  # No --n-gpu-layers: on a capable node llamaFast can still offload, but that is
  # a separate decision from "which binary runs" and the old presence-only gate
  # (renderD128 + an ICD file exists) was what put -ngl 999 on a GPU whose driver
  # is incomplete. Re-add it behind a probe that proves the GPU actually works.
  llamaLauncher = pkgs.writeShellScriptBin "hart-llm-server" ''
    set -eu
    # ── Work out the CPU budget FROM THIS MACHINE, at boot ────────────────────
    # This must be computed here, not baked by whoever built the image and not
    # hand-tuned per box: a user installs HART OS on hardware nobody has seen,
    # and it has to divide its own cores sensibly with no operator present.
    #
    # Count PHYSICAL cores, not nproc. nproc counts SMT siblings, so on the fleet
    # box (i7-3630QM: 4 cores / 8 threads) the old `nproc - 1` asked for SEVEN
    # threads on FOUR real cores. Measured 2026-08-24 with the desktop in use:
    # load average 10.26, llama-server at 457% CPU, the compositor fighting it at
    # 94%, and a trivial 16-token request returning NOTHING in 200s. A second
    # sibling on the same physical core adds contention and cache thrash to
    # llama.cpp's compute-bound GEMM rather than throughput, so that setting made
    # the desktop AND inference slower at once.
    CPUDIR=/sys/devices/system/cpu
    CORE_IDS=$(${pkgs.coreutils}/bin/cat $CPUDIR/cpu[0-9]*/topology/core_id 2>/dev/null \
                 | ${pkgs.coreutils}/bin/sort -n -u || true)
    NCORES=$(printf '%s\n' "$CORE_IDS" | ${pkgs.gnugrep}/bin/grep -c . || true)
    # Explicit if/fi, not `[ x ] && y`: this runs under `set -e`, and a trailing
    # test that evaluates false makes the enclosing list fail. A boot-critical
    # launcher must not be able to exit because a machine had a normal core count.
    if [ -z "$NCORES" ] || [ "$NCORES" -lt 1 ]; then
      NCORES=$(${pkgs.coreutils}/bin/nproc)
    fi

    # Give inference a MINORITY of the machine so interactive work keeps real
    # headroom rather than merely higher priority.
    NINFER=$(( NCORES * ${toString config.hart.llm.cpuSharePercent} / 100 ))
    if [ "$NINFER" -lt 1 ]; then NINFER=1; fi
    if [ "$NINFER" -gt "$NCORES" ]; then NINFER=$NCORES; fi

    # Take the LAST cores and leave the FIRST ones alone: the desktop and most
    # kernel work gravitate to low-numbered CPUs (the compositor was measured on
    # CPU 0), so counting from the top keeps inference off them. Include each
    # chosen core's SMT siblings, otherwise a sibling stays free to be scheduled
    # against us on the very core we are trying to own.
    PICK=$(printf '%s\n' "$CORE_IDS" | ${pkgs.coreutils}/bin/tail -n "$NINFER")
    CPUS=""
    for c in $CPUDIR/cpu[0-9]*; do
      cid=$(${pkgs.coreutils}/bin/cat "$c/topology/core_id" 2>/dev/null || echo "")
      if [ -z "$cid" ]; then continue; fi
      for p in $PICK; do
        if [ "$cid" = "$p" ]; then
          n=$(${pkgs.coreutils}/bin/basename "$c" | ${pkgs.gnugrep}/bin/grep -oE '[0-9]+')
          CPUS="''${CPUS:+$CPUS,}$n"
        fi
      done
    done

    THREADS="${toString config.hart.llm.threads}"
    if [ "$THREADS" -le 0 ]; then THREADS=$NINFER; fi

    echo "hart-llm: ''${NCORES} physical cores; inference gets ''${NINFER} (cpus ''${CPUS:-all}), ''${THREADS} threads" >&2

    # taskset pins the affinity from inside the unit, so the budget follows the
    # hardware instead of a static AllowedCPUs= that would be wrong on every
    # machine except the one it was written for.
    if [ -n "$CPUS" ] && [ "$NINFER" -lt "$NCORES" ]; then
      set -- ${pkgs.util-linux}/bin/taskset -c "$CPUS" ${llamaDispatch}/bin/llama-server
    else
      set -- ${llamaDispatch}/bin/llama-server
    fi
    exec "$@" \
      --model "${config.hart.llm.modelPath}" \
      --port "${toString cfg.ports.llm}" \
      --ctx-size "${toString config.hart.llm.contextSize}" \
      --threads "$THREADS" \
      --parallel ${toString config.hart.llm.parallelSlots}
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

    parallelSlots = lib.mkOption {
      type = lib.types.int;
      default = 1;
      description = ''
        Concurrent request slots passed to llama-server as --parallel.

        MUST be set explicitly. llama.cpp's "auto" is 4, and the slots SHARE the
        kv_unified pool, so 4 slots against --ctx-size 12288 give each request a
        quarter of the budget while the wire-layer trim in llm_outbound_logger.py
        is computing against the full n_ctx. Nunba's own launcher already caps
        this for the same reason ("overrides llama.cpp's auto (=4) that
        over-subscribed the shared kv_unified pool"); this module had simply
        never passed the flag.

        1 is right for the CPU floor: a node that can only run one request at a
        time in useful latency should queue the second rather than halve both.
        Raise it on a node with real parallel headroom.
      '';
    };

    cpuSharePercent = lib.mkOption {
      type = lib.types.int;
      default = 50;
      description = ''
        Percentage of the machine's PHYSICAL cores inference may use, resolved on
        the node at boot rather than baked into the image.

        This has to be computed per machine: HART OS gets installed on hardware
        nobody has seen, with no operator to tune it. 50 means "at most half the
        cores", which on the 4-core fleet box is 2 and on a 16-core workstation
        is 8. The launcher takes the HIGHEST-numbered cores (plus their SMT
        siblings) and pins with taskset, leaving the low-numbered CPUs, where the
        compositor and most kernel work land, untouched.

        Raise toward 100 on a dedicated inference node; lower it on a machine
        whose interactive feel matters more than local token rate.
      '';
    };

    cpuQuota = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "200%";
      description = ''
        OPTIONAL extra systemd CPUQuota ceiling (100% = one core).

        Normally unnecessary and deliberately null: `cpuSharePercent` already
        bounds inference by pinning it to a subset of cores computed on the node,
        and a quota written here would be a fixed number chosen by whoever edited
        this file, which is wrong on every machine except theirs.

        Kept because a quota bounds differently from affinity: affinity says
        WHICH cores, a quota says HOW MUCH of them, so a steward who wants
        inference to use half of two cores can say so. Note CPUWeight alone
        bounds nothing at all -- it only arbitrates once contention has already
        started, which is why the desktop was still losing at CPUWeight=50 while
        llama-server sat at 457% CPU (measured 2026-08-24).
      '';
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

    # Publish the ISA-correct llama.cpp on PATH — the discovery point HARTOS's
    # OWN model machinery already looks at, instead of this module being the only
    # thing that knows where the binary lives.
    #
    # `ModelLifecycleManager._find_llama_server_binary()` searches ~/.nunba and
    # ~/.trueflow, then falls back to `shutil.which('llama-server')`. On HART OS
    # neither app dir exists and, before this, PATH had no llama-server either —
    # `command -v llama-server` on the box returned nothing — so every HARTOS
    # consumer of that finder was silently degraded even while hart-llm.service
    # itself ran fine off its baked ExecStart path:
    #   • integrations/vision/lightweight_backend.py (which explicitly comments
    #     "reuse model_lifecycle's finder") got None -> "caption disabled";
    #   • integrations/service_tools/model_onboarding.py takes its "binary not
    #     found, downloading..." branch, which would fetch a GENERIC upstream
    #     build — i.e. an avx2/fma one that SIGILLs on this CPU, reintroducing the
    #     exact crash this module's cmakeFlags exist to prevent;
    #   • model_lifecycle's own G3 _launch_llama_server_direct supervision and
    #     mcp_server's runtime model switch had nothing to launch.
    # Publishing the DISPATCHER (not a fixed variant) makes the OS the supplier of
    # the binary and leaves *which flags / which port* to the code that already
    # owns those decisions (model_onboarding's "optimal params",
    # core.port_registry), rather than duplicating them here.
    environment.systemPackages = [ llamaDispatch ];

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
        # The launcher (see `llamaLauncher` in the `let` above). It supplies the
        # model path, the declared port and the pinned ctx-size, then execs the
        # per-CPU dispatcher, so this unit and anything that finds `llama-server`
        # on PATH run the same binary choice.
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
        # Optional hard ceiling; null by default because the launcher already
        # bounds inference by pinning it to a node-computed subset of cores.
        CPUQuota = lib.mkIf (config.hart.llm.cpuQuota != null) config.hart.llm.cpuQuota;
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
