"""The Nix Nunba package ships BUILD_INFO.txt, the same file the Windows nightly ships.

Measured 2026-10-05: the owner's Windows install answered BUILD_SHA / HARTOS_SHA /
BUILD_TIME from C:\\Program Files (x86)\\HevolveAI\\Nunba\\BUILD_INFO.txt in one read;
the HART OS node (generation 13) had no such file under /nix/store/*-nunba-1.0.0/lib/nunba
and its Nunba revision could only be recovered by reading nixos/packages/nunba.nix.
A first-class preinstalled Nunba must be able to say which Nunba it is and which
HARTOS it was shipped inside, from the node itself.
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PKG = os.path.join(ROOT, 'nixos', 'packages', 'nunba.nix')
MOD = os.path.join(ROOT, 'nixos', 'modules', 'hart-nunba.nix')


def _read(p):
    return open(p, encoding='utf-8').read()


def test_the_package_writes_build_info_with_the_windows_keys():
    src = _read(PKG)
    m = re.search(r'cat > \$out/lib/nunba/BUILD_INFO\.txt <<EOF\n(.*?)\nEOF\n', src, re.S)
    assert m, 'nunba.nix must write $out/lib/nunba/BUILD_INFO.txt in its installPhase'
    body = m.group(1)
    assert 'BUILD_SHA=${nunbaRev}' in body, 'BUILD_SHA must be the pinned Nunba rev, interpolated, never typed'
    assert 'HARTOS_SHA=${hartRev}' in body, 'HARTOS_SHA must be the HART OS rev handed in by the module'
    assert 'BUILD_TIME=' in body and 'BUILD_PLATFORM=nixos' in body


def test_the_package_accepts_hart_rev_and_the_module_passes_it():
    src = _read(PKG)
    assert re.search(r'^, hartRev \? "unknown"', src, re.M), 'nunba.nix must take hartRev with an honest default'
    mod = _read(MOD)
    assert re.search(r'^\{ config, lib, pkgs, hartSrc \? /etc/hart, hartRev \? "unknown", \.\.\. \}:', mod, re.M), (
        'hart-nunba.nix must declare hartRev in its module args (flake.nix passes it as a specialArg)')
    assert 'callPackage ../packages/nunba.nix { inherit hartSrc hartRev; }' in mod, (
        'hart-nunba.nix must hand hartRev to the package')


def test_the_floor_and_the_daemon_build_the_same_nunba_derivation():
    """hart-liquid-ui.nix's NUNBA_STATIC_DIR floor and hart-nunba.nix's daemon must
    call packages/nunba.nix with the SAME arguments, or the store holds two 155M
    Nunba trees (measured on generation 14, 2026-10-11: one HARTOS_SHA=unknown,
    one real, on a disk with 237M free)."""
    ui = _read(os.path.join(ROOT, 'nixos', 'modules', 'hart-liquid-ui.nix'))
    mod = _read(MOD)
    assert 'callPackage ../packages/nunba.nix' not in ui, (
        'hart-liquid-ui.nix must not call packages/nunba.nix itself: the ONE package is hart.nunba.package')
    assert '${config.hart.nunba.package}/lib/nunba/static' in ui
    assert ui.count('callPackage ../packages/nunba.nix') == 0
    real_calls = [l for l in mod.splitlines()
                  if 'callPackage ../packages/nunba.nix' in l and 'literalExpression' not in l]
    assert len(real_calls) == 1, ('exactly one call site, in hart-nunba.nix (the option defaultText is documentation, not a call)', real_calls)
    assert re.search(r'package = lib\.mkOption \{\s*\n\s*type = lib\.types\.package;\s*\n\s*default = nunbaPkg;', mod), (
        'hart-nunba.nix must expose the package as hart.nunba.package with nunbaPkg as its default')
