# HART OS — SSO / LDAP Authentication (sssd)
#
# Enterprise single sign-on via sssd with LDAP and Kerberos backends.
# Integrates with PAM for system-wide authentication.
#
# CLI: hart-sso status|test|cache-clear

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.sso;
in
{
  options.hart.sso = {
    enable = lib.mkEnableOption "HART OS SSO/LDAP authentication (sssd)";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "SSO domain name (e.g., 'corp.example.com').";
      example = "corp.example.com";
    };

    ldapUri = lib.mkOption {
      type = lib.types.str;
      description = "LDAP server URI.";
      example = "ldaps://ldap.corp.example.com";
    };

    ldapBaseDn = lib.mkOption {
      type = lib.types.str;
      description = "LDAP base distinguished name for user lookups.";
      example = "dc=corp,dc=example,dc=com";
    };

    kerberosRealm = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Kerberos realm for authentication (leave empty to disable Kerberos).";
      example = "CORP.EXAMPLE.COM";
    };

    ldapTlsCaCert = lib.mkOption {
      type = lib.types.str;
      default = "/etc/ssl/certs/ca-certificates.crt";
      description = "Path to CA certificate for LDAP TLS verification.";
    };

    cacheCredentials = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Cache credentials for offline login.";
    };

    createHomeDir = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Automatically create home directories for SSO users on first login.";
    };

    minUid = lib.mkOption {
      type = lib.types.int;
      default = 10000;
      description = "Minimum UID for SSO-provided users (avoids local user conflicts).";
    };

    maxUid = lib.mkOption {
      type = lib.types.int;
      default = 60000;
      description = "Maximum UID for SSO-provided users.";
    };

    defaultShell = lib.mkOption {
      type = lib.types.str;
      default = "/run/current-system/sw/bin/bash";
      description = "Default login shell for SSO users.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Required packages ──
    environment.systemPackages = with pkgs; [
      sssd
      krb5

      (writeShellScriptBin "hart-sso" ''
        case "''${1:-status}" in
          status)
            echo "=== HART OS SSO Authentication ==="
            echo "Domain:     ${cfg.domain}"
            echo "LDAP URI:   ${cfg.ldapUri}"
            echo "Base DN:    ${cfg.ldapBaseDn}"
            echo "Kerberos:   ${if cfg.kerberosRealm != "" then cfg.kerberosRealm else "disabled"}"
            echo "Cache:      ${if cfg.cacheCredentials then "enabled" else "disabled"}"
            echo "Auto home:  ${if cfg.createHomeDir then "yes" else "no"}"
            echo ""
            echo "sssd service:"
            systemctl status sssd --no-pager 2>/dev/null || echo "  sssd not running"
            ;;
          test)
            user="''${2:-}"
            if [ -z "$user" ]; then
              echo "Usage: hart-sso test <username>"
              exit 1
            fi
            echo "Looking up user: $user"
            echo ""
            echo "NSS lookup:"
            getent passwd "$user" 2>/dev/null || echo "  User not found in NSS"
            echo ""
            echo "SSO info:"
            ${sssd}/bin/sss_cache -u "$user" 2>/dev/null
            id "$user" 2>/dev/null || echo "  Cannot resolve user"
            ;;
          cache-clear)
            echo "Clearing sssd cache..."
            sudo ${sssd}/bin/sss_cache -E 2>/dev/null && echo "Cache cleared." \
              || echo "Failed — ensure sssd is running"
            sudo systemctl restart sssd 2>/dev/null && echo "sssd restarted." \
              || echo "Failed to restart sssd"
            ;;
          help|--help|-h)
            echo "hart-sso — HART OS SSO/LDAP Authentication"
            echo ""
            echo "  hart-sso status            Show SSO configuration"
            echo "  hart-sso test <user>        Look up an SSO user"
            echo "  hart-sso cache-clear        Flush credential cache"
            ;;
          *)
            echo "Unknown command: $1 (try: hart-sso help)"
            exit 1
            ;;
        esac
      '')
    ];

    # ── sssd configuration ──
    environment.etc."sssd/sssd.conf" = {
      mode = "0600";
      text = ''
        [sssd]
        services = nss, pam
        config_file_version = 2
        domains = ${cfg.domain}

        [nss]
        filter_groups = root
        filter_users = root

        [pam]
        offline_credentials_expiration = 7

        [domain/${cfg.domain}]
        id_provider = ldap
        auth_provider = ${if cfg.kerberosRealm != "" then "krb5" else "ldap"}
        access_provider = ldap

        ldap_uri = ${cfg.ldapUri}
        ldap_search_base = ${cfg.ldapBaseDn}
        ldap_tls_cacert = ${cfg.ldapTlsCaCert}
        ldap_id_use_start_tls = True
        ldap_tls_reqcert = demand

        cache_credentials = ${if cfg.cacheCredentials then "True" else "False"}

        min_id = ${toString cfg.minUid}
        max_id = ${toString cfg.maxUid}

        default_shell = ${cfg.defaultShell}
        fallback_homedir = /home/%u
      '' + lib.optionalString (cfg.kerberosRealm != "") ''

        krb5_realm = ${cfg.kerberosRealm}
        krb5_server = ${cfg.ldapUri}
        krb5_kpasswd = ${cfg.ldapUri}
      '';
    };

    # ── Kerberos configuration ──
    environment.etc."krb5.conf" = lib.mkIf (cfg.kerberosRealm != "") {
      text = ''
        [libdefaults]
          default_realm = ${cfg.kerberosRealm}
          dns_lookup_realm = true
          dns_lookup_kdc = true
          forwardable = true
          proxiable = true
          default_ccache_name = FILE:/tmp/krb5cc_%{uid}

        [realms]
          ${cfg.kerberosRealm} = {
            admin_server = ${cfg.ldapUri}
          }

        [domain_realm]
          .${cfg.domain} = ${cfg.kerberosRealm}
          ${cfg.domain} = ${cfg.kerberosRealm}
      '';
    };

    # ── sssd systemd service ──
    systemd.services.sssd = {
      description = "System Security Services Daemon";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "notify";
        ExecStart = "${pkgs.sssd}/bin/sssd -i --logger=journald";
        Restart = "on-failure";
        RestartSec = 5;
        PIDFile = "/run/sssd.pid";
      };

      preStart = ''
        mkdir -p /var/lib/sss/db /var/lib/sss/pipes /var/lib/sss/mc /var/lib/sss/pubconf
        chmod 0700 /var/lib/sss/db
      '';
    };

    # ── PAM integration ──
    security.pam.services.login.makeHomeDir = cfg.createHomeDir;
    security.pam.services.sshd.makeHomeDir = cfg.createHomeDir;

    security.pam.services.login.sssdStrictAccess = true;
    security.pam.services.sshd.sssdStrictAccess = true;

    # ── NSS integration: resolve users/groups via sssd ──
    system.nssDatabases.passwd = [ "sss" ];
    system.nssDatabases.group = [ "sss" ];
    system.nssDatabases.shadow = [ "sss" ];

    # ── Auto-create home directories on login ──
    security.pam.services.login.pamMount = cfg.createHomeDir;
  };
}
