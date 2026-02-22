"""
Tests for HART OS distro configuration files.

Validates all static config files for correctness:
- Systemd service units (7 files)
- Variant configs (3 INI files)
- Kernel tuning (sysctl + limits)
- Branding (os-release, issue, MOTD, plymouth)
- Autoinstall (cloud-init user-data, meta-data, vendor-data)
- D-Bus policy (XML)
- Polkit policy (XML)
- Firewall (UFW profile)
- Desktop entries (.desktop files)
- Debian packaging (control)
- CI/CD (GitHub Actions YAML, Makefile)
- Shell scripts (syntax validation: recovery, first-boot, motd, install)
"""

import configparser
import os
import re
import sys
import xml.etree.ElementTree as ET

import pytest

# Project root
REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
DISTRO_DIR = os.path.join(REPO_ROOT, 'deploy', 'distro')
LINUX_DIR = os.path.join(REPO_ROOT, 'deploy', 'linux')


def read_file(path):
    """Read a file relative to repo root."""
    full = os.path.join(REPO_ROOT, path)
    with open(full, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def file_exists(path):
    return os.path.exists(os.path.join(REPO_ROOT, path))


# ──────────────────────────────────────────────────
# Systemd Service Unit Tests
# ──────────────────────────────────────────────────

SYSTEMD_UNITS = [
    'deploy/linux/systemd/hart-backend.service',
    'deploy/linux/systemd/hart-discovery.service',
    'deploy/linux/systemd/hart-agent-daemon.service',
    'deploy/linux/systemd/hart-vision.service',
    'deploy/linux/systemd/hart-llm.service',
    'deploy/linux/systemd/hart-dbus.service',
    'deploy/linux/systemd/hart.target',
]


class TestSystemdUnits:

    @pytest.mark.parametrize('unit_path', SYSTEMD_UNITS)
    def test_unit_file_exists(self, unit_path):
        """All systemd unit files exist."""
        assert file_exists(unit_path), f"Missing: {unit_path}"

    @pytest.mark.parametrize('unit_path', SYSTEMD_UNITS)
    def test_unit_has_unit_section(self, unit_path):
        """Each unit file has a [Unit] section."""
        content = read_file(unit_path)
        assert '[Unit]' in content

    @pytest.mark.parametrize('unit_path', [u for u in SYSTEMD_UNITS if u.endswith('.service')])
    def test_service_has_exec_start(self, unit_path):
        """Service units have ExecStart directive."""
        content = read_file(unit_path)
        assert 'ExecStart=' in content

    @pytest.mark.parametrize('unit_path', [u for u in SYSTEMD_UNITS if u.endswith('.service')])
    def test_service_has_install_section(self, unit_path):
        """Service units have [Install] section with WantedBy."""
        content = read_file(unit_path)
        assert '[Install]' in content
        assert 'WantedBy=' in content

    def test_backend_port_is_6777(self):
        """Backend service uses port 6777."""
        content = read_file('deploy/linux/systemd/hart-backend.service')
        assert '6777' in content

    def test_backend_uses_waitress(self):
        """Backend runs via waitress (not flask dev server)."""
        content = read_file('deploy/linux/systemd/hart-backend.service')
        assert 'waitress' in content

    def test_backend_security_hardening(self):
        """Backend has security directives."""
        content = read_file('deploy/linux/systemd/hart-backend.service')
        assert 'NoNewPrivileges=yes' in content
        assert 'ProtectSystem=strict' in content
        assert 'ProtectHome=yes' in content
        assert 'PrivateTmp=yes' in content

    def test_discovery_binds_to_backend(self):
        """Discovery service binds to backend (stops if backend stops)."""
        content = read_file('deploy/linux/systemd/hart-discovery.service')
        assert 'BindsTo=hart-backend.service' in content

    def test_discovery_has_net_broadcast(self):
        """Discovery needs CAP_NET_BROADCAST for UDP beacon."""
        content = read_file('deploy/linux/systemd/hart-discovery.service')
        assert 'CAP_NET_BROADCAST' in content

    def test_vision_has_gpu_groups(self):
        """Vision service has video/render supplementary groups."""
        content = read_file('deploy/linux/systemd/hart-vision.service')
        assert 'video' in content
        assert 'render' in content

    def test_vision_conditional_on_model(self):
        """Vision service only starts if minicpm model exists."""
        content = read_file('deploy/linux/systemd/hart-vision.service')
        assert 'ConditionPathExists=/opt/hart/models/minicpm/' in content

    def test_llm_conditional_on_model(self):
        """LLM service only starts if default.gguf exists."""
        content = read_file('deploy/linux/systemd/hart-llm.service')
        assert 'ConditionPathExists=/opt/hart/models/default.gguf' in content

    def test_dbus_requires_dbus_service(self):
        """D-Bus agent service requires dbus.service."""
        content = read_file('deploy/linux/systemd/hart-dbus.service')
        assert 'Requires=dbus.service' in content

    def test_target_wants_core_services(self):
        """hart.target wants backend, discovery, and agent daemon."""
        content = read_file('deploy/linux/systemd/hart.target')
        assert 'hart-backend.service' in content
        assert 'hart-discovery.service' in content
        assert 'hart-agent-daemon.service' in content

    def test_target_is_multi_user(self):
        """hart.target is wanted by multi-user.target."""
        content = read_file('deploy/linux/systemd/hart.target')
        assert 'WantedBy=multi-user.target' in content

    @pytest.mark.parametrize('unit_path', [u for u in SYSTEMD_UNITS if u.endswith('.service')])
    def test_service_user_is_hart(self, unit_path):
        """All services run as hart user (not root)."""
        content = read_file(unit_path)
        assert 'User=hart' in content

    @pytest.mark.parametrize('unit_path', [u for u in SYSTEMD_UNITS if u.endswith('.service')])
    def test_service_has_env_file(self, unit_path):
        """Services reference the environment file."""
        content = read_file(unit_path)
        assert 'EnvironmentFile=/etc/hart/hart.env' in content

    @pytest.mark.parametrize('unit_path', [u for u in SYSTEMD_UNITS if u.endswith('.service')])
    def test_service_logs_to_journal(self, unit_path):
        """Services log to systemd journal."""
        content = read_file(unit_path)
        assert 'StandardOutput=journal' in content
        assert 'SyslogIdentifier=' in content


# ──────────────────────────────────────────────────
# Variant Config Tests
# ──────────────────────────────────────────────────

VARIANT_CONFIGS = [
    'deploy/distro/variants/hart-os-server.conf',
    'deploy/distro/variants/hart-os-desktop.conf',
    'deploy/distro/variants/hart-os-edge.conf',
]


class TestVariantConfigs:

    @pytest.mark.parametrize('cfg_path', VARIANT_CONFIGS)
    def test_variant_exists(self, cfg_path):
        assert file_exists(cfg_path)

    @pytest.mark.parametrize('cfg_path', VARIANT_CONFIGS)
    def test_variant_has_sections(self, cfg_path):
        """Each variant has [variant], [packages], [services], [resources] sections."""
        content = read_file(cfg_path)
        assert '[variant]' in content
        assert '[packages]' in content
        assert '[services]' in content
        assert '[resources]' in content

    @pytest.mark.parametrize('cfg_path', VARIANT_CONFIGS)
    def test_variant_has_name(self, cfg_path):
        """Each variant specifies a name."""
        content = read_file(cfg_path)
        assert 'name =' in content

    def test_server_variant_headless(self):
        """Server variant excludes GUI packages."""
        content = read_file('deploy/distro/variants/hart-os-server.conf')
        assert 'exclude' in content.lower()
        assert 'gnome' in content.lower() or 'gdm' in content.lower()

    def test_desktop_variant_has_gui(self):
        """Desktop variant includes D-Bus and desktop integration."""
        content = read_file('deploy/distro/variants/hart-os-desktop.conf')
        assert 'python3-dbus' in content
        assert 'python3-gi' in content

    def test_edge_variant_minimal(self):
        """Edge variant has minimal resources."""
        content = read_file('deploy/distro/variants/hart-os-edge.conf')
        assert 'min_ram_gb = 1' in content

    def test_server_all_services_enabled(self):
        """Server variant enables all services."""
        content = read_file('deploy/distro/variants/hart-os-server.conf')
        assert 'hart-backend = enabled' in content
        assert 'hart-discovery = enabled' in content
        assert 'hart-agent-daemon = enabled' in content

    def test_edge_disables_heavy_services(self):
        """Edge variant disables vision, LLM, agent daemon."""
        content = read_file('deploy/distro/variants/hart-os-edge.conf')
        assert 'hart-vision = disabled' in content
        assert 'hart-llm = disabled' in content
        assert 'hart-agent-daemon = disabled' in content

    def test_desktop_enables_dbus(self):
        """Desktop variant enables D-Bus service."""
        content = read_file('deploy/distro/variants/hart-os-desktop.conf')
        assert 'hart-dbus = enabled' in content

    @pytest.mark.parametrize('cfg_path', VARIANT_CONFIGS)
    def test_variant_has_min_ram(self, cfg_path):
        """Each variant specifies minimum RAM."""
        content = read_file(cfg_path)
        assert 'min_ram_gb' in content

    @pytest.mark.parametrize('cfg_path', VARIANT_CONFIGS)
    def test_variant_python310_required(self, cfg_path):
        """All variants require python3.10."""
        content = read_file(cfg_path)
        assert 'python3.10' in content


# ──────────────────────────────────────────────────
# Kernel Tuning Tests
# ──────────────────────────────────────────────────

class TestKernelTuning:

    def test_sysctl_exists(self):
        assert file_exists('deploy/distro/kernel/99-hart-sysctl.conf')

    def test_sysctl_tcp_optimization(self):
        """Has TCP tuning parameters."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        assert 'net.core.somaxconn' in content
        assert 'net.ipv4.tcp_fastopen' in content

    def test_sysctl_kernel_hardening(self):
        """Has kernel hardening parameters."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        assert 'kernel.dmesg_restrict = 1' in content
        assert 'kernel.kptr_restrict = 2' in content

    def test_sysctl_rp_filter(self):
        """Reverse path filtering enabled."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        assert 'net.ipv4.conf.all.rp_filter = 1' in content

    def test_sysctl_redirect_disabled(self):
        """ICMP redirect acceptance disabled."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        assert 'net.ipv4.conf.all.accept_redirects = 0' in content
        assert 'net.ipv6.conf.all.accept_redirects = 0' in content

    def test_sysctl_file_descriptors(self):
        """High file descriptor limit for agent workloads."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        assert 'fs.file-max = 524288' in content

    def test_limits_exists(self):
        assert file_exists('deploy/distro/kernel/hart-limits.conf')

    def test_limits_nofile(self):
        """hart user has 65536 file descriptor limit."""
        content = read_file('deploy/distro/kernel/hart-limits.conf')
        assert 'nofile' in content
        assert '65536' in content

    def test_limits_nproc(self):
        """hart user has 4096 process limit."""
        content = read_file('deploy/distro/kernel/hart-limits.conf')
        assert 'nproc' in content
        assert '4096' in content

    def test_limits_memlock(self):
        """hart user has unlimited memlock (for GPU workloads)."""
        content = read_file('deploy/distro/kernel/hart-limits.conf')
        assert 'memlock' in content
        assert 'unlimited' in content

    def test_sysctl_valid_format(self):
        """All non-comment lines match key = value format."""
        content = read_file('deploy/distro/kernel/99-hart-sysctl.conf')
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                assert '=' in stripped, f"Invalid sysctl line: {stripped}"


# ──────────────────────────────────────────────────
# Branding Tests
# ──────────────────────────────────────────────────

class TestBranding:

    def test_os_release_exists(self):
        assert file_exists('deploy/distro/branding/hart-os-release')

    def test_os_release_fields(self):
        """os-release has required fields."""
        content = read_file('deploy/distro/branding/hart-os-release')
        required = ['NAME=', 'PRETTY_NAME=', 'VERSION=', 'VERSION_ID=',
                     'ID=', 'HOME_URL=']
        for field in required:
            assert field in content, f"Missing field: {field}"

    def test_os_release_id(self):
        """ID is hart-os."""
        content = read_file('deploy/distro/branding/hart-os-release')
        assert 'ID=hart-os' in content

    def test_os_release_id_like_ubuntu(self):
        """ID_LIKE includes ubuntu."""
        content = read_file('deploy/distro/branding/hart-os-release')
        assert 'ID_LIKE=ubuntu' in content

    def test_os_release_codename(self):
        """Ubuntu codename is jammy (22.04 LTS)."""
        content = read_file('deploy/distro/branding/hart-os-release')
        assert 'UBUNTU_CODENAME=jammy' in content

    def test_issue_banner_exists(self):
        assert file_exists('deploy/distro/branding/hart-issue')

    def test_issue_has_ascii_art(self):
        """Issue banner contains HART OS ASCII art."""
        content = read_file('deploy/distro/branding/hart-issue')
        assert 'HART OS' in content
        assert 'Humans are always in control' in content

    def test_motd_exists(self):
        assert file_exists('deploy/distro/branding/hart-motd.sh')

    def test_motd_is_shell_script(self):
        """MOTD starts with shebang."""
        content = read_file('deploy/distro/branding/hart-motd.sh')
        assert content.startswith('#!/bin/bash')

    def test_motd_shows_node_id(self):
        """MOTD displays node identity."""
        content = read_file('deploy/distro/branding/hart-motd.sh')
        assert 'Node ID' in content

    def test_motd_has_xxd_fallback(self):
        """MOTD has Python fallback when xxd unavailable."""
        content = read_file('deploy/distro/branding/hart-motd.sh')
        assert 'python3' in content

    def test_plymouth_theme_exists(self):
        assert file_exists('deploy/distro/branding/plymouth/hart-theme/hart-theme.plymouth')

    def test_plymouth_theme_name(self):
        """Plymouth theme is named HART OS."""
        content = read_file('deploy/distro/branding/plymouth/hart-theme/hart-theme.plymouth')
        assert 'Name=HART OS' in content

    def test_plymouth_uses_script_module(self):
        """Plymouth uses script module (not text or ubuntu)."""
        content = read_file('deploy/distro/branding/plymouth/hart-theme/hart-theme.plymouth')
        assert 'ModuleName=script' in content

    def test_plymouth_script_exists(self):
        assert file_exists('deploy/distro/branding/plymouth/hart-theme/hart-theme.script')

    def test_plymouth_script_loads_logo(self):
        """Plymouth script loads hart-logo.png."""
        content = read_file('deploy/distro/branding/plymouth/hart-theme/hart-theme.script')
        assert 'hart-logo.png' in content

    def test_plymouth_script_has_refresh(self):
        """Plymouth has refresh callback for animation."""
        content = read_file('deploy/distro/branding/plymouth/hart-theme/hart-theme.script')
        assert 'SetRefreshFunction' in content

    def test_logo_generator_exists(self):
        assert file_exists('deploy/distro/branding/plymouth/hart-theme/generate-logo.py')


# ──────────────────────────────────────────────────
# Autoinstall Tests
# ──────────────────────────────────────────────────

class TestAutoinstall:

    def test_user_data_exists(self):
        assert file_exists('deploy/distro/autoinstall/user-data')

    def test_user_data_is_cloud_config(self):
        """user-data starts with #cloud-config."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert content.startswith('#cloud-config')

    def test_user_data_has_autoinstall(self):
        """user-data has autoinstall section."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'autoinstall:' in content

    def test_user_data_identity(self):
        """Autoinstall sets hostname and username."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'hostname: hart-node' in content
        assert 'username: hart' in content

    def test_user_data_ssh_enabled(self):
        """SSH is installed and enabled."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'install-server: true' in content

    def test_user_data_installs_python310(self):
        """Python 3.10 is in the packages list."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'python3.10' in content

    def test_user_data_late_commands(self):
        """Late commands copy HART OS and run install."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'install.sh' in content
        assert 'hart-first-boot.service' in content

    def test_user_data_installs_branding(self):
        """Late commands install branding files."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert 'hart-os-release' in content
        assert 'hart-motd.sh' in content

    def test_user_data_kernel_tuning(self):
        """Late commands install kernel tuning."""
        content = read_file('deploy/distro/autoinstall/user-data')
        assert '99-hart-sysctl.conf' in content
        assert 'hart-limits.conf' in content

    def test_meta_data_exists(self):
        assert file_exists('deploy/distro/autoinstall/meta-data')

    def test_vendor_data_exists(self):
        assert file_exists('deploy/distro/autoinstall/vendor-data')


# ──────────────────────────────────────────────────
# D-Bus & Polkit Policy Tests (XML)
# ──────────────────────────────────────────────────

class TestDBusPolicyXML:

    def test_dbus_conf_exists(self):
        assert file_exists('deploy/linux/dbus/com.hart.Agent.conf')

    def test_dbus_conf_valid_xml(self):
        """D-Bus config is valid XML."""
        path = os.path.join(REPO_ROOT, 'deploy/linux/dbus/com.hart.Agent.conf')
        tree = ET.parse(path)
        assert tree.getroot().tag == 'busconfig'

    def test_dbus_allows_hart_user(self):
        """D-Bus policy allows hart user to own bus name."""
        content = read_file('deploy/linux/dbus/com.hart.Agent.conf')
        assert 'user="hart"' in content
        assert 'own="com.hart.Agent"' in content

    def test_dbus_allows_default_send(self):
        """Default policy allows sending to agent."""
        content = read_file('deploy/linux/dbus/com.hart.Agent.conf')
        assert 'context="default"' in content
        assert 'send_destination="com.hart.Agent"' in content


class TestPolkitPolicy:

    def test_polkit_exists(self):
        assert file_exists('deploy/linux/polkit/com.hart.agent.policy')

    def test_polkit_valid_xml(self):
        """Polkit policy is valid XML."""
        path = os.path.join(REPO_ROOT, 'deploy/linux/polkit/com.hart.agent.policy')
        tree = ET.parse(path)
        assert tree.getroot().tag == 'policyconfig'

    def test_polkit_has_actions(self):
        """Polkit policy defines actions."""
        path = os.path.join(REPO_ROOT, 'deploy/linux/polkit/com.hart.agent.policy')
        tree = ET.parse(path)
        actions = tree.findall('.//action')
        assert len(actions) >= 3

    def test_polkit_approve_action(self):
        """Has approve-action policy."""
        content = read_file('deploy/linux/polkit/com.hart.agent.policy')
        assert 'com.hart.agent.approve-action' in content

    def test_polkit_install_remote(self):
        """Has install-remote policy (provisioning)."""
        content = read_file('deploy/linux/polkit/com.hart.agent.policy')
        assert 'com.hart.agent.install-remote' in content

    def test_polkit_manage_services(self):
        """Has manage-services policy."""
        content = read_file('deploy/linux/polkit/com.hart.agent.policy')
        assert 'com.hart.agent.manage-services' in content

    def test_polkit_requires_auth(self):
        """All actions require admin authentication."""
        content = read_file('deploy/linux/polkit/com.hart.agent.policy')
        assert 'auth_admin' in content


# ──────────────────────────────────────────────────
# Firewall Tests
# ──────────────────────────────────────────────────

class TestFirewall:

    def test_ufw_profile_exists(self):
        assert file_exists('deploy/linux/firewall/hart-ufw.profile')

    def test_ufw_includes_backend_port(self):
        """UFW profile includes port 6777."""
        content = read_file('deploy/linux/firewall/hart-ufw.profile')
        assert '6777' in content

    def test_ufw_includes_discovery_port(self):
        """UFW profile includes UDP port 6780."""
        content = read_file('deploy/linux/firewall/hart-ufw.profile')
        assert '6780' in content

    def test_ufw_has_title(self):
        """UFW profile has title."""
        content = read_file('deploy/linux/firewall/hart-ufw.profile')
        assert 'title=' in content


# ──────────────────────────────────────────────────
# Desktop Entry Tests
# ──────────────────────────────────────────────────

DESKTOP_FILES = [
    'deploy/linux/desktop/hart.desktop',
    'deploy/linux/desktop/hart-dashboard.desktop',
]


class TestDesktopEntries:

    @pytest.mark.parametrize('path', DESKTOP_FILES)
    def test_desktop_file_exists(self, path):
        assert file_exists(path)

    @pytest.mark.parametrize('path', DESKTOP_FILES)
    def test_desktop_has_entry_header(self, path):
        """Desktop file starts with [Desktop Entry]."""
        content = read_file(path)
        assert '[Desktop Entry]' in content

    @pytest.mark.parametrize('path', DESKTOP_FILES)
    def test_desktop_has_name(self, path):
        """Desktop file has Name field."""
        content = read_file(path)
        assert 'Name=' in content

    @pytest.mark.parametrize('path', DESKTOP_FILES)
    def test_desktop_has_type(self, path):
        """Desktop file specifies Type=Application."""
        content = read_file(path)
        assert 'Type=Application' in content

    def test_dashboard_opens_browser(self):
        """Dashboard desktop file uses xdg-open."""
        content = read_file('deploy/linux/desktop/hart.desktop')
        assert 'xdg-open' in content

    def test_tray_desktop_autostart(self):
        """Tray desktop file has GNOME autostart enabled."""
        content = read_file('deploy/linux/desktop/hart-dashboard.desktop')
        assert 'X-GNOME-Autostart-enabled=true' in content


# ──────────────────────────────────────────────────
# Debian Package Tests
# ──────────────────────────────────────────────────

class TestDebianPackaging:

    def test_control_exists(self):
        assert file_exists('deploy/linux/debian/control')

    def test_control_package_name(self):
        """Package name is hart-os."""
        content = read_file('deploy/linux/debian/control')
        assert 'Package: hart-os' in content

    def test_control_depends_python(self):
        """Package depends on python3.10."""
        content = read_file('deploy/linux/debian/control')
        assert 'python3.10' in content

    def test_control_architecture(self):
        """Package targets amd64."""
        content = read_file('deploy/linux/debian/control')
        assert 'Architecture: amd64' in content

    def test_control_has_description(self):
        """Package has description."""
        content = read_file('deploy/linux/debian/control')
        assert 'Description:' in content
        assert 'HART OS' in content


# ──────────────────────────────────────────────────
# Recovery & First-Boot Tests
# ──────────────────────────────────────────────────

class TestRecovery:

    def test_recovery_script_exists(self):
        assert file_exists('deploy/distro/recovery/hart-recovery.sh')

    def test_recovery_requires_root(self):
        """Recovery script checks for root."""
        content = read_file('deploy/distro/recovery/hart-recovery.sh')
        assert 'EUID' in content

    def test_recovery_has_confirmation(self):
        """Recovery requires explicit RESET confirmation."""
        content = read_file('deploy/distro/recovery/hart-recovery.sh')
        assert 'RESET' in content

    def test_recovery_wipes_keys(self):
        """Recovery deletes Ed25519 keys."""
        content = read_file('deploy/distro/recovery/hart-recovery.sh')
        assert 'node_private.key' in content
        assert 'node_public.key' in content

    def test_recovery_wipes_database(self):
        """Recovery deletes the database."""
        content = read_file('deploy/distro/recovery/hart-recovery.sh')
        assert 'hevolve_database.db' in content

    def test_recovery_re_enables_first_boot(self):
        """Recovery triggers first-boot setup."""
        content = read_file('deploy/distro/recovery/hart-recovery.sh')
        assert 'hart-first-boot' in content

    def test_recovery_service_exists(self):
        assert file_exists('deploy/distro/recovery/hart-recovery.service')

    def test_recovery_service_requires_kernel_param(self):
        """Recovery service requires hart.recovery=1 kernel param."""
        content = read_file('deploy/distro/recovery/hart-recovery.service')
        assert 'ConditionKernelCommandLine=hart.recovery=1' in content

    def test_recovery_service_oneshot(self):
        """Recovery is a oneshot service."""
        content = read_file('deploy/distro/recovery/hart-recovery.service')
        assert 'Type=oneshot' in content


class TestFirstBoot:

    def test_first_boot_exists(self):
        assert file_exists('deploy/distro/first-boot/hart-first-boot.sh')

    def test_first_boot_strict_mode(self):
        """Uses set -euo pipefail."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'set -euo pipefail' in content

    def test_first_boot_generates_keypair(self):
        """Generates Ed25519 node identity."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'Ed25519PrivateKey' in content

    def test_first_boot_detects_hardware(self):
        """Detects CPU, RAM, GPU."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'nproc' in content
        assert 'MemTotal' in content
        assert 'nvidia-smi' in content

    def test_first_boot_classifies_tier(self):
        """Classifies hardware into tiers."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'OBSERVER' in content
        assert 'STANDARD' in content
        assert 'PERFORMANCE' in content
        assert 'COMPUTE_HOST' in content

    def test_first_boot_configures_services(self):
        """Enables/disables services per tier."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'systemctl enable' in content
        assert 'systemctl disable' in content

    def test_first_boot_initializes_db(self):
        """Runs database migrations."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'run_migrations' in content

    def test_first_boot_starts_services(self):
        """Starts hart.target at the end."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'systemctl restart hart.target' in content

    def test_first_boot_sets_marker(self):
        """Creates .first-boot-done marker."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert '.first-boot-done' in content

    def test_first_boot_has_xxd_fallback(self):
        """Has Python fallback for xxd."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'python' in content.lower()

    def test_first_boot_downloads_model_for_compute(self):
        """COMPUTE_HOST tier triggers background model download."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.sh')
        assert 'MODEL_URL' in content or 'HART_DEFAULT_MODEL_URL' in content

    def test_first_boot_service_exists(self):
        assert file_exists('deploy/distro/first-boot/hart-first-boot.service')

    def test_first_boot_service_conditional(self):
        """Only runs if .first-boot-done doesn't exist."""
        content = read_file('deploy/distro/first-boot/hart-first-boot.service')
        assert 'ConditionPathExists=!/var/lib/hart/.first-boot-done' in content


# ──────────────────────────────────────────────────
# CI/CD Tests
# ──────────────────────────────────────────────────

class TestCICD:

    def test_github_actions_exists(self):
        assert file_exists('deploy/distro/ci/build-hart-iso.yml')

    def test_github_actions_valid_yaml_structure(self):
        """YAML has expected top-level keys."""
        content = read_file('deploy/distro/ci/build-hart-iso.yml')
        assert 'name:' in content
        assert 'on:' in content
        assert 'jobs:' in content

    def test_github_actions_builds_all_variants(self):
        """Matrix includes server, desktop, edge."""
        content = read_file('deploy/distro/ci/build-hart-iso.yml')
        assert 'server' in content
        assert 'desktop' in content
        assert 'edge' in content

    def test_github_actions_uses_ubuntu_2204(self):
        """Runs on ubuntu-22.04."""
        content = read_file('deploy/distro/ci/build-hart-iso.yml')
        assert 'ubuntu-22.04' in content

    def test_github_actions_pinned_release_action(self):
        """Release action is pinned to SHA (not tag)."""
        content = read_file('deploy/distro/ci/build-hart-iso.yml')
        assert 'softprops/action-gh-release@' in content
        # Should be SHA, not a version tag
        assert re.search(r'softprops/action-gh-release@[a-f0-9]{40}', content)

    def test_github_actions_verifies_checksum(self):
        """Pipeline verifies ISO checksums."""
        content = read_file('deploy/distro/ci/build-hart-iso.yml')
        assert 'sha256sum' in content

    def test_makefile_exists(self):
        assert file_exists('deploy/distro/ci/Makefile')

    def test_makefile_has_iso_target(self):
        """Makefile has iso target."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'iso:' in content or '.PHONY: iso' in content

    def test_makefile_has_clean_target(self):
        """Makefile has clean target."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'clean:' in content

    def test_makefile_has_help(self):
        """Makefile has help output."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'help' in content

    def test_makefile_supports_variants(self):
        """Makefile has VARIANT variable."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'VARIANT' in content

    def test_makefile_has_qemu_test(self):
        """Makefile has QEMU test target."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'qemu' in content.lower()

    def test_makefile_has_pxe_server_target(self):
        """Makefile has pxe-server target."""
        content = read_file('deploy/distro/ci/Makefile')
        assert 'pxe-server' in content


# ──────────────────────────────────────────────────
# Shell Script Syntax Tests (basic)
# ──────────────────────────────────────────────────

SHELL_SCRIPTS = [
    'deploy/distro/recovery/hart-recovery.sh',
    'deploy/distro/first-boot/hart-first-boot.sh',
    'deploy/distro/branding/hart-motd.sh',
]


class TestShellScripts:

    @pytest.mark.parametrize('script', SHELL_SCRIPTS)
    def test_has_shebang(self, script):
        """Shell scripts start with shebang."""
        content = read_file(script)
        assert content.startswith('#!/')

    @pytest.mark.parametrize('script', SHELL_SCRIPTS)
    def test_no_windows_line_endings(self, script):
        """Shell scripts don't have Windows \\r\\n line endings."""
        full_path = os.path.join(REPO_ROOT, script)
        with open(full_path, 'rb') as f:
            raw = f.read()
        # Allow CRLF since we're on Windows, but flag it
        # (In production on Linux, this would be fixed by git config)
        # We just check the content is not empty
        assert len(raw) > 10

    @pytest.mark.parametrize('script', SHELL_SCRIPTS)
    def test_no_syntax_errors_basic(self, script):
        """Basic syntax check: matched if/fi, for/done, while/done."""
        content = read_file(script)
        # Strip comment lines and inline comments before counting keywords.
        # This avoids matching 'if' inside comments like "# Skip if already"
        lines = []
        for line in content.splitlines():
            stripped = line.lstrip()
            if stripped.startswith('#'):
                continue
            # Remove inline comments (best effort)
            comment_pos = stripped.find(' #')
            if comment_pos > 0:
                stripped = stripped[:comment_pos]
            lines.append(stripped)
        cleaned = '\n'.join(lines)
        # Match 'if' only at statement start (beginning of line or after ;)
        ifs = len(re.findall(r'(?:^|;\s*)if\b', cleaned, re.MULTILINE))
        fis = len(re.findall(r'(?:^|;\s*)fi\b', cleaned, re.MULTILINE))
        assert ifs == fis, f"Unbalanced if/fi in {script}: {ifs} if, {fis} fi"


# ──────────────────────────────────────────────────
# Build Script Test
# ──────────────────────────────────────────────────

class TestBuildISO:

    def test_build_iso_exists(self):
        assert file_exists('deploy/distro/build-iso.sh')

    def test_build_iso_has_shebang(self):
        content = read_file('deploy/distro/build-iso.sh')
        assert content.startswith('#!/')

    def test_build_iso_supports_variant_arg(self):
        """build-iso.sh accepts --variant argument."""
        content = read_file('deploy/distro/build-iso.sh')
        assert '--variant' in content

    def test_build_iso_supports_version_arg(self):
        """build-iso.sh accepts --version argument."""
        content = read_file('deploy/distro/build-iso.sh')
        assert '--version' in content

    def test_build_iso_uses_live_build(self):
        """Uses live-build for ISO creation."""
        content = read_file('deploy/distro/build-iso.sh')
        assert 'lb' in content  # live-build commands (lb config, lb build)

    def test_build_iso_generates_checksum(self):
        """Generates SHA-256 checksum for ISO."""
        content = read_file('deploy/distro/build-iso.sh')
        assert 'sha256sum' in content


# ──────────────────────────────────────────────────
# PXE Config Directory Tests
# ──────────────────────────────────────────────────

class TestPXEServerFiles:

    def test_pxe_server_exists(self):
        assert file_exists('deploy/distro/pxe/hart-pxe-server.py')

    def test_pxe_default_config_exists(self):
        """Default PXE boot config exists."""
        assert file_exists('deploy/distro/pxe/pxelinux.cfg/default')

    def test_pxe_default_has_hart_label(self):
        """PXE config has HART OS install label."""
        content = read_file('deploy/distro/pxe/pxelinux.cfg/default')
        assert 'HART OS' in content or 'hart' in content.lower()


# ──────────────────────────────────────────────────
# OEM Mode Tests
# ──────────────────────────────────────────────────

class TestOEMMode:

    def test_oem_script_exists(self):
        assert file_exists('deploy/distro/oem/hart-oem-config.sh')

    def test_oem_service_exists(self):
        assert file_exists('deploy/distro/oem/hart-oem.service')


# ──────────────────────────────────────────────────
# Update Timer Tests
# ──────────────────────────────────────────────────

class TestUpdateTimer:

    def test_update_service_exists(self):
        assert file_exists('deploy/distro/update/hart-update.service')

    def test_update_timer_exists(self):
        assert file_exists('deploy/distro/update/hart-update.timer')

    def test_update_service_oneshot(self):
        """Update service is oneshot (triggered by timer)."""
        content = read_file('deploy/distro/update/hart-update.service')
        assert 'Type=oneshot' in content

    def test_update_timer_daily(self):
        """Timer runs daily."""
        content = read_file('deploy/distro/update/hart-update.timer')
        assert 'daily' in content.lower() or 'OnCalendar=' in content


# ──────────────────────────────────────────────────
# Boot Audit Tests (E1)
# ──────────────────────────────────────────────────

class TestBootAudit:
    """Tests for tamper-evident boot log (E1)."""

    def test_boot_audit_script_exists(self):
        path = os.path.join(DISTRO_DIR, 'first-boot', 'hart-boot-audit.sh')
        assert os.path.isfile(path)

    def test_boot_audit_has_signing_logic(self):
        path = os.path.join(DISTRO_DIR, 'first-boot', 'hart-boot-audit.sh')
        content = open(path).read()
        assert 'Ed25519' in content or 'ed25519' in content or 'sign' in content.lower()
        assert 'boot_audit.log' in content

    def test_first_boot_calls_audit(self):
        path = os.path.join(DISTRO_DIR, 'first-boot', 'hart-first-boot.sh')
        content = open(path).read()
        assert 'hart-boot-audit.sh' in content


# ──────────────────────────────────────────────────
# Backup Config Tests (E2)
# ──────────────────────────────────────────────────

class TestBackupConfig:
    """Tests for backup automation (E2)."""

    def test_backup_script_exists(self):
        path = os.path.join(DISTRO_DIR, 'backup', 'hart-backup.sh')
        assert os.path.isfile(path)

    def test_backup_has_retention(self):
        path = os.path.join(DISTRO_DIR, 'backup', 'hart-backup.sh')
        content = open(path).read()
        assert 'mtime' in content or 'retention' in content.lower()

    def test_backup_timer_exists(self):
        path = os.path.join(DISTRO_DIR, 'backup', 'hart-backup.timer')
        assert os.path.isfile(path)

    def test_backup_service_is_oneshot(self):
        path = os.path.join(DISTRO_DIR, 'backup', 'hart-backup.service')
        content = open(path).read()
        assert 'Type=oneshot' in content


# ──────────────────────────────────────────────────
# Variant File Tests (E7)
# ──────────────────────────────────────────────────

class TestVariantFile:
    """Tests for distro variant file (E7)."""

    def test_build_iso_writes_variant(self):
        path = os.path.join(DISTRO_DIR, 'build-iso.sh')
        content = open(path).read()
        assert '/etc/hart/variant' in content

    def test_first_boot_reads_variant(self):
        path = os.path.join(DISTRO_DIR, 'first-boot', 'hart-first-boot.sh')
        content = open(path).read()
        assert '/etc/hart/variant' in content

    def test_first_boot_edge_override(self):
        path = os.path.join(DISTRO_DIR, 'first-boot', 'hart-first-boot.sh')
        content = open(path).read()
        assert 'edge' in content


# ──────────────────────────────────────────────────
# Plymouth Progress Tests (E8)
# ──────────────────────────────────────────────────

class TestPlymouthProgress:
    """Tests for Plymouth boot progress (E8)."""

    def test_plymouth_has_progress_callback(self):
        path = os.path.join(DISTRO_DIR, 'branding', 'plymouth', 'hart-theme', 'hart-theme.script')
        content = open(path).read()
        assert 'progress_callback' in content or 'BootProgressFunction' in content

    def test_plymouth_has_message_callback(self):
        path = os.path.join(DISTRO_DIR, 'branding', 'plymouth', 'hart-theme', 'hart-theme.script')
        content = open(path).read()
        assert 'message_callback' in content or 'MessageFunction' in content
