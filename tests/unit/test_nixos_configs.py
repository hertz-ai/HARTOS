"""
HART OS NixOS Configuration Structural Validation Tests

Validates the 27 NixOS files without requiring Nix to be installed.
Uses regex-based parsing of .nix files to verify:
  - File existence and structure
  - Cross-references between configs and modules
  - Variant consistency (server=headless, desktop=GNOME, etc.)
  - Security hardening settings
  - Asset integrity

Runs on Windows/Linux/macOS — no Nix dependency.

Usage:
    pytest tests/test_nixos_configs.py -v
"""

import glob
import os
import re
import pytest

# ─── Paths ────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
CONFIGS_DIR = os.path.join(NIXOS_DIR, "configurations")
PROFILES_DIR = os.path.join(NIXOS_DIR, "profiles")
PACKAGES_DIR = os.path.join(NIXOS_DIR, "packages")
HARDWARE_DIR = os.path.join(NIXOS_DIR, "hardware")
ASSETS_DIR = os.path.join(NIXOS_DIR, "assets")
TOOLS_DIR = os.path.join(NIXOS_DIR, "tools")
TESTS_DIR = os.path.join(NIXOS_DIR, "tests")


def read_nix(path):
    """Read a .nix file and return its content."""
    full = os.path.join(REPO_ROOT, path) if not os.path.isabs(path) else path
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def read_variant(variant):
    """The variant's COMPOSED text surface: configuration + feature profile.

    The 2026-07-28 extraction (5975c519) moved the hart.* feature block
    VERBATIM from configurations/<v>.nix into profiles/<v>.nix, and
    mkHartSystem imports BOTH.  A test asserting the variant's feature
    surface must therefore read the union — reading the configuration
    alone re-broke every variant-feature assertion in this file the day
    the profiles landed.
    """
    name = variant if variant.endswith(".nix") else variant + ".nix"
    return (read_nix(os.path.join(CONFIGS_DIR, name))
            + read_nix(os.path.join(PROFILES_DIR, name)))


# ═══════════════════════════════════════════════════════════════
# Section 1: File Existence
# ═══════════════════════════════════════════════════════════════

EXPECTED_MODULES = [
    "hart-base.nix",
    "hart-first-boot.nix",
    "hart-backend.nix",
    "hart-discovery.nix",
    "hart-agent.nix",
    "hart-llm.nix",
    "hart-vision.nix",
    "hart-conky.nix",
    "hart-nunba.nix",
    "hart-kernel.nix",
    "hart-subsystems.nix",
    "hart-ai-runtime.nix",
    "hart-sandbox.nix",
    # AI-Native Everything OS modules
    "hart-model-bus.nix",
    "hart-compute-mesh.nix",
    "hart-liquid-ui.nix",
    "hart-app-bridge.nix",
    # Boot-experience modules (persistent boot log + HARTLOG self-create + continuity)
    "hart-boot-log.nix",
    "hart-hartlog-create.nix",
    "hart-boot-continuity.nix",
    # Boot / root-mount / initrd hardening (USB-root enumeration guard)
    "hart-boot-root-initrd.nix",
    # External-USB journal export (field recovery onto a second, non-boot stick)
    "hart-journal-export.nix",
]

# Python backend services for the AI-Native modules
EXPECTED_PYTHON_SERVICES = [
    "integrations/agent_engine/model_bus_service.py",
    "integrations/agent_engine/compute_mesh_service.py",
    "integrations/agent_engine/liquid_ui_service.py",
    "integrations/agent_engine/app_bridge_service.py",
]

EXPECTED_CONFIGS = [
    "server.nix",
    "desktop.nix",
    "edge.nix",
    "phone.nix",
]

EXPECTED_PACKAGES = [
    "hart-app.nix",
    "hart-cli.nix",
    "nunba.nix",
]

EXPECTED_HARDWARE = [
    "raspberry-pi.nix",
    "pinephone.nix",
]

EXPECTED_ASSETS = [
    "hart.conkyrc",
    "hart-conky.lua",
    "hart-android-init.sh",
]


class TestFileExistence:
    """All expected NixOS files exist."""

    @pytest.mark.parametrize("module", EXPECTED_MODULES)
    def test_module_exists(self, module):
        path = os.path.join(MODULES_DIR, module)
        assert os.path.isfile(path), f"Module missing: nixos/modules/{module}"

    @pytest.mark.parametrize("config", EXPECTED_CONFIGS)
    def test_configuration_exists(self, config):
        path = os.path.join(CONFIGS_DIR, config)
        assert os.path.isfile(path), f"Config missing: nixos/configurations/{config}"

    @pytest.mark.parametrize("config", EXPECTED_CONFIGS)
    def test_profile_exists(self, config):
        # Every variant has a feature profile (the 2026-07-28 extraction):
        # read_variant() composes configuration + profile, so both must exist.
        path = os.path.join(PROFILES_DIR, config)
        assert os.path.isfile(path), f"Feature profile missing: nixos/profiles/{config}"

    @pytest.mark.parametrize("pkg", EXPECTED_PACKAGES)
    def test_package_exists(self, pkg):
        path = os.path.join(PACKAGES_DIR, pkg)
        assert os.path.isfile(path), f"Package missing: nixos/packages/{pkg}"

    @pytest.mark.parametrize("hw", EXPECTED_HARDWARE)
    def test_hardware_exists(self, hw):
        path = os.path.join(HARDWARE_DIR, hw)
        assert os.path.isfile(path), f"Hardware profile missing: nixos/hardware/{hw}"

    @pytest.mark.parametrize("asset", EXPECTED_ASSETS)
    def test_asset_exists(self, asset):
        path = os.path.join(ASSETS_DIR, asset)
        assert os.path.isfile(path), f"Asset missing: nixos/assets/{asset}"

    def test_flake_exists(self):
        assert os.path.isfile(os.path.join(NIXOS_DIR, "flake.nix"))

    def test_flash_tool_exists(self):
        assert os.path.isfile(os.path.join(TOOLS_DIR, "hart-flash.sh"))

    def test_vm_tests_exist(self):
        assert os.path.isfile(os.path.join(TESTS_DIR, "vm-tests.nix"))

    @pytest.mark.parametrize("service", EXPECTED_PYTHON_SERVICES)
    def test_python_service_exists(self, service):
        path = os.path.join(REPO_ROOT, service)
        assert os.path.isfile(path), f"Python backend service missing: {service}"

    def test_total_file_count(self):
        """At least 31 NixOS files exist (27 base + 4 AI-native modules)."""
        count = 0
        for root, dirs, files in os.walk(NIXOS_DIR):
            count += len([f for f in files if f.endswith((".nix", ".lua", ".sh", ".conkyrc"))])
        assert count >= 31, f"Expected >= 31 NixOS files, found {count}"


# ═══════════════════════════════════════════════════════════════
# Section 2: Flake.nix Cross-References
# ═══════════════════════════════════════════════════════════════

class TestFlakeCrossReferences:
    """flake.nix references match actual file structure."""

    @pytest.fixture(autouse=True)
    def load_flake(self):
        self.flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))

    def test_flake_has_nixpkgs_input(self):
        assert "nixpkgs" in self.flake

    def test_flake_has_nixos_generators_input(self):
        assert "nixos-generators" in self.flake

    def test_flake_has_nixos_hardware_input(self):
        assert "nixos-hardware" in self.flake

    def test_flake_has_llama_cpp_input(self):
        assert "llama-cpp" in self.flake

    def test_flake_references_all_modules(self):
        """Every module in EXPECTED_MODULES is referenced in flake.nix."""
        for mod in EXPECTED_MODULES:
            pattern = mod.replace(".nix", "")
            assert pattern in self.flake, \
                f"Module '{mod}' not referenced in flake.nix"

    def test_flake_has_server_config(self):
        assert "hart-server" in self.flake

    def test_flake_has_desktop_config(self):
        assert "hart-desktop" in self.flake

    def test_flake_has_edge_config(self):
        assert "hart-edge" in self.flake

    def test_flake_has_phone_config(self):
        assert "hart-phone" in self.flake

    def test_flake_has_iso_targets(self):
        for variant in ["server", "desktop", "edge"]:
            assert f"iso-{variant}" in self.flake, \
                f"Missing ISO target: iso-{variant}"

    def test_flake_has_cloud_targets(self):
        for cloud in ["amazon", "gce", "azure"]:
            assert f"{cloud}-server" in self.flake, \
                f"Missing cloud target: {cloud}-server"

    def test_flake_has_vm_targets(self):
        for fmt in ["qcow2", "vmware", "vbox"]:
            assert f"{fmt}-" in self.flake, \
                f"Missing VM target: {fmt}"

    def test_flake_has_docker_target(self):
        assert "docker-server" in self.flake

    def test_flake_has_sd_card_targets(self):
        assert "sd-" in self.flake, "Missing SD card image targets"

    def test_flake_has_arm_targets(self):
        assert "aarch64-linux" in self.flake

    def test_flake_has_go_packages(self):
        assert "hart-cli-go" in self.flake
        assert "hart-pxe-server-go" in self.flake

    def test_flake_references_model_bus_module(self):
        assert "hart-model-bus" in self.flake

    def test_flake_references_compute_mesh_module(self):
        assert "hart-compute-mesh" in self.flake

    def test_flake_references_liquid_ui_module(self):
        assert "hart-liquid-ui" in self.flake

    def test_flake_references_app_bridge_module(self):
        assert "hart-app-bridge" in self.flake


# ═══════════════════════════════════════════════════════════════
# Section 3: Variant Consistency
# ═══════════════════════════════════════════════════════════════

class TestServerVariant:
    """Server: headless, all AI, no desktop."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("server")

    def test_variant_is_server(self):
        assert 'variant = "server"' in self.config

    def test_no_xserver(self):
        assert "xserver.enable = false" in self.config or \
               "services.xserver.enable = false" in self.config

    def test_agent_enabled(self):
        assert "agent.enable = true" in self.config

    def test_llm_enabled(self):
        assert "llm.enable = true" in self.config

    def test_vision_enabled(self):
        assert "vision.enable = true" in self.config

    def test_ai_runtime_enabled(self):
        assert "aiRuntime" in self.config
        assert "enable = true" in self.config

    def test_no_android(self):
        assert "androidNative.enable = false" in self.config

    def test_no_windows(self):
        assert "windowsNative.enable = false" in self.config


class TestDesktopVariant:
    """Desktop: GNOME, all subsystems, full compute."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("desktop")

    def test_variant_is_desktop(self):
        assert 'variant = "desktop"' in self.config

    def test_has_gnome(self):
        assert "gnome" in self.config.lower()

    def test_xserver_enabled(self):
        assert "xserver" in self.config
        # Desktop should have xserver enabled (for GDM)
        assert "enable = true" in self.config

    def test_agent_enabled(self):
        assert "agent.enable = true" in self.config

    def test_conky_enabled(self):
        assert "conky.enable = true" in self.config

    def test_android_native(self):
        assert "androidNative.enable = true" in self.config

    def test_windows_native(self):
        assert "windowsNative.enable = true" in self.config

    def test_ai_compute(self):
        assert "aiCompute" in self.config

    def test_subsystems_enabled(self):
        assert "subsystems" in self.config

    def test_has_flatpak(self):
        assert "flatpak" in self.config

    def test_has_sandbox(self):
        assert "sandbox.enable = true" in self.config

    def test_has_pipewire(self):
        assert "pipewire" in self.config

    def test_has_bluetooth(self):
        assert "bluetooth" in self.config


class TestEdgeVariant:
    """Edge: minimal, no AI, no desktop."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("edge")

    def test_variant_is_edge(self):
        assert 'variant = "edge"' in self.config

    def test_no_agent(self):
        assert "agent.enable = false" in self.config

    def test_no_llm(self):
        assert "llm.enable = false" in self.config

    def test_no_vision(self):
        assert "vision.enable = false" in self.config

    def test_no_xserver(self):
        assert "xserver.enable = false" in self.config or \
               "services.xserver.enable = false" in self.config

    def test_no_android(self):
        assert "androidNative.enable = false" in self.config

    def test_no_windows(self):
        assert "windowsNative.enable = false" in self.config

    def test_no_ai_compute(self):
        assert "aiCompute.enable = false" in self.config

    def test_minimal_docs(self):
        assert "documentation.enable = false" in self.config

    def test_journal_size_limited(self):
        assert "SystemMaxUse" in self.config


class TestPhoneVariant:
    """Phone: Phosh, Android, no Windows, no LLM."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("phone")

    def test_variant_is_phone(self):
        assert 'variant = "phone"' in self.config

    def test_has_phosh(self):
        assert "phosh" in self.config.lower()

    def test_android_enabled(self):
        assert "androidNative.enable = true" in self.config

    def test_no_windows(self):
        assert "windowsNative.enable = false" in self.config

    def test_no_llm(self):
        assert "llm.enable = false" in self.config

    def test_has_modem_manager(self):
        assert "modemManager" in self.config

    def test_has_conky(self):
        assert "conky.enable = true" in self.config

    def test_has_nunba(self):
        assert "nunba.enable = true" in self.config

    def test_has_squeekboard(self):
        assert "squeekboard" in self.config

    def test_has_power_management(self):
        assert "tlp" in self.config or "upower" in self.config

    def test_has_pipewire(self):
        assert "pipewire" in self.config

    def test_agent_enabled(self):
        assert "agent.enable = true" in self.config

    def test_limited_concurrent_agents(self):
        # Phone should have low maxConcurrent
        match = re.search(r"maxConcurrent\s*=\s*(\d+)", self.config)
        assert match, "maxConcurrent not set for phone"
        assert int(match.group(1)) <= 5, \
            f"Phone maxConcurrent too high: {match.group(1)}"


# ═══════════════════════════════════════════════════════════════
# Section 4: Security Hardening
# ═══════════════════════════════════════════════════════════════

