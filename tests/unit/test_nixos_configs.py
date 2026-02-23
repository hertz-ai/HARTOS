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

import os
import re
import pytest

# ─── Paths ────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
CONFIGS_DIR = os.path.join(NIXOS_DIR, "configurations")
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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "server.nix"))

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "desktop.nix"))

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

    def test_nunba_enabled(self):
        assert "nunba.enable = true" in self.config

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "edge.nix"))

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "phone.nix"))

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
        content = read_nix(os.path.join(CONFIGS_DIR, config))
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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "server.nix"))

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "desktop.nix"))

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "edge.nix"))

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
        self.config = read_nix(os.path.join(CONFIGS_DIR, "phone.nix"))

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
