"""The right-click "Ask <Product>" menu brands to the installed product.

Steward request (2026-07-24): right-click anywhere should offer "Ask Nunba" or
"Ask HART" based on which product the user installed. The shell serves the SAME
code as HART OS (the OS) and as the Nunba desktop companion; the ONE canonical
signal is core.port_registry.is_os_mode(). render_desktop_shell injects
window.HART_PRODUCT accordingly, and hartAskMenu.js (loaded by the shell) reads it.

Behavioural: renders the REAL shell via the REAL service with is_os_mode patched
both ways, and asserts the injected product var + that the menu module is loaded.
(hartAskMenu.js's runtime menu behaviour is validated on a real boot, like the
rest of the shell JS; its syntax is node --check'd in CI.)

Run (dev box, targeted):
    python -m pytest tests/unit/test_ask_menu_product_branding.py -v \
        --noconftest -p no:cacheprovider
"""
from unittest.mock import patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _render():
    return LiquidUIService().render_desktop_shell()


def test_product_is_HART_in_os_mode():
    with patch('core.port_registry.is_os_mode', return_value=True):
        html = _render()
    assert "window.HART_PRODUCT = 'HART';" in html, "OS mode must brand the menu 'HART'"
    assert '/shell/static/hartAskMenu.js' in html, "the Ask-menu module must be loaded"


def test_product_is_Nunba_in_companion_mode():
    with patch('core.port_registry.is_os_mode', return_value=False):
        html = _render()
    assert "window.HART_PRODUCT = 'Nunba';" in html, (
        "companion (non-OS) mode must brand the menu 'Nunba'")
    assert '/shell/static/hartAskMenu.js' in html


def test_product_falls_back_to_HART_if_probe_raises():
    # is_os_mode blowing up must never break the shell render; it defaults to HART.
    with patch('core.port_registry.is_os_mode', side_effect=RuntimeError("boom")):
        html = _render()
    assert "window.HART_PRODUCT = 'HART';" in html
