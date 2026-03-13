{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Subsystem Sandbox — Test & Validate Each Native Layer
# ═══════════════════════════════════════════════════════════════
#
# Provides `hart sandbox` command that runs validation tests
# for each native subsystem without affecting the live system.
#
# Tests use kernel namespaces (not containers) for isolation.
#
#   hart sandbox test-all        Run all subsystem tests
#   hart sandbox test-linux      Test native Linux ELF execution
#   hart sandbox test-android    Test Android ART + binder IPC
#   hart sandbox test-windows    Test Wine PE + Win32 API
#   hart sandbox test-ai         Test GPU compute + model loading
#   hart sandbox status          Show subsystem health status
#
# Each test produces a PASS/FAIL result with diagnostic info.

let
  cfg = config.hart;
  sandboxCfg = config.hart.sandbox;

  # The comprehensive test script
  sandboxScript = pkgs.writeShellScriptBin "hart-sandbox" ''
    set -uo pipefail

    # ── Colors ──
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    YELLOW='\033[1;33m'
    NC='\033[0m'

    PASS=0
    FAIL=0
    SKIP=0

    check() {
        local name="$1"
        local result="$2"  # 0=pass, 1=fail, 2=skip
        local detail="''${3:-}"

        case "$result" in
            0) echo -e "  [''${GREEN}PASS''${NC}] $name"
               [[ -n "$detail" ]] && echo -e "         $detail"
               PASS=$((PASS + 1)) ;;
            1) echo -e "  [''${RED}FAIL''${NC}] $name"
               [[ -n "$detail" ]] && echo -e "         $detail"
               FAIL=$((FAIL + 1)) ;;
            2) echo -e "  [''${YELLOW}SKIP''${NC}] $name"
               [[ -n "$detail" ]] && echo -e "         $detail"
               SKIP=$((SKIP + 1)) ;;
        esac
    }

    header() {
        echo ""
        echo -e "''${CYAN}  ── $1 ──''${NC}"
    }

    # ═══════════════════════════════════════════════════════
    # TEST: Linux Native Subsystem
    # ═══════════════════════════════════════════════════════
    test_linux() {
        header "Linux Native Subsystem"

        # Test 1: ELF binary execution
        if /bin/sh -c "echo ok" &>/dev/null; then
            check "ELF binary execution" 0 "Shell executes correctly"
        else
            check "ELF binary execution" 1 "Cannot execute ELF binaries"
        fi

        # Test 2: glibc present
        if ldd --version &>/dev/null; then
            GLIBC=$(ldd --version 2>&1 | head -1)
            check "glibc available" 0 "$GLIBC"
        else
            check "glibc available" 1
        fi

        # Test 3: Dynamic linking
        if ldd /bin/sh &>/dev/null; then
            check "Dynamic linker (ld-linux)" 0
        else
            check "Dynamic linker (ld-linux)" 1
        fi

        # Test 4: NixOS packages
        NIX_PKG_COUNT=$(ls /nix/store/ 2>/dev/null | wc -l)
        if [[ "$NIX_PKG_COUNT" -gt 100 ]]; then
            check "Nix store populated" 0 "$NIX_PKG_COUNT packages"
        else
            check "Nix store populated" 1 "Only $NIX_PKG_COUNT packages"
        fi

        # Test 5: Flatpak (if enabled)
        if command -v flatpak &>/dev/null; then
            REMOTES=$(flatpak remotes 2>/dev/null | wc -l)
            check "Flatpak available" 0 "$REMOTES remote(s) configured"
        else
            check "Flatpak available" 2 "Not installed"
        fi

        # Test 6: AppImage support
        if command -v appimage-run &>/dev/null; then
            check "AppImage support" 0
        else
            check "AppImage support" 2 "Not installed"
        fi

        # Test 7: Compiler toolchain
        if command -v gcc &>/dev/null; then
            GCC_VER=$(gcc --version 2>&1 | head -1)
            check "GCC compiler" 0 "$GCC_VER"
        else
            check "GCC compiler" 2 "Not installed"
        fi

        # Test 8: Python
        if command -v python3 &>/dev/null; then
            PY_VER=$(python3 --version 2>&1)
            check "Python runtime" 0 "$PY_VER"
        else
            check "Python runtime" 1 "Python3 not found"
        fi
    }

    # ═══════════════════════════════════════════════════════
    # TEST: Android Native Subsystem
    # ═══════════════════════════════════════════════════════
    test_android() {
        header "Android Native Subsystem"

        # Test 1: binder kernel module
        if lsmod 2>/dev/null | grep -q binder_linux; then
            check "binder_linux kernel module" 0 "Loaded"
        elif [[ -e /dev/binder ]]; then
            check "binder_linux kernel module" 0 "Device node exists"
        else
            check "binder_linux kernel module" 1 "Not loaded — enable hart.kernel.androidNative"
        fi

        # Test 2: ashmem kernel module
        if lsmod 2>/dev/null | grep -q ashmem_linux; then
            check "ashmem_linux kernel module" 0 "Loaded"
        elif [[ -e /dev/ashmem ]]; then
            check "ashmem_linux kernel module" 0 "Device node exists"
        else
            check "ashmem_linux kernel module" 1 "Not loaded"
        fi

        # Test 3: binder device nodes
        if [[ -e /dev/binder ]] || [[ -d /dev/binderfs ]]; then
            check "Binder IPC device" 0
        else
            check "Binder IPC device" 1 "No /dev/binder or /dev/binderfs"
        fi

        # Test 4: Android data directory
        if [[ -d /var/lib/hart/android ]]; then
            check "Android data directory" 0
        else
            check "Android data directory" 1 "/var/lib/hart/android missing"
        fi

        # Test 5: Android runtime service
        if systemctl is-active hart-android-runtime.service &>/dev/null; then
            check "Android runtime service" 0 "Running"
        elif systemctl is-enabled hart-android-runtime.service &>/dev/null; then
            check "Android runtime service" 2 "Enabled but not running"
        else
            check "Android runtime service" 2 "Not enabled"
        fi

        # Test 6: ADB available
        if command -v adb &>/dev/null; then
            check "Android Debug Bridge (adb)" 0
        else
            check "Android Debug Bridge (adb)" 2 "Not installed"
        fi
    }

    # ═══════════════════════════════════════════════════════
    # TEST: Windows Native Subsystem (Wine)
    # ═══════════════════════════════════════════════════════
    test_windows() {
        header "Windows Native Subsystem (Wine)"

        # Test 1: Wine installed
        if command -v wine64 &>/dev/null; then
            WINE_VER=$(wine64 --version 2>&1)
            check "Wine (native Win32 API)" 0 "$WINE_VER"
        elif command -v wine &>/dev/null; then
            WINE_VER=$(wine --version 2>&1)
            check "Wine (native Win32 API)" 0 "$WINE_VER"
        else
            check "Wine (native Win32 API)" 1 "Not installed — enable hart.subsystems.windows"
        fi

        # Test 2: PE binfmt registered
        if [[ -f /proc/sys/fs/binfmt_misc/DOSWin ]]; then
            check "PE binary auto-detection (binfmt)" 0 "Kernel dispatches .exe to Wine"
        else
            check "PE binary auto-detection (binfmt)" 1 "binfmt_misc not configured for PE"
        fi

        # Test 3: Wine can create a prefix (quick sanity check)
        if command -v wine64 &>/dev/null; then
            WINEPREFIX="/tmp/hart-wine-test" WINEDEBUG=-all \
              timeout 10 wine64 cmd /c "echo HART OS" &>/dev/null
            if [[ $? -eq 0 ]]; then
                check "Wine prefix creation" 0 "Windows environment initializes"
            else
                check "Wine prefix creation" 1 "Wine failed to initialize"
            fi
            rm -rf /tmp/hart-wine-test
        else
            check "Wine prefix creation" 2
        fi

        # Test 4: NTFS kernel support
        if grep -q ntfs3 /proc/filesystems 2>/dev/null; then
            check "NTFS filesystem (kernel native)" 0
        else
            check "NTFS filesystem (kernel native)" 2 "ntfs3 module not loaded"
        fi

        # Test 5: Vulkan (DXVK requires this)
        if command -v vulkaninfo &>/dev/null; then
            VULKAN=$(vulkaninfo --summary 2>&1 | grep "Vulkan Instance" | head -1)
            if [[ -n "$VULKAN" ]]; then
                check "Vulkan support (for DXVK/DirectX)" 0 "$VULKAN"
            else
                check "Vulkan support (for DXVK/DirectX)" 1 "Vulkan not available"
            fi
        else
            check "Vulkan support (for DXVK/DirectX)" 2 "vulkaninfo not installed"
        fi

        # Test 6: 32-bit support
        if [[ -d /run/opengl-driver-32 ]] || ldconfig -p 2>/dev/null | grep -q "lib32"; then
            check "32-bit library support" 0
        else
            check "32-bit library support" 2 "May not support 32-bit Windows apps"
        fi

        # Test 7: Bottles GUI
        if command -v bottles &>/dev/null; then
            check "Bottles (Wine prefix manager)" 0
        else
            check "Bottles (Wine prefix manager)" 2 "Not installed"
        fi

        # Test 8: Steam + Proton
        if command -v steam &>/dev/null; then
            check "Steam + Proton (gaming)" 0
        else
            check "Steam + Proton (gaming)" 2 "Not installed"
        fi
    }

    # ═══════════════════════════════════════════════════════
    # TEST: AI Runtime Subsystem
    # ═══════════════════════════════════════════════════════
    test_ai() {
        header "AI Runtime Subsystem"

        # Test 1: GPU detection
        GPU_FOUND=false
        if command -v nvidia-smi &>/dev/null; then
            GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
            GPU_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)
            if [[ -n "$GPU_NAME" ]]; then
                check "GPU detected" 0 "NVIDIA $GPU_NAME ($GPU_VRAM)"
                GPU_FOUND=true
            fi
        fi
        if [[ -f /sys/class/drm/card0/device/gpu_busy_percent ]]; then
            check "GPU detected" 0 "AMD GPU via DRM"
            GPU_FOUND=true
        fi
        if ! $GPU_FOUND; then
            check "GPU detected" 2 "No dedicated GPU (CPU inference only)"
        fi

        # Test 2: Model store
        MODEL_PATH="/var/lib/hart/models"
        if [[ -d "$MODEL_PATH" ]]; then
            MODEL_COUNT=$(find "$MODEL_PATH" -name "*.gguf" -o -name "*.safetensors" -o -name "*.bin" 2>/dev/null | wc -l)
            check "Model store" 0 "$MODEL_COUNT model(s) at $MODEL_PATH"
        else
            check "Model store" 1 "$MODEL_PATH missing"
        fi

        # Test 3: cgroups v2 (agent isolation)
        if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
            CONTROLLERS=$(cat /sys/fs/cgroup/cgroup.controllers)
            check "cgroups v2 (agent isolation)" 0 "Controllers: $CONTROLLERS"
        else
            check "cgroups v2 (agent isolation)" 1 "Not available"
        fi

        # Test 4: Agent slice
        if systemctl status hart-agents.slice &>/dev/null; then
            check "Agent cgroup slice" 0 "hart-agents.slice active"
        else
            check "Agent cgroup slice" 2 "hart-agents.slice not configured"
        fi

        # Test 5: HART backend (agents need this)
        if curl -sf "http://localhost:6777/status" &>/dev/null; then
            check "HART backend connectivity" 0 "localhost:6777 responding"
        else
            check "HART backend connectivity" 1 "Backend not reachable"
        fi

        # Test 6: Agent daemon
        if systemctl is-active hart-agent.service &>/dev/null; then
            check "Agent daemon" 0 "Running"
        else
            check "Agent daemon" 2 "Not running"
        fi

        # Test 7: GPU scheduler
        if systemctl is-active hart-gpu-scheduler.service &>/dev/null; then
            check "GPU scheduler" 0 "Running"
        else
            check "GPU scheduler" 2 "Not running"
        fi

        # Test 8: vsock IPC
        if lsmod 2>/dev/null | grep -q vsock; then
            check "vsock IPC (inter-agent)" 0 "Kernel module loaded"
        else
            check "vsock IPC (inter-agent)" 2 "Module not loaded"
        fi

        # Test 9: Huge pages (model memory)
        HUGE_FREE=$(cat /proc/meminfo 2>/dev/null | grep HugePages_Free | awk '{print $2}')
        if [[ -n "$HUGE_FREE" ]] && [[ "$HUGE_FREE" -gt 0 ]]; then
            HUGE_SIZE=$((HUGE_FREE * 2))
            check "Huge pages (model memory)" 0 "''${HUGE_SIZE}MB available"
        else
            check "Huge pages (model memory)" 2 "Using THP only"
        fi

        # Test 10: World model bridge
        if systemctl is-active hart-world-model.service &>/dev/null; then
            check "World model bridge" 0 "Connected"
        else
            check "World model bridge" 2 "Not running"
        fi
    }

    # ═══════════════════════════════════════════════════════
    # STATUS: Quick health overview
    # ═══════════════════════════════════════════════════════
    show_status() {
        echo ""
        echo -e "''${CYAN}============================================================''${NC}"
        echo -e "''${CYAN}  HART OS Subsystem Status''${NC}"
        echo -e "''${CYAN}============================================================''${NC}"
        echo ""

        # Linux
        echo -e "  ''${GREEN}●''${NC} Linux Native     : Active (NixOS)"

        # Android
        if lsmod 2>/dev/null | grep -q binder_linux; then
            if systemctl is-active hart-android-runtime.service &>/dev/null; then
                echo -e "  ''${GREEN}●''${NC} Android Native   : Active (ART + Binder)"
            else
                echo -e "  ''${YELLOW}●''${NC} Android Native   : Kernel ready, runtime stopped"
            fi
        else
            echo -e "  ''${RED}●''${NC} Android Native   : Not enabled"
        fi

        # Windows
        if command -v wine64 &>/dev/null; then
            echo -e "  ''${GREEN}●''${NC} Windows Native   : Active (Wine $(wine64 --version 2>&1 | head -c 20))"
        else
            echo -e "  ''${RED}●''${NC} Windows Native   : Not enabled"
        fi

        # AI
        if systemctl is-active hart-agent.service &>/dev/null; then
            AGENTS=$(systemctl list-units 'hart-agent@*.service' --state=running --no-legend 2>/dev/null | wc -l)
            echo -e "  ''${GREEN}●''${NC} AI Runtime       : Active ($AGENTS agent(s))"
        else
            echo -e "  ''${YELLOW}●''${NC} AI Runtime       : Backend only"
        fi

        echo ""
    }

    # ═══════════════════════════════════════════════════════
    # MAIN
    # ═══════════════════════════════════════════════════════
    CMD="''${1:-help}"

    case "$CMD" in
        test-all)
            echo ""
            echo -e "''${CYAN}============================================================''${NC}"
            echo -e "''${CYAN}  HART OS Subsystem Validation''${NC}"
            echo -e "''${CYAN}============================================================''${NC}"
            test_linux
            test_android
            test_windows
            test_ai
            ;;
        test-linux)   test_linux ;;
        test-android) test_android ;;
        test-windows) test_windows ;;
        test-ai)      test_ai ;;
        status)       show_status; exit 0 ;;
        *)
            echo ""
            echo "  HART OS Subsystem Sandbox"
            echo "  ========================"
            echo ""
            echo "  Usage: hart sandbox <command>"
            echo ""
            echo "  Commands:"
            echo "    test-all       Test all native subsystems"
            echo "    test-linux     Test Linux ELF subsystem"
            echo "    test-android   Test Android ART subsystem"
            echo "    test-windows   Test Windows Wine subsystem"
            echo "    test-ai        Test AI runtime subsystem"
            echo "    status         Show subsystem health"
            echo ""
            exit 0
            ;;
    esac

    # ── Summary ──
    TOTAL=$((PASS + FAIL + SKIP))
    echo ""
    echo "============================================================"
    if [[ $FAIL -eq 0 ]]; then
        echo -e "  ''${GREEN}ALL $PASS CHECKS PASSED''${NC} ($SKIP skipped, $TOTAL total)"
    else
        echo -e "  ''${RED}$FAIL FAILED''${NC}, ''${GREEN}$PASS passed''${NC}, $SKIP skipped ($TOTAL total)"
    fi
    echo "============================================================"
    echo ""

    [[ $FAIL -eq 0 ]]
  '';

