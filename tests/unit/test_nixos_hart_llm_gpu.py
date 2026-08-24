"""Config-shape guard for hart-llm.nix's llama.cpp build.

History (two real-HW boots on the same Lenovo/Ivy Bridge box):
  * 2026-06-24: the assistant ran 100% on the CPU and slowly, because the module
    used the CPU-only llama.cpp `.default` and passed NO --n-gpu-layers. The
    "fix" then was a Vulkan GPU build + a launcher that gated -ngl on a present
    render node.
  * 2026-08-24: that Vulkan build NEVER actually ran on this hardware. The CPU is
    an i7-3630QM (Ivy Bridge): avx + f16c + sse4.2 but NO avx2/fma/bmi2. Stock
    nixpkgs llama-cpp compiles libggml-cpu.so WITH avx2/fma and no runtime
    fallback, so llama-server hit an illegal instruction (SIGILL) and core-dumped
    at startup -- a 6760+ restart crash loop over ~19h, and the local LLM was
    never reachable, so every goal agent stalled. Only libggml-cpu.so carried the
    bad insns (1028); Vulkan was a red herring, and the Intel HD 4000's Mesa ANV
    Vulkan is "incomplete" on Ivy Bridge anyway (a second crash vector).

This locks in the real fix, proven on the box (objdump: 0 avx2/fma insns in the
new libggml-cpu.so; a live /v1/chat/completions round trip on :808): build
llama.cpp for THIS ISA (avx2/fma/bmi2/avx512 OFF, avx/f16c ON, native OFF) and
drop Vulkan (vulkanSupport = false) so there is no GPU-offload crash path. The
behavioural proof is the on-box round trip; this guard catches a regression that
silently re-enables avx2/fma (which would SIGILL the Ivy Bridge floor again) or
reintroduces the Vulkan offload path.
"""
import pathlib

_LLM = (
    pathlib.Path(__file__).resolve().parents[2] / "nixos" / "modules" / "hart-llm.nix"
).read_text(encoding="utf-8")


def test_llama_cpu_backend_avoids_avx2_fma():
    # THE regression guard. Ivy Bridge has no avx2/fma; a libggml-cpu.so built with
    # them SIGILLs at startup and crash-loops hart-llm. These flags must stay OFF.
    assert "-DGGML_AVX2=OFF" in _LLM and "-DGGML_FMA=OFF" in _LLM, (
        "hart-llm must build llama.cpp with GGML_AVX2=OFF and GGML_FMA=OFF -- the box "
        "CPU (i7-3630QM, Ivy Bridge) lacks them and an avx2/fma libggml-cpu.so "
        "core-dumps on SIGILL at startup (the 6760-restart crash loop, 2026-08-24).")
    assert "-DGGML_NATIVE=OFF" in _LLM, (
        "GGML_NATIVE must be OFF -- a -march=native build bakes in the builder's ISA "
        "(avx2/fma on CI), which SIGILLs on the older fleet CPU.")


def test_vulkan_offload_is_disabled():
    # The only real-fleet GPU is the Intel HD 4000, whose Mesa ANV Vulkan is
    # incomplete on Ivy Bridge and aborts ggml's Vulkan init -- a crash vector, not
    # a speed-up. A CPU-only binary cannot take that path.
    assert "vulkanSupport = false" in _LLM, (
        "hart-llm must build llama.cpp with vulkanSupport = false -- the fleet's Intel "
        "HD 4000 Vulkan is incomplete and crashes ggml at device init.")


def test_launcher_is_cpu_only():
    # No GPU backend in the binary => no GPU gate and no --n-gpu-layers in the
    # launcher. renderD128/ICD probing is gone; the exec is pure CPU.
    assert "renderD128" not in _LLM and "icd.d" not in _LLM, (
        "the hart-llm launcher must NOT probe for a Vulkan render node -- the binary is "
        "CPU-only, so the old renderD128/icd.d GPU gate must be removed.")


def test_llama_binary_is_published_on_path_for_hartos_finders():
    # HARTOS's own ModelLifecycleManager._find_llama_server_binary() falls back to
    # shutil.which('llama-server'). If this module keeps the binary to itself (only
    # in hart-llm.service's ExecStart), that finder returns None on HART OS and its
    # consumers degrade silently: lightweight_backend disables captioning, and
    # model_onboarding takes its "binary not found, downloading..." branch, which
    # pulls a GENERIC avx2 build and SIGILLs on the fleet CPU. Publishing the same
    # ISA-correct derivation on PATH is what makes the OS the supplier.
    assert "environment.systemPackages" in _LLM and "llama-server" in _LLM, (
        "hart-llm must put its llama.cpp build on PATH (environment.systemPackages) "
        "so HARTOS's _find_llama_server_binary() finds the ISA-correct binary "
        "instead of nothing (caption disabled) or a downloaded avx2 one (SIGILL).")


def test_cpu_inference_does_not_starve_the_os():
    # CPU inference must NOT starve the interactive desktop/shell: the LLM is a
    # BACKGROUND citizen (CPUWeight below the UI's default 100) and the launcher
    # leaves one core for the OS (nproc - 1).
    assert "CPUWeight = 50" in _LLM, (
        "hart-llm CPUWeight must be BELOW the UI services' default 100 (was 150 = above "
        "it, so CPU inference out-prioritised and stalled the desktop).")
    assert "nproc) - 1" in _LLM, (
        "the launcher must size CPU threads to nproc - 1 (leave one core for the OS) so "
        "CPU inference never saturates every core.")
