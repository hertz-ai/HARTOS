"""The pip version cap has ONE source: pip-constraint.txt at the repo root.

pip 26.3 refuses requirements.txt's autobahn pin, so everything that upgrades
pip and then installs requirements.txt must stay below it.  Review of
HARTOS #168 (20 Nov): the cap was written in three places two ways
(pip<26.3, pip==26.2.2), and three installers had no cap at all.  Now CI
(PIP_CONSTRAINT), both Dockerfiles and both shell installers pass the file
with -c, and deploy/remote/install_remote.py reads it.  These fail on a
second copy of the cap or on an uncapped pip upgrade.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSTRAINT = ROOT / 'pip-constraint.txt'

# Files that upgrade pip and then install requirements.txt.
INSTALLERS = [
    'Dockerfile',
    'deploy/cloud/Dockerfile.prod',
    'deploy/linux/install.sh',
    'deploy/distro/build-iso.sh',
    'deploy/remote/install_remote.py',
]
# Workflows that install requirements.txt; each sets PIP_CONSTRAINT.
WORKFLOWS = ['coverage-baseline', 'flake-checks', 'nix-check', 'release',
             'security-scan']

_PIP_VERSION_LITERAL = re.compile(r'\bpip\s*(==|<=|<|~=|>=|!=)\s*\d')
# A pip upgrade: "install --upgrade pip ..." or install_remote's
# 'install --upgrade "{_pip_cap()}"'.
_PIP_UPGRADE = re.compile(r'install\s+(--upgrade|-U)\s+["\']?(pip\b|\{_pip_cap\(\)\})')


def _cap_line():
    lines = [ln.strip() for ln in CONSTRAINT.read_text(encoding='utf-8').splitlines()]
    return [ln for ln in lines if ln and not ln.startswith('#')][-1]


def test_install_remote_sends_the_cap_from_the_file():
    """Behaviour: the remote installer's commands carry the file's cap."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'install_remote_under_test', ROOT / 'deploy/remote/install_remote.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._pip_cap() == _cap_line()
    assert _cap_line().startswith('pip')


def test_source_guard_no_second_copy_of_the_pip_cap():
    for rel in INSTALLERS + [f'.github/workflows/{w}.yml' for w in WORKFLOWS]:
        for n, line in enumerate((ROOT / rel).read_text(encoding='utf-8').splitlines(), 1):
            assert not _PIP_VERSION_LITERAL.search(line), (
                f'{rel}:{n} writes a pip version; the cap lives only in '
                f'pip-constraint.txt: {line.strip()}')


def test_source_guard_every_installer_pip_upgrade_is_capped():
    for rel in INSTALLERS:
        text = (ROOT / rel).read_text(encoding='utf-8')
        upgrades = [ln for ln in text.splitlines() if _PIP_UPGRADE.search(ln)]
        assert upgrades, f'{rel} no longer upgrades pip; update this test'
        for line in upgrades:
            assert ('pip-constraint.txt' in line or '_pip_cap()' in line), (
                f'{rel} upgrades pip without the cap: {line.strip()}')


def test_source_guard_every_workflow_points_at_the_one_file():
    for w in WORKFLOWS:
        text = (ROOT / f'.github/workflows/{w}.yml').read_text(encoding='utf-8')
        m = re.search(r'PIP_CONSTRAINT:\s*\$\{\{\s*github\.workspace\s*\}\}/(\S+)', text)
        assert m, f'{w}.yml sets no PIP_CONSTRAINT'
        assert m.group(1) == 'pip-constraint.txt', f'{w}.yml points at {m.group(1)}'