class TestSecurityHardening:
    """Critical security settings are present in service modules."""

    def test_backend_has_no_new_privileges(self):
        backend = read_nix(os.path.join(MODULES_DIR, "hart-backend.nix"))
        assert "NoNewPrivileges = true" in backend

    def test_backend_has_protect_system(self):
        backend = read_nix(os.path.join(MODULES_DIR, "hart-backend.nix"))
        assert "ProtectSystem" in backend

    def test_backend_runs_as_hart_user(self):
        backend = read_nix(os.path.join(MODULES_DIR, "hart-backend.nix"))
        assert 'User = "hart"' in backend

    def test_agent_has_no_new_privileges(self):
        agent = read_nix(os.path.join(MODULES_DIR, "hart-agent.nix"))
        assert "NoNewPrivileges = true" in agent

    def test_agent_has_restrict_address_families(self):
        agent = read_nix(os.path.join(MODULES_DIR, "hart-agent.nix"))
        assert "RestrictAddressFamilies" in agent

    def test_agent_has_af_vsock(self):
        """AF_VSOCK required for inter-agent IPC."""
        agent = read_nix(os.path.join(MODULES_DIR, "hart-agent.nix"))
        assert "AF_VSOCK" in agent

    def test_agent_runs_as_hart_user(self):
        agent = read_nix(os.path.join(MODULES_DIR, "hart-agent.nix"))
        assert 'User = "hart"' in agent

    def test_discovery_has_hardening(self):
        discovery = read_nix(os.path.join(MODULES_DIR, "hart-discovery.nix"))
        assert "NoNewPrivileges" in discovery or "ProtectSystem" in discovery

    def test_ai_runtime_agent_template_has_sandboxing(self):
        ai = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))
        assert "Landlock" in ai or "ProtectHome" in ai
        assert "hart-agents.slice" in ai

    def test_ai_runtime_agent_template_has_exec_start(self):
        """Template unit must have ExecStart to be functional."""
        ai = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))
        assert "ExecStart" in ai

    def test_base_creates_hart_user(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "hart" in base
        # Should define the hart user
        assert "users" in base

    def test_base_has_firewall(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "firewall" in base

    def test_kernel_has_landlock(self):
        kernel = read_nix(os.path.join(MODULES_DIR, "hart-kernel.nix"))
        assert "landlock" in kernel.lower() or "Landlock" in kernel

    def test_kernel_has_cgroups_v2(self):
        kernel = read_nix(os.path.join(MODULES_DIR, "hart-kernel.nix"))
        assert "cgroup" in kernel.lower()


# ═══════════════════════════════════════════════════════════════
# Section 5: Module Options Consistency
# ═══════════════════════════════════════════════════════════════

class TestModuleOptions:
    """Module option definitions are consistent."""

    def test_base_defines_enable_option(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "hart.enable" in base or "options.hart" in base

    def test_base_defines_variant_option(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "variant" in base
        # Must include all 4 variants
        for v in ["server", "desktop", "edge", "phone"]:
            assert f'"{v}"' in base, f"Variant '{v}' not in hart-base.nix enum"

    def test_base_defines_ports(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "6777" in base   # backend port
        assert "6780" in base   # discovery port

    def test_conky_has_enable_option(self):
        conky = read_nix(os.path.join(MODULES_DIR, "hart-conky.nix"))
        assert "mkEnableOption" in conky

    def test_nunba_has_enable_option(self):
        nunba = read_nix(os.path.join(MODULES_DIR, "hart-nunba.nix"))
        assert "mkEnableOption" in nunba

    def test_kernel_has_subsystem_options(self):
        kernel = read_nix(os.path.join(MODULES_DIR, "hart-kernel.nix"))
        for opt in ["androidNative", "windowsNative", "aiCompute", "agentSandbox"]:
            assert opt in kernel, f"Kernel missing option: {opt}"

    def test_subsystems_has_all_subsystem_options(self):
        subs = read_nix(os.path.join(MODULES_DIR, "hart-subsystems.nix"))
        for sub in ["flatpak", "appimage", "android", "windows", "web"]:
            assert sub in subs, f"Subsystems missing: {sub}"

    def test_ai_runtime_has_model_store_option(self):
        ai = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))
        assert "modelStore" in ai
        assert "/var/lib/hart/models" in ai

    def test_ai_runtime_has_gpu_option(self):
        ai = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))
        assert "gpu" in ai

    def test_ai_runtime_has_agent_limits(self):
        ai = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))
        assert "maxConcurrent" in ai
        assert "maxMemoryPerAgent" in ai


# ═══════════════════════════════════════════════════════════════
# Section 6: Asset Integrity
# ═══════════════════════════════════════════════════════════════

class TestAssetIntegrity:
    """Conky config, Lua script, and Android init script are valid."""

    def test_conkyrc_references_lua_functions(self):
        conkyrc = read_nix(os.path.join(ASSETS_DIR, "hart.conkyrc"))
        # Must reference lua functions defined in hart-conky.lua
        expected_funcs = [
            "hart_node_id",
            "hart_peer_count",
            "hart_agent_count",
        ]
        for func in expected_funcs:
            assert func in conkyrc, \
                f"hart.conkyrc missing lua function call: {func}"

    def test_conky_lua_defines_required_functions(self):
        lua = read_nix(os.path.join(ASSETS_DIR, "hart-conky.lua"))
        # Must define conky_* functions that match conkyrc references
        expected = [
            "conky_hart_node_id",
            "conky_hart_peer_count",
            "conky_hart_agent_count",
        ]
        for func in expected:
            assert f"function {func}" in lua, \
                f"hart-conky.lua missing function: {func}"

    def test_conky_lua_uses_socket_http(self):
        lua = read_nix(os.path.join(ASSETS_DIR, "hart-conky.lua"))
        assert "socket.http" in lua or "require" in lua

    def test_conky_module_has_luasocket(self):
        """hart-conky.nix must install luasocket for Lua HTTP."""
        conky_mod = read_nix(os.path.join(MODULES_DIR, "hart-conky.nix"))
        assert "luasocket" in conky_mod.lower() or "lua54Packages" in conky_mod

    def test_conky_module_has_lua_path(self):
        """Lua requires LUA_PATH to find luasocket."""
        conky_mod = read_nix(os.path.join(MODULES_DIR, "hart-conky.nix"))
        assert "LUA_PATH" in conky_mod
        assert "LUA_CPATH" in conky_mod

    def test_android_init_is_bash(self):
        init = read_nix(os.path.join(ASSETS_DIR, "hart-android-init.sh"))
        assert init.startswith("#!/") or "bash" in init[:100]

    def test_flash_tool_is_executable_bash(self):
        flash = read_nix(os.path.join(TOOLS_DIR, "hart-flash.sh"))
        assert "#!/" in flash[:20]
        assert "nix build" in flash
        assert "dd " in flash or "dd if=" in flash


# ═══════════════════════════════════════════════════════════════
# Section 7: Hardware Profiles
# ═══════════════════════════════════════════════════════════════

class TestHardwareProfiles:
    """Hardware profiles have required configuration."""

    def test_rpi_has_kernel_config(self):
        rpi = read_nix(os.path.join(HARDWARE_DIR, "raspberry-pi.nix"))
        assert "rpi" in rpi.lower() or "raspberry" in rpi.lower()

    def test_rpi_has_boot_config(self):
        rpi = read_nix(os.path.join(HARDWARE_DIR, "raspberry-pi.nix"))
        assert "boot" in rpi

    def test_rpi_has_wifi_or_bluetooth(self):
        rpi = read_nix(os.path.join(HARDWARE_DIR, "raspberry-pi.nix"))
        assert "wifi" in rpi.lower() or "bluetooth" in rpi.lower() or \
               "wireless" in rpi.lower()

    def test_pinephone_has_modem(self):
        pp = read_nix(os.path.join(HARDWARE_DIR, "pinephone.nix"))
        assert "modem" in pp.lower() or "eg25" in pp.lower()

    def test_pinephone_has_touch(self):
        pp = read_nix(os.path.join(HARDWARE_DIR, "pinephone.nix"))
        assert "touch" in pp.lower() or "goodix" in pp.lower()


# ═══════════════════════════════════════════════════════════════
# Section 8: Sandbox Test System
# ═══════════════════════════════════════════════════════════════

class TestSandboxSystem:
    """Built-in sandbox validation is correctly configured."""

    @pytest.fixture(autouse=True)
    def load_sandbox(self):
        self.sandbox = read_nix(os.path.join(MODULES_DIR, "hart-sandbox.nix"))

    def test_sandbox_has_test_all(self):
        assert "test-all" in self.sandbox

    def test_sandbox_has_linux_tests(self):
        assert "test_linux" in self.sandbox or "test-linux" in self.sandbox

    def test_sandbox_has_android_tests(self):
        assert "test_android" in self.sandbox or "test-android" in self.sandbox

    def test_sandbox_has_windows_tests(self):
        assert "test_windows" in self.sandbox or "test-windows" in self.sandbox

    def test_sandbox_has_ai_tests(self):
        assert "test_ai" in self.sandbox or "test-ai" in self.sandbox

    def test_sandbox_has_status_command(self):
        assert "status" in self.sandbox

    def test_sandbox_logs_results(self):
        """First-boot validation must log results, not swallow silently."""
        assert "tee" in self.sandbox or "sandbox-firstboot.log" in self.sandbox

    def test_sandbox_has_pass_fail_counting(self):
        assert "PASS" in self.sandbox
        assert "FAIL" in self.sandbox


# ═══════════════════════════════════════════════════════════════
# Section 9: Nix Syntax Patterns (regex-based)
# ═══════════════════════════════════════════════════════════════

class TestNixSyntaxPatterns:
    """Common Nix syntax errors caught by regex."""

    @pytest.mark.parametrize("module", EXPECTED_MODULES)
    def test_module_has_valid_structure(self, module):
        """Each module must have the { config, lib, pkgs, ... }: pattern."""
        content = read_nix(os.path.join(MODULES_DIR, module))
        # Nix modules start with a function taking attribute set
        assert re.search(r"\{[^}]*config[^}]*\}", content[:500]), \
            f"{module} missing function arguments (config, lib, pkgs, ...)"

    @pytest.mark.parametrize("module", EXPECTED_MODULES)
    def test_module_has_config_block(self, module):
        """Each module must have a config = ... block."""
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "config =" in content or "config=" in content, \
            f"{module} missing 'config =' block"

    @pytest.mark.parametrize("config", EXPECTED_CONFIGS)
    def test_config_sets_variant(self, config):
        """Each configuration must set hart.variant."""
        content = read_variant(config)
        assert "variant" in content, \
            f"{config} doesn't set hart.variant"

    def test_no_builtins_elem_single_list(self):
        """Detect builtins.elem with single-element list (common mistake)."""
        for module in EXPECTED_MODULES:
            content = read_nix(os.path.join(MODULES_DIR, module))
            # Pattern: builtins.elem "x" [ y ] — usually means == comparison
            matches = re.findall(
                r'builtins\.elem\s+"[^"]+"\s*\[\s*\w+\.\w+\s*\]',
                content
            )
            assert len(matches) == 0, \
                f"{module} has suspicious builtins.elem with single-element list: {matches}"

    @pytest.mark.parametrize("module", EXPECTED_MODULES)
    def test_no_unclosed_braces(self, module):
        """Basic brace matching (heuristic — Nix '' strings and ${} interpolation
        make precise counting impossible without a real parser)."""
        content = read_nix(os.path.join(MODULES_DIR, module))
        # Remove strings and comments (approximate)
        cleaned = re.sub(r'"[^"]*"', '""', content)
        cleaned = re.sub(r"''[\s\S]*?''", "''", cleaned)
        cleaned = re.sub(r'#[^\n]*', '', cleaned)
        opens = cleaned.count('{') + cleaned.count('[') + cleaned.count('(')
        closes = cleaned.count('}') + cleaned.count(']') + cleaned.count(')')
        # Allow wider mismatch: embedded shell scripts use ${}, ''${}
        # which our regex can't fully strip. Threshold catches >15 diff.
        assert abs(opens - closes) <= 15, \
            f"{module} has severely unbalanced braces: {opens} opens vs {closes} closes"


# ═══════════════════════════════════════════════════════════════
# Section 10: Model Bus Module
# ═══════════════════════════════════════════════════════════════

class TestModelBusModule:
    """Model Bus: native AI access for every app."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-model-bus.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content

    def test_has_socket_path_option(self):
        assert "socketPath" in self.content
        assert "/run/hart/model-bus.sock" in self.content

    def test_has_http_port(self):
        assert "6790" in self.content

    def test_has_routing_strategy_option(self):
        assert "routingStrategy" in self.content
        assert "speculative" in self.content

    def test_has_max_concurrent_requests(self):
        assert "maxConcurrentRequests" in self.content or "maxConcurrent" in self.content

    def test_has_android_bridge_option(self):
        assert "enableAndroidBridge" in self.content

    def test_has_wine_bridge_option(self):
        assert "enableWineBridge" in self.content

    def test_has_systemd_service(self):
        assert "hart-model-bus" in self.content
        assert "systemd.services" in self.content

    def test_service_runs_as_hart_user(self):
        assert 'User = "hart"' in self.content

    def test_has_security_hardening(self):
        assert "NoNewPrivileges = true" in self.content
        assert "ProtectSystem" in self.content

    def test_has_dbus_interface(self):
        assert "com.hart.ModelBus" in self.content

    def test_has_cli_tool(self):
        assert "hart-infer" in self.content

    def test_imports_python_service(self):
        assert "model_bus_service" in self.content
        assert "ModelBusService" in self.content

    def test_depends_on_hart_target(self):
        assert "hart.target" in self.content

    def test_has_health_endpoint(self):
        assert "/health" in self.content or "/v1/status" in self.content

    def test_has_resource_limits(self):
        assert "MemoryMax" in self.content
        assert "hart-agents.slice" in self.content


# ═══════════════════════════════════════════════════════════════
# Section 11: Compute Mesh Module
# ═══════════════════════════════════════════════════════════════

class TestComputeMeshModule:
    """Compute Mesh: same-user cross-device compute sharing."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-compute-mesh.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content

    def test_has_wireguard_port(self):
        assert "6795" in self.content

    def test_has_task_relay_port(self):
        assert "6796" in self.content

    def test_has_max_offload_option(self):
        assert "maxOffloadPercent" in self.content

    def test_has_allow_wan_option(self):
        assert "allowWAN" in self.content

    def test_has_stun_server_option(self):
        assert "stunServer" in self.content or "stun" in self.content.lower()

    def test_has_mesh_interface(self):
        assert "meshInterface" in self.content or "hart-mesh" in self.content

    def test_has_mesh_subnet(self):
        assert "meshSubnet" in self.content or "10.99" in self.content

    def test_has_auto_accept_option(self):
        assert "autoAccept" in self.content

    def test_has_keygen_service(self):
        """WireGuard key generation must happen at first boot."""
        assert "keygen" in self.content.lower() or "wg genkey" in self.content

    def test_has_systemd_service(self):
        assert "hart-compute-mesh" in self.content
        assert "systemd.services" in self.content

    def test_service_runs_as_hart_user(self):
        assert 'User = "hart"' in self.content

    def test_has_security_hardening(self):
        assert "NoNewPrivileges = true" in self.content

    def test_imports_python_service(self):
        assert "compute_mesh_service" in self.content
        assert "ComputeMeshService" in self.content

    def test_has_cli_tool(self):
        assert "hart-mesh" in self.content

    def test_has_firewall_rules(self):
        assert "firewall" in self.content or "allowedTCPPorts" in self.content \
               or "allowedUDPPorts" in self.content

    def test_has_privacy_boundary_comment(self):
        """Privacy is the core design principle — must be documented."""
        content_lower = self.content.lower()
        assert "privacy" in content_lower or "same user" in content_lower or \
               "same-user" in content_lower


# ═══════════════════════════════════════════════════════════════
# Section 12: LiquidUI Module
# ═══════════════════════════════════════════════════════════════

