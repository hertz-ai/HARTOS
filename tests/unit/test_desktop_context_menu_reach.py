"""The desktop right-click menu must not be gated on naming the backdrop.

WHAT WAS BROKEN
  The desktop context menu is the ONLY way into Personalize, and Personalize is
  where the orb-style picker lives (hartPersonalize.js, which reads and writes
  HartSession.orb_style and drives the live orb). The handler decided "this is a
  desktop right-click" with:

      if (e.target.classList.contains('wallpaper') || e.target === document.body)

  That names the BACKDROP, and the backdrop kept growing. `.hart-ambient`,
  `.hart-bloom-canvas`, `.hart-grain`, `.hart-vignette` and `.hart-hero` all
  paint over `.wallpaper` and cover the screen, so a real right-click on the
  desktop almost always landed on one of THOSE, failed the test, and fell through
  to the two-item "Open in New Panel / Properties" menu whose entries are wired
  to ''.

  Net effect: the orb chooser was written, shipped, loaded by the page, and
  impossible to open. Reported as "orb selection was there but not able to see
  that". Wallpaper and auto-arrange were lost the same way.

THE RULE
  Test for chrome and invert, rather than enumerating the backdrop. New
  decoration layers then count as desktop by default, which is the safe
  direction: a missed layer costs a menu, a missed chrome element only means the
  desktop menu appears somewhere slightly wrong.

Run:
  pytest tests/unit/test_desktop_context_menu_reach.py -v --noconftest
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHELL = os.path.join(REPO, "integrations", "agent_engine", "liquid_ui_service.py")


def _src():
    with open(SHELL, encoding="utf-8") as fh:
        return fh.read()


def _handler():
    """The contextmenu handler body, comments stripped."""
    src = _src()
    i = src.index("document.addEventListener('contextmenu'")
    body = src[i:i + 2500]
    return "\n".join(
        l for l in body.splitlines() if not l.lstrip().startswith("//")
    )


def test_desktop_menu_is_not_gated_on_the_backdrop_element():
    """The exact regression."""
    code = _handler()
    assert "classList.contains('wallpaper')" not in code, (
        "the desktop right-click test names the backdrop again. Every layer that "
        "paints over .wallpaper (bloom canvas, grain, vignette, ambient, hero) "
        "then swallows the menu, and Personalize becomes unreachable."
    )


def test_desktop_menu_is_decided_by_excluding_chrome():
    code = _handler()
    assert "closest(" in code, (
        "the handler must decide by walking up from the click target to real "
        "chrome, not by matching the target itself")
    # The containers that genuinely are NOT the desktop.
    for sel in (".panel-container", ".taskbar", ".hart-topbar", ".start-menu"):
        assert sel in code, "chrome selector %s missing from the exclusion" % sel


def test_personalize_is_still_offered_and_still_opens_the_hub():
    """Guard the payload, not just the gate: reaching the menu is worthless if
    the entry that leads to the orb picker is gone."""
    code = _handler()
    assert "'Personalize'" in code, "the Personalize entry is gone from the menu"
    assert 'openPanel("wallpaper_manager")' in code, (
        "Personalize must still open the hub panel that hartPersonalize renders")


def test_the_orb_picker_is_actually_wired_behind_that_panel():
    """The chain the user is trying to reach, end to end: menu -> panel ->
    hartPersonalize -> orb_style. A break anywhere makes the picker invisible
    again, which is how this defect presented."""
    src = _src()
    assert "hartRenderPersonalize" in src, "the hub renderer is not called"
    assert 'src="/shell/static/hartPersonalize.js"' in src, (
        "hartPersonalize.js is not loaded by the shell, so the hub cannot render")
    js = os.path.join(REPO, "integrations", "agent_engine", "static",
                      "hartPersonalize.js")
    with open(js, encoding="utf-8") as fh:
        pj = fh.read()
    assert "orb_style" in pj, "the personalize hub no longer owns the orb style"


def test_the_js_still_balances_after_fstring_expansion():
    """This JS lives inside a Python f-string, so every literal brace is doubled.
    A stray single brace here does not fail the Python parse, it silently emits
    broken JavaScript and the whole shell script dies at load."""
    src = _src()
    i = src.index("document.addEventListener('contextmenu'")
    body = src[i:i + 2500]
    # Expand the f-string escaping the way Python will.
    rendered = body.replace("{{", "\x00").replace("}}", "\x01")
    assert "{" not in rendered.split("\x01")[0].replace("\x00", ""), (
        "a single (unescaped) brace appears in the contextmenu handler; inside "
        "an f-string it must be doubled or the emitted JS is malformed")
