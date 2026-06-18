#!/usr/bin/env python
"""HART OS Liquid UI visual + behavioural test harness.

Serves the REAL ``LiquidUIService`` Flask app via waitress on an ephemeral
port, drives headless Chromium (Playwright) across device viewports, writes one
PNG per viewport to ``.uitest_shots/``, and captures console/page errors +
failed requests so a silently-broken feature (a 200 asset that throws at
runtime) FAILS the run rather than passing on faith.

This is the screenshot+DOM superset of
``tests/unit/test_liquid_ui_shell_static_route.py`` — it reuses the SAME serve
substrate (waitress, ``_create_flask_app``, the ``/shell/static`` route), so it
exercises the production shell, the Files panel, the orb/hero, and every
``/api/shell/*`` route. It does NOT fork a parallel harness; it extends the
"derive the feature list from the live render, then prove each works"
discipline.

Run with the Playwright-equipped interpreter (the repo venv/venv311 do NOT have
Playwright; miniconda does — chromium-1208 is already under
``%LOCALAPPDATA%\\ms-playwright``)::

    C:\\Users\\sathi\\miniconda3\\python.exe scripts/ui_visual_harness.py [scenario]

Scenarios: ``shell`` (base desktop render, default), ``files`` (open the Files
panel first). PNGs land in an ABSOLUTE Windows path under the repo
(``.uitest_shots/``) — never ``/tmp`` (miniconda-on-Windows resolves ``/tmp`` to
``C:\\tmp``, which the reviewing agent can't predict).
"""
import json
import os
import socket
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OUT_DIR = os.path.join(REPO, ".uitest_shots")

# Device dimensions spanning the responsive range the OS must serve.
VIEWPORTS = [
    ("mobile", 390, 844),
    ("tablet", 820, 1180),
    ("desktop", 1920, 1080),
    ("ultrawide", 3440, 1440),
]


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port):
    import logging

    import waitress

    logging.getLogger("waitress").setLevel(logging.ERROR)
    waitress.serve(app, host="127.0.0.1", port=port, threads=4)


