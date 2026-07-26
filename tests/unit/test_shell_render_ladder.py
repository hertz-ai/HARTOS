"""The shell render auto-fallback ladder: the served body-class must track the
RUNTIME render rung the session tier landed on, so a hung GPU rung that the
paint-watchdog drops to a lower tier re-renders in the rung that actually paints
(steward 2026-07-19: "both, one shd auto fallback to other").

Ladder (mapped onto the existing session-supervisor tier ladder):
  Tier-1 hart-comp -> vulkan       (GSK vulkan + WebKit accel)  body.gpu-hardware
  Tier-2 sway      -> webkit-cairo (GSK cairo  + WebKit accel)  body.gpu-hardware
  Tier-3 cage      -> software     (GTK3 floor, WebKit off)     body.gpu-software webkit-flat

Each tier's session launcher writes /run/hart/shell-render; the backend
``read_shell_render_mode`` reads it. BOTH GPU rungs enable WebKit compositing, so
both light up the shell micro-animations + live glass (body.gpu-hardware); only
the software floor stays flat/opaque. Behavioural: patch the two runtime readers
and render the REAL shell, asserting the emitted body class.
"""
import os

import pytest

import integrations.agent_engine.liquid_ui_service as lus
from integrations.agent_engine.liquid_ui_service import LiquidUIService, read_shell_render_mode


@pytest.fixture(autouse=True)
def _no_force_env(monkeypatch):
    """The dev/operator FORCE-ON env must not leak in from the test host."""
    monkeypatch.delenv('LIQUID_UI_PREFER_HW_GL', raising=False)
    monkeypatch.setenv('HART_OS_MODE', '1')


def _body_class(monkeypatch, rung, gpu='hardware'):
    """Render the real shell with the two runtime readers patched to (rung, gpu),
    and return the emitted ``<body class="...">`` value."""
    monkeypatch.setattr(lus, 'read_shell_render_mode', lambda: rung)
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: gpu)
    html = LiquidUIService().render_desktop_shell()
    import re
    m = re.search(r'<body class="([^"]*)"', html)
    assert m, 'shell rendered no <body class>'
    return m.group(1)


def test_read_shell_render_mode_fail_software(monkeypatch, tmp_path):
    """The reader fail-SOFTWARE: a missing/unknown verdict yields 'software' (the
    safe floor); only the two known GPU rungs pass through."""
    missing = tmp_path / 'nope'
    monkeypatch.setattr(lus, '_SHELL_RENDER_FILE', str(missing))
    assert read_shell_render_mode() == 'software'          # absent -> floor
    for good in ('vulkan', 'webkit-cairo'):
        p = tmp_path / 'r'
        p.write_text(good)
        monkeypatch.setattr(lus, '_SHELL_RENDER_FILE', str(p))
        assert read_shell_render_mode() == good
    p.write_text('garbage')
    assert read_shell_render_mode() == 'software'           # unknown -> floor


def test_webkit_cairo_lights_animations_but_glass_stays_opaque(monkeypatch):
    """webkit-cairo (the node's stable Tier-1 rung) lights the micro-animations
    (gpu-hardware, ALIVE) but its GSK=cairo WebView cannot composite backdrop-blur, so
    the glass MUST be solidified (webkit-flat) -- else it reads see-through and the
    home bleeds THROUGH the App Store panel (2026-07-24). Alive AND legible."""
    cls = _body_class(monkeypatch, 'webkit-cairo', gpu='hardware')
    assert 'gpu-hardware' in cls, f'webkit-cairo did not light animations (got {cls!r})'
    assert 'webkit-flat' in cls, (
        f'webkit-cairo glass is NOT solidified -> it would read see-through (got {cls!r})')


def test_only_vulkan_rung_frosts_the_glass(monkeypatch):
    """ONLY the vulkan rung actually composites backdrop-blur (GSK=vulkan), so it is
    the ONLY rung that frosts: gpu-hardware AND no webkit-flat."""
    cls = _body_class(monkeypatch, 'vulkan', gpu='hardware')
    assert 'gpu-hardware' in cls and 'webkit-flat' not in cls, (
        f'vulkan (the blur-compositing rung) must frost, not solidify (got {cls!r})')


def test_preferhwgl_flag_does_NOT_frost_a_cairo_rung(monkeypatch):
    """The glass signal is the RUNG, not the preferHardwareGL flag. Forcing
    LIQUID_UI_PREFER_HW_GL=1 while the rung is webkit-cairo must STILL solidify the
    glass -- hart-comp forces webkit-cairo even with preferHardwareGL on (vulkan
    demoted, hart-comp.nix:562), and cairo can't paint blur, so keying off the flag
    would frost a cairo surface right back into see-through. This guards that exact
    regression."""
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    cls = _body_class(monkeypatch, 'webkit-cairo', gpu='hardware')
    assert 'webkit-flat' in cls, (
        f'preferHardwareGL flag must NOT frost a cairo rung (got {cls!r})')


def test_software_rung_stays_flat_floor(monkeypatch):
    """The cage software floor stays calm + opaque: gpu-software + webkit-flat, so
    the animations are shed and glass is solidified (no per-frame blur on cairo)."""
    cls = _body_class(monkeypatch, 'software', gpu='hardware')
    assert 'gpu-software' in cls and 'webkit-flat' in cls, f'floor not flat (got {cls!r})'
    assert 'gpu-hardware' not in cls


def test_gles_probe_gates_the_rung(monkeypatch):
    """A GPU rung is honoured ONLY when the compositor GLES probe also says
    hardware. If the probe says software (no GLES), even a webkit-cairo rung stays
    on the floor -- never arm GPU effects on a box that can't composite."""
    cls = _body_class(monkeypatch, 'webkit-cairo', gpu='software')
    assert 'gpu-software' in cls, f'expected floor when GLES probe=software (got {cls!r})'


def test_force_env_is_on_only_not_off(monkeypatch):
    """LIQUID_UI_PREFER_HW_GL == '1' FORCES hardware (dev/operator); any other value
    (incl. the node default '0') is NOT a force-off -- it falls through to the rung
    file so the ladder still governs (a hard '0' would defeat the ladder)."""
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    assert 'gpu-hardware' in _body_class(monkeypatch, 'software', gpu='hardware')  # forced on
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '0')
    # '0' is not force-off: webkit-cairo rung still wins -> hardware
    assert 'gpu-hardware' in _body_class(monkeypatch, 'webkit-cairo', gpu='hardware')
