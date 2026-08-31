"""XDG desktop-entry discovery — the OS's own list of launchable applications.

WHY THIS EXISTS. AppRegistry is the central catalog, and everything downstream
already works off it: ``installed_app_manifest()`` puts entries into the glass
shell's ``window.MANIFEST``, ``search()`` fuzzy-matches them, ``openPanel`` routes
an entry with ``exec`` to the launcher. What was missing was a SOURCE. Only apps
installed *through* HART's own installer were ever registered, so everything the
image itself ships was invisible to the OS.

Measured on the box 2026-08-29:
    /api/shell/apps            -> 4 entries, all Wine file-type stubs
    /api/shell/apps/installed  -> 1 entry, the flatpak Firefox
    /run/current-system/sw/share/applications -> 162 real .desktop files
`nix-env -q` sees nothing because NixOS's system profile is not a nix-env user
profile, and the old .desktop scan hardcoded /usr/share/applications and
~/.local/share/applications, NEITHER of which exists on this OS. So the machine
could not name its own applications, which is why typing "firefox" at the
assistant could not open Firefox: there was no list to match against.

This module is the one place that knows how to find and read .desktop files.
It is deliberately dependency-free and does no registry work of its own; the
registry side is AppManifest.from_desktop_entry + AppRegistry.load_desktop_entries,
alongside the existing panel importers.

Portability: the directory lookup is the XDG base-directory spec, so it is correct
on any Linux desktop. _EXTRA_DIRS afterwards is a purely additive safety net for
trees a stripped-down systemd service environment will not name in XDG_DATA_DIRS;
on a distro that does not use them they simply do not exist.
"""

import logging
import os

logger = logging.getLogger('hevolve.platform')

#: Scanned in addition to whatever XDG names, because a systemd service often runs
#: with almost no environment and would otherwise miss every application on the
#: machine. Additive only: a missing directory costs one failed listdir.
_EXTRA_DIRS = (
    '/run/current-system/sw/share/applications',      # NixOS system profile
    '/var/lib/flatpak/exports/share/applications',    # flatpak, system install
)


def entry_dirs(env=None, home=None):
    """Application directories to scan, in XDG precedence order (first wins).

    XDG_DATA_HOME (default ~/.local/share) then each XDG_DATA_DIRS entry (default
    /usr/local/share:/usr/share), each with /applications appended.
    """
    env = os.environ if env is None else env
    home = home or env.get('HOME') or os.path.expanduser('~')

    roots = [env.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')]
    roots += [p for p in (env.get('XDG_DATA_DIRS')
                          or '/usr/local/share:/usr/share').split(':') if p]

    dirs = [os.path.join(r, 'applications') for r in roots]
    dirs.append(os.path.join(home, '.local', 'share', 'flatpak',
                             'exports', 'share', 'applications'))
    dirs.extend(_EXTRA_DIRS)

    seen, ordered = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def parse_entry(text):
    """The [Desktop Entry] group as a dict, or None if it is not a launchable app.

    Returns None for anything that must never appear in a launcher: a non-Application
    Type, NoDisplay=true, or Hidden=true. That filter is the whole reason the four
    Wine stubs were listed before -- they are file-type associations, marked
    NoDisplay, and the old code never opened the files to find out.

    Hand-parsed rather than via configparser: real .desktop files carry duplicate
    keys and % sequences that configparser rejects or mangles, and one bad file
    must not take down the enumeration of every app after it.
    """
    fields, in_group = {}, False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            if in_group:
                break              # a later group ([Desktop Action ...]) — stop
            in_group = line == '[Desktop Entry]'
            continue
        if not in_group or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        # Localised keys look like Name[de]. Keep only the unlocalised one, or a
        # translation would overwrite the name we match against.
        if '[' not in key:
            fields.setdefault(key, value.strip())

    if fields.get('Type', 'Application') != 'Application':
        return None
    if fields.get('NoDisplay', '').lower() == 'true':
        return None
    if fields.get('Hidden', '').lower() == 'true':
        return None
    if not fields.get('Name'):
        return None
    return fields


def discover(env=None, home=None):
    """{desktop_id: fields} for every launchable application on this machine.

    Deduped with first-directory-wins, which is what the spec says: a user's own
    entry in ~/.local/share shadows the system copy of the same id.

    Never raises. A missing or unreadable directory is simply not there, and an
    unreadable file is skipped rather than aborting the scan -- an OS that lists
    no apps because of one bad file would be worse than one that lists all but it.
    """
    found = {}
    for d in entry_dirs(env, home):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith('.desktop'):
                continue
            app_id = fname[:-len('.desktop')]
            if app_id in found:
                continue
            try:
                with open(os.path.join(d, fname), 'r', encoding='utf-8',
                          errors='replace') as fh:
                    entry = parse_entry(fh.read())
            except OSError:
                continue
            if entry is not None:
                found[app_id] = entry
    return found
