{ config, lib, pkgs, ... }:

# HART OS Backend Module
# Flask/Waitress API server on port 6777
# Ported from deploy/linux/systemd/hart-backend.service

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  config = lib.mkIf cfg.enable {

    systemd.services.hart-backend = {
      description = "HART OS Backend (Flask/Waitress)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      # NO network-online.target: the backend binds 0.0.0.0:6777 LOCALLY and
      # serves the shell's local API without the internet. On an offline live
      # USB, waiting on network-online stalls the boot ~90-120s (systemd-
      # networkd-wait-online timeout) before :6777 ever serves, which floods
      # the shell with "Connection refused" to localhost:6777. Only depend on
      # hart-first-boot (data-dir/StateDirectory provisioning).
      after = [ "hart-first-boot.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      # Crash-loop containment: Restart=on-failure/RestartSec=5 (below) would
      # otherwise restart a persistently-failing backend every 5s forever,
      # pinning a core at boot. Cap at 5 fast failures within 5 min, then let
      # systemd mark the unit failed and move on — the shell still boots (it
      # degrades to "backend unavailable") rather than the OS hanging on a
      # doomed service. A legitimately slow start (see TimeoutStartSec below)
      # is a single long attempt, so it never trips this fast-failure limit.
      startLimitIntervalSec = 300;
      startLimitBurst = 5;

      environment = {
        # Recipe/prompts data must land in the service's WRITABLE StateDirectory
        # (cfg.dataDir), not the /nix/store package dir (read-only) nor the
        # /etc/hartos-release default /var/lib/hartos (outside this sandbox ->
        # EROFS). This is get_data_dir()'s priority-2 signal, so get_recipe_
        # prompts_dir() -> cfg.dataDir/data/prompts. Fixes the boot crash
        # OSError [Errno 30] Read-only file system: '.../prompts'.
        HARTOS_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HARTOS_BACKEND_PORT = toString cfg.ports.backend;
        HART_DISCOVERY_PORT = toString cfg.ports.discovery;
        HART_LLM_PORT = toString cfg.ports.llm;
        HART_VISION_PORT = toString cfg.ports.vision;
        HART_VERSION = cfg.version;

        # ── Local-only inference (P0b: "never a remote proxy") ──
        # The agent_engine + /chat run IN this backend process; their LLM endpoint
        # comes from core.port_registry.get_local_llm_url(). Pin it to the LOCAL
        # llama-server (hart-llm, cfg.ports.llm) so a chat / agent dispatch from the
        # Nunba UI ALWAYS resolves to on-device inference, never a stale
        # ~/.nunba/llama_config.json external endpoint or a cloud fallback. This is
        # the resolver's TOP probe candidate: on cold boot (llama not up yet) it
        # falls through to the others and finally back to this URL as a stable
        # placeholder, so pinning it is safe and makes local-only the default
        # OS-mode posture (port_registry.is_os_mode is already true via os-release).
        HEVOLVE_LOCAL_LLM_URL = "http://127.0.0.1:${toString cfg.ports.llm}/v1";

        # ── Realtime origin (P0a: SSE/WAMP reaches the Nunba UI) ──
        # This backend is the ORIGIN of HARTOS realtime: it serves REST /api/social
        # + root /chat and emits push events via core.platform.events
        # .broadcast_sse_safe (SSE) and the crossbarhttp3 publisher (WAMP). The
        # transport TARGETS are already correct here with no extra env: is_os_mode
        # is true via /etc/os-release (ID=hart-os) so core.port_registry resolves
        # OS-mode ports, and the WAMP publish + the Nunba UI both default to
        # localhost:8088 (port_registry crossbar=8088 in both modes; apiBase.js
        # WAMP_LOCAL_URL ws://localhost:8088/ws) — so nothing is "moved" to pin.
        # The SSE event-stream broker route + the :8088 WAMP ROUTER themselves are
        # served by the UI-server layer (the React SPA's same-origin Flask host),
        # NOT duplicated here — a second SSE broker / router in this module would be
        # a parallel path. The UI is SSE-primary / WAMP-fallback, so SSE is the
        # OS-mode realtime path.

        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        # Thread count scales by variant: edge=4, server=50, desktop=24
        ExecStart = let
          threads = if cfg.variant == "edge" then "4"
                    else if cfg.variant == "desktop" then "24"
                    else "50";
        in "${hartApp.python}/bin/python -m waitress --port=${toString cfg.ports.backend} --threads=${threads} hart_intelligence_entry:app";

        # Environment file for API keys (optional, user-provided)
        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 5;
        # No WatchdogSec: waitress never sends sd_notify(WATCHDOG=1), so a watchdog
        # timer would SIGABRT the backend every 120s once it is actually serving.
        # Restart=on-failure still covers real crashes.
        # 600s (not 30s): the backend imports langchain + chromadb + autogen at
        # startup, which alone can take ~170s frozen and is far slower on USB /
        # SD-card live media. A 30s start timeout SIGKILLs the process mid-import
        # before it ever binds :6777, so the shell only ever sees "connection
        # refused" and the crash-loop guard above burns through its attempts on a
        # backend that was actually making progress. 600s covers cold USB boots.
        TimeoutStartSec = 600;
        TimeoutStopSec = 15;

        # The backend's STARTUP legitimately logs hundreds of lines (the full
        # ML-stack import narrates), and hart-security's global journald rate
        # limit (RateLimitBurst=200/30s, added 2026-08-12 against runaway
        # loggers) can swallow the tail of that burst -- including one-shot
        # wiring proofs like "Local subscribers bootstrapped: ... ota-push"
        # that vm-test-run-hart-ota-central greps for (red on every gate run
        # since the cap landed). Exempting THIS unit keeps the global cap for
        # everything else; a boot-time burst from the designed-noisy unit is
        # not the runaway the cap exists to stop.
        LogRateLimitIntervalSec = 30;
        LogRateLimitBurst = 5000;

        # The canonical DB must be WRITABLE BY hart BEFORE the backend touches
        # it (real-HW 2026-08-14). Root-running importers (hart-ota's python
        # sets HEVOLVE_DB_PATH and imports the app) can be the FIRST to connect,
        # and sqlite creates the file as root:root 0644. The backend (User=hart)
        # then reads it fine but every write fails "attempt to write a readonly
        # database", init_db/run_migrations die (swallowed as a non-fatal
        # warning), tables added since the seed never exist, and every consumer
        # ticks into `no such table: agent_goals` forever -- the constant
        # SSE/log churn measured feeding the shell leak. The `+` prefix runs
        # this ONE step as root; create-with-owner if missing, normalize if
        # present. Idempotent, self-healing on every start, and ordering-proof
        # against the state-persist bind mount (we run strictly before ExecStart).
        ExecStartPre = "+" + pkgs.writeShellScript "hart-backend-db-owner" ''
          db="${cfg.dataDir}/hevolve_database.db"
          if [ ! -e "$db" ]; then
            install -o hart -g hart -m 0660 /dev/null "$db"
          else
            chown hart:hart "$db" 2>/dev/null || true
            chmod 0660 "$db" 2>/dev/null || true
          fi
          # sqlite sidecar files inherit badness the same way; normalize if present
          for f in "$db-wal" "$db-shm" "$db-journal"; do
            [ -e "$f" ] && chown hart:hart "$f" 2>/dev/null && chmod 0660 "$f" 2>/dev/null || true
          done
        '';

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.dataDir
          cfg.logDir
          "${cfg.dataDir}/agent_data"
        ];
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

        # Resource limits — scale by variant. The backend boot-imports the full
        # ML stack (langchain/chromadb/autogen) + 24-50 waitress thread stacks;
        # the old caps (1G desktop / 2G server) were OVERRUN, so the cgroup denied
        # new thread stacks -> RuntimeError: can't start new thread (caught by the
        # e2e boot smoke, and it would bite real hardware too — the cap is the
        # same there). Give the ML init real headroom; edge stays minimal (no
        # heavy ML), so its small cap is correct.
        # Edge caps raised 384M/256M/32 -> 640M/512M/64 (2026-07-28, measured):
        # a full hart_intelligence_entry import is 275 MB RSS / 11 threads ON THE
        # DEV BOX WITH THE ML STACK ABSENT — i.e. 275 MB is the FLOOR of the
        # module-scope import, and it is variant-independent (nothing slims it on
        # edge). The old MemoryHigh=256M sat BELOW that floor (perpetual reclaim)
        # and MemoryMax=384M barely above it, so a real edge device would
        # OOM-kill its backend at boot; TasksMax=32 left ~17 for the app after
        # waitress's 4 workers + interpreter housekeeping. The old sizing
        # reasoned "edge stays minimal (no heavy ML)" — true of the VARIANT, not
        # of the IMPORT. Slimming the import itself (lazy module-scope init) is
        # the real fix and is tracked; these caps are the honest cost of the
        # import that exists today.
        MemoryMax = if cfg.variant == "edge" then "640M"
                    else if cfg.variant == "desktop" then "3G"
                    else "4G";
        MemoryHigh = if cfg.variant == "edge" then "512M"
                     else if cfg.variant == "desktop" then "2560M"
                     else "3584M";
        CPUWeight = if cfg.variant == "edge" then 50 else 100;
        TasksMax = if cfg.variant == "edge" then 64 else 512;
        IOWeight = if cfg.variant == "edge" then 50 else 100;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-backend";
      };
    };
  };
}
