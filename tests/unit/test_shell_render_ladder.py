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


@pytest.mark.parametrize('rung', ['vulkan', 'webkit-cairo'])
def test_gpu_rungs_light_animations_but_glass_opaque_without_blur(monkeypatch, rung):
    """A GPU rung lights the micro-animations (body.gpu-hardware) so the desktop is
    ALIVE -- but the glass stays OPAQUE (body.webkit-flat) because backdrop-blur
    composites ONLY under WebKit accelerated compositing (preferHardwareGL), NOT from
    the GSK rung. webkit-cairo composites transforms yet its WebView is cairo, so its
    blur never paints; keying the glass off the rung left it see-through and the home
    bled THROUGH the App Store panel (2026-07-24). Alive AND legible: the two signals
    are decoupled."""
    cls = _body_class(monkeypatch, rung, gpu='hardware')   # no force-on env (fixture)
    assert 'gpu-hardware' in cls, f'{rung} did not light gpu-hardware/animations (got {cls!r})'
    assert 'webkit-flat' in cls, (
        f'{rung} glass is NOT solidified without blur -> it would read see-through '
        f'(got {cls!r})')


def test_force_hw_gl_gives_frosted_glass(monkeypatch):
    """LIQUID_UI_PREFER_HW_GL == '1' is the ONE signal that backdrop-blur actually
    composites (WebKit accelerated compositing on) -> frosted glass: gpu-hardware AND
    NO webkit-flat. This is the 'chase blur next' path; the default node stays opaque."""
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    cls = _body_class(monkeypatch, 'webkit-cairo', gpu='hardware')
    assert 'gpu-hardware' in cls and 'webkit-flat' not in cls, (
        f'force-on (blur composites) must give frosted glass, not opaque (got {cls!r})')


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
