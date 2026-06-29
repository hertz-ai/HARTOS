{ config, lib, pkgs, hartVersion, hartVariant, ... }:

# HART OS Base Module
# Core system: identity, branding, networking, security, users
# Shared by all variants (server, desktop, edge)

let
  cfg = config.hart;
in
{
  # ─── Options ────────────────────────────────────────────────
  options.hart = {
    enable = lib.mkEnableOption "HART OS services";

    version = lib.mkOption {
      type = lib.types.str;
      default = hartVersion;
      description = "HART OS version string";
    };

    variant = lib.mkOption {
      type = lib.types.enum [ "server" "desktop" "edge" "phone" ];
      default = hartVariant;
      description = "HART OS variant (server, desktop, edge, phone)";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/hart";
      description = "Persistent data directory";
    };

    logDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/log/hart";
      description = "Log directory";
    };

    package = lib.mkOption {
      type = lib.types.package;
      description = "The HART application package (set in variant config)";
    };

    # OS-mode ports: privileged (<1024) — HART OS owns the machine.
    # This frees user-space ports (1024-65535) for user applications.
    # App-mode ports (6777, 6780, etc.) are used when running alongside other software.
    ports = {
      backend = lib.mkOption {
        type = lib.types.port;
        default = 6777;
        description = "Backend API port (Flask/Waitress). 6777 is non-privileged so the unprivileged hart user can bind it (677 is < 1024 and needs CAP_NET_BIND_SERVICE — the unit grants no such capability, and the hart-backend.nix header already documents 6777).";
      };
      discovery = lib.mkOption {
        type = lib.types.port;
        default = 678;
        description = "UDP peer discovery port (OS-mode: 678, app-mode: 6780)";
      };
      llm = lib.mkOption {
        type = lib.types.port;
        default = 808;
        description = "Local LLM inference port (OS-mode: 808, app-mode: 8080)";
      };
      vision = lib.mkOption {
        type = lib.types.port;
        default = 989;
        description = "Vision sidecar port (OS-mode: 989, app-mode: 9891)";
      };
      websocket = lib.mkOption {
        type = lib.types.port;
        default = 546;
        description = "WebSocket port for frame streaming (OS-mode: 546, app-mode: 5460)";
      };
      diarization = lib.mkOption {
        type = lib.types.port;
        default = 800;
        description = "Speaker diarization port (OS-mode: 800, app-mode: 8004)";
      };
      dlna_stream = lib.mkOption {
        type = lib.types.port;
        default = 855;
        description = "DLNA MJPEG stream port (OS-mode: 855, app-mode: 8554)";
      };
      mesh_wg = lib.mkOption {
        type = lib.types.port;
        default = 679;
        description = "WireGuard mesh port (OS-mode: 679, app-mode: 6795)";
      };
      mesh_relay = lib.mkOption {
        type = lib.types.port;
        default = 680;
        description = "Mesh relay port (OS-mode: 680, app-mode: 6796)";
      };
    };
  };

  # ─── Configuration ──────────────────────────────────────────
  config = lib.mkIf cfg.enable {

    # ── nixpkgs.config (allowUnfree / permittedInsecurePackages) is set ONCE
    #    at the flake level (`nixpkgsConfig` in flake.nix), NOT here.  hart-base
    #    is imported by vm-tests' runNixOSTest nodes, which receive read-only
    #    pkgs; a module-level nixpkgs.config there triggers "defined multiple
    #    times" (#70).  Real builds get it via mkSystem/mkImage. ──

    # ── Branding ──
    environment.etc = {
      "os-release".text = ''
        NAME="HART OS"
        PRETTY_NAME="HART OS ${cfg.version} (Sentient)"
        VERSION="${cfg.version}"
        VERSION_ID="${cfg.version}"
        VERSION_CODENAME=sentient
        ID=hart-os
        ID_LIKE=linux
        HOME_URL="https://hevolve.ai"
        SUPPORT_URL="https://github.com/hertz-ai/HARTOS/issues"
        BUG_REPORT_URL="https://github.com/hertz-ai/HARTOS/issues"
        PRIVACY_POLICY_URL="https://hevolve.ai/privacy"
      '';

      "hart/variant".text = cfg.variant;

      # MOTD: dynamic system info on login
      "profile.d/hart-motd.sh" = {
        mode = "0755";
        text = ''
          #!/bin/bash
          CYAN='\033[0;36m'
          GREEN='\033[0;32m'
          YELLOW='\033[1;33m'
          NC='\033[0m'

          echo ""
          echo -e "''${CYAN}  HART OS ${cfg.version} — Crowdsourced Agentic Intelligence''${NC}"
          echo ""

          if [[ -f ${cfg.dataDir}/node_public.key ]]; then
            NODE_ID=$(xxd -p ${cfg.dataDir}/node_public.key | tr -d '\n' | head -c 16)
            echo -e "  Node ID:    ''${GREEN}''${NODE_ID}...''${NC}"
          fi

          BACKEND=$(systemctl is-active hart-backend.service 2>/dev/null || echo "unknown")
          if [[ "$BACKEND" == "active" ]]; then
            echo -e "  Backend:    ''${GREEN}running''${NC}"
          else
            echo -e "  Backend:    ''${YELLOW}''${BACKEND}''${NC}"
          fi

          echo -e "  Variant:    ${cfg.variant}"
          echo -e "  Uptime:     $(uptime -p 2>/dev/null || echo 'unknown')"

          IP=$(hostname -I 2>/dev/null | awk '{print $1}')
          echo ""
          echo -e "  Dashboard:  http://''${IP:-localhost}:${toString cfg.ports.backend}"
          echo -e "  CLI:        ''${GREEN}hart status''${NC}"
          echo ""
        '';
      };
    };

    # ── TTY login banner ──
    # NixOS's default getty greeting is "<<< Welcome to NixOS ... >>>" — a
    # user-visible leak on Ctrl+Alt+F2..F6. Rebrand it (HART hides NixOS).
    # \m = machine arch, \l = tty line (literal getty escapes, not nix).
    services.getty.greetingLine =
      lib.mkForce ''<<< Welcome to HART OS ${cfg.version} (Sentient) (\m) - \l >>>'';

    # ── Distro name (boot menu + system strings) ──
    # The ISO boot menu showed "NixOS <nixpkgs-version> HART OS Desktop" — a
    # user-visible "NixOS" leak at boot (confirmed from a CI QEMU console dump;
    # isoImage.appendToMenuLabel only APPENDS, it can't drop the "NixOS"
    # prefix). distroName is the supported override the boot-menu + various
    # system strings derive from. distroId is left as the default so tooling
    # that keys on os-release ID=nixos still works; the user-facing NAME is
    # already "HART OS" via the explicit os-release above.
    system.nixos.distroName = lib.mkForce "HART OS";

    # Don't ship the NixOS manual / `nixos-help` — it's a "NixOS" reference a
    # user can surface, and building it adds time to the (already slow) ISO
    # build. mkDefault so a variant can still re-enable docs if it wants; edge
    # already turns ALL docs off, and that explicit setting wins over this.
    documentation.nixos.enable = lib.mkDefault false;

    # ── No nix-channels (flake-based OS; the last boot-console "NixOS" leak) ──
    # The installer-CD profile wires up the NixOS/Nixpkgs *channel*, whose
    # activation prints "unpacking the NixOS/Nixpkgs sources..." to the console
    # and symlinks /root/.nix-defexpr/channels — which fails read-only on the
    # live ISO ("ln: failed to create symbolic link ...channels"). Both are
    # NixOS tells in the boot log. HART OS is flake-based and never uses
    # channels, so disable the subsystem entirely: no unpack message, no channel
    # symlink error, and nothing for a user to `nix-channel --list` into
    # "nixpkgs". (Android hides Linux; HART OS hides NixOS — down to the console.)
    nix.channel.enable = lib.mkForce false;

    # ── Users ──
    users.users.hart = {
      isSystemUser = true;
      group = "hart";
      home = cfg.dataDir;
      createHome = true;
      description = "HART OS service user";
    };
    users.groups.hart = {};

    # Default admin user for interactive login
    users.users.hart-admin = {
      isNormalUser = true;
      description = "HART OS Administrator";
      # video + render: open /dev/dri/card* (KMS/DRM) + /dev/dri/renderD* (GPU).
      # input: open /dev/input/* (libinput keyboard/mouse/touch) — WITHOUT it a
      # Wayland compositor (cage/sway/hart-comp) launched by greetd cannot read
      # the seat's input devices (EACCES on /dev/input) and boots dead-input. The
      # `seat` group (libseat/seatd backend) is added by hart-session-supervisor
      # .nix ONLY when it enables services.seatd (the group exists only then), so
      # referencing it here unconditionally cannot fail eval on a seatd-less node.
      extraGroups = [ "wheel" "hart" "video" "render" "input" ];
      # Baked SHA-512 hash of "hart" (login = hart-admin / hart). `initialPassword`
      # is applied by a runtime activation step that does NOT reliably persist on a
      # read-only / baked live-ISO squashfs — it left hart-admin with no usable
      # password, so every GDM greeter login failed ("authentication didn't work").
      # `hashedPassword` is baked into the image and works (server.nix uses it for
      # the same reason). The INSTALLER should re-prompt for the user's own password
      # on install; this baked default exists only to reach that first login.
      hashedPassword = "$6$hartos00$4CRZoq04d/q2rp1.FAXAXMqZeUkDfh90FYFA2vpl4b/3JWAs1EvmjW7dgDf/wt.mjt6iIovSKaZmZtJkoj0dx1";
    };

    # ── Power actions via logind (polkit grant) — #133 ──
    # The Liquid-UI shell server (which serves /api/shell/power/action) runs as the
    # `hart` SERVICE user, a system daemon with NO active graphical session. When
    # the user taps a power button it asks logind (org.freedesktop.login1.Manager)
    # to Reboot / PowerOff / Suspend / Hibernate / SetRebootToFirmwareSetup. polkit's
    # default login1 policy returns `yes` only for an active LOCAL session; a system
    # daemon falls through to allow_inactive/allow_any = auth_admin, so the call is
    # DENIED non-interactively and the box silently never reboots (the #133 symptom).
    #
    # This rule grants the `hart` shell user (and, belt-and-suspenders, any active
    # local seat such as hart-admin's graphical session) the login1 power actions
    # outright, so shell_os_apis.shell_power_action's native D-Bus call is authorized
    # and actually executes. Scope is the enumerated power verbs ONLY — no broader
    # privilege is conferred. (The shell server still gates every call behind
    # @_require_shell_auth + the action whitelist + the firmware-capability probe.)
    security.polkit = {
      enable = lib.mkDefault true;
      extraConfig = ''
        polkit.addRule(function(action, subject) {
          var hartPowerActions = {
            "org.freedesktop.login1.reboot": true,
            "org.freedesktop.login1.reboot-multiple-sessions": true,
            "org.freedesktop.login1.power-off": true,
            "org.freedesktop.login1.power-off-multiple-sessions": true,
            "org.freedesktop.login1.suspend": true,
            "org.freedesktop.login1.suspend-multiple-sessions": true,
            "org.freedesktop.login1.hibernate": true,
            "org.freedesktop.login1.hibernate-multiple-sessions": true,
            "org.freedesktop.login1.set-reboot-to-firmware-setup": true,
            "org.freedesktop.login1.lock-sessions": true
          };
          if (hartPowerActions[action.id] === true) {
            if (subject.user == "hart" || (subject.local && subject.active)) {
              return polkit.Result.YES;
            }
          }
        });
      '';
    };

    # ── Networking ──
    networking = {
      hostName = lib.mkDefault "hart-node";
      firewall = {
        # mkDefault: the docker-server image format (nixpkgs docker-image.nix)
        # sets networking.firewall.enable = false at normal priority for the OCI
        # container; a plain `true` here (this base module is included by EVERY
        # variant) collided with it ("conflicting definition values"), eval-
        # failing only the docker-server target. mkDefault yields to the
        # container's false, while ISO/host variants — which have no competing
        # definition — still resolve to true (unchanged firewall behaviour).
        enable = lib.mkDefault true;
        allowedTCPPorts = [ cfg.ports.backend 22 ];
        allowedUDPPorts = [ cfg.ports.discovery ];
      };
    };

    # ── Kernel tuning (P2P gossip + compute workloads) ──
    boot.kernel.sysctl = {
      # Networking: optimize for P2P gossip
      # mkDefault so hart-kernel.nix specialized values win
      "net.core.rmem_max" = lib.mkDefault 16777216;
      "net.core.wmem_max" = lib.mkDefault 16777216;
      "net.ipv4.tcp_fastopen" = lib.mkDefault 3;
      "net.core.somaxconn" = lib.mkDefault 4096;
      "net.ipv4.tcp_tw_reuse" = lib.mkDefault 1;
      "net.ipv4.tcp_fin_timeout" = lib.mkDefault 15;

      # Memory: favor compute workloads
      "vm.swappiness" = lib.mkDefault 10;
      "vm.dirty_ratio" = lib.mkDefault 40;
      "vm.dirty_background_ratio" = lib.mkDefault 10;
      "vm.overcommit_memory" = lib.mkDefault 1;

      # Security: kernel hardening (mkForce — our stricter values override nixpkgs)
      "kernel.dmesg_restrict" = lib.mkForce 1;
      "kernel.kptr_restrict" = lib.mkForce 2;
      "net.ipv4.conf.all.rp_filter" = lib.mkForce 1;
      "net.ipv4.conf.default.rp_filter" = lib.mkForce 1;
      "net.ipv4.icmp_echo_ignore_broadcasts" = lib.mkForce 1;
      "net.ipv4.conf.all.accept_redirects" = lib.mkForce 0;
      "net.ipv4.conf.default.accept_redirects" = lib.mkForce 0;
      "net.ipv6.conf.all.accept_redirects" = lib.mkForce 0;
      "net.ipv6.conf.default.accept_redirects" = lib.mkForce 0;

      # File descriptors: agent workloads
      "fs.file-max" = lib.mkDefault 524288;
      "fs.inotify.max_user_watches" = lib.mkDefault 524288;
    };

    # ── SSH ──
    services.openssh = {
      enable = true;
      settings = {
        PermitRootLogin = lib.mkDefault "no";
        PasswordAuthentication = true;  # For first login; disable after key setup
      };
    };

    # ── System packages (available to all users) ──
    environment.systemPackages = with pkgs; [
      vim
      htop
      curl
      git
      rsync
      xxd
      jq
      tmux
    ];

    # ── Directories ──
    systemd.tmpfiles.rules = [
      # 0770 (group-writable), NOT 0750: the hart-session-supervisor selector runs
      # as hart-admin (in the `hart` GROUP, not the `hart` OWNER) and MUST create +
      # write the session-tier latch + crash-window files that live directly under
      # this dir. At 0750 the group has only r-x, so every latch/window write fails
      # "Permission denied" and a tier-drop can NEVER persist — the real-HW boot
      # loop (the selector retries the same broken tier forever). hart-session-
      # supervisor.nix declares the SAME `d /var/lib/hart 0770` rule; the two are
      # now IDENTICAL, so tmpfiles de-dupes them and the mode is deterministic
      # regardless of rule ordering. (Previously this was 0750 while the supervisor
      # set 0770 — a same-path mode CONFLICT whose winner tmpfiles decided by file
      # ordering = nondeterministic; a 0750 win silently reinstated the boot loop.)
      # Do NOT revert this to 0750. The restrictive subdirs below keep their own
      # 0750/0700 modes — a 0770 parent does not relax them.
      "d ${cfg.dataDir} 0770 hart hart -"
      "d ${cfg.dataDir}/agent_data 0750 hart hart -"
      "d ${cfg.dataDir}/models 0750 hart hart -"
      "d ${cfg.logDir} 0750 hart hart -"
    ];

    # ── Systemd target: hart.target groups all HART services ──
    # NO network-online.target: hart.target is a pure grouping target; hart-backend
    # (the shell's local :6777 API) is partOf+wantedBy it, so ANY network-online wait
    # here is inherited by the backend and stalls the boot-critical path ~90-120s on
    # an offline live USB (NetworkManager-wait-online timeout) -> the glass shell's
    # BACKEND-served panels connection-refuse to localhost:6777 ("Reconnecting").
    # Net-needing HART services (dns/ota/firewall/sso/subsystems) each declare their
    # OWN best-effort `wants=network-online` locally, so dropping it from the group
    # changes nothing for them while freeing the offline boot path. multi-user.target
    # already implies local-fs/sysinit ordering for the grouped services.
    systemd.targets.hart = {
      description = "HART OS Services";
      wantedBy = [ "multi-user.target" ];
    };

    # ── NixOS metadata ──
    system.stateVersion = "24.11";
  };
}
