"""The appliance's Python must carry lxml, or every outreach email is refused.

The outreach blank check (integrations/agent_engine outreach send path, HARTOS
52c3be2b3) reads an email through lxml and refuses to send when lxml is
missing ("lxml is not installed").  requirements.txt pins lxml==6.1.2 for
the desktop and central, but the NixOS appliance builds its interpreter from
nixos/packages/hart-app.nix's pythonEnv, which did not list it, and none of
the packages it lists pull it in (measured 2026-11-23, FAB-Q2).  So on the
appliance every outreach email was refused.

This reads the package list as text: nix is not available on the Windows
test hosts, so the derivation itself is not evaluated here.
"""
import os
import re

_NIX = os.path.join(os.path.dirname(__file__), '..', '..',
                    'nixos', 'packages', 'hart-app.nix')


def _python_env_packages():
    with open(_NIX, encoding='utf-8') as f:
        text = f.read()
    start = text.index('pythonEnv = python.withPackages')
    body = text[text.index('[', start) + 1:text.index(']);', start)]
    names = set()
    for line in body.splitlines():
        line = line.split('#', 1)[0].strip()
        for word in re.findall(r'[A-Za-z0-9_.-]+', line):
            names.add(word)
    return names


def test_the_appliance_python_lists_lxml():
    assert 'lxml' in _python_env_packages()


def test_the_list_is_read_from_the_real_block():
    # Guard the reader itself: packages the file has always listed are seen,
    # so a pass above is not a reader that sees nothing (or everything).
    names = _python_env_packages()
    assert {'flask', 'beautifulsoup4', 'sqlalchemy'} <= names
    assert 'withPackages' not in names
