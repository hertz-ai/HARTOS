"""The Calamares hartcfg module writes DECLARATIVE machine-local config.

Plan step 5 (docs/architecture/HART_INSTALLER_UNION_PLAN.md): the GUI's
config-writer must feed the SAME generator as the CLI and express GUI choices
as NixOS options in local.nix — never by mutating the target's /etc. These
tests drive the real pure helpers (importable without libcalamares); the
Calamares-facing run() is integration surface exercised on the live ISO, and
the generator itself is covered by the hart-installer-dualboot nixosTest.

Run (dev box):
    python -m pytest tests/unit/test_calamares_hartcfg.py -v \
        --noconftest -p no:cacheprovider
"""
import importlib.util
import pathlib

import pytest

_MOD = pathlib.Path(__file__).parents[2] / "nixos" / "installer" / "calamares" / "hartcfg-main.py"
spec = importlib.util.spec_from_file_location("hartcfg_main", _MOD)
hartcfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hartcfg)


def test_firmware_is_a_probe_not_a_model_name():
    assert hartcfg.pick_firmware(True) == "efi"
    assert hartcfg.pick_firmware(False) == "bios"


def test_full_choices_render_declarative_options():
    out = hartcfg.render_local_nix(
        hostname="steward-box",
        username="sathi",
        fullname="The Steward",
        hashed_password="$6$salt$hash",
        autologin=True,
        lang="en_IN.UTF-8",
        keyboard_layout="us",
    )
    assert 'networking.hostName = "steward-box";' in out
    assert "users.users.sathi = {" in out
    assert "isNormalUser = true;" in out
    assert 'description = "The Steward";' in out
    assert 'hashedPassword = "$6$salt$hash";' in out
    assert 'extraGroups = [ "wheel" "networkmanager" ];' in out
    assert 'autoLogin = { enable = true; user = "sathi"; }' in out
    assert 'i18n.defaultLocale = "en_IN.UTF-8";' in out
    assert 'services.xserver.xkb.layout = "us";' in out
    # It is a module, not a fragment.
    assert out.startswith("{ ... }:")
    assert out.rstrip().endswith("}")


def test_empty_choices_render_an_empty_module():
    out = hartcfg.render_local_nix()
    assert out == "{ ... }:\n{\n}\n"


def test_no_user_means_no_autologin_block():
    out = hartcfg.render_local_nix(hostname="h", autologin=True)
    assert "autoLogin" not in out
    assert "users.users" not in out


def test_nix_string_quoting_defuses_injection():
    # A hostile "fullname" must stay INSIDE its string literal: the text may
    # survive as inert, escaped CONTENT, but must never appear as a code line
    # at module indentation, and the quote/interpolation metacharacters must
    # arrive escaped.
    out = hartcfg.render_local_nix(
        username="u",
        fullname='x"; users.users.evil = { }; # ${builtins.exec}',
    )
    assert "\n  users.users.evil" not in out   # never a top-level code line
    assert '\\"' in out                        # the quote arrived escaped
    assert "\\${" in out                       # interpolation defanged
    # The whole payload sits on the description line, inside the literal.
    desc_line = next(l for l in out.splitlines() if "description" in l)
    assert "users.users.evil" in desc_line


def test_password_hash_is_sha512_crypt():
    if not hasattr(hartcfg, "hash_password"):
        pytest.skip("helper absent")
    try:
        h = hartcfg.hash_password("secret")
    except ImportError:
        pytest.skip("crypt module unavailable on this OS (Windows dev box)")
    assert h.startswith("$6$")


def test_render_local_nix_carries_USER_choices_only():
    """The dual-boot clock is NOT rendered here any more, and that is the fix.

    Facts probed off the machine belong to hart-write-install-config, the ONE
    generator BOTH installers call, which writes them to hardware-local.nix.
    While the clock lived in this per-front-end renderer it reached the CLI
    and missed the GUI (task #24) — the parallel path the steward called out.
    """
    out = hartcfg.render_local_nix(hostname="h", username="u")
    assert "networking.hostName" in out          # user choice: still here
    assert "hardwareClockInLocalTime" not in out  # probed fact: not our job
