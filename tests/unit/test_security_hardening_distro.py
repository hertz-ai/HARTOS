"""
Tests for HART OS Security Hardening — Distro Config Files (Phase A + B).

Validates that all deployment configuration files exist and contain
the required security directives. No network calls, no real SSH,
no systemd — purely static file analysis.

Covers:
  1. AppArmor profiles (4 profiles)
  2. Systemd service hardening (6 services)
  3. Journald configuration
  4. Fail2ban jails and filters
  5. Unattended upgrades
  6. Audit rules
  7. DNS hardening (resolved.conf)
  8. Sudoers least-privilege
  9. Immutable key handling (first-boot + recovery)
  10. TFTP path traversal prevention (PXE server)
  11. Mandatory update signatures
  12. OEM SSH key wipe
  13. Network provisioner input validation
  14. Boot audit
  15. Backup system
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_file(*path_parts):
    """Read a file relative to repo root and return its content."""
    filepath = os.path.join(REPO_ROOT, *path_parts)
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


# ===================================================================
# 1. AppArmor Profiles
# ===================================================================

class TestAppArmorProfiles:
    """Validate AppArmor profiles exist and contain required directives."""

    PROFILES = ['hart-backend', 'hart-discovery', 'hart-vision', 'hart-llm']

    @pytest.mark.parametrize('profile_name', PROFILES)
    def test_apparmor_profile_exists(self, profile_name):
        """Each AppArmor profile file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'apparmor', profile_name)
        assert os.path.isfile(path), f"AppArmor profile missing: {path}"

    @pytest.mark.parametrize('profile_name', PROFILES)
    def test_apparmor_profile_has_profile_block(self, profile_name):
        """Each profile must have a valid profile block (opening brace)."""
        content = _read_file('deploy', 'linux', 'apparmor', profile_name)
        # AppArmor profiles define a path followed by {
        assert '{' in content, f"{profile_name}: missing profile block opening brace"
        assert '}' in content, f"{profile_name}: missing profile block closing brace"

    @pytest.mark.parametrize('profile_name', PROFILES)
    def test_apparmor_profile_has_includes(self, profile_name):
        """Each profile must include tunables/global."""
        content = _read_file('deploy', 'linux', 'apparmor', profile_name)
        assert '#include <tunables/global>' in content, \
            f"{profile_name}: missing tunables/global include"

    @pytest.mark.parametrize('profile_name', PROFILES)
    def test_apparmor_profile_has_deny_rules(self, profile_name):
        """Each profile must have at least one deny rule."""
        content = _read_file('deploy', 'linux', 'apparmor', profile_name)
        assert 'deny ' in content, \
            f"{profile_name}: no deny rules found"

    def test_backend_denies_etc_shadow(self):
        """Backend profile must deny /etc/shadow access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-backend')
        assert 'deny /etc/shadow' in content

    def test_backend_denies_root_home(self):
        """Backend profile must deny /root/ access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-backend')
        assert 'deny /root/' in content

    def test_discovery_denies_database_write(self):
        """Discovery profile must deny database write access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-discovery')
        assert 'deny /var/lib/hart/hevolve_database.db' in content

    def test_vision_allows_nvidia_devices(self):
        """Vision profile must allow /dev/nvidia* access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-vision')
        assert '/dev/nvidia*' in content

    def test_vision_denies_private_key(self):
        """Vision profile must deny private key access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-vision')
        assert 'deny /var/lib/hart/node_private.key' in content

    def test_llm_allows_model_read(self):
        """LLM profile must allow model file reading."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-llm')
        assert '/opt/hart/models/**' in content

    def test_llm_denies_private_key(self):
        """LLM profile must deny private key access."""
        content = _read_file('deploy', 'linux', 'apparmor', 'hart-llm')
        assert 'deny /var/lib/hart/node_private.key' in content


# ===================================================================
# 2. Systemd Service Hardening
# ===================================================================

