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
    bad insns (1028). Rebuilding the Vulkan variant WITH the ISA fix separates
    two independent faults: `llama-server --version` stops core-dumping (so the
    STARTUP crash was the CPU backend, not Vulkan), yet it still cannot serve on
    this box, failing model load with "vkDestroyFence: Invalid device" because
    the HD 4000's Mesa ANV driver is incomplete on Ivy Bridge.

The fix must not trade one regression for a bigger one. nixpkgs' avx2 baseline is
DELIBERATE (-DGGML_NATIVE:BOOL=FALSE sets INS_ENB=ON, enabling GGML_AVX2/FMA), so
turning it off fleet-wide would cost every Haswell-or-newer node its fast kernels,
and dropping vulkanSupport would cost every GPU node its offload. b4154 has no
runtime CPU dispatch (GGML_CPU_ALL_VARIANTS / GGML_BACKEND_DL absent), so instead
there are TWO builds and the launcher picks by /proc/cpuinfo: the stock build for
capable nodes, an avx-only floor for pre-Haswell ones. Proven on the box (objdump:
0 avx2/fma insns; a live /v1/chat/completions round trip on :808).
"""
import pathlib

_LLM = (
    pathlib.Path(__file__).resolve().parents[2] / "nixos" / "modules" / "hart-llm.nix"
).read_text(encoding="utf-8")

# Assertions run against CODE, not prose. This file documents its own history at
# length (why the presence-only GPU gate was wrong, why avx2 is fatal here), so a
# plain substring search over the whole text would match the explanation of a
# thing and claim the thing is present. Both `nix` and the embedded shell use
# `#` for comments, so dropping from the first `#` on each line is enough.
_CODE = "\n".join(line.split("#")[0] for line in _LLM.splitlines())


def test_portable_variant_avoids_avx2_fma():
    # The floor. Ivy Bridge has no avx2/fma; a libggml-cpu.so built with them
    # SIGILLs at startup and crash-loops hart-llm (6762 restarts, 2026-08-24).
    assert "-DGGML_AVX2=OFF" in _CODE and "-DGGML_FMA=OFF" in _CODE, (
        "hart-llm must build a portable llama.cpp variant with GGML_AVX2=OFF and "
        "GGML_FMA=OFF -- pre-Haswell fleet CPUs lack them and an avx2/fma "
        "libggml-cpu.so core-dumps on SIGILL at startup.")
    assert "-DGGML_NATIVE=OFF" in _CODE, (
        "GGML_NATIVE must be OFF on the portable variant -- a -march=native build "
        "bakes in the builder's ISA, which SIGILLs on older fleet CPUs.")


def test_capable_nodes_keep_the_stock_avx2_and_vulkan_baseline():
    # THE anti-regression guard, and the reason there are two variants at all.
    # nixpkgs' llama-cpp passes -DGGML_NATIVE:BOOL=FALSE, which sets INS_ENB=ON and
    # therefore GGML_AVX2/GGML_FMA ON: a deliberate Haswell-2013+ baseline. Fixing
    # one 2012 laptop by turning that off fleet-wide (and dropping vulkanSupport)
    # would cost every capable node its fast kernels AND its GPU offload -- a far
    # bigger regression than the bug being fixed. So an UNMODIFIED stock build must
    # remain, and Vulkan must still be enabled for it.
    assert "llamaFast" in _CODE and "vulkanSupport = true" in _CODE, (
        "hart-llm must keep an unmodified stock llama-cpp build (vulkanSupport = "
        "true, nixpkgs' avx2/fma baseline) for capable nodes -- the portable "
        "avx-only build is a FLOOR for old CPUs, not a fleet-wide downgrade.")
    # The portable build must not be the only one, i.e. the ISA flags must not be
    # applied to the package the capable nodes get.
    assert "llamaPortable" in _CODE, (
        "the avx2/fma-disabled build must be a SEPARATE variant (llamaPortable), "
        "never an override applied to the one build everyone uses.")


def test_binary_is_chosen_by_the_cpus_real_flags():
    # b4154 has no runtime CPU dispatch (GGML_CPU_ALL_VARIANTS / GGML_BACKEND_DL do
    # not exist in that source tree), so the choice must happen at launch, keyed on
    # what the CPU actually advertises -- not a build-time guess, so one fleet image
    # boots correctly on untested hardware.
    assert "/proc/cpuinfo" in _CODE, (
        "the launcher must select the llama.cpp variant from /proc/cpuinfo's real "
        "CPU flags, so one image runs on both avx2 and pre-avx2 nodes.")
    assert "avx2" in _CODE and "fma" in _CODE, (
        "variant selection must test for BOTH avx2 and fma -- the portable build "
        "exists precisely because a CPU can lack them.")


def test_gpu_offload_gate_is_not_presence_only():
    # The old gate passed -ngl 999 whenever /dev/dri/renderD128 and an ICD json
    # existed. On the box both existed while the driver was incomplete. Presence is
    # not health, so that gate must not come back unexamined.
    assert "renderD128" not in _CODE and "icd.d" not in _CODE, (
        "the hart-llm launcher must NOT re-add the presence-only GPU gate "
        "(renderD128 + an ICD file existing); that is not proof the GPU works.")


def test_llama_binary_is_published_on_path_for_hartos_finders():
    # HARTOS's own ModelLifecycleManager._find_llama_server_binary() falls back to
    # shutil.which('llama-server'). If this module keeps the binary to itself (only
    # in hart-llm.service's ExecStart), that finder returns None on HART OS and its
    # consumers degrade silently: lightweight_backend disables captioning, and
    # model_onboarding takes its "binary not found, downloading..." branch, which
    # pulls a GENERIC avx2 build and SIGILLs on the fleet CPU. Publishing the same
    # ISA-correct derivation on PATH is what makes the OS the supplier.
    assert "environment.systemPackages" in _CODE and "llama-server" in _CODE, (
        "hart-llm must put its llama.cpp build on PATH (environment.systemPackages) "
        "so HARTOS's _find_llama_server_binary() finds the ISA-correct binary "
        "instead of nothing (caption disabled) or a downloaded avx2 one (SIGILL).")


def test_cpu_inference_does_not_starve_the_os():
    # CPUWeight is necessary but NOT sufficient: it only arbitrates once
    # contention has begun. Measured on the box 2026-08-24 at CPUWeight=50, the
    # LLM still reached 457% CPU with the compositor at 94% and load average
    # 10.26, and a 16-token request returned nothing in 200s.
    assert "CPUWeight = 50" in _CODE, (
        "hart-llm CPUWeight must be BELOW the UI services' default 100 (was 150 = above "
        "it, so CPU inference out-prioritised and stalled the desktop).")
    # The real bound is core affinity, pinned by the launcher.
    assert "taskset" in _CODE, (
        "the launcher must PIN inference with taskset -- CPUWeight alone does not bound "
        "anything, so the desktop only wins after it has already started losing.")
    assert "cpuSharePercent" in _CODE, (
        "how much of the machine inference may take must be an option, not a constant.")


def test_cpu_budget_is_computed_on_the_node_not_baked():
    # HART OS is installed on hardware nobody has seen, with no operator to tune
    # it, so the budget must be derived at boot from THIS machine's topology.
    # A static AllowedCPUs=/thread count would be correct only on the machine it
    # was written for.
    assert "topology/core_id" in _CODE, (
        "core count must come from the running machine's topology (physical cores), "
        "not from a baked constant.")
    assert "nproc) - 1" not in _CODE, (
        "must NOT size threads as nproc-1: nproc counts SMT siblings, so on a 4-core/"
        "8-thread CPU that asked for 7 threads on 4 real cores and slowed BOTH the "
        "desktop and inference.")


def test_parallel_slots_are_capped():
    # llama.cpp's auto value is 4, and the slots share the kv_unified pool, so
    # 4 slots against ctx-size 12288 give each request a quarter of the budget
    # while the wire-layer trim computes against the full n_ctx.
    assert "--parallel" in _CODE and "parallelSlots" in _CODE, (
        "the launcher must pass an explicit --parallel cap; llama.cpp's auto (=4) "
        "over-subscribes the shared kv_unified pool.")