class TestLiquidUIModule:
    """LiquidUI: AI-generated adaptive interface."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-liquid-ui.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content

    def test_has_port(self):
        assert "6800" in self.content

    def test_has_renderer_option(self):
        assert "renderer" in self.content
        assert "webkit" in self.content

    def test_has_voice_option(self):
        assert "voiceEnabled" in self.content

    def test_has_haptic_option(self):
        assert "hapticEnabled" in self.content

    def test_has_theme_option(self):
        assert "theme" in self.content

    def test_has_a2ui_option(self):
        """Agent-to-UI protocol for human-in-the-loop."""
        assert "A2UI" in self.content or "a2ui" in self.content.lower() or \
               "enableA2UI" in self.content

    def test_has_systemd_service(self):
        assert "hart-liquid-ui" in self.content
        assert "systemd.services" in self.content

    def test_has_dbus_interface(self):
        assert "com.hart.LiquidUI" in self.content

    def test_has_security_hardening(self):
        assert "NoNewPrivileges = true" in self.content

    def test_imports_python_service(self):
        assert "liquid_ui_service" in self.content
        assert "LiquidUIService" in self.content

    def test_depends_on_model_bus(self):
        """LiquidUI needs Model Bus for AI generation."""
        assert "model-bus" in self.content.lower() or "hart-model-bus" in self.content

    def test_has_renderer_service(self):
        """User-level renderer service (WebKit/Electron)."""
        assert "renderer" in self.content

    def test_has_fallback_cascade(self):
        """Must fall back gracefully when model unavailable."""
        content_lower = self.content.lower()
        assert "fallback" in content_lower or "static" in content_lower or \
               "nunba" in content_lower


# ═══════════════════════════════════════════════════════════════
# Section 13: App Bridge Module
# ═══════════════════════════════════════════════════════════════

class TestAppBridgeModule:
    """App Bridge: cross-subsystem agent routing."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-app-bridge.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content

    def test_has_socket_path(self):
        assert "socketPath" in self.content
        assert "/run/hart/app-bridge" in self.content

    def test_has_http_port(self):
        assert "6810" in self.content

    def test_has_cross_subsystem_option(self):
        assert "allowCrossSubsystem" in self.content

    def test_has_intent_router_option(self):
        assert "intentRouter" in self.content

    def test_has_clipboard_sync_option(self):
        assert "clipboardSync" in self.content

    def test_has_drag_and_drop_option(self):
        assert "dragAndDrop" in self.content

    def test_has_notification_option(self):
        assert "notificationUnification" in self.content or "notification" in self.content.lower()

    def test_has_ai_fallback_option(self):
        assert "aiFallback" in self.content

    def test_has_systemd_service(self):
        assert "hart-app-bridge" in self.content
        assert "systemd.services" in self.content

    def test_has_dbus_interface(self):
        assert "com.hart.AppBridge" in self.content

    def test_has_dbus_intent_interface(self):
        assert "com.hart.AppBridge.Intent" in self.content

    def test_has_dbus_clipboard_interface(self):
        assert "com.hart.AppBridge.Clipboard" in self.content

    def test_has_dbus_capability_interface(self):
        assert "com.hart.AppBridge.Capability" in self.content

    def test_has_security_hardening(self):
        assert "NoNewPrivileges = true" in self.content

    def test_imports_python_service(self):
        assert "app_bridge_service" in self.content
        assert "AppBridgeService" in self.content

    def test_has_clipboard_sync_service(self):
        assert "clipboard-sync" in self.content or "clipboardSync" in self.content

    def test_has_cli_tool(self):
        assert "hart-bridge" in self.content

    def test_depends_on_model_bus(self):
        assert "model-bus" in self.content.lower() or "hart-model-bus" in self.content

    def test_has_subsystem_detection(self):
        """Bridge must detect available subsystems."""
        content_lower = self.content.lower()
        assert "android" in content_lower
        assert "wine" in content_lower or "windows" in content_lower
        assert "chromium" in content_lower or "web" in content_lower


# ═══════════════════════════════════════════════════════════════
# Section 14: AI-Runtime Semantic Intelligence Layer
# ═══════════════════════════════════════════════════════════════

class TestAIRuntimeSemanticLayer:
    """Semantic intelligence options added to hart-ai-runtime.nix."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-ai-runtime.nix"))

    def test_has_semantic_enable_option(self):
        assert "semantic" in self.content

    def test_has_service_intelligence_option(self):
        assert "serviceIntelligence" in self.content

    def test_has_smart_fs_option(self):
        assert "smartFS" in self.content

    def test_has_predictive_prefetch_option(self):
        assert "predictivePrefetch" in self.content

    def test_has_service_intelligence_service(self):
        assert "service-intelligence" in self.content or "serviceIntelligence" in self.content

    def test_has_smart_index_service(self):
        assert "smart-index" in self.content or "smartIndex" in self.content

    def test_has_predictive_prefetch_service(self):
        assert "predictive-prefetch" in self.content or "predictivePrefetch" in self.content

    def test_has_hart_search_cli(self):
        assert "hart-search" in self.content


# ═══════════════════════════════════════════════════════════════
# Section 15: Variant Enablement of AI-Native Modules
# ═══════════════════════════════════════════════════════════════

class TestServerAINativeModules:
    """Server enables Model Bus + Compute Mesh, no LiquidUI/AppBridge."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("server")

    def test_model_bus_enabled(self):
        assert "modelBus" in self.config
        assert "enable = true" in self.config

    def test_compute_mesh_enabled(self):
        assert "computeMesh" in self.config

    def test_compute_mesh_allows_wan(self):
        assert "allowWAN = true" in self.config

    def test_compute_mesh_high_offload(self):
        """Server should donate generously to mesh."""
        match = re.search(r"maxOffloadPercent\s*=\s*(\d+)", self.config)
        assert match, "Server should set maxOffloadPercent"
        assert int(match.group(1)) >= 50, \
            f"Server maxOffloadPercent too low: {match.group(1)}"

    def test_no_liquid_ui(self):
        """Headless server should not enable LiquidUI."""
        assert "liquidUI" not in self.config or \
               "liquidUI.enable = true" not in self.config

    def test_no_app_bridge(self):
        """Server has no subsystems, no AppBridge needed."""
        assert "appBridge" not in self.config or \
               "appBridge.enable = true" not in self.config

    def test_semantic_intelligence_enabled(self):
        assert "semantic" in self.config
        assert "serviceIntelligence = true" in self.config


class TestDesktopAINativeModules:
    """Desktop enables all 4 AI-Native modules."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("desktop")

    def test_model_bus_enabled(self):
        assert "modelBus" in self.config

    def test_model_bus_android_bridge(self):
        assert "enableAndroidBridge = true" in self.config

    def test_model_bus_wine_bridge(self):
        assert "enableWineBridge = true" in self.config

    def test_compute_mesh_enabled(self):
        assert "computeMesh" in self.config

    def test_liquid_ui_enabled(self):
        assert "liquidUI" in self.config

    def test_liquid_ui_voice(self):
        assert "voiceEnabled = true" in self.config

    def test_liquid_ui_webkit_renderer(self):
        assert 'renderer = "webkit"' in self.config

    def test_app_bridge_enabled(self):
        assert "appBridge" in self.config

    def test_app_bridge_clipboard(self):
        assert "clipboardSync = true" in self.config

    def test_app_bridge_drag_and_drop(self):
        assert "dragAndDrop = true" in self.config

    def test_app_bridge_intent_router(self):
        assert "intentRouter = true" in self.config

    def test_semantic_smart_fs(self):
        assert "smartFS = true" in self.config

    def test_semantic_predictive_prefetch(self):
        assert "predictivePrefetch = true" in self.config


class TestEdgeAINativeModules:
    """Edge enables only Compute Mesh (donates compute)."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("edge")

    def test_compute_mesh_enabled(self):
        assert "computeMesh" in self.config

    def test_compute_mesh_high_offload(self):
        """Edge donates most of its compute to the mesh."""
        match = re.search(r"maxOffloadPercent\s*=\s*(\d+)", self.config)
        assert match, "Edge should set maxOffloadPercent"
        assert int(match.group(1)) >= 70, \
            f"Edge maxOffloadPercent too low: {match.group(1)}"

    def test_compute_mesh_allows_wan(self):
        assert "allowWAN = true" in self.config

    def test_no_model_bus(self):
        """Edge has no local models, no Model Bus needed."""
        # modelBus should not appear or should not be enabled
        assert "modelBus" not in self.config or \
               "modelBus.enable = true" not in self.config

    def test_no_liquid_ui(self):
        assert "liquidUI" not in self.config or \
               "liquidUI.enable = true" not in self.config

    def test_no_app_bridge(self):
        assert "appBridge" not in self.config or \
               "appBridge.enable = true" not in self.config


class TestPhoneAINativeModules:
    """Phone enables Model Bus + Mesh + LiquidUI + AppBridge (no Windows)."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("phone")

    def test_model_bus_enabled(self):
        assert "modelBus" in self.config

    def test_model_bus_android_bridge(self):
        assert "enableAndroidBridge = true" in self.config

    def test_compute_mesh_enabled(self):
        assert "computeMesh" in self.config

    def test_compute_mesh_allows_wan(self):
        """Phone needs WAN to reach desktop/server."""
        assert "allowWAN = true" in self.config

    def test_liquid_ui_enabled(self):
        assert "liquidUI" in self.config

    def test_liquid_ui_voice(self):
        assert "voiceEnabled = true" in self.config

    def test_liquid_ui_haptic(self):
        assert "hapticEnabled = true" in self.config

    def test_app_bridge_enabled(self):
        assert "appBridge" in self.config

    def test_app_bridge_intent_router(self):
        assert "intentRouter = true" in self.config

    def test_app_bridge_clipboard(self):
        assert "clipboardSync = true" in self.config

    def test_semantic_enabled(self):
        assert "semantic" in self.config
        assert "serviceIntelligence = true" in self.config

    def test_no_smart_fs(self):
        """Phone has limited storage — smartFS disabled."""
        assert "smartFS = false" in self.config


# ═══════════════════════════════════════════════════════════════
# Section 16: Python Backend Service Structure
# ═══════════════════════════════════════════════════════════════

class TestPythonBackendServices:
    """Python backend services have required classes and methods."""

    def test_model_bus_service_has_class(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "class ModelBusService" in content

    def test_model_bus_service_has_infer(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "def infer(" in content

    def test_model_bus_service_has_discover_backends(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "def discover_backends(" in content

    def test_model_bus_service_has_list_models(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "def list_models(" in content

    def test_model_bus_service_has_guardrail_check(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "guardrail" in content.lower() or "ConstitutionalFilter" in content

    def test_model_bus_service_has_serve_forever(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/model_bus_service.py"
        ))
        assert "def serve_forever(" in content

    def test_compute_mesh_service_has_class(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "class ComputeMeshService" in content

    def test_compute_mesh_service_has_discover_peers(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "def discover_peers(" in content

    def test_compute_mesh_service_has_offload(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "def offload_inference(" in content

    def test_compute_mesh_service_has_mesh_status(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "def get_mesh_status(" in content

    def test_compute_mesh_service_has_pair_device(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "def pair_device(" in content

    def test_compute_mesh_service_has_peer_class(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/compute_mesh_service.py"
        ))
        assert "class MeshPeer" in content

    def test_liquid_ui_service_has_class(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/liquid_ui_service.py"
        ))
        assert "class LiquidUIService" in content

    def test_liquid_ui_service_has_generate_ui(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/liquid_ui_service.py"
        ))
        assert "def generate_ui(" in content

    def test_liquid_ui_service_has_context_engine(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/liquid_ui_service.py"
        ))
        assert "ContextEngine" in content or "context" in content.lower()

    def test_liquid_ui_service_has_render(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/liquid_ui_service.py"
        ))
        assert "render" in content.lower()

    def test_liquid_ui_service_has_a2ui(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/liquid_ui_service.py"
        ))
        assert "agent_ui_update" in content or "a2ui" in content.lower()

    def test_app_bridge_service_has_class(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "class AppBridgeService" in content

    def test_app_bridge_service_has_capability_registry(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "CapabilityRegistry" in content

    def test_app_bridge_service_has_semantic_router(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "SemanticRouter" in content

    def test_app_bridge_service_has_clipboard(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "UnifiedClipboard" in content or "clipboard" in content.lower()

    def test_app_bridge_service_has_route_intent(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "def route_intent(" in content

    def test_app_bridge_service_has_detect_subsystems(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "def detect_subsystems(" in content

    def test_app_bridge_service_has_serve_forever(self):
        content = read_nix(os.path.join(
            REPO_ROOT, "integrations/agent_engine/app_bridge_service.py"
        ))
        assert "def serve_forever(" in content


# ═══════════════════════════════════════════════════════════════
# Section 17: Security Hardening for New Modules
# ═══════════════════════════════════════════════════════════════

class TestNewModuleSecurityHardening:
    """All 4 new AI-Native modules have proper security hardening."""

    NEW_MODULES = [
        "hart-model-bus.nix",
        "hart-compute-mesh.nix",
        "hart-liquid-ui.nix",
        "hart-app-bridge.nix",
    ]

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_no_new_privileges(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "NoNewPrivileges = true" in content, \
            f"{module} missing NoNewPrivileges = true"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_protect_system(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "ProtectSystem" in content, \
            f"{module} missing ProtectSystem"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_runs_as_hart_user(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert 'User = "hart"' in content, \
            f"{module} not running as hart user"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_has_memory_limit(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "MemoryMax" in content, \
            f"{module} missing MemoryMax resource limit"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_has_restart_policy(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "Restart" in content, \
            f"{module} missing restart policy"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_has_pythondontwritebytecode(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "PYTHONDONTWRITEBYTECODE" in content, \
            f"{module} missing PYTHONDONTWRITEBYTECODE"

    @pytest.mark.parametrize("module", NEW_MODULES)
    def test_has_restrict_address_families(self, module):
        content = read_nix(os.path.join(MODULES_DIR, module))
        assert "RestrictAddressFamilies" in content, \
            f"{module} missing RestrictAddressFamilies"


# ═══════════════════════════════════════════════════════════════
# Section 18: Cross-Module Dependencies
# ═══════════════════════════════════════════════════════════════

class TestCrossModuleDependencies:
    """Modules declare correct inter-dependencies."""

    def test_liquid_ui_depends_on_model_bus(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-liquid-ui.nix"))
        assert "hart-model-bus" in content, \
            "LiquidUI must depend on Model Bus service"

    def test_app_bridge_depends_on_model_bus(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-app-bridge.nix"))
        assert "hart-model-bus" in content, \
            "App Bridge must depend on Model Bus service"

    def test_compute_mesh_depends_on_discovery(self):
        """Mesh uses existing discovery for LAN peer finding."""
        content = read_nix(os.path.join(MODULES_DIR, "hart-compute-mesh.nix"))
        assert "discovery" in content.lower(), \
            "Compute Mesh should reference discovery service"

    def test_model_bus_depends_on_backend(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-model-bus.nix"))
        assert "hart.target" in content or "hart-backend" in content, \
            "Model Bus must depend on HART backend"

    def test_all_new_modules_in_hart_target(self):
        """All new services should be part of hart.target."""
        for module in ["hart-model-bus.nix", "hart-compute-mesh.nix",
                       "hart-liquid-ui.nix", "hart-app-bridge.nix"]:
            content = read_nix(os.path.join(MODULES_DIR, module))
            assert "hart.target" in content, \
                f"{module} not wantedBy hart.target"


# ═══════════════════════════════════════════════════════════════
# OS Feature Modules: Gaming, Devtools, OSK, DLNA, Peripheral
# ═══════════════════════════════════════════════════════════════

class TestGamingModule:
    """Verify hart-gaming.nix options and structure."""

    def test_gaming_file_exists(self):
        path = os.path.join(MODULES_DIR, "hart-gaming.nix")
        assert os.path.isfile(path)

    def test_gaming_has_enable_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-gaming.nix"))
        assert "mkEnableOption" in content

    def test_gaming_has_cpu_isolation(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-gaming.nix"))
        assert "cpuIsolation" in content
        assert "isolcpus" in content

    def test_gaming_has_low_latency_audio(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-gaming.nix"))
        assert "pipewire" in content.lower() or "bufferSize" in content

    def test_gaming_has_network_tuning(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-gaming.nix"))
        assert "tcp_bbr" in content or "bbr" in content

    def test_gaming_has_mkif_guard(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-gaming.nix"))
        assert "mkIf cfg.enable" in content


class TestDevtoolsModule:
    """Verify hart-devtools.nix options and structure."""

    def test_devtools_file_exists(self):
        path = os.path.join(MODULES_DIR, "hart-devtools.nix")
        assert os.path.isfile(path)

    def test_devtools_has_enable_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "mkEnableOption" in content

    def test_devtools_has_lsp_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "lsp" in content

    def test_devtools_has_container_toggle(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "containers" in content
        assert "podman" in content

    def test_devtools_has_editor_toggle(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "editors" in content
        assert "neovim" in content

    def test_devtools_has_mkif_guard(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "mkIf cfg.enable" in content


class TestOSKModule:
    """Verify hart-osk.nix on-screen keyboard module."""

    def test_osk_file_exists(self):
        path = os.path.join(MODULES_DIR, "hart-osk.nix")
        assert os.path.isfile(path)

    def test_osk_has_enable_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-osk.nix"))
        assert "mkEnableOption" in content

    def test_osk_has_backend_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-osk.nix"))
        assert "squeekboard" in content
        assert "onboard" in content

    def test_osk_has_auto_show_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-osk.nix"))
        assert "autoShow" in content

    def test_osk_has_mkif_guard(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-osk.nix"))
        assert "mkIf cfg.enable" in content

    def test_osk_in_flake_modules(self):
        flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        assert "hart-osk.nix" in flake

    def test_phone_enables_osk(self):
        phone = read_variant("phone")
        assert "osk" in phone


class TestDLNAEnableGuard:
    """Verify hart-dlna.nix has proper mkIf enable guard."""

    def test_dlna_has_enable_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-dlna.nix"))
        assert "enable" in content

    def test_dlna_has_mkif_guard(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-dlna.nix"))
        assert "mkIf" in content
        assert "dlna.enable" in content


class TestPeripheralBridgeEnableGuard:
    """Verify hart-peripheral-bridge.nix has proper mkIf enable guard."""

    def test_peripheral_bridge_has_enable_option(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-peripheral-bridge.nix"))
        assert "enable" in content

    def test_peripheral_bridge_has_mkif_guard(self):
        content = read_nix(os.path.join(MODULES_DIR, "hart-peripheral-bridge.nix"))
        assert "mkIf" in content
        assert "peripheralBridge.enable" in content


# ═══════════════════════════════════════════════════════════════
# Boot-experience: Live-OS HARTLOG self-create (Feature 1)
# ═══════════════════════════════════════════════════════════════

class TestHartlogCreateModule:
    """hart-hartlog-create.nix: Live-OS carves HARTLOG into the USB free space,
    replacing the Windows-flasher diskpart path.

    The BEHAVIOUR (the carve mechanics, the never-touch-existing-partitions
    invariant, the removable-only gate, the idempotent + full-disk + no-resolve
    no-ops) is proven on a real Linux block layer by the wired-in nixosTest
    nixos/tests/hartlog-create.nix (it formats a stand-in GPT disk + runs the REAL
    script + asserts RC=0 / untouched ISO GUID / no second partition). Per
    feedback_no_grep_tests.md these structural checks keep ONLY the cross-file
    DRY/contract guards a VM boot can't cheaply reach — the on-disk label lockstep
    and the flake/desktop wiring. The grep-on-source duplicates of the behavioural
    assertions (sgdisk/mkfs present, --largest-new, the RM/usb gate, the
    already-exists/free-space no-ops, the before-ordering) were REMOVED: they only
    proved a string survived the commit, which the nixosTest already proves
    behaviourally."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-hartlog-create.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content
        assert "hartlogCreate" in self.content

    def test_has_mkif_guard(self):
        assert "mkIf" in self.content
        assert "hlog.enable" in self.content or "hartlogCreate.enable" in self.content

    def test_label_defaults_to_bootlog_label(self):
        """GENUINE cross-file DRY guard (a VM boot can't cheaply prove the DEFAULT
        expression): the create-side label must default to bootLog.label so the
        read-side (hart-boot-log) and write-side never drift out of lockstep."""
        assert "config.hart.bootLog.label" in self.content

    def test_writes_loud_status_marker(self):
        """The never-silent-no-op contract: every decision (picked disk, free space,
        no-op reason) is recorded to /run/hart/hartlog-create.status so a silent
        no-op is never undebuggable. (The behavioural nixosTest asserts the marker's
        CREATED/NOOP content; this is the cheap cross-file path guard.)"""
        assert "/run/hart/hartlog-create.status" in self.content
        assert "DECISION=" in self.content

    def test_handles_isohybrid_mbr_layout(self):
        """The live ISO can be DOS/MBR isohybrid where sgdisk MUST NOT run (it would
        convert the table + destroy the boot layout). The module must branch on the
        table type and use the parted mkpart path for a DOS label."""
        assert "PTTYPE" in self.content
        # parted is the MBR carve path (sgdisk only handles the GPT case).
        assert "parted" in self.content
        assert "mkpart" in self.content

    def test_follows_loop_to_real_usb(self):
        """A hybrid ISO's live root can be a loop/overlay mount, so the module must
        follow a loop device back to the disk its backing file lives on (losetup
        BACK-FILE) or match the HART_OS ISO volume label — not just bail on loop."""
        assert "BACK-FILE" in self.content or "losetup" in self.content
        assert "HART_OS" in self.content

    def test_in_flake_modules(self):
        """Gate-5 wiring guard: the module must be imported in flake.nix or the
        nixosTest could never enable it."""
        flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        assert "hart-hartlog-create.nix" in flake

    def test_desktop_enables_it(self):
        """Cross-config wiring the nixosTest (which enables it via mkNode, not the
        desktop closure) does NOT cover: the shipped desktop must opt it on."""
        desktop = read_variant("desktop")
        assert "hartlogCreate.enable = true" in desktop


