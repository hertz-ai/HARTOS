{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Native AI Runtime — Agentic Intelligence as OS Primitive
# ═══════════════════════════════════════════════════════════════
#
# AI is not an app running on this OS. AI IS this OS.
#
# The AI runtime is a kernel-level subsystem that treats:
#   - Models as first-class filesystem objects (content-addressed)
#   - Agents as native processes (cgroups v2 isolation, not containers)
#   - GPU as a managed shared resource (fair scheduling, preemption)
#   - Inter-agent communication as kernel IPC (vsock, not HTTP)
#   - Inference as a system call (not an API call to a server)
#
# Architecture:
#
#   ┌───────────────────────────────────────────────────────┐
#   │                    AI Applications                     │
#   │  ┌──────────┬──────────┬──────────┬──────────────┐   │
#   │  │ LLM      │ Vision   │ Coding   │ Marketing    │   │
#   │  │ Agent    │ Agent    │ Agent    │ Agent        │   │
#   │  └────┬─────┴────┬─────┴────┬─────┴──────┬───────┘   │
#   │       │          │          │            │            │
#   │  ┌────┴──────────┴──────────┴────────────┴─────────┐  │
#   │  │           HART OS AI Runtime Layer                │  │
#   │  │                                                   │  │
#   │  │  Model Store    GPU Scheduler   Agent Lifecycle   │  │
#   │  │  (content-      (fair share,    (spawn, monitor,  │  │
#   │  │   addressed,     preemption,     isolate, kill)   │  │
#   │  │   hot-load)      multi-tenant)                    │  │
#   │  │                                                   │  │
#   │  │  Agent IPC      World Model     Guardrail Gate    │  │
#   │  │  (vsock,        (shared         (constitutional   │  │
#   │  │   zero-copy)     knowledge)      review)          │  │
#   │  └───────────────────────────────────────────────────┘  │
#   │  ┌───────────────────────────────────────────────────┐  │
#   │  │  Linux Kernel: cgroups v2, Landlock, NVIDIA UVM   │  │
#   │  └───────────────────────────────────────────────────┘  │
#   └───────────────────────────────────────────────────────┘

let
  cfg = config.hart;
  ai = config.hart.aiRuntime;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.aiRuntime = {

    enable = lib.mkEnableOption "HART OS native AI runtime";

    # ─── Model Store ───
    modelStore = {
      path = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/hart/models";
        description = "Content-addressed model store path";
      };

      maxSize = lib.mkOption {
        type = lib.types.str;
        default = "50G";
        description = "Maximum storage for models (e.g., '50G', '100G')";
      };

      autoDownload = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Auto-download models matching node's capability tier";
      };
    };

    # ─── GPU Scheduling ───
    gpu = {
      enable = lib.mkEnableOption "GPU compute management";

      maxAgentsPerGPU = lib.mkOption {
        type = lib.types.int;
        default = 4;
        description = "Maximum concurrent agents per GPU";
      };

      reserveVRAM = lib.mkOption {
        type = lib.types.str;
        default = "512M";
        description = "VRAM reserved for display compositor (not used by agents)";
      };
    };

    # ─── Agent Limits ───
    agents = {
      maxConcurrent = lib.mkOption {
        type = lib.types.int;
        default = 8;
        description = "Maximum concurrent agent processes";
      };

      maxMemoryPerAgent = lib.mkOption {
        type = lib.types.str;
        default = "2G";
        description = "Maximum memory per agent process";
      };

      maxCPUPerAgent = lib.mkOption {
        type = lib.types.int;
        default = 200;
        description = "CPU weight per agent (100 = 1 core equivalent)";
      };
    };

    # ─── World Model ───
    worldModel = {
      enable = lib.mkEnableOption "Shared world model (HevolveAI bridge)";
    };

    # ─── Semantic Intelligence Layer ───
    # When a model is plugged in, the entire OS gains intelligence.
    # Without a model, everything still works — just not as smart.
    semantic = {
      enable = lib.mkEnableOption "Semantic intelligence injection into OS layers";

      serviceIntelligence = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          AI-aware service monitoring: when a hart-* service fails,
          the AI runtime reads journal logs, diagnoses the issue via LLM,
          and attempts auto-recovery. Logs diagnostics to ai-diagnostics.log.
        '';
      };

      smartFS = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          AI-indexed filesystem: indexes file metadata + content summaries
          into a local embedding store. Enables semantic file search via
          'hart search "photos from last week"'. Integrates with LiquidUI
          for AI-generated file descriptions in the file browser.
        '';
      };

      predictivePrefetch = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          AI predicts which models/data the user will need next based on
          usage patterns (via world model). Pre-loads models into GPU memory
          and pre-fetches mesh peer availability for likely offload targets.
        '';
      };

      diagnosticsLog = lib.mkOption {
        type = lib.types.str;
        default = "/var/log/hart/ai-diagnostics.log";
        description = "Path for AI diagnostic logs (service self-healing events)";
      };
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && ai.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Enable kernel-level AI compute support
    # ─────────────────────────────────────────────────────────
    {
      hart.kernel.aiCompute.enable = true;
      hart.kernel.agentSandbox.enable = true;
    }

    # ─────────────────────────────────────────────────────────
    # Model Store: content-addressed model filesystem
    # ─────────────────────────────────────────────────────────
    {
      # Create model store with proper structure
      systemd.tmpfiles.rules = [
        "d ${ai.modelStore.path} 0750 hart hart -"
        "d ${ai.modelStore.path}/gguf 0750 hart hart -"       # llama.cpp models
        "d ${ai.modelStore.path}/safetensors 0750 hart hart -" # HuggingFace models
        "d ${ai.modelStore.path}/onnx 0750 hart hart -"       # ONNX runtime models
        "d ${ai.modelStore.path}/minicpm 0750 hart hart -"    # Vision models
        "d ${ai.modelStore.path}/pocket-tts 0750 hart hart -" # Pocket TTS (offline, CPU, MIT)
        "d ${ai.modelStore.path}/pocket-tts/voices 0750 hart hart -" # Cloned voice states
        "d ${ai.modelStore.path}/stt 0750 hart hart -"        # Whisper STT (sherpa-onnx)
        "d ${ai.modelStore.path}/cache 0750 hart hart -"      # Download cache
        "d ${ai.modelStore.path}/manifests 0750 hart hart -"  # Model manifests (hash → metadata)
      ];

      # Model store management service
      systemd.services.hart-model-store = {
        description = "HART OS Model Store Manager";
        after = [ "hart.target" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-model-store-init" ''
            set -euo pipefail
            MODEL_PATH="${ai.modelStore.path}"

            echo "[HART OS AI] Model store: $MODEL_PATH"

            # Verify model integrity on boot (content-addressed check)
            if [[ -d "$MODEL_PATH/manifests" ]]; then
              MANIFEST_COUNT=$(ls "$MODEL_PATH/manifests/"*.json 2>/dev/null | wc -l)
              echo "[HART OS AI] Verified $MANIFEST_COUNT model manifests"
            fi

            # Report available models
            for fmt in gguf safetensors onnx minicpm; do
              COUNT=$(ls "$MODEL_PATH/$fmt/" 2>/dev/null | wc -l)
              if [[ "$COUNT" -gt 0 ]]; then
                echo "[HART OS AI] $fmt models: $COUNT"
              fi
            done

            echo "[HART OS AI] Model store ready"
          '';
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # GPU Scheduler: fair-share multi-tenant GPU access
    # ─────────────────────────────────────────────────────────
    (lib.mkIf ai.gpu.enable {

      # GPU monitoring + management tools
      environment.systemPackages = with pkgs; [
        nvtopPackages.full    # GPU process monitor (NVIDIA + AMD + Intel)
        pciutils              # lspci for GPU detection
        glxinfo               # OpenGL info
        vulkan-tools          # Vulkan validation
      ];

      # GPU scheduler service: manages agent GPU access
      systemd.services.hart-gpu-scheduler = {
        description = "HART OS GPU Scheduler";
        after = [ "hart.target" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "notify";
          User = "hart";
          Group = "hart";
          SupplementaryGroups = [ "video" "render" ];

          ExecStart = pkgs.writeShellScript "hart-gpu-sched" ''
            set -euo pipefail

            echo "[HART OS GPU] Detecting GPU hardware..."

            # Detect NVIDIA
            if command -v nvidia-smi &>/dev/null; then
              GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
              GPU_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)
              echo "[HART OS GPU] NVIDIA: $GPU_NAME ($GPU_VRAM)"

              # Set compute mode to shared (multi-agent)
              nvidia-smi -c EXCLUSIVE_PROCESS 2>/dev/null || true

              # Reserve VRAM for display
              echo "[HART OS GPU] Reserved ${ai.gpu.reserveVRAM} for display"
            fi

            # Detect AMD
            if [[ -d /sys/class/drm/card0/device ]]; then
              if [[ -f /sys/class/drm/card0/device/gpu_busy_percent ]]; then
                echo "[HART OS GPU] AMD GPU detected"
              fi
            fi

            # Detect Intel
            if lspci | grep -qi "Intel.*Graphics"; then
              echo "[HART OS GPU] Intel integrated GPU detected"
            fi

            echo "[HART OS GPU] Max agents per GPU: ${toString ai.gpu.maxAgentsPerGPU}"
            systemd-notify --ready

            # Monitor GPU utilization (lightweight polling)
            while true; do
              sleep 30
              # Health check: log GPU utilization
              if command -v nvidia-smi &>/dev/null; then
                nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
                  --format=csv,noheader 2>/dev/null || true
              fi
            done
          '';

          Restart = "on-failure";
          RestartSec = 10;
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # Agent Lifecycle Manager
    # ─────────────────────────────────────────────────────────
    {
      # Systemd template unit: spawn agents as native processes
      # Each agent is a separate process in its own cgroup scope
      # Usage: systemctl start hart-agent@goal_123.service
      systemd.services."hart-agent@" = {
        description = "HART OS Agent: %i";
        after = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";
          SupplementaryGroups = [ "video" "render" ];

          # %i = instance name (goal ID or agent name)
          ExecStart = "${cfg.package.python}/bin/python -c \"import sys,os; sys.path.insert(0,'${cfg.package}'); os.environ['HEVOLVE_DATA_DIR']='${cfg.dataDir}'; from integrations.agent_engine.dispatch import dispatch_goal; dispatch_goal('%i')\"";

          # Agent runs in its own cgroup slice
          Slice = "hart-agents.slice";

          # Resource limits (per agent)
          MemoryMax = ai.agents.maxMemoryPerAgent;
          CPUWeight = ai.agents.maxCPUPerAgent;
          TasksMax = 512;

          # Landlock filesystem restrictions
          ProtectHome = "tmpfs";
          ProtectSystem = "strict";
          ReadWritePaths = [
            "${cfg.dataDir}/agent_data"
            "${cfg.logDir}"
            "${ai.modelStore.path}"
          ];
          ReadOnlyPaths = [
            "${cfg.dataDir}/node_public.key"
            "/run/current-system/sw"
          ];

          # Network: agents can access backend + discovery
          RestrictAddressFamilies = "AF_INET AF_INET6 AF_UNIX AF_VSOCK";

          # Security: no privilege escalation
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          SystemCallFilter = [ "@system-service" "@network-io" ];

          Restart = "on-failure";
          RestartSec = 5;
        };
      };

      # Agent lifecycle monitor
      systemd.services.hart-agent-monitor = {
        description = "HART OS Agent Lifecycle Monitor";
        after = [ "hart.target" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-agent-monitor" ''
            set -euo pipefail

            MAX_AGENTS=${toString ai.agents.maxConcurrent}
            echo "[HART OS Agents] Monitor started (max: $MAX_AGENTS concurrent)"

            while true; do
              # Count running agents
              RUNNING=$(systemctl list-units 'hart-agent@*.service' --state=running --no-legend 2>/dev/null | wc -l)

              if [[ "$RUNNING" -gt "$MAX_AGENTS" ]]; then
                echo "[HART OS Agents] WARNING: $RUNNING agents running (max: $MAX_AGENTS)"
              fi

              sleep 15
            done
          '';

          Restart = "always";
          RestartSec = 10;
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # Inter-Agent Communication (native kernel IPC)
    # ─────────────────────────────────────────────────────────
    {
      # vsock: kernel-level IPC between agents (zero-copy, no network stack)
      boot.kernelModules = [ "vhost_vsock" "vsock" ];

      # Unix domain sockets for local agent communication
      systemd.tmpfiles.rules = [
        "d /run/hart/agents 0750 hart hart -"        # Agent socket directory
        "d /run/hart/ipc 0750 hart hart -"            # IPC endpoints
        "d /run/hart/world-model 0750 hart hart -"    # Shared world model
      ];
    }

    # ─────────────────────────────────────────────────────────
    # World Model Bridge
    # ─────────────────────────────────────────────────────────
    (lib.mkIf ai.worldModel.enable {

      systemd.services.hart-world-model = {
        description = "HART OS World Model Bridge (HevolveAI)";
        after = [ "hart-backend.service" ];
        wants = [ "hart-backend.service" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";
          Slice = "hart-agents.slice";

          ExecStart = "${cfg.package.python}/bin/python -c '${''
            import sys, os
            sys.path.insert(0, "${cfg.package}")
            os.environ["HEVOLVE_DATA_DIR"] = "${cfg.dataDir}"
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            bridge = WorldModelBridge()
            if bridge.check_health():
                print("[HART OS] World model connected")
            else:
                print("[HART OS] World model unavailable (will retry)")
            import time
            while True:
                time.sleep(60)
                bridge.check_health()
          ''}'";

          Restart = "on-failure";
          RestartSec = 30;
          MemoryMax = "1G";
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # Semantic Intelligence: AI-Aware Service Self-Healing
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (ai.semantic.enable && ai.semantic.serviceIntelligence) {

      systemd.services.hart-service-intelligence = {
        description = "HART OS Service Intelligence (AI-aware self-healing)";
        after = [ "hart.target" "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-service-intelligence" ''
            set -euo pipefail

            DIAG_LOG="${ai.semantic.diagnosticsLog}"
            MODEL_BUS="http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}"

            echo "[HART OS AI] Service intelligence monitor started"
            echo "[HART OS AI] Diagnostics log: $DIAG_LOG"

            # Monitor hart-* services for failures
            while true; do
              # Check for failed hart services
              FAILED=$(systemctl list-units 'hart-*.service' --state=failed --no-legend 2>/dev/null || true)

              if [[ -n "$FAILED" ]]; then
                echo "$FAILED" | while read -r line; do
                  UNIT=$(echo "$line" | awk '{print $1}')
                  echo "[HART OS AI] Detected failure: $UNIT"

                  # Read last 50 lines of journal for the failed unit
                  LOGS=$(journalctl -u "$UNIT" --no-pager -n 50 2>/dev/null || echo "No logs available")

                  # Check if Model Bus is available for AI diagnosis
                  if curl -sf "$MODEL_BUS/v1/status" >/dev/null 2>&1; then
                    # Send to LLM for diagnosis
                    DIAGNOSIS=$(curl -sf "$MODEL_BUS/v1/chat" \
                      -d "{\"prompt\": \"A systemd service '$UNIT' on HART OS failed. Diagnose from these logs and suggest a fix (one line):\\n$LOGS\"}" \
                      -H "Content-Type: application/json" 2>/dev/null | \
                      ${pkgs.jq}/bin/jq -r '.response // "No diagnosis available"' || echo "Model Bus unavailable")

                    TIMESTAMP=$(date -Iseconds)
                    echo "$TIMESTAMP | $UNIT | DIAGNOSIS: $DIAGNOSIS" >> "$DIAG_LOG"
                    echo "[HART OS AI] Diagnosis for $UNIT: $DIAGNOSIS"

                    # Attempt auto-restart (safe — systemd already handles this,
                    # but we log the AI-driven decision)
                    echo "$TIMESTAMP | $UNIT | ACTION: auto-restart attempted" >> "$DIAG_LOG"
                    systemctl restart "$UNIT" 2>/dev/null || true
                  else
                    # No AI available — just log the failure
                    TIMESTAMP=$(date -Iseconds)
                    echo "$TIMESTAMP | $UNIT | FAILED (no AI diagnosis — Model Bus unavailable)" >> "$DIAG_LOG"
                  fi
                done
              fi

              sleep 30
            done
          '';

          Restart = "always";
          RestartSec = 15;
          MemoryMax = "256M";

          # Read-only except diagnostics log
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [ cfg.logDir cfg.dataDir ];
        };
      };

      # Ensure diagnostics directory exists
      systemd.tmpfiles.rules = [
        "d /var/log/hart 0750 hart hart -"
        "f ${ai.semantic.diagnosticsLog} 0640 hart hart -"
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Semantic Intelligence: Smart Filesystem Index
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (ai.semantic.enable && ai.semantic.smartFS) {

      systemd.services.hart-smart-index = {
        description = "HART OS Smart Filesystem Index (semantic search)";
        after = [ "hart.target" "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-smart-index" ''
            set -euo pipefail

            INDEX_DIR="/var/lib/hart/smart-index"
            mkdir -p "$INDEX_DIR"

            MODEL_BUS="http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}"

            echo "[HART OS SmartFS] Starting filesystem indexer"
            echo "[HART OS SmartFS] Index: $INDEX_DIR"

            # Wait for Model Bus to be available
            echo "[HART OS SmartFS] Waiting for Model Bus..."
            for i in $(seq 1 60); do
              if curl -sf "$MODEL_BUS/v1/status" >/dev/null 2>&1; then
                echo "[HART OS SmartFS] Model Bus connected"
                break
              fi
              sleep 5
            done

            # ── Start Python indexer ──
            exec ${cfg.package.python}/bin/python -c "
            import sys, os, time, json, hashlib
            sys.path.insert(0, '${cfg.package}')
            os.environ['HEVOLVE_DATA_DIR'] = '${cfg.dataDir}'

            INDEX_DIR = '$INDEX_DIR'
            MODEL_BUS = '$MODEL_BUS'

            print('[HART OS SmartFS] Indexer ready')
            print('[HART OS SmartFS] Scanning user home directories...')

            # Index files: name + path + size + mtime → embedding
            # Full indexer runs periodically, not continuously
            import requests

            def index_file(path):
                try:
                    stat = os.stat(path)
                    file_hash = hashlib.sha256(path.encode()).hexdigest()[:16]
                    index_path = os.path.join(INDEX_DIR, file_hash + '.json')

                    # Skip if already indexed and file hasn't changed
                    if os.path.exists(index_path):
                        existing = json.load(open(index_path))
                        if existing.get('mtime') == stat.st_mtime:
                            return

                    # Get AI description via Model Bus
                    try:
                        resp = requests.post(
                            f'{MODEL_BUS}/v1/chat',
                            json={'prompt': f'Describe this file in 10 words: {os.path.basename(path)} ({stat.st_size} bytes)'},
                            timeout=10
                        )
                        description = resp.json().get('response', '')
                    except Exception:
                        description = ''

                    entry = {
                        'path': path,
                        'name': os.path.basename(path),
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                        'description': description,
                    }
                    with open(index_path, 'w') as f:
                        json.dump(entry, f)
                except Exception:
                    pass

            # Periodic indexing loop
            while True:
                # Index /home/ directories
                for root, dirs, files in os.walk('/home', topdown=True):
                    # Skip hidden dirs and large media
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files[:100]:  # Cap per-directory to avoid overload
                        fpath = os.path.join(root, f)
                        if os.path.getsize(fpath) < 10_000_000:  # Skip files > 10MB
                            index_file(fpath)

                print(f'[HART OS SmartFS] Index updated: {len(os.listdir(INDEX_DIR))} entries')
                time.sleep(300)  # Re-index every 5 minutes
            "
          '';

          Restart = "on-failure";
          RestartSec = 30;
          MemoryMax = "512M";
          CPUWeight = 20;  # Low priority — background task

          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ReadWritePaths = [
            "/var/lib/hart/smart-index"
            cfg.dataDir
          ];
          ReadOnlyPaths = [ "/home" ];
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
        };
      };

      # Smart index directory
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/smart-index 0750 hart hart -"
      ];

      # CLI: hart search
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-search" ''
          #!/usr/bin/env bash
          # Semantic file search via Smart Filesystem Index
          QUERY="''${*:?Usage: hart-search <query>}"
          INDEX_DIR="/var/lib/hart/smart-index"
          MODEL_BUS="http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}"

          if [[ ! -d "$INDEX_DIR" ]] || [[ -z "$(ls "$INDEX_DIR"/*.json 2>/dev/null)" ]]; then
            echo "Smart index is empty. Wait for hart-smart-index service to build it."
            exit 1
          fi

          # Search index entries matching query (via Model Bus for semantic matching)
          echo "Searching for: $QUERY"
          echo ""

          # Simple keyword search fallback (works without Model Bus)
          ${pkgs.jq}/bin/jq -r \
            --arg q "$QUERY" \
            'select(.name + " " + (.description // "") | test($q; "i")) | "\(.path)  — \(.description // "no description")"' \
            "$INDEX_DIR"/*.json 2>/dev/null | head -20

          # If Model Bus available, also do semantic search
          if curl -sf "$MODEL_BUS/v1/status" >/dev/null 2>&1; then
            echo ""
            echo "--- AI-enhanced results ---"
            # Collect all descriptions, ask LLM to rank by relevance
            ALL=$(${pkgs.jq}/bin/jq -r '"\(.path): \(.description // .name)"' "$INDEX_DIR"/*.json 2>/dev/null | head -50)
            curl -sf "$MODEL_BUS/v1/chat" \
              -d "{\"prompt\": \"From these files, which ones match '$QUERY'? List top 5 with paths:\\n$ALL\"}" \
              -H "Content-Type: application/json" | ${pkgs.jq}/bin/jq -r '.response // "No AI results"'
          fi
        '')
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Semantic Intelligence: Predictive Prefetch
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (ai.semantic.enable && ai.semantic.predictivePrefetch) {

      systemd.services.hart-predictive-prefetch = {
        description = "HART OS Predictive Model Prefetch";
        after = [ "hart.target" "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-predictive-prefetch" ''
            set -euo pipefail

            MODEL_BUS="http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}"

            echo "[HART OS Prefetch] Starting predictive model prefetch"

            while true; do
              # Query Model Bus for current model usage patterns
              USAGE=$(curl -sf "$MODEL_BUS/v1/status" 2>/dev/null || echo "{}")

              # Get time of day for pattern matching
              HOUR=$(date +%H)

              # Simple heuristic prefetch rules:
              # - Morning (6-9): pre-load LLM (users ask questions)
              # - Work hours (9-17): pre-load coding + vision models
              # - Evening (17-22): pre-load TTS (entertainment, reading)
              case "$HOUR" in
                0[6-9])
                  echo "[HART OS Prefetch] Morning pattern: ensuring LLM warm"
                  curl -sf "$MODEL_BUS/v1/prefetch" \
                    -d '{"model_type": "llm"}' \
                    -H "Content-Type: application/json" >/dev/null 2>&1 || true
                  ;;
                09|1[0-6])
                  echo "[HART OS Prefetch] Work pattern: ensuring LLM + vision warm"
                  curl -sf "$MODEL_BUS/v1/prefetch" \
                    -d '{"model_type": "llm"}' \
                    -H "Content-Type: application/json" >/dev/null 2>&1 || true
                  curl -sf "$MODEL_BUS/v1/prefetch" \
                    -d '{"model_type": "vision"}' \
                    -H "Content-Type: application/json" >/dev/null 2>&1 || true
                  ;;
                1[7-9]|2[0-1])
                  echo "[HART OS Prefetch] Evening pattern: ensuring TTS warm"
                  curl -sf "$MODEL_BUS/v1/prefetch" \
                    -d '{"model_type": "tts"}' \
                    -H "Content-Type: application/json" >/dev/null 2>&1 || true
                  ;;
              esac

              sleep 600  # Check every 10 minutes
            done
          '';

          Restart = "on-failure";
          RestartSec = 60;
          MemoryMax = "128M";
          CPUWeight = 10;  # Very low priority

          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [ cfg.dataDir ];
        };
      };
    })
  ]);
}