def _wait_up(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _probe_dom(page):
    """Informational DOM presence probe (broad, selector-tolerant)."""
    def has(sel):
        try:
            return page.query_selector(sel) is not None
        except Exception:
            return False

    def count(sel):
        try:
            return len(page.query_selector_all(sel))
        except Exception:
            return 0

    return {
        "canvas": count("canvas"),
        "buttons": count("button"),
        "ds_panels": count(".ds-panel, [class*='panel']"),
        "has_orb": has("canvas") or has("[class*='orb']") or has("[id*='orb']"),
        "has_hero": has("[class*='hero']") or has("[id*='hero']"),
        "has_dock_or_taskbar": has("[class*='dock']") or has("[class*='taskbar']") or has("[class*='task-bar']"),
        "body_text_len": len((page.inner_text("body") or "")) if has("body") else 0,
    }


def _run_interactions(page):
    """Click the real chrome (start menu, tray, icon, context menu) and report
    which handlers actually FIRE — the diagnosis static screenshots can't give."""
    import time as _t
    r = {}

    def vis(sel):
        try:
            return page.eval_on_selector(sel, "el=>{const s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&el.offsetHeight>0;}")
        except Exception:
            return False

    def npanels():
        try:
            return len(page.query_selector_all(".panel"))
        except Exception:
            return 0

    # 1) Start menu — single-click the start button
    try:
        page.click(".start-btn", timeout=4000)
        _t.sleep(0.7)
        r["start_menu_opens"] = vis(".start-menu")
        try:
            page.screenshot(path=os.path.join(OUT_DIR, "interact_startmenu.png"))
        except Exception:
            pass
        page.keyboard.press("Escape")
        _t.sleep(0.3)
    except Exception as exc:
        r["start_menu_err"] = str(exc)[:160]

    # 2) Tray button -> opens a panel
    try:
        before = npanels()
        page.click(".tray-btn", timeout=4000)
        _t.sleep(0.8)
        r["tray_opens_panel"] = npanels() > before
    except Exception as exc:
        r["tray_err"] = str(exc)[:160]

    # 3) Desktop icon — present? single-click selects? double-click opens?
    try:
        ic = page.query_selector(".desktop-icon")
        r["desktop_icon_present"] = bool(ic)
        if ic:
            ic.click(timeout=4000)
            _t.sleep(0.3)
            r["single_click_selects"] = page.eval_on_selector(".desktop-icon", "el=>el.classList.contains('selected')")
            before = npanels()
            ic.dblclick(timeout=4000)
            _t.sleep(0.8)
            r["dblclick_opens_panel"] = npanels() > before
    except Exception as exc:
        r["icon_err"] = str(exc)[:160]

    # 4) Context menu — right-click an icon shows #ctx-menu?
    try:
        ic = page.query_selector(".desktop-icon")
        if ic:
            ic.click(button="right", timeout=4000)
            _t.sleep(0.5)
            r["ctx_menu_shows"] = vis("#ctx-menu")
    except Exception as exc:
        r["ctx_err"] = str(exc)[:160]

    return r


def main(scenario="shell"):
    from integrations.agent_engine.liquid_ui_service import LiquidUIService

    os.makedirs(OUT_DIR, exist_ok=True)
    port = _free_port()
    svc = LiquidUIService(port=port)
    app = svc._create_flask_app()
    threading.Thread(target=_serve, args=(app, port), daemon=True).start()
    if not _wait_up(port):
        print(json.dumps({"ok": False, "error": "server did not come up"}))
        return 1

    # Load via localhost (NOT 127.0.0.1): the shell's client const is
    # SHELL='http://localhost:{port}', so loading on the same host keeps every
    # /api/shell/* fetch same-origin (else the browser CORS-blocks them).
    url = f"http://localhost:{port}/"
    skip_onboard = scenario != "onboarding"
    from playwright.sync_api import sync_playwright

    # Interaction testing only needs one viewport (clicks behave the same); we
    # care about WHICH handlers fire, not the layout.
    vps = [v for v in VIEWPORTS if v[0] == "desktop"] if scenario == "interact" else VIEWPORTS

    results = []
    any_pageerror = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, w, h in vps:
            page = browser.new_page(viewport={"width": w, "height": h})
            errors, failed, clog = [], [], []
            # default-arg binding captures THIS iteration's lists (closure-safe)
            page.on("console", lambda m, e=errors, c=clog: (c.append(f"{m.type}: {m.text}"), e.append(f"console: {m.text}") if m.type == "error" else None))
            page.on("pageerror", lambda exc, e=errors: e.append(f"pageerror: {exc}"))
            page.on("requestfailed", lambda r, f=failed: f.append(f"{r.method} {r.url}"))

            # Skip the once-per-session boot splash (#hart-boot checks this key
            # and removes itself immediately) so we capture the DESKTOP, not the
            # intro animation.
            page.add_init_script("try{sessionStorage.setItem('hart_booted','1');}catch(e){}")
            if skip_onboard:
                # Make the onboarding gate report 'already onboarded' so the
                # z-12000 ceremony overlay never covers the desktop.
                _onb = lambda route: route.fulfill(status=200, content_type="application/json", body='{"onboarded": true}')
                page.route("**/api/onboarding/status*", _onb)
                page.route("**/api/onboarding/profile*", _onb)

            page.goto(url, wait_until="load", timeout=30000)
            time.sleep(2.5)  # let JS render the shell + the orb/hero first frame

            if skip_onboard:
                # Defensive: drop any lingering splash/onboarding overlay.
                try:
                    page.evaluate(
                        "()=>{var b=document.getElementById('hart-boot');if(b)b.remove();"
                        "var o=document.getElementById('hart-onboarding');if(o)o.classList.remove('open');"
                        "document.documentElement.classList.remove('onboarding-active');}"
                    )
                    time.sleep(1.0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"dismiss overlays: {exc}")

            if scenario == "files":
                # Open the Files panel (Super+E binding -> file_manager panel)
                try:
                    page.evaluate("window.openPanel && window.openPanel('file_manager')")
                    time.sleep(1.5)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"open files: {exc}")

            inter = _run_interactions(page) if scenario == "interact" else None

            png = os.path.join(OUT_DIR, f"{scenario}_{name}.png")
            page.screenshot(path=png, full_page=False)
            size = os.path.getsize(png) if os.path.exists(png) else 0
            dom = _probe_dom(page)

            page_errs = [e for e in errors if e.startswith("pageerror")]
            if page_errs:
                any_pageerror = True
            results.append({
                "viewport": name, "w": w, "h": h,
                "png": png, "bytes": size,
                "dom": dom,
                "interact": inter,
                "console_errors": errors[:25],
                "console_log": clog[-45:] if scenario == "interact" else [],
                "failed_requests": failed[:25],
            })
            page.close()
        browser.close()

    print(json.dumps({
        "ok": not any_pageerror,
        "scenario": scenario,
        "out_dir": OUT_DIR,
        "results": results,
    }, indent=2))
    return 0 if not any_pageerror else 2


if __name__ == "__main__":
    scen = sys.argv[1] if len(sys.argv) > 1 else "shell"
    sys.exit(main(scen))