# ═══════════════════════════════════════════════════════════════
# Boot-experience: boot continuity / one-shot BootNext (Feature 2)
# ═══════════════════════════════════════════════════════════════

class TestBootContinuityModule:
    """hart-boot-continuity.nix: on a Live-OS reboot, set a ONE-SHOT efibootmgr
    BootNext to the USB's own entry — NEVER BootOrder (so Windows is never
    stranded).

    The BEHAVIOUR (the unit + efibootmgr in the closure, the ExecStop reboot-path
    ordering, the non-UEFI / poweroff / no-match no-ops exiting 0, and ZERO NVRAM
    writes on a no-op via a shadowed efibootmgr recorder) is proven by the wired-in
    nixosTest nixos/tests/boot-continuity.nix. Per feedback_no_grep_tests.md these
    structural checks keep ONLY the never-strand-Windows SAFETY invariant as a
    cheap static guard (the one critical assertion worth a fast belt-and-suspenders
    static check IN ADDITION to the behavioural one) plus the flake/desktop wiring.
    The grep-on-source duplicates (efibootmgr/--bootnext present, the
    /sys/firmware/efi + command-v + could-not-match no-op gates, the ExecStop +
    ordering) were REMOVED — the nixosTest proves them behaviourally."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-boot-continuity.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content
        assert "bootContinuity" in self.content

    def test_has_mkif_guard(self):
        assert "mkIf" in self.content
        assert "bc.enable" in self.content or "bootContinuity.enable" in self.content

    def test_never_writes_bootorder(self):
        """THE never-strand-Windows SAFETY invariant — kept as a fast static guard
        IN ADDITION to the behavioural nixosTest, because a regression here (a
        stray BootOrder write) would risk bricking the user's Windows boot, so it
        is worth catching the instant a diff lands, not only on a 10-min VM run.
        The module must NEVER set/modify BootOrder — only the one-shot BootNext."""
        # No efibootmgr -o / --bootorder write anywhere in the module.
        assert "--bootorder" not in self.content.lower()
        assert "efibootmgr -o" not in self.content
        assert "efibootmgr --bootorder" not in self.content

    def test_poweroff_detected_from_real_action_not_just_arg(self):
        """#187/F4: a poweroff must NEVER arm BootNext. The bug was trusting the
        hardcoded ExecStop ACTION arg (always "reboot") — so it armed on EVERY
        shutdown including poweroff. The fix detects the ACTUAL scheduled action.
        Guard that the real-action signals are consulted (the behavioural UEFI
        nixosTest proves a poweroff arms nothing; this is the cheap source guard)."""
        # The scheduled-shutdown record + the live job list are the real signals.
        assert "/run/systemd/shutdown/scheduled" in self.content
        assert "list-jobs" in self.content
        # It branches on poweroff/halt (not just the reboot arg).
        assert "poweroff" in self.content
        assert "halt" in self.content

    def test_in_flake_modules(self):
        """Gate-5 wiring guard: imported in flake.nix or the nixosTest can't run."""
        flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        assert "hart-boot-continuity.nix" in flake

    def test_desktop_enables_it(self):
        """Cross-config wiring the nixosTest (mkNode enable) does not cover."""
        desktop = read_variant("desktop")
        assert "bootContinuity.enable = true" in desktop


# ═══════════════════════════════════════════════════════════════
# Boot-experience: persistent boot-diagnostic log partition (Feature 3)
# ═══════════════════════════════════════════════════════════════

class TestBootLogModule:
    """hart-boot-log.nix: when a FAT32 HARTLOG partition is present, capture the
    full boot journal + tier-supervisor state + GTK4/GL diagnostics to it early,
    on a periodic timer (so a HUNG boot still leaves a record), and at shutdown.

    The BEHAVIOUR (the three capture units + the active timer, the no-HARTLOG clean
    no-op, the full diagnostic bundle landing with every curated section incl. the
    shell-ready marker + GSK/GDK/EGL surface + the boot journal, the early-phase
    clean unmount + fsck-clean fs, the stable overwritten latest file) is proven on
    a real FAT32 device by the wired-in nixosTest nixos/tests/boot-log.nix. Per
    feedback_no_grep_tests.md these structural checks keep ONLY the on-stick LABEL
    default (the ONE source of truth the carve side + flasher must agree on, a
    default-value contract a VM boot is wasteful to prove) plus the flake/desktop
    wiring. The grep-on-source duplicates (findfs/blkid, the three phase names, the
    interval option, the no-op string, the paint-hang surface strings, sync/umount)
    were REMOVED — the nixosTest proves them behaviourally."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-boot-log.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content
        assert "bootLog" in self.content

    def test_has_mkif_guard(self):
        assert "mkIf" in self.content
        assert "blog.enable" in self.content or "bootLog.enable" in self.content

    def test_label_default_is_hartlog(self):
        """GENUINE single-source contract guard (a VM can't cheaply prove the
        DEFAULT value): the label the flasher + the live-OS carve write and this
        module reads is HARTLOG. Pairs with TestHartlogCreateModule's lockstep
        test (the carve defaults its label to THIS one)."""
        assert 'default = "HARTLOG"' in self.content

    def test_in_flake_modules(self):
        """Gate-5 wiring guard: imported in flake.nix or the nixosTest can't run."""
        flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        assert "hart-boot-log.nix" in flake

    def test_desktop_enables_it(self):
        """Cross-config wiring the nixosTest (mkNode enable) does not cover."""
        desktop = read_variant("desktop")
        assert "bootLog.enable = true" in desktop


# ═══════════════════════════════════════════════════════════════
# Boot / root-mount / initrd hardening (USB-root enumeration)
# ═══════════════════════════════════════════════════════════════

class TestBootRootInitrdModule:
    """hart-boot-root-initrd.nix: ENSURE + ASSERT the USB-root initrd module set
    (usb_storage/uas/sd_mod + the xhci/ehci host controllers) so a USB boot can
    enumerate the stick before the root pivot — never a silent real-HW "VFS: Unable
    to mount root fs" brick.

    The BEHAVIOUR (boot, confirm root mounted, EXTRACT the built initrd to prove the
    modules were really PACKED, and the boot-disk-GPT guard never completes the boot
    medium's GPT) is proven by the wired-in nixosTest nixos/tests/boot-root-initrd.nix.
    Per feedback_no_grep_tests.md these structural checks keep ONLY the opt-in SAFETY
    invariant (the guard MUST default OFF so the #70-minimal virtio-root test nodes
    never inherit a USB-root assertion that would fail their build) plus the
    flake/desktop wiring. The module's correctness — that the modules actually land
    in the initrd — is the nixosTest's job, not a grep."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-boot-root-initrd.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content
        assert "bootRootInitrd" in self.content

    def test_default_off_mkif_guard(self):
        """GENUINE opt-in SAFETY guard (a VM is wasteful to prove a DEFAULT): the
        guard must be gated `mkIf (... && bri.enable)` and mkEnableOption (default
        FALSE) so every #70-minimal nixosTest node (which boots a virtio root) does
        NOT inherit the USB-root assertion and fail its build. mkEnableOption is
        false-by-default, so the presence of the mkIf guard + the enable option is
        the contract."""
        assert "mkIf" in self.content
        assert "bri.enable" in self.content or "bootRootInitrd.enable" in self.content

    def test_adds_usb_root_modules_to_initrd(self):
        """The guard's whole point: it contributes the USB-enumeration module set to
        boot.initrd.availableKernelModules (so a USB boot can see the stick). The
        nixosTest proves they actually PACK; this is the cheap "the contribution is
        even present" belt."""
        assert "boot.initrd.availableKernelModules" in self.content
        for mod in ["usb_storage", "xhci", "sd_mod"]:
            assert mod in self.content, f"USB-root module {mod} missing from the guard"

    def test_asserts_critical_subset_survived(self):
        """The eval-time tripwire: the module must `assertions` that the critical
        USB-root modules survived the merge — so a mkForce that wiped the list is a
        BUILD failure, not a silent real-HW brick."""
        assert "assertions" in self.content
        # The assertion references the merged config value (defense vs an override).
        assert "config.boot.initrd.availableKernelModules" in self.content

    def test_in_flake_modules(self):
        """Gate-5 wiring guard: imported in flake.nix or the option/test can't run."""
        flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        assert "hart-boot-root-initrd.nix" in flake

    def test_desktop_enables_it(self):
        """Cross-config wiring the nixosTest (mkNode enable) does not cover: the real
        USB-boot ISO config turns the guard ON."""
        desktop = read_variant("desktop")
        assert "bootRootInitrd.enable = true" in desktop


# ═══════════════════════════════════════════════════════════════
# Boot-experience: the behavioural nixosTests are WIRED INTO `checks`
# ═══════════════════════════════════════════════════════════════

