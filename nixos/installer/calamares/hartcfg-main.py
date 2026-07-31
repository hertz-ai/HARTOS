#!/usr/bin/env python3
"""Calamares job module `hartcfg` — writes the HART union config, then installs.

The GUI twin of `hart-install --mounted` (plan step 5): Calamares' stock
partition/mount modules prepare the target, then THIS module — replacing the
stock `nixos` job that writes a stock NixOS configuration.nix — hands the
mounted root to the SAME `hart-write-install-config` generator the CLI uses.
One writer for the installed configuration; the GUI cannot produce a different
system than the CLI (the step-5 invariant).

GUI choices (hostname, user, locale, keyboard) land in local.nix — the
generator's always-referenced extension point — as DECLARATIVE NixOS options,
hashedPassword included. Stock's flow instead runs the `users` job after
install to mutate the target's /etc/shadow; that mutation step is deliberately
dropped, not ported: on a declarative system the config IS the truth.

Pure helpers (render_local_nix, pick_firmware) are import-safe without
libcalamares so tests/unit/test_calamares_hartcfg.py exercises them on the dev
box; run() is the thin Calamares-facing shell.
"""
import subprocess

try:
    import libcalamares  # only present inside Calamares
except ImportError:  # pragma: no cover - dev box / unit tests
    libcalamares = None


def pick_firmware(efi_dir_exists: bool) -> str:
    """The one hardware branch, by probe — mirrors hart-install exactly."""
    return "efi" if efi_dir_exists else "bios"


def _nix_str(value: str) -> str:
    """Quote a value as a Nix string literal (minimal, for our own inputs)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'


def render_local_nix(
    hostname: str = "",
    username: str = "",
    fullname: str = "",
    hashed_password: str = "",
    autologin: bool = False,
    lang: str = "",
    keyboard_layout: str = "",
) -> str:
    """The machine-local module from GUI choices.

    USER CHOICES ONLY. Facts probed off the machine (the dual-boot clock)
    are written by hart-write-install-config into hardware-local.nix — the
    ONE generator both installers already call — so a probe cannot land in
    one front-end and miss the other, which is exactly what happened with
    time.hardwareClockInLocalTime (task #24). Declarative-only: the user is
    created by NixOS from this file, never by mutating the target's /etc.
    """
    lines = ["{ ... }:", "{"]
    if hostname:
        lines.append(f"  networking.hostName = {_nix_str(hostname)};")
    if lang:
        lines.append(f"  i18n.defaultLocale = {_nix_str(lang)};")
    if keyboard_layout:
        lines.append(f"  services.xserver.xkb.layout = {_nix_str(keyboard_layout)};")
    if username:
        lines.append(f"  users.users.{username} = {{")
        lines.append("    isNormalUser = true;")
        if fullname:
            lines.append(f"    description = {_nix_str(fullname)};")
        lines.append('    extraGroups = [ "wheel" "networkmanager" ];')
        if hashed_password:
            lines.append(f"    hashedPassword = {_nix_str(hashed_password)};")
        lines.append("  };")
        if autologin:
            lines.append(
                f"  services.displayManager.autoLogin = {{ enable = true; user = {_nix_str(username)}; }};"
            )
    lines.append("}")
    return "\n".join(lines) + "\n"


def hash_password(plaintext: str) -> str:
    """SHA-512 crypt via mkpasswd-compatible stdlib crypt (Linux-only)."""
    import crypt

    return crypt.crypt(plaintext, crypt.mksalt(crypt.METHOD_SHA512))


def _gs(key: str, default=""):
    gs = libcalamares.globalstorage
    return gs.value(key) if gs.contains(key) else default


def run():  # pragma: no cover - integration path, exercised on the live ISO
    """Calamares job entry point."""
    import os

    root = _gs("rootMountPoint")
    if not root:
        return ("No root mount point", "partition/mount must run before hartcfg")

    password = _gs("password")
    local = render_local_nix(
        hostname=_gs("hostname"),
        username=_gs("username"),
        fullname=_gs("fullname"),
        hashed_password=hash_password(password) if password else "",
        autologin=bool(_gs("autoLoginUser")),
        lang=(_gs("localeConf", {}) or {}).get("LANG", ""),
        keyboard_layout=_gs("keyboardLayout"),
    )
    os.makedirs(f"{root}/etc/nixos", exist_ok=True)
    with open(f"{root}/etc/nixos/local.nix", "w") as f:
        f.write(local)

    firmware = pick_firmware(os.path.isdir("/sys/firmware/efi"))
    libcalamares.job.setprogress(0.1)
    flake_ref = subprocess.check_output(
        ["hart-write-install-config", root, "desktop", firmware], text=True
    ).strip().splitlines()[-1]

    libcalamares.job.setprogress(0.3)
    subprocess.check_call(
        ["nixos-install", "--root", root, "--flake", flake_ref, "--no-root-passwd"]
    )
    libcalamares.job.setprogress(1.0)
    return None
