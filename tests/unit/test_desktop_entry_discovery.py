"""The OS must be able to see the applications it ships with.

WHAT WAS BROKEN. AppRegistry is the central catalog and everything downstream
already worked off it -- installed_app_manifest() feeds window.MANIFEST, search()
fuzzy-matches, openPanel routes an entry with `exec` to the launcher. What was
missing was a SOURCE: only apps installed THROUGH HART's own installer were ever
registered, so everything the image itself ships was invisible.

Measured on the box 2026-08-29:
    /api/shell/apps            -> 4 entries, all Wine file-type stubs
    /api/shell/apps/installed  -> 1 entry, the flatpak Firefox
    /run/current-system/sw/share/applications -> 162 real .desktop files

`nix-env -q` sees nothing (NixOS's system profile is not a nix-env user profile)
and the old .desktop scan hardcoded /usr/share/applications and
~/.local/share/applications, NEITHER of which exists on this OS. The consequence
the user hit: typing "firefox" at the assistant could not open Firefox, because
there was no list to match the name against.

Run:
  pytest tests/unit/test_desktop_entry_discovery.py -v
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.platform import desktop_entries as DE          # noqa: E402
from core.platform.app_manifest import AppManifest, AppType  # noqa: E402
from core.platform.app_registry import AppRegistry       # noqa: E402


def write_entry(directory, filename, body):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
        fh.write(body)


APP = ("[Desktop Entry]\n"
       "Type=Application\n"
       "Name=%s\n"
       "Exec=%s\n"
       "Icon=%s\n")


# ── where we look ───────────────────────────────────────────────────────────

def test_xdg_precedence_is_followed():
    """XDG_DATA_HOME first, then XDG_DATA_DIRS in order. The spec says a user's
    own entry shadows the system one, and the dedupe below relies on this."""
    dirs = DE.entry_dirs(
        env={"XDG_DATA_HOME": "/u/data", "XDG_DATA_DIRS": "/a/share:/b/share"},
        home="/home/x")
    assert dirs[0] == os.path.join("/u/data", "applications")
    assert dirs[1] == os.path.join("/a/share", "applications")
    assert dirs[2] == os.path.join("/b/share", "applications")


def test_spec_defaults_when_the_environment_is_empty():
    """A systemd service often gets almost no environment. The spec's own
    defaults must apply rather than nothing being scanned."""
    dirs = DE.entry_dirs(env={}, home="/home/x")
    assert os.path.join("/home/x", ".local", "share", "applications") in dirs
    assert os.path.join("/usr/share", "applications") in dirs
    assert os.path.join("/usr/local/share", "applications") in dirs


def test_the_nixos_and_flatpak_trees_are_always_scanned():
    """The actual failure on the box: XDG_DATA_DIRS did not name the NixOS
    profile, so every application on the machine was invisible."""
    dirs = DE.entry_dirs(env={}, home="/home/x")
    assert "/run/current-system/sw/share/applications" in dirs
    assert "/var/lib/flatpak/exports/share/applications" in dirs
    assert os.path.join("/home/x", ".local", "share", "flatpak",
                        "exports", "share", "applications") in dirs


def test_directories_are_not_scanned_twice():
    dirs = DE.entry_dirs(env={"XDG_DATA_DIRS": "/usr/share:/usr/share"},
                         home="/home/x")
    assert len(dirs) == len(set(dirs))


# ── what counts as an application ───────────────────────────────────────────

def test_a_normal_entry_parses():
    e = DE.parse_entry(APP % ("Firefox", "firefox %u", "firefox"))
    assert e["Name"] == "Firefox"
    assert e["Exec"] == "firefox %u"


def test_nodisplay_entries_are_not_applications():
    """Exactly what the four Wine stubs are: file-type associations that must
    never appear in a launcher. The old code never opened the files, which is
    the only reason they were listed."""
    assert DE.parse_entry(
        "[Desktop Entry]\nType=Application\nName=Wine CHM\nNoDisplay=true\n") is None


def test_hidden_entries_are_not_applications():
    assert DE.parse_entry(
        "[Desktop Entry]\nType=Application\nName=Old App\nHidden=true\n") is None


@pytest.mark.parametrize("kind", ["Link", "Directory"])
def test_non_application_types_are_skipped(kind):
    assert DE.parse_entry("[Desktop Entry]\nType=%s\nName=Thing\n" % kind) is None


def test_an_entry_with_no_name_is_skipped():
    assert DE.parse_entry("[Desktop Entry]\nType=Application\n") is None


def test_only_the_first_group_is_read():
    """Desktop Actions live in later groups and carry their own Name=. Reading
    on would overwrite the app's name with an action's."""
    e = DE.parse_entry("[Desktop Entry]\nType=Application\nName=Firefox\n"
                       "[Desktop Action new-window]\nName=New Window\n")
    assert e["Name"] == "Firefox"


def test_localised_names_do_not_replace_the_plain_one():
    e = DE.parse_entry(
        "[Desktop Entry]\nType=Application\nName=Files\nName[de]=Dateien\n")
    assert e["Name"] == "Files"


def test_comments_and_blank_lines_are_ignored():
    e = DE.parse_entry(
        "# a comment\n\n[Desktop Entry]\n# another\nType=Application\nName=Calc\n")
    assert e["Name"] == "Calc"


# ── the scan ────────────────────────────────────────────────────────────────

def test_the_box_scenario_end_to_end(tmp_path, monkeypatch):
    """A tree shaped like the real box: NoDisplay Wine stubs in the service
    user's home, real applications in the NixOS profile. The old code returned
    only the stubs; this must return only the real apps."""
    home = tmp_path / "home"
    for stub in ("chm", "hlp", "msp", "vbs"):
        write_entry(str(home / ".local" / "share" / "applications"),
                    "wine-extension-%s.desktop" % stub,
                    "[Desktop Entry]\nType=Application\n"
                    "Name=Wine %s\nNoDisplay=true\n" % stub)
    nix = tmp_path / "nix" / "applications"
    write_entry(str(nix), "firefox.desktop", APP % ("Firefox", "firefox", "firefox"))
    write_entry(str(nix), "audacity.desktop", APP % ("Audacity", "audacity", "aud"))
    monkeypatch.setattr(DE, "_EXTRA_DIRS", (str(nix),))

    found = DE.discover(env={}, home=str(home))
    assert sorted(found) == ["audacity", "firefox"], sorted(found)
    assert not any("wine" in k for k in found), "NoDisplay stubs leaked in"


def test_every_entry_is_returned(tmp_path, monkeypatch):
    """The route used to do apps[:100] against a 162-app machine, hiding a third
    of it with nothing to say it had been cut."""
    nix = tmp_path / "applications"
    for i in range(162):
        write_entry(str(nix), "app%03d.desktop" % i,
                    APP % ("App %03d" % i, "app%03d" % i, "icon"))
    monkeypatch.setattr(DE, "_EXTRA_DIRS", (str(nix),))
    assert len(DE.discover(env={}, home=str(tmp_path / "nohome"))) == 162


def test_a_user_entry_shadows_the_system_one(tmp_path, monkeypatch):
    """XDG precedence: same desktop id, the earlier directory wins."""
    home = tmp_path / "home"
    write_entry(str(home / ".local" / "share" / "applications"), "firefox.desktop",
                APP % ("Firefox Nightly", "firefox-nightly", "ff"))
    nix = tmp_path / "applications"
    write_entry(str(nix), "firefox.desktop", APP % ("Firefox", "firefox", "ff"))
    monkeypatch.setattr(DE, "_EXTRA_DIRS", (str(nix),))
    assert DE.discover(env={}, home=str(home))["firefox"]["Name"] == "Firefox Nightly"


def test_a_missing_directory_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(DE, "_EXTRA_DIRS", ("/definitely/not/here",))
    assert DE.discover(env={}, home=str(tmp_path)) == {}


def test_non_desktop_files_are_ignored(tmp_path, monkeypatch):
    nix = tmp_path / "applications"
    write_entry(str(nix), "firefox.desktop", APP % ("Firefox", "firefox", "ff"))
    write_entry(str(nix), "mimeinfo.cache", "junk")
    write_entry(str(nix), "README", "junk")
    monkeypatch.setattr(DE, "_EXTRA_DIRS", (str(nix),))
    assert list(DE.discover(env={}, home=str(tmp_path))) == ["firefox"]


# ── into the registry the rest of the OS already uses ───────────────────────

def test_a_desktop_entry_becomes_a_launchable_manifest():
    """DESKTOP_APP is what installed_app_manifest() surfaces into
    window.MANIFEST, and `exec` is what makes openPanel launch it instead of
    trying to iframe a native binary."""
    m = AppManifest.from_desktop_entry("firefox", {
        "Name": "Firefox", "Icon": "firefox", "Comment": "Browse the web",
        "Keywords": "browser;www;", "Categories": "Network;WebBrowser;"})
    assert m.id == "firefox"
    assert m.name == "Firefox"
    assert m.type == AppType.DESKTOP_APP.value
    assert m.entry["exec"] == "firefox"
    assert m.description == "Browse the web"
    assert "browser" in m.tags and "WebBrowser" in m.tags


def test_the_registry_can_find_a_discovered_app_by_name():
    """The end of the chain the user actually hit: typing a name must resolve."""
    reg = AppRegistry()
    reg.load_desktop_entries({
        "firefox": {"Name": "Firefox", "Keywords": "browser;"},
        "audacity": {"Name": "Audacity", "Keywords": "audio;"},
    })
    assert [m.id for m in reg.search("firefox")] == ["firefox"]
    assert reg.search("Firefox")[0].id == "firefox"


def test_a_discovered_app_can_be_found_by_what_it_does():
    """Keywords and Categories become tags, so 'browser' reaches Firefox even
    though the word is nowhere in its name."""
    reg = AppRegistry()
    reg.load_desktop_entries({"firefox": {"Name": "Firefox",
                                          "Keywords": "browser;www;"}})
    assert [m.id for m in reg.search("browser")] == ["firefox"]


def test_discovered_apps_reach_the_shell_manifest():
    """installed_app_manifest() is what the glass desktop merges into
    window.MANIFEST, which is what acSend searches when a name is typed."""
    reg = AppRegistry()
    reg.load_desktop_entries({"firefox": {"Name": "Firefox", "Icon": "firefox"}})
    man = reg.installed_app_manifest()
    assert "firefox" in man
    assert man["firefox"]["title"] == "Firefox"
    assert man["firefox"]["exec"] == "firefox"


def test_a_desktop_entry_never_displaces_a_panel():
    """Panels load first at bootstrap and own their ids. A .desktop file that
    happens to share a name must not replace the shell's own surface."""
    reg = AppRegistry()
    reg.load_panel_manifest({"feed": {"title": "Feed", "route": "/feed"}})
    added = reg.load_desktop_entries({"feed": {"Name": "Some Other Feed"}})
    assert added == 0
    assert reg.get("feed").name == "Feed"
    assert reg.get("feed").type == AppType.NUNBA_PANEL.value


def test_loading_is_idempotent():
    """Bootstrap may run more than once in a process; a second pass must not
    duplicate or raise."""
    reg = AppRegistry()
    entries = {"firefox": {"Name": "Firefox"}}
    assert reg.load_desktop_entries(entries) == 1
    assert reg.load_desktop_entries(entries) == 0
    assert reg.count() == 1
