"""Source guards for the real-HW boot fixes (steward's flash, 2026-07-09).

These are labelled test_source_guard_* ON PURPOSE: each protects a Nix/Rust invariant
that the CI *build* passes WITHOUT (the closure still compiles), so only a real-hardware
BOOT would otherwise catch its removal. The behavioural half of each fix is the CI cargo
build + the nixosTest boot; these guards stop a silent regression that removes the fix
while leaving the build green. They are supplements, never the sole test.

Invariants guarded:
1. hart-comp gets /run/opengl-driver/lib on LD_LIBRARY_PATH — without it the smithay EGL
   backend dlopens libEGL.so.1, can't find it, PANICS (rc=134) and the compositor drops
   to sway. The build succeeds regardless; only a boot on a real GPU exposes it.
2. panic = "unwind" in the compositor crate — required for catch_unwind to convert a
   third-party (smithay) EGL/GLES load panic into a pixman degrade instead of abort.
3. udev.rs wraps build_gles_renderer in catch_unwind — the degrade-not-die guarantee.
4. The roaming desktop uses opportunistic DoT (dns.fallbackToPlaintext) so a captive /
   port-853-blocked network degrades to plaintext DNS instead of failing all resolution
   (the "connected to the internet, flatpak still couldn't reach flathub" symptom).
"""
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _read(rel):
    with open(os.path.join(REPO, rel), 'r', encoding='utf-8') as f:
        return f.read()


def test_source_guard_hart_comp_exports_opengl_driver_lib():
    src = _read('nixos/modules/hart-comp.nix')
    assert 'LD_LIBRARY_PATH=/run/opengl-driver/lib' in src, (
        "hart-comp session wrapper must export LD_LIBRARY_PATH=/run/opengl-driver/lib "
        "so the smithay EGL/GLES backend can dlopen libEGL.so.1 (else rc=134 → drop to sway)")


def test_source_guard_compositor_panics_unwind_not_abort():
    src = _read('compositor/Cargo.toml')
    assert re.search(r'^\s*panic\s*=\s*"unwind"', src, re.M), (
        "compositor [profile.release] must be panic = \"unwind\" so catch_unwind can "
        "catch smithay's EGL/GLES load panic and degrade to pixman (abort makes it a no-op)")
    assert not re.search(r'^\s*panic\s*=\s*"abort"', src, re.M), (
        "panic = \"abort\" would re-break the degrade-not-die guarantee")


def test_source_guard_gles_init_is_catch_unwind_wrapped():
    src = _read('compositor/src/udev.rs')
    assert 'catch_unwind' in src and 'build_gles_renderer' in src, (
        "the GLES arm must wrap build_gles_renderer in catch_unwind so a libEGL/libGLESv2 "
        "load panic degrades to the pixman floor in-process, never a lower-tier drop")


def test_source_guard_desktop_dns_opportunistic_fallback():
    src = _read('nixos/configurations/desktop.nix')
    assert re.search(r'dns\.fallbackToPlaintext\s*=\s*true', src), (
        "the roaming desktop must set dns.fallbackToPlaintext = true (opportunistic DoT) "
        "so a captive / port-853-blocked network still resolves names")