class TestSystemdHardening:
    """Validate that all 6 systemd service files have hardening directives."""

    SERVICES = [
        'hart-backend.service',
        'hart-discovery.service',
        'hart-vision.service',
        'hart-llm.service',
        'hart-agent-daemon.service',
        'hart-dbus.service',
    ]

    REQUIRED_DIRECTIVES = [
        'ProtectClock=yes',
        'SystemCallFilter=@system-service',
        'LockPersonality=yes',
        'ProtectKernelModules=yes',
    ]

    @pytest.mark.parametrize('service_name', SERVICES)
    def test_service_file_exists(self, service_name):
        """Each systemd service file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'systemd', service_name)
        assert os.path.isfile(path), f"Service file missing: {path}"

    @pytest.mark.parametrize('directive', REQUIRED_DIRECTIVES)
    @pytest.mark.parametrize('service_name', SERVICES)
    def test_service_has_hardening_directive(self, service_name, directive):
        """Each service must contain the required hardening directive."""
        content = _read_file('deploy', 'linux', 'systemd', service_name)
        assert directive in content, \
            f"{service_name}: missing hardening directive '{directive}'"


# ===================================================================
# 3. Journald Configuration
# ===================================================================

class TestJournaldConfig:
    """Validate journald configuration has required settings."""

    def test_journald_config_exists(self):
        """Journald config file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'journald',
                            'hart-journald.conf')
        assert os.path.isfile(path)

    def test_journald_has_system_max_use(self):
        """Journald must limit disk usage."""
        content = _read_file('deploy', 'distro', 'journald', 'hart-journald.conf')
        assert 'SystemMaxUse=' in content

    def test_journald_has_max_retention(self):
        """Journald must have a retention policy."""
        content = _read_file('deploy', 'distro', 'journald', 'hart-journald.conf')
        assert 'MaxRetentionSec=' in content

    def test_journald_has_compression(self):
        """Journald must have compression enabled."""
        content = _read_file('deploy', 'distro', 'journald', 'hart-journald.conf')
        assert 'Compress=yes' in content


# ===================================================================
# 4. Fail2ban
# ===================================================================

class TestFail2ban:
    """Validate fail2ban jail and filter configurations."""

    def test_ssh_jail_exists(self):
        """SSH jail config file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'fail2ban',
                            'hart-sshd.conf')
        assert os.path.isfile(path)

    def test_ssh_jail_has_maxretry(self):
        """SSH jail must have a maxretry setting."""
        content = _read_file('deploy', 'linux', 'fail2ban', 'hart-sshd.conf')
        assert 'maxretry' in content.lower()

    def test_ssh_jail_has_bantime(self):
        """SSH jail must have a bantime setting."""
        content = _read_file('deploy', 'linux', 'fail2ban', 'hart-sshd.conf')
        assert 'bantime' in content.lower()

    def test_api_filter_exists(self):
        """API filter config file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'fail2ban',
                            'hart-api-filter.conf')
        assert os.path.isfile(path)

    def test_api_filter_has_rate_limit_regex(self):
        """API filter must detect rate limit violations."""
        content = _read_file('deploy', 'linux', 'fail2ban', 'hart-api-filter.conf')
        assert 'rate limit' in content.lower()

    def test_api_filter_has_auth_failure_regex(self):
        """API filter must detect authentication failures."""
        content = _read_file('deploy', 'linux', 'fail2ban', 'hart-api-filter.conf')
        assert 'authentication failed' in content.lower()


# ===================================================================
# 5. Unattended Upgrades
# ===================================================================

class TestUnattendedUpgrades:
    """Validate unattended-upgrades configuration."""

    def test_unattended_upgrades_exists(self):
        """Unattended upgrades config file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'apt',
                            '50hart-unattended-upgrades')
        assert os.path.isfile(path)

    def test_has_security_origin(self):
        """Must have security repository as an allowed origin."""
        content = _read_file('deploy', 'distro', 'apt',
                             '50hart-unattended-upgrades')
        assert 'security' in content.lower()

    def test_no_auto_reboot(self):
        """Auto-reboot must be disabled (false)."""
        content = _read_file('deploy', 'distro', 'apt',
                             '50hart-unattended-upgrades')
        assert 'Automatic-Reboot' in content
        assert '"false"' in content


# ===================================================================
# 6. Audit Rules
# ===================================================================

class TestAuditRules:
    """Validate Linux audit rules for HART OS."""

    def test_audit_rules_exist(self):
        """Audit rules file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'audit',
                            'hart-audit.rules')
        assert os.path.isfile(path)

    def test_monitors_private_key_access(self):
        """Audit rules must monitor private key access."""
        content = _read_file('deploy', 'linux', 'audit', 'hart-audit.rules')
        assert 'node_private.key' in content

    def test_monitors_code_modifications(self):
        """Audit rules must monitor code directory modifications."""
        content = _read_file('deploy', 'linux', 'audit', 'hart-audit.rules')
        assert '/opt/hart/' in content
        assert 'hart_code_change' in content

    def test_monitors_systemctl(self):
        """Audit rules must monitor systemctl usage."""
        content = _read_file('deploy', 'linux', 'audit', 'hart-audit.rules')
        assert 'systemctl' in content


# ===================================================================
# 7. DNS Hardening
# ===================================================================

