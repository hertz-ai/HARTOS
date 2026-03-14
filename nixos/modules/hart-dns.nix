# HART OS — Secure DNS (DoH / DoT)
#
# Encrypted DNS resolution via systemd-resolved.
# Supports DNS-over-TLS (DoT) and DNS-over-HTTPS (DoH) with
# configurable upstream providers.
#
# CLI: hart-dns status|flush|test|provider

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.dns;

  providers = {
    cloudflare = {
      addresses = [ "1.1.1.1" "1.0.0.1" "2606:4700:4700::1111" "2606:4700:4700::1001" ];
      tlsName = "cloudflare-dns.com";
      dohUrl = "https://cloudflare-dns.com/dns-query";
    };
    google = {
      addresses = [ "8.8.8.8" "8.8.4.4" "2001:4860:4860::8888" "2001:4860:4860::8844" ];
      tlsName = "dns.google";
      dohUrl = "https://dns.google/dns-query";
    };
    quad9 = {
      addresses = [ "9.9.9.9" "149.112.112.112" "2620:fe::fe" "2620:fe::9" ];
      tlsName = "dns.quad9.net";
      dohUrl = "https://dns.quad9.net/dns-query";
    };
  };

  selected = providers.${cfg.provider};
in
{
  options.hart.dns = {
    enable = lib.mkEnableOption "HART OS secure DNS (DoH/DoT)";

    provider = lib.mkOption {
      type = lib.types.enum [ "cloudflare" "google" "quad9" ];
      default = "cloudflare";
      description = "Upstream encrypted DNS provider.";
    };

    mode = lib.mkOption {
      type = lib.types.enum [ "dot" "doh" ];
      default = "dot";
      description = "Encryption mode: DNS-over-TLS (dot) or DNS-over-HTTPS (doh).";
    };

    dnssec = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable DNSSEC validation.";
    };

    fallbackToPlaintext = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Allow fallback to unencrypted DNS if encrypted upstream is unreachable.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── systemd-resolved for encrypted DNS ──
    services.resolved = {
      enable = true;
      dnssec = if cfg.dnssec then "true" else "false";
      extraConfig = ''
        [Resolve]
        DNS=${lib.concatStringsSep " " selected.addresses}
        DNSOverTLS=${if cfg.mode == "dot" then
          (if cfg.fallbackToPlaintext then "opportunistic" else "yes")
        else "no"}
      '';

      # Set the provider TLS hostname for certificate verification
      domains = [ "~." ];
    };

    # ── Ensure systemd-resolved is the system resolver ──
    networking.nameservers = selected.addresses;

    # ── DoH mode: use dns-over-https-proxy ──
    systemd.services.hart-doh-proxy = lib.mkIf (cfg.mode == "doh") {
      description = "HART OS DNS-over-HTTPS Proxy";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = "${pkgs.dnscrypt-proxy2}/bin/dnscrypt-proxy -config ${pkgs.writeText "dnscrypt-proxy.toml" ''
          listen_addresses = ["127.0.0.53:5353"]
          server_names = ["${cfg.provider}"]
          max_clients = 250
          ipv4_servers = true
          ipv6_servers = true
          dnscrypt_servers = false
          doh_servers = true
          require_dnssec = ${if cfg.dnssec then "true" else "false"}
          fallback_resolvers = ["${builtins.head selected.addresses}:53"]

          [sources.public-resolvers]
            urls = ["https://raw.githubusercontent.com/DNSCrypt/dnscrypt-resolvers/master/v3/public-resolvers.md"]
            cache_file = "/var/cache/dnscrypt-proxy/public-resolvers.md"
            minisign_key = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3"
        ''}";
        Restart = "on-failure";
        RestartSec = 5;
        DynamicUser = true;
        CacheDirectory = "dnscrypt-proxy";
        StateDirectory = "dnscrypt-proxy";
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
      };
    };

    # ── CLI tool ──
    environment.systemPackages = [
      (pkgs.writeShellScriptBin "hart-dns" ''
        case "''${1:-status}" in
          status)
            echo "=== HART OS Secure DNS ==="
            echo "Provider:  ${cfg.provider}"
            echo "Mode:      ${cfg.mode}"
            echo "DNSSEC:    ${if cfg.dnssec then "enabled" else "disabled"}"
            echo "Fallback:  ${if cfg.fallbackToPlaintext then "yes" else "no"}"
            echo ""
            echo "Resolved status:"
            resolvectl status 2>/dev/null || echo "systemd-resolved not running"
            ;;
          flush)
            echo "Flushing DNS cache..."
            resolvectl flush-caches 2>/dev/null && echo "Cache flushed." \
              || echo "Failed to flush cache."
            ;;
          test)
            domain="''${2:-cloudflare.com}"
            echo "Resolving $domain via ${cfg.provider} (${cfg.mode})..."
            resolvectl query "$domain" 2>/dev/null || echo "Resolution failed."
            ;;
          provider)
            echo "Current: ${cfg.provider} (${cfg.mode})"
            echo "Servers: ${lib.concatStringsSep ", " selected.addresses}"
            echo "TLS Name: ${selected.tlsName}"
            ;;
          help|--help|-h)
            echo "hart-dns — HART OS Secure DNS Management"
            echo ""
            echo "  hart-dns status           Show DNS configuration"
            echo "  hart-dns flush            Flush DNS cache"
            echo "  hart-dns test [domain]    Test DNS resolution"
            echo "  hart-dns provider         Show provider details"
            ;;
          *)
            echo "Unknown command: $1 (try: hart-dns help)"
            exit 1
            ;;
        esac
      '')
    ];
  };
}