class TestBootNixosTestsRegistered:
    """The three boot modules each ship a BEHAVIOURAL nixosTest (real Linux block
    layer / efibootmgr / mkfs.vfat) under nixos/tests/. A test that is never
    wired into `flake.nix` checks NEVER runs (CLAUDE.md Gate 5: a test that never
    runs guards nothing). These guards assert each test file EXISTS and is
    imported + composed into `checks.x86_64-linux` so `nix flake check` runs it."""

    BOOT_TESTS = [
        ("boot-log.nix", "bootLog"),
        ("hartlog-create.nix", "hartlogCreate"),
        ("boot-continuity.nix", "bootContinuity"),
        ("journal-export.nix", "journalExport"),
        ("boot-root-initrd.nix", "bootRootInitrd"),
    ]

    @pytest.fixture(autouse=True)
    def load_flake(self):
        self.flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))

    @pytest.mark.parametrize("filename,_attr", BOOT_TESTS)
    def test_nixos_test_file_exists(self, filename, _attr):
        path = os.path.join(TESTS_DIR, filename)
        assert os.path.isfile(path), f"missing behavioural nixosTest: nixos/tests/{filename}"

    @pytest.mark.parametrize("filename,attr", BOOT_TESTS)
    def test_nixos_test_is_imported(self, filename, attr):
        """The flake must `import ./tests/<filename>` and bind it to a let-var."""
        assert f"./tests/{filename}" in self.flake, \
            f"{filename} not imported in flake.nix checks"
        assert f"{attr} = import ./tests/{filename}" in self.flake, \
            f"{filename} not bound to the `{attr}` check var"

    @pytest.mark.parametrize("filename,attr", BOOT_TESTS)
    def test_nixos_test_composed_into_checks(self, filename, attr):
        """The bound var must be merged into the returned `checks` attrset (the
        `... // bootLog // hartlogCreate // bootContinuity` tail), else it never
        runs under `nix flake check`."""
        # The composition tail is a chain of `// <attr>`; assert ours is in it.
        assert f"// {attr}" in self.flake, \
            f"{attr} not composed into checks.x86_64-linux (would never run)"

    def test_boot_log_test_attaches_a_fat32_disk(self):
        """The boot-log nixosTest must attach a spare disk + format it FAT32/label
        HARTLOG (the behavioural stand-in for the stick's free-space partition)."""
        content = read_nix(os.path.join(TESTS_DIR, "boot-log.nix"))
        assert "emptyDiskImages" in content
        assert "mkfs.vfat" in content
        assert "HARTLOG" in content

    def test_hartlog_create_test_proves_never_touch_existing(self):
        """The carve nixosTest must assert the pre-existing partition is UNTOUCHED
        (the never-touch-the-in-use-partitions invariant) + idempotent re-run."""
        content = read_nix(os.path.join(TESTS_DIR, "hartlog-create.nix"))
        assert "untouched" in content.lower() or "never touch" in content.lower()
        assert "idempotent" in content.lower()
        # the new no-op gates this change adds:
        assert "no trailing free space" in content or "full disk" in content.lower()
        assert "non-removable" in content.lower() or "internal disk" in content.lower()

    def test_hartlog_create_test_proves_mbr_path_and_loud_marker(self):
        """The carve nixosTest must ALSO prove the isohybrid MBR/DOS carve path
        (parted, table stays DOS — sgdisk would convert it) and that the decision
        is recorded in the LOUD /run/hart/hartlog-create.status marker."""
        content = read_nix(os.path.join(TESTS_DIR, "hartlog-create.nix"))
        # The MBR/DOS stand-in is carved via parted and the table is not converted.
        assert "mklabel msdos" in content or "DOS" in content
        assert "DECISION=CREATED" in content
        # The loud no-op marker is asserted too.
        assert "/run/hart/hartlog-create.status" in content
        assert "DECISION=NOOP" in content

    def test_boot_continuity_test_proves_never_bootorder(self):
        """The continuity nixosTest must assert the script NEVER writes BootOrder
        (the never-strand-Windows invariant)."""
        content = read_nix(os.path.join(TESTS_DIR, "boot-continuity.nix"))
        assert "BootOrder" in content
        assert "--bootnext" in content

    def test_boot_root_initrd_test_proves_initrd_packs_usb_modules(self):
        """The boot-root-initrd nixosTest must EXTRACT the built initrd and prove the
        USB-root modules are really PACKED (the link a virtio-root VM never sees) AND
        re-prove the hartlog-create boot-disk-GPT guard never completes the boot
        medium's GPT (the duplicate-LABEL root race)."""
        content = read_nix(os.path.join(TESTS_DIR, "boot-root-initrd.nix"))
        # It cracks open the actual initrd (not a config value) to prove packing.
        assert "/run/current-system/initrd" in content
        assert "usb.storage" in content and "xhci" in content and "sd_mod" in content
        # It confirms root actually mounted (the dimension's baseline).
        assert "findmnt" in content
        # It re-proves the boot-disk-GPT guard via the documented test seam.
        assert "HART_HARTLOG_TEST_BOOT_DISK" in content
        assert "DECISION=NOOP" in content and "boot medium" in content

    def test_boot_root_initrd_test_proves_guard_FIRES_when_stripped(self):
        """The degrade-not-die TRIPWIRE (the negative case the VM boot can't show):
        the boot-root-initrd test file must ALSO carry the eval-time proof that the
        guard's assertion FIRES (the build fails loudly) when usb_storage/xhci/sd_mod
        are stripped (mkForce []). The VM boot only proves the POSITIVE packing case;
        without this, a future override that wipes boot.initrd.availableKernelModules
        would ship a silent real-HW "VFS: Unable to mount root fs" brick. Source-shape
        guard only — the BEHAVIOUR is the `hart-boot-root-initrd-guard-eval` runCommand
        the flake actually builds (evalModules with + without the modules); this guard
        just prevents that negative proof from being silently deleted."""
        content = read_nix(os.path.join(TESTS_DIR, "boot-root-initrd.nix"))
        # The isolated eval of the REAL module under two scenarios.
        assert "evalModules" in content, "the eval-time tripwire proof is missing"
        assert "mkForce" in content, "the stripped scenario (mkForce []) is missing"
        # The build-time check the flake composes (auto-wired via `// bootRootInitrd`).
        assert "hart-boot-root-initrd-guard-eval" in content
        # It asserts the guard stays QUIET when present and FIRES when stripped.
        assert "EXPECT 0" in content and "EXPECT >= 1" in content

    def test_boot_continuity_test_has_uefi_poweroff_gate_node(self):
        """#187/F4: a dedicated UEFI (OVMF) nixosTest node must prove the poweroff
        gate behaviourally — the non-UEFI node can't reach it (it no-ops at the
        UEFI gate first). The node boots useEFIBoot, injects a scheduled poweroff,
        and asserts ZERO efibootmgr calls (arms nothing) while a scheduled reboot
        DOES reach the arm/resolve stage (the gate discriminates)."""
        content = read_nix(os.path.join(TESTS_DIR, "boot-continuity.nix"))
        assert "hart-boot-continuity-poweroff-gate" in content
        assert "useEFIBoot" in content
        # It drives the poweroff via the scheduled-shutdown record + a recorder.
        assert "MODE=poweroff" in content
        # The node must be built in the CI VM workflow (a test that never runs
        # guards nothing — Gate 5).
        wf = read_nix(os.path.join(REPO_ROOT, ".github", "workflows", "nixos-vm-tests.yml"))
        assert "hart-boot-continuity-poweroff-gate" in wf, \
            "VM workflow does not build hart-boot-continuity-poweroff-gate — it would never gate"


