"""The glass shell's primary controls must be keyboard- and screen-reader-
accessible (#109 accessibility batch).

Before this, every shell control was an onclick <div>/<span> with no role,
tabindex, aria-label, or keyboard handler — a keyboard/AT dead zone vs Win11
(UIA) / macOS (NSAccessibility). This adds role="button"+tabindex+aria-label +
Enter/Space activation, aria-hidden on decorative icon glyphs (so a screen
reader reads the label, not the ligature word), a global :focus-visible ring,
aria-live regions for the agent status / chat log / toasts, and re-enables zoom.

Behavioural: drives the REAL render_desktop_shell() and asserts the accessibility
contract is present in the emitted document.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _render():
    return LiquidUIService().render_desktop_shell()


def test_primary_controls_are_keyboard_operable():
    html = _render()
    # Start button + tray buttons + the 4 power buttons are role=button, focusable,
    # and Enter/Space-activatable.
    assert 'class="start-btn" role="button" tabindex="0"' in html
    assert 'class="tray-btn" role="button" tabindex="0" aria-label="Notifications"' in html
    assert html.count('class="power-btn" role="button" tabindex="0"') == 4
    # The shared keyboard-activation handler (Enter/Space -> click).
    assert "event.key==='Enter'||event.key===' '" in html


def test_decorative_icons_are_hidden_from_screen_readers():
    html = _render()
    # Icon glyphs are aria-hidden so the accessible name comes from aria-label /
    # text, never the ligature word ("notifications").
    assert 'material-icons-round" aria-hidden="true">notifications' in html
    assert 'material-icons-round" aria-hidden="true">lock' in html


def test_global_focus_ring_present():
    html = _render()
    # Keyboard focus is visible on the shell chrome (it had no focus style before).
    assert '.start-btn:focus-visible,.tray-btn:focus-visible' in html


def test_live_regions_announce_dynamic_content():
    html = _render()
    assert 'id="agent-status" role="status" aria-live="polite"' in html
    assert 'id="ac-messages" role="log" aria-live="polite"' in html
    assert 'id="toast-container" role="status" aria-live="polite"' in html


def test_zoom_is_not_disabled():
    html = _render()
    # WCAG 1.4.4: the viewport must not block user zoom.
    assert 'user-scalable=no' not in html


def test_a11y_panel_sends_canonical_keys_and_reloads():
    """The settings toggles must send the SERVER's canonical keys (the old code
    derived the key from the label → sent reduce_motion / large_text, which the
    server silently dropped), and reload so the render re-applies the state."""
    html = _render()
    assert "'High Contrast', 'high_contrast'," in html
    assert "'Reduce Motion', 'reduced_motion'," in html
    assert '.then(()=>location.reload())' in html


def test_a11y_state_is_consumed_by_the_render():
    """High-contrast / reduced-motion in the live a11y state apply as <html>
    classes (the render previously never consumed the state at all)."""
    import integrations.agent_engine.shell_os_apis as sapi
    sapi._A11Y_SETTINGS.update({'high_contrast': True, 'reduced_motion': True})
    try:
        html = _render()
        assert 'class="a11y-contrast a11y-rmotion"' in html
        assert 'html.a11y-contrast{' in html
        assert 'html.a11y-rmotion *' in html
    finally:
        sapi._A11Y_SETTINGS.update({'high_contrast': False, 'reduced_motion': False})
    # ...and OFF emits no a11y class.
    assert '<html lang="en" class="">' in _render()
