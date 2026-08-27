"""The native bloom must stay WIRED, not merely written.

THE BUG THIS EXISTS FOR
  compositor/src/bloom.rs was created 2026-07-20 as M1 of the NATIVE SHELL
  PARITY PROGRAM: 256 lines, five passing unit tests, a documented performance
  contract. It was never declared with `mod bloom;` in main.rs, so rustc never
  compiled it. Every reference to the word "bloom" in the entire compositor sat
  inside bloom.rs itself, referring to itself.

  Nothing failed. No build broke, no test went red, no warning fired, because an
  undeclared file in a Rust crate is not an error, it is simply not part of the
  program. The desktop kept clearing to flat black through five weeks and an
  unknown number of boots, and the milestone read as "written".

  A cargo test cannot catch this: the tests inside an undeclared module do not
  run either. It takes a source-level guard, which is why this one is in Python
  and rides the unit job that runs on every push regardless of cargo features.

WHAT IT GUARDS
  1. The module is declared (the actual bug).
  2. The composed field reaches the frame (declared-but-unused would be the same
     bug wearing a better disguise).
  3. BOTH backends satisfy the trait, so real hardware gets it and not just the
     dev-box winit path.
  4. The palette is not re-read from disk per frame (the compose-once contract).

Run:
  pytest tests/unit/test_native_shell_bloom_wiring.py -v --noconftest
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "compositor", "src")


def _read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return fh.read()


def _strip_rust_comments(src):
    """Drop // and /* */ so guards read CODE, not the prose that legitimately
    discusses the very thing we are asserting is present or absent."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("//")
    )


def test_bloom_module_is_declared():
    """THE regression. Without this line the file is not part of the program."""
    code = _strip_rust_comments(_read("main.rs"))
    assert re.search(r"^\s*mod bloom;", code, re.M), (
        "compositor/src/bloom.rs is not declared with `mod bloom;` in main.rs, so "
        "rustc does not compile it and the desktop falls back to the flat splash "
        "clear. This is exactly how M1 sat dormant from 2026-07-20."
    )


def test_bloom_module_is_gated_like_its_only_consumer():
    """comp_core consumes it and is gated to any(winit, smithay). A wider gate
    would compile the module into the pure-logic floor where nothing calls it;
    a narrower one would drop the backdrop on a backend that renders."""
    code = _read("main.rs")
    m = re.search(r'(#\[cfg\([^\]]*\)\]\s*)?\n\s*mod bloom;', code)
    assert m and m.group(1), "`mod bloom;` must carry a cfg gate"
    gate = m.group(1)
    assert 'feature = "winit"' in gate and 'feature = "smithay"' in gate, (
        "bloom must be gated to any(winit, smithay), matching comp_core, its only "
        "consumer. Found gate: %s" % gate.strip()
    )


def test_the_composed_field_actually_reaches_the_frame():
    """Declared but never pushed is the same defect with better camouflage."""
    code = _strip_rust_comments(_read("comp_core.rs"))
    assert "bloom_mut()" in code, "nothing reads the bloom cache while building a frame"
    assert re.search(r"elements\.push\(HartRenderElement::Memory", code), (
        "the bloom buffer is never pushed as a render element, so it is composed "
        "and then thrown away"
    )


def test_bloom_is_the_bottom_element():
    """It is the BACKDROP. Smithay draws the element list front-to-back, so the
    backdrop must be pushed last; pushing it earlier would paint it over the
    windows and hide the desktop."""
    code = _strip_rust_comments(_read("comp_core.rs"))
    body = code[code.index("pub fn build_frame_elements"):]
    body = body[: body.index("\n}")]
    bloom_at = body.index("bloom_mut()")
    # Every other element push must come BEFORE the bloom push.
    for marker in ("HartRenderElement::Surface", "HartRenderElement::Solid"):
        if marker in body:
            assert body.rindex(marker) < bloom_at, (
                "%s is pushed after the bloom backdrop, which would draw it "
                "underneath the backdrop and hide it" % marker
            )


def test_both_backends_implement_the_accessor():
    """The DRM backend is the one that runs on real hardware. A winit-only impl
    would compile on the dev box and leave the actual desktop black."""
    for backend in ("wayland.rs", "winit.rs"):
        code = _strip_rust_comments(_read(backend))
        assert "fn bloom_mut(&mut self)" in code, (
            "%s does not implement CompState::bloom_mut" % backend
        )
        assert re.search(r"pub bloom:\s*crate::comp_core::BloomCache", code), (
            "%s has no bloom field to hand back" % backend
        )


def test_the_palette_is_not_read_from_disk_every_frame():
    """bloom::theme_palette opens a JSON file. build_frame_elements runs per
    frame, so calling it there would be a file read at 60Hz behind a static
    image, breaking the compose-once contract the module documents."""
    code = _strip_rust_comments(_read("comp_core.rs"))
    body = code[code.index("pub fn build_frame_elements"):]
    body = body[: body.index("\n}")]
    assert "theme_palette()" not in body, (
        "build_frame_elements calls bloom::theme_palette() directly, which reads "
        "a theme file on every frame. Resolve it once inside BloomCache instead."
    )


def test_the_compose_is_cached_against_size_and_palette():
    """Recomposing per frame would walk every pixel at 60Hz."""
    code = _strip_rust_comments(_read("comp_core.rs"))
    assert re.search(r"self\.key\s*!=\s*Some\(\(w,\s*h,\s*pal\)\)", code), (
        "BloomCache must skip the compose when (size, palette) is unchanged"
    )