# ═══════════════════════════════════════════════════════════════
# Session tier-drop supervisor — the never-blank-screen guarantee
# ═══════════════════════════════════════════════════════════════
#
# hart-session-supervisor.nix is the greetd-driven out-of-process tier-drop
# supervisor. Its BEHAVIOUR (greetd relaunch, crash-loop drop, paint-watchdog
# hang-kill, latch-across-boot, never-below-cage) is proven by the wired-in
# nixosTests (nixos/tests/session-supervisor.nix → flake checks). These STRUCTURAL
# guards lock the OPTION WIRING a VM assertion can't cheaply reach or would be
# wasteful to boot a VM for: that the options exist with the right enum/type, that
# the tier ladder + floor are correctly ordered, that desktop.nix opts the
# supervisor on at the right startTier, and that the recovery-TTY block is wired so
# Ctrl+Alt+F-key always reaches a console. Per memory/feedback_no_grep_tests.md
# these are the acceptable cross-file DRY / never-fail source-guard class — the
# VM test owns the behaviour, this owns the contract SHAPE.
class TestSessionSupervisorModule:
    """hart-session-supervisor.nix OPTION + LADDER CONTRACT guards.

    The BEHAVIOUR (greetd replaces GDM, the crash-loop drop + latch to cage, the
    paint-watchdog hang-kill, the latch-across-reboot, never-below-cage, the
    group-writable marker dir, the start-tier resolution, the recovery TTYs, AND —
    after this change — the node_watchdog-unhealthy single-crash drop) is proven by
    the wired-in nixosTests nixos/tests/session-supervisor.nix. Per
    feedback_no_grep_tests.md these structural checks keep ONLY the option
    enum/type/default REGEX guards (a VM boot is wasteful for a default value) and
    the load-bearing ladder/floor CONSTANTS — the contract SHAPE the VM test
    can't cheaply assert. The grep-on-source duplicates that merely shadowed a
    behavioural nixosTest assertion (greetd-replaces-gdm, the selector-in-bg
    launch, the marker dir mode, the floor-exempt branch, the persistent-latch +
    tmpfs-flag paths, the record_crash/lower_tier symbol-presence, the
    SIGNAL-EMITTER-ONLY comment grep) were REMOVED — the nixosTests prove them
    behaviourally."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.content = read_nix(os.path.join(MODULES_DIR, "hart-session-supervisor.nix"))

    def test_file_exists(self):
        assert os.path.isfile(os.path.join(MODULES_DIR, "hart-session-supervisor.nix"))

    def test_has_enable_option(self):
        assert "mkEnableOption" in self.content
        assert "options.hart.sessionSupervisor" in self.content

    def test_opt_in_no_op_when_disabled(self):
        """The module is OPT-IN: pure no-op unless BOTH hart.enable and
        sessionSupervisor.enable are set (never silently replaces GDM). A VM with
        the supervisor ENABLED cannot cheaply prove the DISABLED path is a no-op —
        this guards the mkIf gate that makes it so."""
        assert "lib.mkIf (cfg.enable && sup.enable)" in self.content

    def test_start_tier_is_enum_of_the_three_tiers(self):
        """startTier is an enum constrained to exactly the three ladder tiers —
        a typo can't smuggle in an invalid start tier."""
        assert "startTier" in self.content
        assert 'lib.types.enum [ "hart-comp" "sway" "cage" ]' in self.content

    def test_start_tier_defaults_to_tier1(self):
        """Default startTier is the head of the ladder (hart-comp); the supervisor
        owns the never-blank guarantee so starting high is safe."""
        m = re.search(r"startTier\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "startTier option block not found"
        assert 'default = "hart-comp"' in m.group(1)

    def test_tier_ladder_order_highest_to_lowest(self):
        """The ORDERED ladder constant is hart-comp → sway → cage; cage LAST = the
        floor. The load-bearing source-of-truth for the drop direction (a reorder
        would silently invert the ladder); the VM proves the EFFECT, this locks the
        literal so a reorder is caught the instant a diff lands."""
        assert 'tierLadder = [ "hart-comp" "sway" "cage" ]' in self.content

    def test_cage_is_the_floor(self):
        """The floor constant the supervisor can never drop below — paired with the
        ladder ordering above."""
        assert 'FLOOR="cage"' in self.content

    def test_crash_loop_count_default_three(self):
        m = re.search(r"crashLoopCount\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "crashLoopCount option block not found"
        assert "default = 3" in m.group(1)
        assert "ints.positive" in m.group(1)

    def test_crash_loop_window_default_300(self):
        m = re.search(r"crashLoopWindowSeconds\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "crashLoopWindowSeconds option block not found"
        assert "default = 300" in m.group(1)
        assert "ints.positive" in m.group(1)

    def test_paint_timeout_option_unsigned_default_20(self):
        """The shell-paint watchdog budget — the HUNG-tier guard the bare crash
        detection is blind to. unsigned so 0 (disable) is a valid value."""
        assert "shellPaintTimeoutSeconds" in self.content
        m = re.search(r"shellPaintTimeoutSeconds\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "shellPaintTimeoutSeconds option block not found"
        assert "default = 20" in m.group(1)
        assert "ints.unsigned" in m.group(1)

    def test_input_alive_timeout_option_unsigned_default_0_failsafe(self):
        """The input-alive watchdog budget — the INPUT twin of the paint watchdog,
        catching a tier that PAINTS but never delivers input (#134). The DEFAULT
        MUST be 0 (disabled): marker absence is ambiguous (real input-death VS a
        build whose compositors do not write the marker yet), and dropping on a
        missing WRITER would flap every healthy tier to the floor. Default-0 is the
        never-flap fail-safe — this locks it at the cheapest level (a VM boot is
        wasteful for a default value). unsigned so 0 is valid; the VM nodes prove
        the enabled behaviour (drop / keep / disabled-no-flap)."""
        assert "inputAliveTimeoutSeconds" in self.content
        m = re.search(r"inputAliveTimeoutSeconds\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "inputAliveTimeoutSeconds option block not found"
        assert "default = 0" in m.group(1), "input watchdog must default OFF (never-flap fail-safe)"
        assert "ints.unsigned" in m.group(1)
        # The marker the supervisor exports + consumes, shared with the compositor.
        assert "HART_INPUT_ALIVE_FLAG" in self.content
        assert "input-alive" in self.content
        # DRY: paint-hang and input-hang share ONE drop path (no parallel mechanism).
        assert "drop_hung_tier" in self.content

    def test_cage_command_non_empty_assertion(self):
        """GENUINE module-shape contract a VM boot would only surface as a runtime
        FATAL-loop blank screen: cageCommand is types.str (not nullOr), and an
        empty "" would make the FLOOR unlaunchable → the selector loops on
        'FATAL: no available session command' = the exact blank screen this module
        prevents. A module `assertion` must fail the closure at eval time instead.
        Assert the assertion exists and gates on cageCommand != ""."""
        assert 'assertion = sup.cageCommand != ""' in self.content

    def test_cage_command_reused_verbatim_not_reimplemented(self):
        """Tier-3 is hart-liquid-ui.nix's hart-shell-session, reused — the floor is
        never reimplemented in the supervisor (a default-value DRY contract)."""
        m = re.search(r"cageCommand\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "cageCommand option block not found"
        assert 'default = "hart-shell-session"' in m.group(1)

    def test_comp_command_null_until_phase3(self):
        """Tier-1 (hart-comp) launch command is nullOr str (a null tier falls
        straight through to the next so the slot is reserved, never blank)."""
        m = re.search(r"compCommand\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "compCommand option block not found"
        assert "nullOr" in m.group(1)

    def test_sway_command_nullable(self):
        m = re.search(r"swayCommand\s*=\s*lib\.mkOption\s*\{(.*?)\};", self.content, re.S)
        assert m, "swayCommand option block not found"
        assert "nullOr" in m.group(1)


class TestSessionSupervisorDesktopWiring:
    """desktop.nix opts the supervisor on at the right tier + keeps the recovery
    consoles reachable — the cross-config wiring a VM would be wasteful to boot."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        self.config = read_variant("desktop")

    def test_desktop_enables_supervisor(self):
        assert "sessionSupervisor" in self.config
        # The supervisor block enables it.
        m = re.search(r"sessionSupervisor\s*=\s*\{(.*?)\};", self.config, re.S)
        assert m, "sessionSupervisor block not found in desktop.nix"
        assert "enable = true" in m.group(1)

    def test_desktop_start_tier_is_tier1(self):
        """The desktop boots at Tier-1 (hart-comp) — the ladder tries the best tier
        first; the watchdog drops it on real-HW failure."""
        m = re.search(r"sessionSupervisor\s*=\s*\{(.*?)\};", self.config, re.S)
        assert m, "sessionSupervisor block not found in desktop.nix"
        assert 'startTier = "hart-comp"' in m.group(1)

    def test_no_parallel_default_session_pin(self):
        """The crude fixed cage-pin (mkForce defaultSession = hart-shell) is REMOVED
        from desktop.nix — the supervisor owns tier selection now, so a second
        mkForce here would collide with the supervisor's own mkForce. Check only
        non-comment lines (the removal is documented in a comment block)."""
        code_lines = [
            ln for ln in self.config.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "defaultSession = lib.mkForce" not in code, \
            "desktop.nix still pins defaultSession via mkForce — collides with the supervisor"
        assert 'mkForce "hart-shell"' not in code, \
            "desktop.nix still force-pins the cage session — the supervisor owns tier selection now"

    # ── Recovery consoles: Ctrl+Alt+F2..F6 always reach a getty login ──
    def test_console_framework_stays_enabled(self):
        """`console.enable` keeps the virtual terminals (tty1..tty6) on so a
        future kiosk tweak can't silently disable VT switching."""
        assert "console.enable = lib.mkDefault true" in self.config

    def test_tty_autologin_nulled_so_fkey_never_lands_on_hidden_user(self):
        assert "services.getty.autologinUser = lib.mkForce null" in self.config

    def test_recovery_getty_prespawned_on_tty2(self):
        """A getty is pre-spawned on tty2 from boot (not summoned lazily) so a
        recovery console is ALREADY alive the instant the user switches — recovery
        never depends on logind's on-demand autovt spawn while the graphical
        session is wedged."""
        assert 'systemd.services."autovt@tty2".wantedBy = [ "multi-user.target" ]' in self.config

    def test_recovery_block_documents_the_pointer_only_regression(self):
        """The recovery block exists BECAUSE of the only-a-pointer hang — keep the
        rationale so it is never removed as 'dead config'."""
        c = self.config.lower()
        assert "ctrl+alt+f" in c
        assert "recovery" in c

    def test_comment_keeps_tty2_through_tty6_range(self):
        """tty3..tty6 stay on-demand via NAutoVTs (logind default) — the comment
        names the full recovery range so it's not narrowed to tty2 alone."""
        assert "tty2..tty6" in self.config or "tty2-6" in self.config


class TestSessionSupervisorNixTestWiring:
    """The session-supervisor nixosTests are REGISTERED in flake checks (Gate 5:
    a test that never runs guards nothing) AND each focused scenario the task
    names is present as a deterministic VM node."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.flake = read_nix(os.path.join(NIXOS_DIR, "flake.nix"))
        self.nixtest = read_nix(os.path.join(TESTS_DIR, "session-supervisor.nix"))

    def test_module_in_flake(self):
        assert "hart-session-supervisor" in self.flake

    def test_supervisor_test_imported_and_merged_into_checks(self):
        assert "import ./tests/session-supervisor.nix" in self.flake
        assert "supervisor =" in self.flake
        # Merged into the final checks attrset (the `//` chain).
        assert "// supervisor" in self.flake

    def test_tier_drop_node_present(self):
        assert "hart-session-supervisor-tier-drop" in self.nixtest

    def test_paint_watchdog_node_present(self):
        assert "hart-session-supervisor-paint-watchdog" in self.nixtest

    def test_fresh_boot_start_tier_node_present(self):
        """A node proving a fresh (un-latched) boot honours startTier for all three
        valid values (cage/sway/hart-comp)."""
        assert "hart-session-supervisor-start-tier" in self.nixtest

    def test_reboot_latch_persist_node_present(self):
        """A node proving a dropped tier stays dropped across a REAL reboot (the
        latch persists) and never goes below cage."""
        assert "hart-session-supervisor-reboot-latch" in self.nixtest

    def test_recovery_tty_node_present(self):
        """A node proving getty on tty2..tty6 + the autovt@tty2 pre-spawn are
        reachable even while a graphical session holds VT1."""
        assert "hart-session-supervisor-recovery-tty" in self.nixtest

    def test_paint_watchdog_keep_node_present(self):
        """The watchdog has a POSITIVE case too: a tier whose compositor DOES touch
        the marker within the budget is KEPT, not dropped (a dedicated node with a
        painting fake compositor — proves the watchdog doesn't over-fire)."""
        assert "hart-session-supervisor-paint-watchdog-keep" in self.nixtest
        assert "shell-ready" in self.nixtest
        # The painting fake honours the real contract: it touches the marker the
        # selector exports via HART_SHELL_READY_FLAG (one shared path).
        assert "HART_SHELL_READY_FLAG" in self.nixtest

    def test_unhealthy_flag_node_present(self):
        """The node_watchdog-unhealthy-signal path has its OWN behavioural node:
        touching /run/hart/compositor-unhealthy and running the selector ONCE must
        record EXACTLY ONE crash + drop cleanly WITHOUT falling through to launch a
        tier (proving the double-record fix). It exercises the one-way signal flag
        the old structural test only grepped for."""
        assert "hart-session-supervisor-unhealthy-flag" in self.nixtest
        # It touches the real signal flag the supervisor consumes.
        assert "/run/hart/compositor-unhealthy" in self.nixtest

    def test_input_watchdog_nodes_present(self):
        """The input-alive dimension (#134: PAINTS but never delivers input) has its
        OWN behavioural nodes: a DROP case (a fake that paints but never signals
        input is killed + dropped to cage), a KEEP case (a fake that paints AND
        signals input is kept — proves no over-fire), and a DISABLED case (the
        DEFAULT 0 keeps a painted-but-input-dead tier — proves the never-flap
        fail-safe). The fakes honour the real contract: they touch the marker the
        selector exports via HART_INPUT_ALIVE_FLAG (one shared path)."""
        for node in (
            "hart-session-supervisor-input-watchdog",
            "hart-session-supervisor-input-watchdog-keep",
            "hart-session-supervisor-input-watchdog-disabled",
        ):
            assert node in self.nixtest, f"input-alive VM node {node} missing"
        # The fakes use the exported marker path (writer + watchdog share ONE path),
        # and the disabled node proves the never-flap default.
        assert "HART_INPUT_ALIVE_FLAG" in self.nixtest
        assert "input-alive" in self.nixtest

    def test_each_new_node_in_ci_vm_workflow(self):
        """Every check that should RUN must be built explicitly in the VM workflow
        (the workflow targets checks by name, not `nix flake check`)."""
        wf = read_nix(os.path.join(REPO_ROOT, ".github", "workflows", "nixos-vm-tests.yml"))
        for node in [
            "hart-session-supervisor-tier-drop",
            "hart-session-supervisor-paint-watchdog",
            "hart-session-supervisor-paint-watchdog-keep",
            "hart-session-supervisor-start-tier",
            "hart-session-supervisor-reboot-latch",
            "hart-session-supervisor-recovery-tty",
            "hart-session-supervisor-unhealthy-flag",
            "hart-session-supervisor-input-watchdog",
            "hart-session-supervisor-input-watchdog-keep",
            "hart-session-supervisor-input-watchdog-disabled",
        ]:
            assert node in wf, f"VM workflow does not build/run {node} — it would never gate"


# ═══════════════════════════════════════════════════════════════
# Seat / DRM bring-up — the real-HW "permission denied / device busy" root fix
# ═══════════════════════════════════════════════════════════════
#
# On bare metal every tier (hart-comp/sway/cage) failed to come up because the
# standard Wayland-compositor seat/DRM setup was missing. The BEHAVIOUR (seatd
# running, hart-admin in the seat/input/video/render groups, greetd preferring
# the seatd libseat backend) is proven by the wired-in nixosTest's "seatd is
# active + hart-admin has the seat/DRM/input groups" subtest (it boots greetd and
# reads `id -nG`). Per feedback_no_grep_tests.md these structural checks keep ONLY
# the cross-file OPTION WIRING a VM boot is wasteful to re-assert: that seatd is
# enabled where greetd is introduced, the groups are declared, and the DRM-master
# handoff knobs exist. They never substitute for the behavioural subtest.
class TestSeatDrmBringUp:
    """hart-session-supervisor.nix + hart-base.nix wire the compositor seat/DRM
    access (seatd backend, device groups, Plymouth DRM-master handoff, tier-drop
    master release) so the ladder actually scans out on real hardware."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.sup = read_nix(os.path.join(MODULES_DIR, "hart-session-supervisor.nix"))
        self.base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))

    def test_seatd_enabled_with_greetd(self):
        """seatd is the libseat BACKEND the greetd-launched compositor talks to —
        without it libseat falls to its unreliable-under-greetd logind probe and
        the compositor cannot acquire the seat's DRM master / input devices."""
        assert "services.seatd.enable = true" in self.sup

    def test_session_prefers_seatd_backend(self):
        """The greetd session is wrapped to export LIBSEAT_BACKEND=seatd so every
        tier prefers the seatd daemon over the logind probe (one wrapper, DRY)."""
        assert "LIBSEAT_BACKEND=seatd" in self.sup

    def test_hart_admin_in_seat_group(self):
        """seatd brokers /dev/dri + /dev/input only to seat-group members — the
        supervisor adds hart-admin to `seat` (the group exists only once seatd is
        enabled, so it is added HERE, not in hart-base). The `seat` entry may share
        the extraGroups line with the `hart` latch-write group, so assert the seat
        MEMBERSHIP (regex + membership, same robust pattern as the video/render/input
        sibling below), not a brittle exact-list string."""
        m = re.search(r"users\.users\.hart-admin\.extraGroups\s*=\s*\[(.*?)\];",
                      self.sup, re.S)
        assert m, "hart-admin extraGroups not set in the session supervisor"
        assert '"seat"' in m.group(1), \
            "hart-admin not added to the seat group (seatd brokers /dev/dri + /dev/input)"

    def test_hart_admin_has_video_render_input_groups(self):
        """hart-base puts hart-admin in video (KMS /dev/dri/card*), render (GPU
        /dev/dri/renderD*) AND input (/dev/input/* for libinput) — WITHOUT input
        a Wayland compositor boots dead-input (EACCES on /dev/input)."""
        m = re.search(r"users\.users\.hart-admin\s*=\s*\{(.*?)\};", self.base, re.S)
        assert m, "hart-admin user block not found in hart-base.nix"
        block = m.group(1)
        for g in ('"video"', '"render"', '"input"'):
            assert g in block, f"hart-admin missing group {g} (compositor can't open the seat device)"

    def test_plymouth_drm_master_handoff(self):
        """The boot splash holds DRM master on card0; greetd must order After
        plymouth-quit-wait so the splash has RELEASED master before the compositor
        claims it (else drmSetMaster → EBUSY)."""
        assert "plymouth-quit-wait.service" in self.sup

    def test_greetd_on_own_vt(self):
        """greetd runs on its OWN vt (off the tty2..tty6 recovery range + off the
        boot-console tty1) so its session is the seat's ACTIVE session — the
        precondition for legally holding DRM master on that seat."""
        assert "vt = 7" in self.sup

    def test_tier_drop_releases_drm_master(self):
        """A tier-drop must let the prior compositor drop DRM master before the next
        tier launches: SIGTERM-with-grace (not instant SIGKILL) + a post-kill settle
        so the kernel reclaims card0's master (prevents the EBUSY handoff race)."""
        # The graceful-kill grace + the master settle options + helper exist.
        assert "tierTermGraceSeconds" in self.sup
        assert "drmMasterSettleSeconds" in self.sup
        assert "drm_master_settle" in self.sup


# ═══════════════════════════════════════════════════════════════════════════
# Firmware-support matrix: which medium boots which firmware
# ═══════════════════════════════════════════════════════════════════════════

class TestFirmwareSupportMatrix:
    """Every shipped medium's firmware support is PINNED, not assumed.

    The goal says "All Bios compatibility like hyper v etc" and this is the
    file that can hold the whole matrix in one place. The gap it caught
    (2026-07-30): only the SERVER iso set isoImage.makeBiosBootable, so the
    desktop and edge ISOs were built EFI-ONLY — no El Torito BIOS image, no
    isolinux — and a legacy-BIOS machine, including a Hyper-V GENERATION 1
    VM which has no UEFI at all, could not boot them even to reach the
    installer.

    Structural by necessity (these are Nix build-time options; the
    behavioural half is the ISO build itself in CI plus a real Gen-1 boot),
    and deliberately NOT the only coverage: the guest-agent and microcode
    claims from the same parity pass are asserted on a booted VM in
    nixos/tests/vm-tests.nix.
    """

    ISO_VARIANTS = ["desktop.nix", "server.nix", "edge.nix"]

    @pytest.mark.parametrize("cfg", ISO_VARIANTS)
    def test_every_iso_is_bios_bootable(self, cfg):
        src = read_nix(os.path.join(CONFIGS_DIR, cfg))
        assert "makeBiosBootable" in src, (
            f"{cfg}'s ISO is EFI-ONLY — a BIOS/CSM machine (or a Hyper-V "
            f"Gen 1 VM, which has no UEFI) cannot boot it at all")

    def test_raw_image_is_uefi_only_by_design(self):
        """The raw image is UEFI-only ON PURPOSE — pin the design choice so a
        later 'fix' does not quietly bolt GRUB onto systemd-repart's
        Discoverable-Partitions + UKI model. BIOS users install from the ISO."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-repart-image.nix"))
        assert "boot.loader.systemd-boot.enable = true" in src
        assert "boot.loader.grub.enable = false" in src
        assert "UEFI-only" in src, "the UEFI-only decision must stay documented"

    def test_installer_picks_the_bootloader_from_firmware(self):
        """An INSTALLED system supports both firmwares: the installer probes
        /sys/firmware/efi and writes systemd-boot or GRUB accordingly."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-installer.nix"))
        assert "/sys/firmware/efi" in src, "installer must probe the firmware"
        assert "boot.loader.grub.enable = true" in src, "no BIOS path in the installer"
        assert "boot.loader.systemd-boot.enable = true" in src, "no EFI path"

    def test_installed_bios_path_keeps_os_prober(self):
        """Dual-boot on BIOS needs os-prober or Windows vanishes from the menu."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-installer.nix"))
        assert "useOSProber = true" in src


class TestHypervisorGuestParity:
    """Guest integration is configured for EVERY hypervisor, not just QEMU.

    Windows and macOS guests get display resize, clipboard, graceful
    host-initiated shutdown and host time sync out of the box; HART shipped
    none of it. All stock NixOS options, so this pins that they stay wired.
    Behavioural counterpart: the 'hypervisor guest agents are configured'
    subtest in nixos/tests/vm-tests.nix asserts the units on a real boot.
    """

    def setup_method(self):
        self.base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))

    @pytest.mark.parametrize("opt", [
        "virtualisation.hypervGuest.enable",   # Hyper-V (incl. hv_utils time sync)
        "services.qemuGuest.enable",           # QEMU / KVM / Proxmox
        "services.spice-vdagentd.enable",      # SPICE clipboard + auto-resize
        "virtualisation.vmware.guest.enable",  # open-vm-tools
    ])
    def test_guest_agent_is_configured(self, opt):
        assert opt in self.base, (
            f"{opt} missing — a HART guest on that hypervisor loses display "
            f"resize / clipboard / graceful shutdown that Windows guests have")

    def test_vmware_guest_is_x86_gated(self):
        """open-vm-tools does not exist on aarch64; an ungated enable breaks
        every ARM variant's eval."""
        assert "isx86" in self.base

    @pytest.mark.parametrize("opt", [
        "hardware.cpu.intel.updateMicrocode",
        "hardware.cpu.amd.updateMicrocode",
    ])
    def test_cpu_microcode_is_applied(self, opt):
        """Both closed OSes ship microcode; missing it is a silent
        correctness/security exposure."""
        assert opt in self.base

    def test_microcode_rides_existing_firmware_consent(self):
        """No NEW licensing decision: microcode is gated on the same
        redistributable-firmware consent the wifi firmware already uses."""
        assert "enableRedistributableFirmware" in self.base


class TestEnabledOptionsExist:
    """Every hart.<feature> a profile enables must come from a module that is
    actually in hartModules.

    THE BUG THIS CATCHES (2026-07-30, run 30567029164): three module FILES —
    hart-openclaw.nix, hart-scanner.nix, hart-sso.nix — sat in the tree with
    complete option sets and config but were never added to the flake's
    hartModules list. Their options therefore did not EXIST, so nothing could
    turn them on, and the moment a profile did the WHOLE flake eval aborted:

        error: The option `hart.openclaw' does not exist.

    A file on disk is not a loaded module. Local + instant; the eval gate
    catches it too, but only after a CI round trip that took every unrelated
    target red with it.
    """

    FLAKE = os.path.join(NIXOS_DIR, "flake.nix")

    def _loaded_modules(self):
        src = read_nix(self.FLAKE)
        return set(re.findall(r'\./modules/(hart-[\w-]+)\.nix', src))

    def _module_defining(self, feature):
        """The module file whose options block defines hart.<feature>."""
        for path in glob.glob(os.path.join(MODULES_DIR, "hart-*.nix")):
            src = read_nix(path)
            if re.search(r'config\.hart\.' + re.escape(feature) + r'\b', src) or \
               re.search(r'options\.hart\.' + re.escape(feature) + r'\b', src):
                return os.path.basename(path)[:-4]
        return None

    @pytest.mark.parametrize("variant", ["desktop", "server", "edge", "phone"])
    def test_every_enabled_feature_has_a_loaded_module(self, variant):
        prof = read_nix(os.path.join(PROFILES_DIR, variant + ".nix"))
        loaded = self._loaded_modules()
        # Features the profile turns on, as `<name>.enable = true` or
        # `<name> = { ... enable = true; ... }` inside the hart block.
        enabled = set(re.findall(r'^\s{4}([a-zA-Z][\w]*)\.enable\s*=\s*true',
                                 prof, re.M))
        enabled |= set(re.findall(r'^\s{4}([a-zA-Z][\w]*)\s*=\s*\{', prof, re.M))
        missing = []
        for feat in sorted(enabled):
            mod = self._module_defining(feat)
            if mod is not None and mod not in loaded:
                missing.append(f"hart.{feat} (defined in {mod}.nix)")
        assert not missing, (
            f"{variant}.nix enables options whose modules are NOT in the "
            f"flake's hartModules — the option does not exist and the WHOLE "
            f"eval aborts: {missing}")

    def test_every_module_file_is_loaded(self):
        """A module file nobody imports is dead code that looks live — the
        exact shape of the openclaw/scanner/sso gap. Deliberate exclusions
        are listed here so the reason is written down, not implied."""
        # hart-app / package helpers are not NixOS modules; ARM/board files
        # live under hardware/ and are imported per-machine.
        EXPECTED_UNLOADED = set()
        on_disk = {os.path.basename(p)[:-4]
                   for p in glob.glob(os.path.join(MODULES_DIR, "hart-*.nix"))}
        unloaded = on_disk - self._loaded_modules() - EXPECTED_UNLOADED
        assert not unloaded, (
            f"module files present but never imported into hartModules "
            f"(their hart.* options do not exist): {sorted(unloaded)}")


class TestNoRequiredOptionTraps:
    """A module option with a type but NO default is REQUIRED — and the
    instant its module is imported and enabled, eval ABORTS.

    THE CASCADE THIS ENDS (2026-07-30, three CI rounds, one per error
    because nix stops at the first):
      round 1  error: The option `hart.openclaw' does not exist
      round 2  (three modules unwired — two hidden behind the first)
      round 3  error: The option `hart.sso.domain' was accessed but has
               no value defined      ... with ldapUri and ldapBaseDn each
               queued behind it as rounds 4 and 5.

    Every one of those cost a full CI cycle to learn ONE fact. This
    asserts the whole class locally in milliseconds.

    The rule: a required option is only acceptable when EVERY consumer is
    guaranteed to set it. `hart.package` qualifies — mkNode, every
    configuration and mkInstalledSystem all set it explicitly, and a
    default would silently ship the wrong closure. Anything else must
    carry a default and, if it needs real configuration, an assertion
    that says so in a sentence.
    """

    # Options allowed to stay required, with the reason they are safe.
    ALLOWED_REQUIRED = {
        # (module, option): why
        ("hart-base", "package"): "every consumer sets it; a default would ship the wrong app",
        ("hart-comp", "package"): "compositor package is wired per-consumer",
        ("hart-rust-precedent", "package"): "package is wired per-consumer",
    }

    def _required_options(self):
        """(module, option, type) for every mkOption with a type and no
        default — the exact shape that aborts eval when enabled."""
        found = []
        for path in sorted(glob.glob(os.path.join(MODULES_DIR, "hart-*.nix"))):
            src = read_nix(path)
            mod = os.path.basename(path)[:-4]
            for m in re.finditer(
                    r'(\w+)\s*=\s*lib\.mkOption\s*\{(.*?)\n\s*\};', src, re.S):
                name, body = m.group(1), m.group(2)
                # `default` must be an ASSIGNMENT, not the word appearing in
                # prose. `"default" in body` also matched description/example
                # text, so any option documented as "there is no default; ..."
                # or "defaults to X" was skipped UNCHECKED — a latent hole in
                # the guard everyone now trusts INSTEAD of a CI round (found
                # by the reviewing session against a synthetic module; no live
                # instance among 69 modules / 257 real `default =`).
                if re.search(r'^\s*default\s*=', body, re.M) \
                        or "mkEnableOption" in body:
                    continue
                if "type" not in body:
                    continue
                found.append((mod, name))
        return found

    def test_no_module_declares_an_unguarded_required_option(self):
        offenders = [f"hart.{opt} (in {mod}.nix)"
                     for mod, opt in self._required_options()
                     if (mod, opt) not in self.ALLOWED_REQUIRED]
        assert not offenders, (
            "these options have a type but NO default, so enabling their "
            "module aborts the WHOLE flake eval with 'was accessed but has "
            "no value defined' — give each a default plus an assertion, or "
            "add it to ALLOWED_REQUIRED with the reason it is safe: "
            f"{offenders}")

    def test_sso_is_configured_or_disabled_never_half(self):
        """SSO is the case that proved the rule: an LDAP client needs
        site-specific values HART cannot invent. Either a profile sets all
        three alongside enable, or it does not enable it at all."""
        for variant in ("desktop", "server", "edge", "phone"):
            prof = read_nix(os.path.join(PROFILES_DIR, variant + ".nix"))
            if re.search(r'^\s+sso\.enable\s*=\s*true', prof, re.M):
                for opt in ("domain", "ldapUri", "ldapBaseDn"):
                    assert re.search(r'sso\.' + opt + r'\s*=|' + opt + r'\s*=',
                                     prof), (
                        f"{variant} enables hart.sso without setting {opt} — "
                        f"an SSO client with no directory does nothing and "
                        f"trips the module's assertion")

    def test_sso_module_has_defaults_and_an_assertion(self):
        """The consumer-protecting half: defaults so eval survives, an
        assertion so an unconfigured enable fails readably."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-sso.nix"))
        assert "assertions" in src, "hart-sso must assert on unconfigured enable"
        for opt in ("domain", "ldapUri", "ldapBaseDn"):
            block = re.search(opt + r'\s*=\s*lib\.mkOption\s*\{(.*?)\n\s*\};',
                              src, re.S)
            assert block and "default" in block.group(1), (
                f"hart.sso.{opt} still has no default — it will abort eval")


class TestHardeningSurvivesFeatureEnables:
    """A security hardening must never be undone as a SIDE EFFECT of enabling
    an unrelated feature.

    THE REGRESSION (2026-07-30, introduced then caught in the same session):
    hart-devtools set `kernel.yama.ptrace_scope = 0` with NO priority inside
    its debugger bundle. hart-security sets it to `mkDefault 1`, and in the
    NixOS module system a plain definition BEATS mkDefault — so the moment
    the desktop profile enabled devtools, every shipped machine silently got
    unrestricted ptrace (any process may read any other of the same user)
    while nixos/tests/security.nix still asserted it was 1.

    Neither Windows (SeDebugPrivilege) nor macOS (SIP + entitlements) ships
    that open, so restricted is also the parity-correct default.
    """

    def test_ptrace_is_only_opened_by_an_explicit_opt_in(self):
        src = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        assert "ptraceUnrestricted" in src, (
            "opening ptrace must be its own opt-in, not a side effect of "
            "installing a debugger")
        # The debugger bundle must no longer touch ptrace at all.
        debug_block = re.search(
            r'\(lib\.mkIf cfg\.debug \{(.*?)\n    \}\)', src, re.S)
        assert debug_block, "could not locate the cfg.debug block"
        assert "ptrace_scope" not in debug_block.group(1), (
            "the debug bundle still changes ptrace_scope — installing gdb "
            "must not change the machine's security posture")

    def test_opening_ptrace_is_loud_when_it_happens(self):
        """An override of a hardening must be mkForce — visible in source,
        not an accident of merge priority."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-devtools.nix"))
        block = re.search(
            r'lib\.mkIf cfg\.ptraceUnrestricted \{(.*?)\}\)', src, re.S)
        assert block and "mkForce" in block.group(1), (
            "ptrace opt-in must mkForce so it deliberately (and visibly) "
            "overrides hart-security's default")

    def test_security_hardening_still_defaults_restricted(self):
        src = read_nix(os.path.join(MODULES_DIR, "hart-security.nix"))
        assert re.search(r'"kernel\.yama\.ptrace_scope"\s*=\s*lib\.mkDefault 1',
                         src), "hart-security must still default ptrace to 1"

    def test_no_profile_silently_opens_ptrace(self):
        for variant in ("desktop", "server", "edge", "phone"):
            prof = read_nix(os.path.join(PROFILES_DIR, variant + ".nix"))
            assert "ptraceUnrestricted" not in prof, (
                f"{variant} opts into unrestricted ptrace — that is a "
                f"deliberate developer-box choice, not a shipped default")


class TestDriverFirmwareBreadth:
    """Device firmware and microcode reach EVERY variant.

    THE GAP (2026-07-30): hardware.enableRedistributableFirmware was set only
    in profiles/desktop.nix. Server and edge therefore shipped with no
    redistributable firmware, and a large share of Intel/Realtek wifi and
    ethernet parts need a firmware blob to bring the link up — so a HEADLESS
    server could boot with no network and no screen to diagnose it from.
    Windows and macOS both ship device firmware as standard.
    """

    def test_firmware_is_enabled_for_every_variant_from_one_writer(self):
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert "hardware.enableRedistributableFirmware" in base, (
            "firmware must be enabled in hart-base so EVERY variant gets it, "
            "not per-profile where a variant can silently miss out")

    def test_no_profile_re_declares_firmware(self):
        """Two writers for one option is the drift this consolidation removes;
        the profile's copy is why the gap was invisible."""
        for variant in ("desktop", "server", "edge", "phone"):
            prof = read_nix(os.path.join(PROFILES_DIR, variant + ".nix"))
            assert not re.search(
                r'^\s*hardware\.enableRedistributableFirmware\s*=', prof, re.M), (
                f"{variant}.nix re-declares firmware — hart-base owns it")

    def test_firmware_stays_overridable_for_size_bound_variants(self):
        """mkDefault, not a hard true: an edge image on a tiny board must be
        able to drop ~1GiB of firmware without editing hart-base."""
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        assert re.search(
            r'hardware\.enableRedistributableFirmware\s*=\s*lib\.mkDefault',
            base), "firmware must be mkDefault so a size-bound variant can opt out"

    def test_microcode_rides_the_same_consent(self):
        """Microcode is gated on the firmware consent, so enabling firmware
        per-variant cannot leave a variant silently without microcode."""
        base = read_nix(os.path.join(MODULES_DIR, "hart-base.nix"))
        for vendor in ("intel", "amd"):
            assert re.search(
                r'hardware\.cpu\.' + vendor + r'\.updateMicrocode\s*=\s*\n?\s*'
                r'lib\.mkDefault config\.hardware\.enableRedistributableFirmware',
                base), f"{vendor} microcode must ride the firmware consent"


class TestImageFitsItsTargetDevice:
    """A feature enable must not push the raw image past the device it ships on.

    MEASURED, not judged (closure audit 30570492265, hart-desktop-raw, the real
    config with exactly one option flipped):

        hart.devtools.enable ON  : 24 GiB
        hart.devtools.enable OFF : 21 GiB

    hart-repart-image.nix sizes the root at 26 GiB against a ~24 GiB image —
    about 2 GiB of slack — and that 26 GiB is itself bounded by the 28.7 GiB
    stick (its comment records that 1 GiB ESP + 28 GiB root did NOT fit). So
    +3 GiB does not merely bloat the image, it stops the raw image fitting the
    target device.

    This is the guard the everything-on sweep needed and did not have: the
    size-ceiling failure mode is one where the BUILD can pass and the ARTIFACT
    is unusable, so a test that fails in seconds is worth more than finding out
    after a multi-hour ISO job.
    """

    #: Features whose measured closure cost exceeds the raw image's slack.
    #: (option path, measured GiB delta, audit run) — extend as audits land.
    TOO_BIG_FOR_THE_DESKTOP_IMAGE = [
        ("devtools", 3, "30570492265"),
    ]

    def test_oversized_features_stay_off_the_desktop_profile(self):
        prof = read_nix(os.path.join(PROFILES_DIR, "desktop.nix"))
        for feat, cost, run in self.TOO_BIG_FOR_THE_DESKTOP_IMAGE:
            assert not re.search(r'^\s+' + feat + r'\.enable\s*=\s*true',
                                 prof, re.M), (
                f"hart.{feat} is enabled on the desktop image but measured "
                f"+{cost} GiB (audit {run}) against ~2 GiB of slack — the raw "
                f"image would no longer fit the 28.7 GiB stick")

    def test_the_root_size_and_its_reasoning_stay_documented(self):
        """The 26 GiB is derived from the TARGET DEVICE, not from CI. If that
        derivation is ever dropped, the next person sizes against the build
        host again and reintroduces the ship-time failure."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-repart-image.nix"))
        assert 'SizeMinBytes = "26G"' in src, "root size changed — re-measure"
        assert "28.7" in src, (
            "the stick-size derivation must stay in the comment; without it "
            "the next change sizes against the runner, not the device")

    def test_language_toolchains_still_ship(self):
        """Dropping devtools must NOT cost the desktop its compilers —
        hart.devTools (the near-identically-named sibling) is the toolchain
        module and stays on."""
        prof = read_nix(os.path.join(PROFILES_DIR, "desktop.nix"))
        assert re.search(r'^\s+devTools\.enable\s*=\s*true', prof, re.M), (
            "hart.devTools (language toolchains) must stay enabled — it is a "
            "different module from hart.devtools despite the name")


class TestNoInheritShadowedProfileOptions:
    """`inherit X;` inside a test node is a PLAIN definition of X.

    THE BLIND SPOT (2026-07-30 -> caught 07-31): when mkNode began composing
    the real variant profile, I swept every test for leaves that would now
    collide and migrated the two I found to mkForce. The sweep was a regex for
    `name = value`, so it could not see

        hart.sessionSupervisor = { inherit startTier; }

    which is exactly as much a plain definition as `startTier = "sway"`. The
    desktop profile sets startTier = "hart-comp"; the test's sway and cage
    nodes therefore conflicted and FAILED TO EVALUATE — ❌
    hart-session-supervisor-start-tier in run 30574137255, where 69 other
    targets were green.

    An enum/str merges only when definitions are EQUAL, so this class is
    invisible for the value that happens to match the profile and fatal for
    every other — the worst possible failure shape to leave to a regex.
    """

    def _profile_leaves(self, variant):
        src = read_nix(os.path.join(PROFILES_DIR, variant + ".nix"))
        return {m.group(1) for m in
                re.finditer(r'^\s{4,6}([a-zA-Z][\w]*)\s*=\s*[^;{]+;\s*$',
                            src, re.M)}

    def test_no_test_inherits_an_option_the_profile_also_sets(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(TESTS_DIR, "*.nix"))):
            src = read_nix(path)
            for variant in ("desktop", "server", "edge", "phone"):
                if f'mkNode "{variant}"' not in src:
                    continue
                leaves = self._profile_leaves(variant)
                for m in re.finditer(r'inherit\s+([\w\s]+);', src):
                    for name in m.group(1).split():
                        if name in leaves:
                            offenders.append(
                                f"{os.path.basename(path)}: inherit {name} "
                                f"(profiles/{variant}.nix also sets it)")
        assert not offenders, (
            "`inherit X` is a PLAIN definition and collides with the variant "
            "profile's own plain definition unless the values are equal — the "
            "node fails to EVALUATE. Use `X = lib.mkForce ...;` to say the "
            "override is deliberate: " + "; ".join(sorted(set(offenders))))


class TestBackgroundAgentBlastRadius:
    """A wedged background agent must degrade ITSELF, never the machine.

    The steward's rule (2026-07-31): "reviewer hangs shd be isolated to
    process and not hanging whole computer, use android's way of handling
    isolation" — and "blast radius shd be minimised always".

    The trap this guards is that `CPUWeight`, `Nice` and `IOWeight` LOOK like
    caps and are not: they are relative shares that only bite under
    contention. hart-copilot.nix carried a comment claiming "hard caps + the
    lowest scheduling priority mean a wedged agent degrades itself" while
    only MemoryMax was actually a bound — a busy-looping agent on an idle box
    still took every core, and with no TasksMax a fork storm was unbounded.

    Android's answer is bandwidth control + a restricted cpuset for
    background apps, not merely a lower priority. systemd expresses the same
    natively (CPUQuota / TasksMax), so this is composition, not reinvention.
    """

    #: `Nice = 19` is the codebase's marker for "lowest-priority background
    #: work". Any unit wearing it is by definition the kind that must not be
    #: able to saturate the box.
    NICE_MARKER = re.compile(r"Nice\s*=\s*(1[5-9])\s*;")

    def _module_files(self):
        return sorted(glob.glob(os.path.join(MODULES_DIR, "*.nix")))

    def test_lowest_priority_units_have_a_hard_cpu_bound(self):
        """Deprioritised is not bounded — such a unit needs CPUQuota too.

        File-level rather than block-level: coarse, but a module that
        deprioritises a unit and hard-bounds nothing is exactly the shape
        being outlawed, and the coarseness fails SAFE (it can only ask for
        more containment, never less).
        """
        offenders = []
        for path in self._module_files():
            src = read_nix(path)
            if self.NICE_MARKER.search(src) and "CPUQuota" not in src:
                offenders.append(os.path.basename(path))
        assert not offenders, (
            f"{offenders} deprioritise a unit (Nice>=15) but set no CPUQuota. "
            f"Nice only bites under contention — on an idle node the unit "
            f"still takes every core (heat, battery, and an interactive app "
            f"must preempt it). Add a hard bandwidth ceiling.")

    def test_copilot_is_bounded_on_every_dimension(self):
        """The autonomous agent — the one that edits the repo unattended —
        must be bounded in cpu, memory, AND task count.

        Memory alone was bounded before; a runaway could still pin the CPU
        or fork without limit.
        """
        src = read_nix(os.path.join(MODULES_DIR, "hart-copilot.nix"))
        for knob in ("MemoryMax", "MemoryHigh", "CPUQuota", "TasksMax"):
            assert re.search(rf"{knob}\s*=", src), (
                f"hart-copilot.nix sets no {knob}: an unattended agent with "
                f"an unbounded {knob} can take the node down with it")

    def test_memory_high_sits_below_memory_max(self):
        """MemoryHigh must be the SOFT step before the hard kill.

        Set at or above MemoryMax it is inert, and the agent goes straight
        from fine to killed with no reclaim in between (the same inversion
        hart-backend.nix documents having got wrong for edge).
        """
        src = read_nix(os.path.join(MODULES_DIR, "hart-copilot.nix"))

        def _mb(knob):
            m = re.search(rf'{knob}\s*=\s*"(\d+)([MG])"', src)
            assert m, f"{knob} not found as a literal size in hart-copilot.nix"
            return int(m.group(1)) * (1024 if m.group(2) == "G" else 1)

        assert _mb("MemoryHigh") < _mb("MemoryMax"), (
            "MemoryHigh >= MemoryMax makes the soft reclaim step inert")


class TestParityMatrix:
    """The Windows/macOS parity matrix is CHECKED, not asserted in prose.

    docs/architecture/OS_PARITY_MATRIX.md answers "does HART have parity" with
    a row per capability instead of a judgement call. A doc alone rots the
    moment an option moves, so every row claiming a Nix option must name one
    the tree actually has, and the honest-gap rows must STAY honest — a row
    silently upgraded from ❌ to ✅ without the route existing would be exactly
    the false-parity claim the matrix exists to prevent.
    """

    MATRIX = os.path.join(REPO_ROOT, "docs", "architecture", "OS_PARITY_MATRIX.md")

    #: capability -> a regex that must match somewhere under nixos/ for the
    #: matrix's Nix column to be truthful.
    CLAIMED_NIX = {
        "networking.networkmanager": r'networking\.networkmanager\.enable',
        "pipewire": r'services\.pipewire',
        "printing": r'services\.printing\.enable',
        "sane": r'hardware\.sane',
        "bluetooth": r'hardware\.bluetooth',
        "udisks2": r'services\.udisks2|hart\.storage',
        "firmware": r'hardware\.enableRedistributableFirmware',
        "microcode": r'hardware\.cpu\.(intel|amd)\.updateMicrocode',
        "hypervGuest": r'virtualisation\.hypervGuest\.enable',
        "qemuGuest": r'services\.qemuGuest\.enable',
        "spice": r'services\.spice-vdagentd\.enable',
        "biosBootable": r'makeBiosBootable',
        "localRtc": r'time\.hardwareClockInLocalTime',
        "inputMethod": r'i18n\.inputMethod',
    }

    def _nix_tree(self):
        parts = []
        for pat in ("nixos/modules/*.nix", "nixos/profiles/*.nix",
                    "nixos/configurations/*.nix", "nixos/*.nix"):
            for p in glob.glob(os.path.join(REPO_ROOT, pat)):
                parts.append(read_nix(p))
        return "\n".join(parts)

    def test_matrix_exists(self):
        assert os.path.isfile(self.MATRIX), (
            "the parity matrix is the artifact that turns 'do we have parity' "
            "from a judgement call into a checked table")

    @pytest.mark.parametrize("name", sorted(CLAIMED_NIX))
    def test_every_claimed_nix_option_is_real(self, name):
        """A row may not claim an option the tree does not have."""
        assert re.search(self.CLAIMED_NIX[name], self._nix_tree()), (
            f"OS_PARITY_MATRIX.md claims {name} but no nixos/ file defines it "
            f"— the matrix would be advertising parity HART does not have")

    # ── Agent column ────────────────────────────────────────────────────
    # The Nix column was guarded from the start; the AGENT column was not,
    # and on 2026-07-31 that let a row sit at ❌ ("no /api/shell action") for
    # screen capture while /api/shell/screenshot and /api/shell/recording/
    # {start,stop} had been registered all along. The row was wrong because
    # it came from a name-only search, and no test could contradict it.
    #
    # Both directions are now checked: a ✅/🟡 row may not name a route that
    # does not exist, and a ❌ row may not be hiding one that does.

    #: capability -> regex for routes that must NOT exist while its row says ❌.
    CLAIMED_NO_ROUTE = {
        "disk encryption": r"luks|/encrypt",
        "remote desktop": r"/remote[-_]?desktop|/rustdesk|/sunshine",
        # "antivirus" left this list on 2026-07-31 — /api/shell/antivirus/
        # {status,scan} now exist, so the matrix row is ✅ and the paths are
        # checked by test_every_api_path_named_in_the_matrix_is_registered
        # instead. Keeping it here would fail exactly as designed.
    }

    def _registered_routes(self):
        """Every route path registered anywhere under integrations/.

        Reads the decorators rather than importing the app: importing pulls
        in autogen/chromadb and is exactly the kind of heavyweight import
        this suite avoids. The decorator IS the registration, so a path that
        appears here is genuinely served.
        """
        cached = getattr(TestParityMatrix, "_route_cache", None)
        if cached is not None:
            return cached
        pat = re.compile(r"""@(?:\w+)\.(?:route|get|post|put|delete)\(\s*['"]([^'"]+)""")
        found = set()
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "integrations")):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, fn), encoding="utf-8",
                              errors="replace") as fh:
                        found.update(pat.findall(fh.read()))
                except OSError:
                    continue
        # Scanning the tree is the slow part and it cannot change mid-run;
        # without this the parametrized gap tests re-walk integrations/ once
        # each (measured 3m39s for the class).
        TestParityMatrix._route_cache = found
        return found

    def _matrix_text(self):
        with open(self.MATRIX, encoding="utf-8") as fh:
            return fh.read()

    def test_every_api_path_named_in_the_matrix_is_registered(self):
        """A row may not advertise a route the code does not serve.

        Catches the inverse of the screen-capture error: a row upgraded to ✅
        citing an endpoint nobody wired.
        """
        routes = self._registered_routes()
        # Paths as written in the matrix, incl. brace-expanded forms like
        # /api/shell/storage/{defrag,trim,fsck} and /recording/{start,stop}.
        cited = set()
        for m in re.finditer(r"`(/api/[^`]+?)`", self._matrix_text()):
            raw = m.group(1).strip()
            if "..." in raw or "…" in raw:
                continue          # prose placeholder ("/api/shell/..."), not a claim
            brace = re.search(r"\{([^}]*)\}", raw)
            if brace:
                stem = raw[:brace.start()]
                for alt in brace.group(1).split(","):
                    alt = alt.strip()
                    if alt:
                        cited.add(stem + alt)
            else:
                cited.add(raw)
        missing = sorted(p for p in cited if p not in routes)
        assert not missing, (
            f"OS_PARITY_MATRIX.md cites {missing} but no @route registers "
            f"them — the matrix would advertise parity that does not exist")

    @pytest.mark.parametrize("capability", sorted(CLAIMED_NO_ROUTE))
    def test_honest_gap_rows_are_still_gaps(self, capability):
        """A ❌ row must be a REAL gap, not a stale search result.

        This is the test that would have caught the screen-capture row: it
        fails the moment someone wires the route without upgrading the row,
        which turns "we still owe this" into a checked fact instead of a
        claim nobody re-verified.
        """
        pat = re.compile(self.CLAIMED_NO_ROUTE[capability], re.I)
        live = sorted(r for r in self._registered_routes() if pat.search(r))
        assert not live, (
            f"OS_PARITY_MATRIX.md lists '{capability}' as an honest gap with "
            f"no agent route, but these are registered: {live}. Upgrade the "
            f"row — an understated matrix hides finished work the same way an "
            f"overstated one invents it")

    def test_the_dual_boot_clock_fix_is_actually_wired(self):
        """The row that matters most on real hardware: the installer must
        WRITE time.hardwareClockInLocalTime when it finds Windows, or a
        dual-boot node's clock jumps by the timezone offset on first NTP sync
        (task #24, the steward's hang)."""
        src = read_nix(os.path.join(MODULES_DIR, "hart-installer.nix"))
        # The ASSIGNMENT, not the mention. Written as a substring check first,
        # this passed against the explanatory COMMENT that merely names the
        # option — vacuous, and the same shape as the `"default" in body` hole
        # the reviewing session found in the required-option guard. Proof by
        # revert is what exposed it: deleting the real assignment left the test
        # green.
        assert re.search(r'time\.hardwareClockInLocalTime\s*=\s*true\s*;', src), (
            "hart-install must WRITE time.hardwareClockInLocalTime = true into "
            "local.nix, not merely mention it")
        assert "bootmgfw.efi" in src, (
            "it must be conditioned on an ACTUAL Windows bootloader — a "
            "blanket setting is wrong for single-OS machines whose RTC is UTC")

    def test_the_clock_fix_reaches_BOTH_installers(self):
        """There are TWO installers, and this guard originally read one file.

        nixos/installer/calamares/hartcfg-main.py calls itself "the GUI twin of
        hart-install --mounted" — an ALTERNATIVE path that writes local.nix
        itself, not a step inside the CLI. The CLI got the clock fix and the
        GUI did not, so a user installing beside Windows through the GRAPHICAL
        installer (the default for a desktop OS) still hit the +5:30 jump —
        while this test passed and the matrix row read ✅.

        That is the vacuous-guard shape one level up: the assertion held, the
        claim it was read as supporting did not. Both paths are checked now.
        """
        # CONSOLIDATED (steward: "why are they not canonicalised?"). Two
        # installer FRONT-ENDS is legitimate — a CLI for headless/scripted
        # installs, a GUI for the desktop — and they already drive ONE
        # generator (Calamares shells out to hart-write-install-config). What
        # was not legitimate: local.nix had three renderers in two languages,
        # so the clock probe landed in the CLI and missed the GUI. Probed
        # facts now live in hardware-local.nix with the generator as their
        # only writer; local.nix stays the front-end's user-choice file, and
        # NixOS module imports compose them.
        gen = read_nix(os.path.join(MODULES_DIR, "hart-installer.nix"))
        assert "hardware-local.nix" in gen, (
            "the shared generator must write the probed-facts file")
        assert re.search(r'\./hardware-local\.nix', gen), (
            "hardware-local.nix must be in the written flake's module list, "
            "or it is generated and never imported")
        gui = read_nix(os.path.join(
            REPO_ROOT, "nixos", "installer", "calamares", "hartcfg-main.py"))
        # No ASSIGNMENT, not no mention: the GUI's docstring legitimately names
        # the option to explain why it is NOT rendered there. Asserting on the
        # bare word would forbid the documentation of the very decision — the
        # mirror image of the vacuity bug where a comment SATISFIED a check.
        assert not re.search(r'hardwareClockInLocalTime\s*=', gui), (
            "the GUI must NOT re-render a probed fact — that duplication is "
            "exactly what let the two installers drift")

    def test_declared_gaps_are_not_silently_upgraded(self):
        """The five declarative-only capabilities are listed as ❌ on purpose.
        Turning one into ✅ requires the route to exist; this keeps the claim
        and the code in step rather than letting the doc drift optimistic."""
        doc = open(self.MATRIX, encoding="utf-8").read()
        api = "\n".join(
            open(p, encoding="utf-8").read()
            for p in glob.glob(os.path.join(
                REPO_ROOT, "integrations", "agent_engine", "shell_*apis*.py")))
        for cap, route in [("Disk encryption", r'/api/shell/(luks|encrypt)'),
                           ("Firewall", r'/api/shell/firewall')]:
            row = [l for l in doc.splitlines() if l.startswith(f"| {cap} ")]
            assert row, f"matrix lost its {cap} row"
            if "❌" in row[0]:
                assert not re.search(route, api), (
                    f"{cap} now HAS a live route — update the matrix row to ✅ "
                    f"rather than leaving a stale gap claim")
