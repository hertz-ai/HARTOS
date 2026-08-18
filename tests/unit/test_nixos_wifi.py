"""
HART OS — Wi-Fi wiring structural tests  (stream: wifi)

Guards the real-HW regression where the glass shell's connectivity indicator
showed "Wi-Fi not available" even though the Lenovo's Intel wifi + NetworkManager
were up. Root cause (investigation 2): the headless `hart-liquid-ui` server unit
sets `.path` to a MINIMAL list, which in NixOS becomes the unit's ENTIRE PATH
(/run/current-system/sw/bin is NOT on it). `networkmanager` (which provides the
`nmcli` every /api/shell/.../wifi route execs) was missing, so each exec raised
FileNotFoundError -> wifi.available=False -> the UI rendered "not available". The
two defense-in-depth halves (NetworkManager + redistributable firmware) were only
TRANSITIVELY satisfied via the GNOME default + the all-hardware installer profile.

This file asserts, structurally:
  1. hart-liquid-ui.nix: `networkmanager` is on the unit's `path` list (and curl +
     coreutils are still there).
  2. desktop.nix: `networking.networkmanager.enable = true` is an explicit, real
     assignment (not just a desktop-default side effect).
  3. desktop.nix: `hardware.enableRedistributableFirmware = true` is explicit.
  4. Boundary: the desktop does NOT enable the standalone `networking.wireless`
     (wpa_supplicant) stack — NetworkManager owns wifi; both-on conflicts.

WHY THIS IS NOT A GREP TEST
---------------------------
`networkmanager` is named in the explanatory COMMENTS in both files. A naive
`"networkmanager" in open(f).read()` would pass even if the real list/assignment
were deleted. So this suite REUSES the comment/string-aware Nix structural reader
proven in test_nixos_dns_firewall.py (its TestNixReaderBoundary exercises the
parser on synthetic inputs) and asserts on the PARSED list tokens / real
assignments — a commented-out mention is invisible to it (proved by the boundary
test below). Nix cannot be evaluated on the Windows dev box, so these are
structural behavioral tests of the configuration source; the full eval is
CI-gated (nix flake check) and the live exec is real-HW.

Usage:
    pytest tests/unit/test_nixos_wifi.py -v
"""

import os
import re
import pytest

