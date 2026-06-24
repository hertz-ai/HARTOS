"""Config-shape guard for the native desktop notification daemon (hart-notify.nix).

The capability audit (2026-06-24) found the glass shell only had in-shell SSE toasts and
NO native org.freedesktop.Notifications daemon — so foreign apps (Wine/Android), AI-composed
.hartapp surfaces, and the robot had no way to raise a desktop notification. This locks in
the fix: mako (the wlroots-native notification server) as a never-fail graphical-session
user service, glass-styled, with notify-send (libnotify) as the brain/app bridge.
"""
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_NOTIFY = (_ROOT / "nixos" / "modules" / "hart-notify.nix").read_text(encoding="utf-8")
_FLAKE = (_ROOT / "nixos" / "flake.nix").read_text(encoding="utf-8")


def test_module_exists_and_option_defaults_on():
    assert "options.hart.notifications" in _NOTIFY
    assert "enable" in _NOTIFY and "default = true" in _NOTIFY, (
        "hart.notifications.enable must exist and default ON (native notifications by default).")


def test_mako_is_the_notifications_daemon():
    # mako owns org.freedesktop.Notifications on a wlroots session (cage/sway/hart-comp);
    # without a daemon owning that D-Bus name every non-shell notification is silently dropped.
    assert "pkgs.mako" in _NOTIFY
    assert "/bin/mako" in _NOTIFY, "mako must be the ExecStart notification daemon."


def test_notify_send_bridge_available():
    # libnotify provides notify-send — the standard client the brain's SSE->native bridge
    # (and any app) calls to fire a notification through org.freedesktop.Notifications.Notify.
    assert "libnotify" in _NOTIFY, "notify-send (libnotify) must be on PATH as the bridge client."


def test_graphical_session_user_service_never_blocks_boot():
    # Same never-fail shape as hart-nunba/hart-conky: a graphical-session USER service,
    # NOT a boot-critical unit and NOT ordered before greetd -> a mako crash can never wedge
    # the seat or block first-paint; Restart respawns it.
    assert "systemd.user.services.hart-notify" in _NOTIFY
    for t in ['after = [ "graphical-session.target" ]',
              'partOf = [ "graphical-session.target" ]',
              'wantedBy = [ "graphical-session.target" ]']:
        assert t in _NOTIFY, "missing graphical-session wiring: " + t
    assert 'Restart = "on-failure"' in _NOTIFY
    # Never-fail: NO `before =` ordering key at all (so it cannot be ordered before greetd
    # or any boot-critical unit). Comments may MENTION greetd to explain this is avoided;
    # the real check is the absence of an actual ordering key.
    assert "before =" not in _NOTIFY, (
        "the notification daemon must NOT declare a before= ordering (it must block nothing).")


def test_glass_styled_to_match_the_shell():
    # Native toasts must feel native to HART OS: the canonical glass palette
    # (background #0F0E17, accent #00D4AA from ThemeService), not stock mako defaults.
    assert "0F0E17" in _NOTIFY, "mako background must be the HART dark glass (#0F0E17)."
    assert "00D4AA" in _NOTIFY, "mako accent border must be the HART accent (#00D4AA)."


def test_imported_in_flake():
    assert "hart-notify.nix" in _FLAKE, (
        "hart-notify.nix must be imported in the flake hartModules so the daemon is built.")