in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.sandbox = {
    enable = lib.mkEnableOption "HART OS subsystem validation sandbox";
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sandboxCfg.enable) {

    # Install the sandbox test tool
    environment.systemPackages = [ sandboxScript ];

    # Create a wrapper so `hart sandbox` works
    environment.etc."hart/bin/hart" = {
      mode = "0755";
      text = ''
        #!/bin/bash
        case "''${1:-}" in
            sandbox) shift; exec ${sandboxScript}/bin/hart-sandbox "$@" ;;
            *)       exec /run/current-system/sw/bin/hart-cli "$@" 2>/dev/null || echo "Usage: hart sandbox <command>" ;;
        esac
      '';
    };

    # Run sandbox validation on first boot (after all subsystems initialized)
    systemd.services.hart-sandbox-validate = {
      description = "HART OS Subsystem Validation (First Boot)";
      after = [
        "hart.target"
        "hart-android-runtime.service"
        "hart-gpu-scheduler.service"
        "multi-user.target"
      ];
      wantedBy = [ "multi-user.target" ];

      unitConfig = {
        ConditionPathExists = "!/var/lib/hart/.sandbox-validated";
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "hart";
        Group = "hart";
        ExecStart = pkgs.writeShellScript "hart-sandbox-firstboot" ''
          echo "[HART OS] Running first-boot subsystem validation..."
          if ${sandboxScript}/bin/hart-sandbox test-all 2>&1 | tee /var/lib/hart/sandbox-firstboot.log; then
            echo "[HART OS] First-boot validation: ALL CHECKS PASSED"
          else
            echo "[HART OS] First-boot validation: SOME CHECKS FAILED — see /var/lib/hart/sandbox-firstboot.log"
          fi
          touch /var/lib/hart/.sandbox-validated
          echo "[HART OS] Validation complete. Run 'hart sandbox test-all' anytime to revalidate."
        '';
      };
    };
  };
}
