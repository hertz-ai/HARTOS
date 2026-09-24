"""hart-ota-sync-src rewrites `path:<root>?dir=<d>` to `path:<root>/<d>` before nix.

Nix 2.24 rejects the `dir` parameter on path: URLs ("path URL ... has
unsupported parameter 'dir'") while github: refs carry it; our OTA refs are
github:hertz-ai/HARTOS/<sha>?dir=nixos, so a path ref spelled the same way is
what an offline apply or the ota-central test hands the script. Measured on
the affa34f nixosTests run (2026-09-24): "cannot resolve source of
path:/tmp/newrepo?dir=nixos", nix's stderr hidden; reproduced on the Samsung
node with the shipped nix 2.24.14. These run the REAL case block, extracted
from the module and un-escaped, under a shell.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
MODULE = os.path.join(ROOT, 'nixos', 'modules', 'hart-ota.nix')


def _case_block() -> str:
    src = open(MODULE, encoding='utf-8').read()
    m = re.search(r'(      REF="\$FLAKE"\n      case "\$REF" in.*?\n      esac\n)', src, re.S)
    assert m, 'hart-ota-sync-src must normalise path:...?dir= refs before nix flake metadata'
    return m.group(1).replace("''${", "${")


def _shell():
    for name in ('bash', 'sh', 'dash'):
        p = shutil.which(name)
        if p:
            return p
    pytest.skip('no POSIX shell on this host')


@pytest.mark.parametrize('flake, expected', [
    ('path:/tmp/newrepo?dir=nixos', 'path:/tmp/newrepo/nixos'),
    ('path:/mnt/usb/HARTOS/?dir=nixos', 'path:/mnt/usb/HARTOS/nixos'),
    ('path:/tmp/newrepo?dir=nixos&rev=abc', 'path:/tmp/newrepo/nixos'),
    ('github:hertz-ai/HARTOS/68a2ff0?dir=nixos', 'github:hertz-ai/HARTOS/68a2ff0?dir=nixos'),
    ('path:/tmp/newrepo/nixos', 'path:/tmp/newrepo/nixos'),
])
def test_the_ref_nix_is_asked_about(flake, expected):
    script = "FLAKE='" + flake + "'\n" + _case_block() + 'printf %s "$REF"\n'
    out = subprocess.run([_shell(), '-c', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == expected
