{ config, lib, pkgs, ... }:

# HART OS DLNA Bridge Module
# Cast remote desktop sessions to DLNA/UPnP renderers (smart TVs)
# Uses SSDP multicast for discovery, MJPEG HTTP stream for casting

let
  cfg = config.hart;
in
{
  options.hart.dlna = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable DLNA screen casting for HART remote desktop";
    };

    streamPort = lib.mkOption {
      type = lib.types.port;
      default = 8554;
      description = "Port for MJPEG stream server";
    };

    renderer = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Enable this device as a DLNA renderer (receive casts)";
      };

      mediaDir = lib.mkOption {
        type = lib.types.str;
        default = "${cfg.dataDir}/dlna-media";
        description = "Directory for received DLNA media";
      };
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.dlna.enable) {

    # ── Firewall: MJPEG stream port + SSDP multicast ──────────
    networking.firewall = {
      allowedTCPPorts = [ config.hart.dlna.streamPort ];
      allowedUDPPorts = [ 1900 ];  # SSDP
    };

    # ── DLNA Renderer (receive casts from other HART nodes) ───
    services.minidlna = lib.mkIf config.hart.dlna.renderer.enable {
      enable = true;
      settings = {
        friendly_name = "HART OS";
        media_dir = [ config.hart.dlna.renderer.mediaDir ];
        inotify = "yes";
        port = 8200;
      };
    };

    # ── Create media directory ─────────────────────────────────
    systemd.tmpfiles.rules = lib.mkIf config.hart.dlna.renderer.enable [
      "d ${config.hart.dlna.renderer.mediaDir} 0755 hart hart -"
    ];

    # ── SSDP multicast routing ─────────────────────────────────
    # Ensure SSDP multicast packets are routed correctly
    boot.kernel.sysctl = {
      "net.ipv4.igmp_max_memberships" = 32;
    };
  };
}
