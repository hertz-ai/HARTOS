{ config, lib, pkgs, ... }:

# HART OS Agent Daemon Module
# Goal engine: dispatches goals, manages ledger, runs tools
# Ported from deploy/linux/systemd/hart-agent-daemon.service
# Only enabled on STANDARD tier and above

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  options.hart.agent = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.variant != "edge";  # Disabled on edge (observer only)
      description = "Enable the agent daemon (goal engine)";
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.agent.enable) {

    systemd.services.hart-agent-daemon = {
      description = "HART OS Agent Daemon (Goal Engine)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "hart-backend.service" ];
      bindsTo = [ "hart-backend.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      environment = {
        # Recipe/prompts data -> the writable StateDirectory (cfg.dataDir), not the
        # read-only /nix/store package dir; get_data_dir()'s priority-2 signal.
        HARTOS_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        ExecStart = "${hartApp.python}/bin/python -c \"from integrations.agent_engine.agent_daemon import AgentDaemon; d = AgentDaemon(); d.run_forever()\"";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 30;
        TimeoutStartSec = 30;

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.dataDir
          cfg.logDir
          "${cfg.dataDir}/agent_data"
          # The session marker dir (0770 hart:hart tmpfs, declared by
          # hart-session-supervisor.nix). This unit READS the markers the
          # chat-serving units hold there (core.foreground.marker_age_s is
          # what lets its yield gate and starvation override see a person
          # being served in another process), which strict allows anyway.
          # It is listed here so that the in-process /chat app this unit
          # also imports (dispatch._native_chat) shares ONE writable dir
          # with the backend rather than a per-unit exception: a genuine
          # user turn served through it lands where every reader looks.
          "/run/hart/session"
        ];
        PrivateTmp = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_VSOCK" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = false;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;

        # Resource limits — scale by variant
        # Under hart-agents.slice (CPUWeight 40): this is the goal engine, the
        # process whose forced ticks drive llama-server, and the agent slice
        # is where agent work is meant to be arbitrated against the session
        # (hart-session.slice at 200, holding hart-liquid-ui; both defined in
        # hart-kernel.nix with the measured numbers). Until 2026-09-23 it sat
        # in system.slice, outside that ratio. Coordinator decision, same
        # day, alongside hart-llm's move: the owner's priority is the desk
        # staying snappy. The slice's MemoryMax 80% and TasksMax 4096 sit
        # above this unit's own caps below, so nothing tightens.
        Slice = "hart-agents.slice";
        MemoryMax = if cfg.variant == "edge" then "128M"
                    else if cfg.variant == "desktop" then "512M"
                    else "1G";
        MemoryHigh = if cfg.variant == "edge" then "96M"
                     else if cfg.variant == "desktop" then "384M"
                     else "768M";
        CPUWeight = if cfg.variant == "edge" then 30 else 80;
        TasksMax = if cfg.variant == "edge" then 16 else 128;
        IOWeight = if cfg.variant == "edge" then 30 else 80;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-agent-daemon";
      };
    };
  };
}