class TestDNSHardening:
    """Validate DNS resolver hardening configuration."""

    def test_resolved_conf_exists(self):
        """DNS resolved config must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'network',
                            'hart-resolved.conf')
        assert os.path.isfile(path)

    def test_dnssec_configured(self):
        """DNSSEC must be configured."""
        content = _read_file('deploy', 'distro', 'network', 'hart-resolved.conf')
        assert 'DNSSEC=' in content

    def test_dns_over_tls_configured(self):
        """DNS-over-TLS must be configured."""
        content = _read_file('deploy', 'distro', 'network', 'hart-resolved.conf')
        assert 'DNSOverTLS=' in content

    def test_llmnr_disabled(self):
        """LLMNR must be disabled (security risk on untrusted networks)."""
        content = _read_file('deploy', 'distro', 'network', 'hart-resolved.conf')
        assert 'LLMNR=no' in content


# ===================================================================
# 8. Sudoers
# ===================================================================

class TestSudoers:
    """Validate sudoers least-privilege configuration."""

    def test_sudoers_file_exists(self):
        """Sudoers drop-in file must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'linux', 'sudoers', 'hart-update')
        assert os.path.isfile(path)

    def test_has_nopasswd_for_specific_commands(self):
        """Must use NOPASSWD only for specific systemctl commands."""
        content = _read_file('deploy', 'linux', 'sudoers', 'hart-update')
        assert 'NOPASSWD:' in content
        assert '/bin/systemctl' in content

    def test_no_unrestricted_all(self):
        """Must NOT have ALL=(ALL) ALL (unrestricted root)."""
        content = _read_file('deploy', 'linux', 'sudoers', 'hart-update')
        # Check that there is no line granting unrestricted ALL access
        assert 'ALL=(ALL) ALL' not in content, \
            "Sudoers must not grant unrestricted ALL access"


# ===================================================================
# 9. Immutable Keys (first-boot + recovery)
# ===================================================================

class TestImmutableKeys:
    """Validate chattr immutability handling for node private key."""

    def test_first_boot_sets_immutable_flag(self):
        """First-boot script must set chattr +i on private key."""
        content = _read_file('deploy', 'distro', 'first-boot',
                             'hart-first-boot.sh')
        assert 'chattr +i' in content
        assert 'node_private.key' in content

    def test_recovery_clears_immutable_before_removal(self):
        """Recovery script must clear chattr -i before key removal."""
        content = _read_file('deploy', 'distro', 'recovery', 'hart-recovery.sh')
        assert 'chattr -i' in content
        # The chattr -i must come before rm -f of the private key
        chattr_pos = content.index('chattr -i')
        rm_pos = content.index('rm -f "$DATA_DIR/node_private.key"')
        assert chattr_pos < rm_pos, \
            "chattr -i must occur before rm -f of the private key"


# ===================================================================
# 10. TFTP Path Traversal Prevention (PXE Server)
# ===================================================================

class TestTFTPPathTraversal:
    """Validate PXE server has path traversal protection."""

    def test_pxe_server_has_commonpath_check(self):
        """PXE server must use os.path.commonpath for traversal prevention."""
        content = _read_file('deploy', 'distro', 'pxe', 'hart-pxe-server.py')
        assert 'os.path.commonpath' in content

    def test_pxe_server_has_normpath(self):
        """PXE server must use os.path.normpath to normalize paths."""
        content = _read_file('deploy', 'distro', 'pxe', 'hart-pxe-server.py')
        assert 'os.path.normpath' in content

    def test_pxe_server_strips_dotdot_components(self):
        """PXE server must filter out '..' path components."""
        content = _read_file('deploy', 'distro', 'pxe', 'hart-pxe-server.py')
        # Should have explicit filtering for '..' in path components
        assert "'..' " in content or '".."' in content or "!= '..'" in content, \
            "PXE server must explicitly filter '..' path components"


# ===================================================================
# 11. Mandatory Update Signatures
# ===================================================================

class TestMandatorySignatures:
    """Validate update service requires Ed25519 signatures."""

    def test_update_service_has_require_signature_env(self):
        """Update service must reference HART_UPDATE_REQUIRE_SIGNATURE."""
        content = _read_file('deploy', 'distro', 'update',
                             'hart-update-service.py')
        assert 'HART_UPDATE_REQUIRE_SIGNATURE' in content

    def test_update_service_defaults_to_requiring_signatures(self):
        """Signature requirement must default to true (secure by default)."""
        content = _read_file('deploy', 'distro', 'update',
                             'hart-update-service.py')
        # The default should be 'true' — unsigned updates rejected by default
        assert "'true'" in content or '"true"' in content, \
            "HART_UPDATE_REQUIRE_SIGNATURE must default to 'true'"


