"""Phase 5 (brain-side): AppRegistry's native-window handle map — the
manifest<->toplevel mirror of the compositor's WindowRegistry.

This is the ADDITIVE "AppRegistry window-handle field" the Phase-5 deliverable
names: a native-window launch path ALONGSIDE the existing iframe panels. The map
is fed by HART-comp's REAL `window.opened`/`window.closed` events (Phase 6 IPC) —
a handle is recorded only on a real map (the compositor never mints one for a
launch that didn't map), so the brain never reasons about a phantom window.

Behavioural: construct the real AppRegistry, drive the real bind/unbind/query
methods, assert the observable state (no mocks — these are pure in-memory ops).
The panel catalog (_apps) is asserted UNTOUCHED by the window-handle map (additive).

    python -m pytest tests/unit/test_app_registry_window_handles.py \
        --noconftest -p no:capture -q
"""
from core.platform.app_registry import AppRegistry


def _reg():
    return AppRegistry()


def test_bind_records_manifest_to_handle():
    r = _reg()
    r.bind_window_handle("blender", "win_9c04")
    assert r.window_handle_for("blender") == "win_9c04"
    assert r.native_windows() == {"blender": "win_9c04"}


def test_unbound_manifest_returns_none():
    r = _reg()
    assert r.window_handle_for("blender") is None
    assert r.native_windows() == {}


def test_unbind_on_close_clears_the_binding():
    r = _reg()
    r.bind_window_handle("blender", "win_9c04")
    r.unbind_window_handle("win_9c04")   # window.closed for that handle
    assert r.window_handle_for("blender") is None
    assert r.native_windows() == {}


def test_relaunch_repoints_manifest_and_stale_close_does_not_orphan():
    # Summon blender (win_a), it maps; relaunch maps a new toplevel (win_b) for the
    # SAME manifest before win_a is destroyed; then win_a closes. The manifest must
    # still point at win_b — mirrors compositor WindowRegistry::on_unmap safety.
    r = _reg()
    r.bind_window_handle("blender", "win_a")
    r.bind_window_handle("blender", "win_b")     # relaunch re-points
    assert r.window_handle_for("blender") == "win_b"
    r.unbind_window_handle("win_a")              # stale close of the OLD handle
    assert r.window_handle_for("blender") == "win_b", \
        "a stale window.closed for win_a must not clear the relaunched win_b"


def test_bind_ignores_empty_inputs():
    r = _reg()
    r.bind_window_handle("", "win_x")
    r.bind_window_handle("app", "")
    assert r.native_windows() == {}


def test_window_handle_map_is_independent_of_the_panel_catalog():
    # The additive map must NOT touch the iframe-panel catalog (_apps). Binding a
    # native window does not register an app; the catalog stays empty here.
    r = _reg()
    r.bind_window_handle("blender", "win_9c04")
    assert r.count() == 0                          # no panels/apps registered
    assert r.list_all() == []
    # …and the window map is still there (the two are orthogonal stores).
    assert r.window_handle_for("blender") == "win_9c04"
