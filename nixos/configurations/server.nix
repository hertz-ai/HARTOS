{ config, lib, pkgs, modulesPath, hartSrc, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Server Variant
# ═══════════════════════════════════════════════════════════════
#
# Headless powerhouse:
#   - All AI services (LLM, vision, agents)
#   - Native kernel extensions (GPU compute, agent sandboxing)
#   - AI runtime with full GPU scheduling
#   - Flatpak for server GUI tools (via SSH X11 forwarding)
#   - No desktop environment, no Android, no Windows
#
# Minimum 4GB RAM. Recommended 16GB+ for LLM hosting.

{
  imports = [
    "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"
  ];

  # ─── Disable ZFS (broken in nixpkgs 24.11 for kernel 6.15) ───
  boot.supportedFilesystems.zfs = lib.mkForce false;
  nixpkgs.config.allowBroken = false;

  # ─── Workaround: systemd-hwdb update fails on WSL2 build hosts ───
  # Replace the hwdb.bin derivation with a minimal stub.
  # The real hwdb.bin will be regenerated on first boot by udev.
  environment.etc."udev/hwdb.bin".source = lib.mkForce (
    pkgs.runCommand "hwdb-stub" {} ''
      # Create minimal valid hwdb binary (KSLP magic + empty index)
      printf 'KSLP\x00\x00\x00\x00' > $out
    ''
  );

  # ─── HART OS Core Services ───
  hart = {
    enable = true;
    variant = "server";

    # All AI services
    agent.enable = true;
    llm.enable = true;
    vision.enable = true;

    # ── Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = false;    # No Android on server
      windowsNative.enable = false;    # No Windows on server
      aiCompute = {
        enable = true;                 # Full GPU compute
        hugePagesCount = 0;            # Auto (set high for dedicated inference)
      };
      agentSandbox.enable = true;      # Isolate agents
    };

    # ── AI Runtime (full power) ──
    aiRuntime = {
      enable = true;
      gpu.enable = true;
      worldModel.enable = true;
      agents = {
        maxConcurrent = 16;            # Server can handle many agents
        maxMemoryPerAgent = "4G";
      };
      # Semantic intelligence: self-healing services + predictive prefetch
      semantic = {
        enable = true;
        serviceIntelligence = true;
        predictivePrefetch = true;
        smartFS = false;               # No user files on server typically
      };
    };

    # ── AI-Native OS Layers ──
    # Model Bus: every app/service gets native AI access
    modelBus.enable = true;

    # Compute Mesh: share this server's GPU with user's other devices
    computeMesh = {
      enable = true;
      maxOffloadPercent = 70;          # Server donates generously
      allowWAN = true;
    };

    # No LiquidUI (headless server)
    # No App Bridge (no subsystems on server)

    # ── Sandbox ──
    sandbox.enable = true;
  };

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # CLI tool
  environment.systemPackages = [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })
  ];

  # ISO branding
  isoImage = {
    isoName = lib.mkForce "hart-os-${config.hart.version}-server-${pkgs.system}.iso";
    volumeID = lib.mkForce "HART_OS";
    appendToMenuLabel = " HART OS Server";
    # Allow both EFI and BIOS boot (needed for QEMU without OVMF)
    makeBiosBootable = lib.mkDefault true;
  };

  # Boot configuration
  boot.loader.timeout = lib.mkForce 5;

  # Serial console for headless/QEMU boot
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  # SSH for remote access (NixOS live env)
  services.openssh = {
    enable = true;
    settings.PermitRootLogin = lib.mkForce "yes";
    settings.PasswordAuthentication = lib.mkForce true;
    settings.UsePAM = lib.mkForce true;
  };

  # Fix: PAM pam_setcred() fails because pam_deny.so (required) in auth stack
  # returns PAM_PERM_DENIED during credential establishment.
  # Override sshd PAM to remove the pam_deny.so auth catchall.
  security.pam.services.sshd.text = lib.mkForce ''
    # Account management.
    account required pam_unix.so

    # Authentication management.
    auth sufficient pam_unix.so likeauth try_first_pass

    # Password management.
    password sufficient pam_unix.so nullok yescrypt

    # Session management.
    session required pam_env.so conffile=/etc/pam/environment readenv=0
    session required pam_unix.so
    session required pam_loginuid.so
    session optional pam_systemd.so
  '';

  # Password: hart123 (SHA-512 hash — works on live ISO where every boot is "initial")
  users.users.root.hashedPassword = lib.mkForce "$6$AfVhhgH5HUHO0Dww$rb/YNzNp6Z29KRrjtweGvBj3Wh/7E92tFREXONqdHxvHFEa1y1rlk3hMbux9jE5NdycqpwQhHokxgdcX1SH6B.";
  users.users.nixos.hashedPassword = lib.mkForce "$6$AfVhhgH5HUHO0Dww$rb/YNzNp6Z29KRrjtweGvBj3Wh/7E92tFREXONqdHxvHFEa1y1rlk3hMbux9jE5NdycqpwQhHokxgdcX1SH6B.";
  users.users.nixos.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJZsU51nixnLUQMV/T4IeXruPZBfe17rB00pNb/WQEDc sathish@hertzai.com"
  ];

  # SSH authorized keys for root
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJZsU51nixnLUQMV/T4IeXruPZBfe17rB00pNb/WQEDc sathish@hertzai.com"
  ];

  # Diagnostic: serve SSH debug info on port 8888
  systemd.services.ssh-diag = {
    description = "SSH Diagnostic HTTP server";
    after = [ "sshd.service" "network.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig.Type = "simple";
    serviceConfig.Restart = "always";
    path = [ pkgs.python3Minimal pkgs.systemd pkgs.gawk pkgs.gnugrep pkgs.coreutils pkgs.glibc.bin ];
    script = ''
      # CGI-like: regenerate on each request via a Python script
      cat > /tmp/diag_server.py << 'PYEOF'
import http.server, subprocess, socketserver

class DiagHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Regenerate fresh diagnostics on each request
        diag = subprocess.run(["/tmp/gen_diag.sh"], capture_output=True, text=True, timeout=15)
        body = diag.stdout + diag.stderr
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", 8888), DiagHandler) as httpd:
    httpd.serve_forever()
PYEOF

      cat > /tmp/gen_diag.sh << 'SHEOF'
#!/bin/sh
export PATH="/run/current-system/sw/bin:$PATH"
echo "=== SSH DIAG ==="
echo "--- passwd entries ---"
getent passwd root 2>/dev/null || grep ^root: /etc/passwd
getent passwd nixos 2>/dev/null || grep ^nixos: /etc/passwd
getent passwd hart-admin 2>/dev/null || grep ^hart-admin: /etc/passwd
echo "--- sshd_config snippet ---"
grep -E "PermitRoot|Password|UsePAM|LogLevel|Subsystem|ForceCommand|MaxSessions|AllowUsers|DenyUsers" /etc/ssh/sshd_config 2>/dev/null
echo "--- sshd journal (last 100) ---"
journalctl -u sshd.service --no-pager -n 100 2>&1
echo "--- logind journal (last 20) ---"
journalctl -u systemd-logind.service --no-pager -n 20 2>&1
echo "--- dmesg last 20 ---"
dmesg | tail -20 2>&1
echo "=== SSH DIAG END ==="
SHEOF
      chmod +x /tmp/gen_diag.sh

      python3 /tmp/diag_server.py
    '';
  };
  networking.firewall.allowedTCPPorts = [ 8888 ];

  # Headless: no desktop
  services.xserver.enable = false;

  # Auto-login on console (first-time setup)
  services.getty.autologinUser = lib.mkDefault "hart-admin";
}