# DRY (Gate 2): reuse the canonical comment/string-aware Nix reader rather than
# re-implementing a second parser. These helpers are the "code under test" of
# test_nixos_dns_firewall.py and are proven there against synthetic inputs.
from tests.unit.test_nixos_dns_firewall import (
    read,
    nix_skeleton,
    strip_comments,
    find_block,
    bool_assignment,
    has_assignment,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
CONFIGS_DIR = os.path.join(NIXOS_DIR, "configurations")

PROFILES_DIR = os.path.join(NIXOS_DIR, "profiles")

LIQUID_UI_NIX = os.path.join(MODULES_DIR, "hart-liquid-ui.nix")
DESKTOP_NIX = os.path.join(CONFIGS_DIR, "desktop.nix")
# The desktop CLOSURE, not one file: the 2026-07/08 refactor moved "what a
# desktop IS" into profiles/desktop.nix (networking.networkmanager lives there
# now) and hoisted hardware.enableRedistributableFirmware into hart-base so
# EVERY variant gets wifi firmware. The guards below are about the closure the
# image actually gets, so they read all three -- otherwise a correct move reads
# as a missing setting (it did: 3 red tests against a working Wi-Fi stack).
DESKTOP_PROFILE_NIX = os.path.join(PROFILES_DIR, "desktop.nix")
HART_BASE_NIX = os.path.join(MODULES_DIR, "hart-base.nix")


def _unit_path_tokens(raw):
    """Return the bare-attr tokens of the hart-liquid-ui systemd unit's
    `path = with pkgs; [ ... ];` list, comment/string-stripped so a token that
    only appears in the surrounding comment is NOT counted."""
    skel = nix_skeleton(raw)
    block = find_block(skel, r"path\s*=\s*with\s+pkgs\s*;\s*\[",
                       open_ch="[", close_ch="]")
    if block is not None:
        return block.split()
    # CONCATENATED FORM (what the module uses today):
    #   path = lib.optional (pkgs ? rustdesk) ... ++ (with pkgs; [ ... ]);
    # The guard's intent is "nmcli is on the unit PATH", not which syntax puts
    # it there -- the plain-form-only parser returned None and reported the
    # block "not found" against a unit that has networkmanager on its PATH.
    # Word boundary matters: without it this matches an unrelated
    # `<something>_path =` earlier in the file and parses the wrong block.
    m = re.search(r"(?:^|[^\w.])path\s*=", skel)
    if m is None:
        return None
    i, depth, end = m.end(), 0, None
    while i < len(skel):
        ch = skel[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth <= 0:
            end = i
            break
        i += 1
    if end is None:
        return None
    toks = []
    for blk in re.findall(r"\[([^\[\]]*)\]", skel[m.end():end]):
        toks.extend(blk.split())
    return toks or None


# ═══════════════════════════════════════════════════════════════
# Section 0: Boundary — prove the test is comment-aware (NOT a grep)
# ═══════════════════════════════════════════════════════════════

class TestParserIsCommentAware:

    def test_commented_token_is_not_a_path_member(self):
        """A `networkmanager` that appears only in a comment after the list must
        NOT be read as a list member — this is what separates the suite from a
        grep over the raw file."""
        synthetic = "path = with pkgs; [ curl coreutils ]; # networkmanager note"
        toks = _unit_path_tokens(synthetic)
        assert toks == ["curl", "coreutils"]
        assert "networkmanager" not in toks

    def test_real_member_is_detected(self):
        synthetic = "path = with pkgs; [ curl coreutils networkmanager ];"
        toks = _unit_path_tokens(synthetic)
        assert "networkmanager" in toks


# ═══════════════════════════════════════════════════════════════
# Section 1: hart-liquid-ui unit PATH provides nmcli (THE fix)
# ═══════════════════════════════════════════════════════════════

class TestLiquidUiUnitPath:

    @pytest.fixture(autouse=True)
    def load(self):
        self.raw = read(LIQUID_UI_NIX)
        self.tokens = _unit_path_tokens(self.raw)

    def test_unit_path_block_exists(self):
        assert self.tokens is not None, \
            "hart-liquid-ui.nix: `path = with pkgs; [ ... ];` block not found"

    def test_networkmanager_on_unit_path(self):
        """nmcli (from networkmanager) MUST be on the unit's minimal PATH or every
        /api/shell/.../wifi nmcli exec raises FileNotFoundError -> wifi shows
        'not available'."""
        assert "networkmanager" in self.tokens, \
            "networkmanager (nmcli) missing from the hart-liquid-ui unit PATH"

    def test_existing_path_members_preserved(self):
        """The fix is ADDITIVE — curl (Model-Bus probe) + coreutils must stay."""
        assert "curl" in self.tokens
        assert "coreutils" in self.tokens


# ═══════════════════════════════════════════════════════════════
# Section 2: desktop closure enables NM + wifi firmware explicitly
# ═══════════════════════════════════════════════════════════════

class TestDesktopWifiEnables:

    @pytest.fixture(autouse=True)
    def load(self):
        self.raw = "\n".join(
            read(f) for f in (DESKTOP_NIX, DESKTOP_PROFILE_NIX, HART_BASE_NIX))
        self.code = strip_comments(self.raw)

    def test_networkmanager_enabled_explicitly(self):
        assert bool_assignment(self.code, "networking.networkmanager.enable") == "true", \
            "desktop.nix must set networking.networkmanager.enable = true explicitly"

    def test_redistributable_firmware_enabled(self):
        assert bool_assignment(self.code, "hardware.enableRedistributableFirmware") == "true", \
            "desktop.nix must set hardware.enableRedistributableFirmware = true (iwlwifi/rtw firmware)"

    def test_standalone_wireless_not_enabled(self):
        """Privacy/invariant boundary: NetworkManager OWNS wifi. The standalone
        networking.wireless (wpa_supplicant) stack must NOT also be enabled — both
        on is a known NixOS conflict and would fight NM for the radio."""
        assert bool_assignment(self.code, "networking.wireless.enable") != "true", \
            "desktop.nix must not enable the standalone wpa_supplicant stack alongside NetworkManager"

    def test_firmware_and_nm_are_real_assignments(self):
        """Comment-aware sanity: both are real attribute assignments, not just
        names that survive in the explanatory comment."""
        assert has_assignment(self.code, "networking.networkmanager.enable")
        assert has_assignment(self.code, "hardware.enableRedistributableFirmware")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
