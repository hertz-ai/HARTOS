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


def test_render_local_nix_emits_local_rtc_when_windows_present():
    """Dual-boot clock through the GUI installer (task #24).

    The GUI is an ALTERNATIVE install path that writes local.nix itself, so a
    fix living only in the CLI installer left the DEFAULT desktop install
    hitting the +5:30 backwards clock jump.
    """
    out = hartcfg.render_local_nix(hostname="h", windows_present=True)
    assert "time.hardwareClockInLocalTime = true;" in out


def test_render_local_nix_omits_local_rtc_on_a_single_os_machine():
    """Blanket-setting it is wrong in the other direction: a single-OS
    machine's RTC really is UTC."""
    out = hartcfg.render_local_nix(hostname="h", windows_present=False)
    assert "hardwareClockInLocalTime" not in out
    # Default must be the safe one for callers that predate the parameter.
    assert "hardwareClockInLocalTime" not in hartcfg.render_local_nix(hostname="h")


def test_windows_probe_checks_both_esp_mount_points(tmp_path):
    """The same two paths the CLI probes — an ESP can be mounted at /boot or
    /boot/efi, and checking only one silently misses half the machines."""
    m = hartcfg
    assert m.windows_bootloader_present(str(tmp_path)) is False
    esp = tmp_path / "boot" / "efi" / "EFI" / "Microsoft" / "Boot"
    esp.mkdir(parents=True)
    (esp / "bootmgfw.efi").write_text("x")
    assert m.windows_bootloader_present(str(tmp_path)) is True
