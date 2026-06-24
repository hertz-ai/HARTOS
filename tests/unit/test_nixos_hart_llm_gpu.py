"""Config-shape guard for the real-HW 2026-06-24 boot: the local assistant ran 100%
on the CPU (the "assistant keeps thinking" slowness) because hart-llm.nix built the
CPU-only llama.cpp (the flake `.default`) and the service passed NO --n-gpu-layers,
with GPU detection NVIDIA-only (/dev/nvidia0) so a Lenovo Intel iGPU got nothing.

This locks in the fix: a Vulkan GPU build (universal across Intel/AMD/NVIDIA, CPU
fallback) + a launcher that passes --n-gpu-layers ONLY when a Vulkan device is actually
present (a Vulkan build with -ngl>0 and no device errors at load and would crash-loop
under Restart=on-failure). The behavioural proof is the CI nixosTest + the real-HW
boot; this guard only catches a regression that silently drops back to CPU-only.
"""
import pathlib

_LLM = (
    pathlib.Path(__file__).resolve().parents[2] / "nixos" / "modules" / "hart-llm.nix"
).read_text(encoding="utf-8")


def test_llama_built_with_vulkan_gpu():
    # Vulkan is the UNIVERSAL GPU backend (Intel mesa ANV / AMD RADV / NVIDIA ICD); the
    # old CPU-only .default build left every layer on the CPU. The Vulkan runtime is
    # already present via desktop.nix hardware.graphics.enable (mesa ICDs).
    assert "vulkanSupport = true" in _LLM, (
        "hart-llm must build llama.cpp with vulkanSupport (the universal GPU backend) — "
        "the CPU-only .default build is the real-HW 'assistant keeps thinking' slowness.")


def test_ngl_is_gated_on_a_real_vulkan_device():
    # A Vulkan build with --n-gpu-layers>0 and NO Vulkan device ERRORS at load and would
    # crash-loop under Restart=on-failure — so -ngl must be gated on a present GPU.
    assert "--n-gpu-layers" in _LLM, (
        "the hart-llm launcher must pass --n-gpu-layers when a GPU is present (it never "
        "did before — every layer ran on the CPU).")
    assert "renderD128" in _LLM and "icd.d" in _LLM, (
        "--n-gpu-layers must be gated on a real render node (/dev/dri/renderD128) AND a "
        "Vulkan ICD (/run/opengl-driver/.../icd.d) being present — never -ngl with no GPU.")


def test_cpu_fallback_does_not_starve_the_os():
    # On CPU fallback the LLM must NOT starve the interactive desktop/shell: it runs as a
    # BACKGROUND citizen (CPUWeight below the UI's default 100) and the launcher leaves one
    # core for the OS (nproc - 1). Was CPUWeight 150 (ABOVE the UI) + a fixed 4 threads,
    # which would stall the whole desktop on CPU-only inference (the steward's catch).
    assert "CPUWeight = 50" in _LLM, (
        "hart-llm CPUWeight must be BELOW the UI services' default 100 (was 150 = above it, "
        "so CPU-fallback inference out-prioritised and stalled the desktop).")
    assert "nproc) - 1" in _LLM, (
        "the launcher must size CPU threads to nproc - 1 (leave one core for the OS) so "
        "CPU-fallback inference never saturates every core.")