# ===================================================================
# 12. OEM SSH Key Wipe
# ===================================================================

class TestOEMSSHKeyWipe:
    """Validate OEM config wipes SSH host keys during prepare."""

    def test_oem_prepare_removes_ssh_host_keys(self):
        """OEM --prepare must remove SSH host key files."""
        content = _read_file('deploy', 'distro', 'oem', 'hart-oem-config.sh')
        assert 'rm -f /etc/ssh/ssh_host_*' in content

    def test_oem_user_setup_regenerates_ssh_keys(self):
        """OEM --user-setup must regenerate SSH host keys with ssh-keygen -A."""
        content = _read_file('deploy', 'distro', 'oem', 'hart-oem-config.sh')
        assert 'ssh-keygen -A' in content


# ===================================================================
# 13. Network Provisioner Input Validation
# ===================================================================

class TestNetworkProvisionerValidation:
    """Validate network provisioner has input validation."""

    def test_has_validate_params_method(self):
        """Network provisioner must have a _validate_params method."""
        content = _read_file('integrations', 'agent_engine',
                             'network_provisioner.py')
        assert '_validate_params' in content

    def test_has_hostname_regex_validation(self):
        """Network provisioner must validate hostnames with a regex."""
        content = _read_file('integrations', 'agent_engine',
                             'network_provisioner.py')
        # Should have a hostname regex pattern
        assert re.search(r'_HOSTNAME_RE|hostname.*re\.compile|re\.compile.*hostname',
                         content), \
            "Network provisioner must have hostname regex validation"

    def test_has_reject_policy_for_strict_mode(self):
        """Network provisioner must support RejectPolicy for strict host key checking."""
        content = _read_file('integrations', 'agent_engine',
                             'network_provisioner.py')
        assert 'RejectPolicy' in content


# ===================================================================
# 14. Boot Audit
# ===================================================================

class TestBootAudit:
    """Validate boot audit script exists and has signing logic."""

    def test_boot_audit_script_exists(self):
        """Boot audit script must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'first-boot',
                            'hart-boot-audit.sh')
        assert os.path.isfile(path)

    def test_boot_audit_has_signing_logic(self):
        """Boot audit must sign entries with Ed25519."""
        content = _read_file('deploy', 'distro', 'first-boot',
                             'hart-boot-audit.sh')
        assert 'Ed25519PrivateKey' in content or 'signature' in content.lower()
        assert '.sign(' in content or 'SIGNATURE' in content

    def test_first_boot_calls_boot_audit(self):
        """First-boot script must call the boot audit script."""
        content = _read_file('deploy', 'distro', 'first-boot',
                             'hart-first-boot.sh')
        assert 'hart-boot-audit.sh' in content


# ===================================================================
# 15. Backup System
# ===================================================================

class TestBackupSystem:
    """Validate backup scripts and systemd timer/service."""

    def test_backup_script_exists(self):
        """Backup shell script must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'backup',
                            'hart-backup.sh')
        assert os.path.isfile(path)

    def test_backup_script_has_tar(self):
        """Backup script must create tar archives."""
        content = _read_file('deploy', 'distro', 'backup', 'hart-backup.sh')
        assert 'tar ' in content or 'tar -' in content

    def test_backup_script_has_sha256sum(self):
        """Backup script must verify integrity with SHA-256."""
        content = _read_file('deploy', 'distro', 'backup', 'hart-backup.sh')
        assert 'sha256sum' in content

    def test_backup_script_has_chattr_handling(self):
        """Backup script must handle immutable flag on private key."""
        content = _read_file('deploy', 'distro', 'backup', 'hart-backup.sh')
        assert 'chattr' in content

    def test_backup_script_has_retention(self):
        """Backup script must have a retention policy."""
        content = _read_file('deploy', 'distro', 'backup', 'hart-backup.sh')
        assert 'RETENTION' in content or 'retention' in content

    def test_backup_timer_exists(self):
        """Backup systemd timer must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'backup',
                            'hart-backup.timer')
        assert os.path.isfile(path)

    def test_backup_timer_has_on_calendar(self):
        """Backup timer must have an OnCalendar schedule."""
        content = _read_file('deploy', 'distro', 'backup', 'hart-backup.timer')
        assert 'OnCalendar=' in content

    def test_backup_service_exists(self):
        """Backup systemd service must exist."""
        path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'backup',
                            'hart-backup.service')
        assert os.path.isfile(path)

    def test_backup_service_is_oneshot(self):
        """Backup service must be Type=oneshot."""
        content = _read_file('deploy', 'distro', 'backup',
                             'hart-backup.service')
        assert 'Type=oneshot' in content
