"""The Teams adapter's library ships wherever HARTOS is installed.

integrations/channels/extensions/teams_adapter.py imports botbuilder.core,
botbuilder.schema and botbuilder.core.teams (the Microsoft Bot Framework
SDK, distribution botbuilder-core).  Without them TeamsAdapter raises
"botbuilder not installed", so the owner's answer to the Teams card ends
409 "register_channel returned False" (review of 0d51bd263, rv_chan: the
HARTOS venv and the installed Nunba both lacked it).  It was declared
nowhere.  Each way HARTOS ships reads its own list:

  * requirements.txt: desktop and central.  Central's Dockerfile installs it
    with --no-deps, so every package of botbuilder-core's closure that
    nothing else pins is pinned here too.  botframework-connector imports
    aiohttp through msrest without declaring it; requirements.txt pins
    aiohttp already.
  * pyproject.toml / setup.py: a pip install of hart-backend.
  * nixos/packages/hart-app.nix pythonEnv: the appliance.  botbuilder is not
    in its nixpkgs pin, so it is built from PyPI's wheels there.

Read as text: nix is not available on the Windows hosts, so the derivation
is not evaluated here (as test_appliance_python_env_carries_lxml).
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
_VERSION = '4.17.1'
#: botbuilder-core's closure (pip's resolution against requirements.txt's
#: pins, 2026-11-23): what requirements.txt must pin for a --no-deps install.
_CLOSURE = {
    'botbuilder-core': _VERSION,
    'botbuilder-schema': _VERSION,
    'botframework-connector': _VERSION,
    'botframework-streaming': _VERSION,
    'msrest': '0.7.1',
    'msal': '1.39.0',
    'azure-core': '1.41.0',
    'isodate': '0.7.2',
    'jsonpickle': '3.4.2',
    'requests-oauthlib': '2.0.0',
    'oauthlib': '4.0.1',
}


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _requirement_pins():
    pins = {}
    for line in _read('requirements.txt').splitlines():
        line = line.split('#', 1)[0].strip()
        m = re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;]+)', line)
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def _named_in(text, block_start):
    # The list's own closing line: an entry can hold a ']' (autobahn[...]).
    start = text.index(block_start)
    body = text[start:text.index('\n]', start)]
    return {_norm(m) for m in re.findall(r'"([A-Za-z0-9_.-]+)', body)}


def test_requirements_pin_the_teams_library_and_its_closure():
    pins = _requirement_pins()
    missing = {n: v for n, v in _CLOSURE.items() if pins.get(n) != v}
    assert not missing, missing


def test_requirements_still_pin_what_botframework_imports_unasked():
    # msrest's aiohttp pipeline is imported by botframework.connector.aio.
    assert 'aiohttp' in _requirement_pins()


def test_a_pip_install_of_hart_backend_brings_it():
    assert 'botbuilder-core' in _named_in(_read('pyproject.toml'),
                                          'dependencies = [')
    assert 'botbuilder-core' in _named_in(_read('setup.py'),
                                          'install_requires = [')


def _nix_python_env():
    text = _read('nixos', 'packages', 'hart-app.nix')
    start = text.index('pythonEnv = python.withPackages')
    body = text[text.index('[', start) + 1:text.index(']);', start)]
    names = set()
    for line in body.splitlines():
        line = line.split('#', 1)[0].strip()
        names.update(re.findall(r'[A-Za-z0-9_.-]+', line))
    return text, names


def test_the_appliance_python_carries_it():
    text, names = _nix_python_env()
    assert 'botbuilderCorePkg' in names
    # Each of the four Bot Framework packages is built at the pinned
    # version from its PyPI wheel, by its sha256.
    for pname in ('botbuilder-core', 'botbuilder-schema',
                  'botframework-connector', 'botframework-streaming'):
        m = re.search(r'pname = "%s";\s*version = "([^"]+)";' % pname, text)
        assert m and m.group(1) == _VERSION, pname
        wheel = pname.replace('-', '_') + '-' + _VERSION + '-'
        assert re.search(r'url = "https://files\.pythonhosted\.org/'
                         r'packages/[0-9a-f/]+/' + re.escape(wheel), text), pname
    assert len(re.findall(r'hash = "sha256-[A-Za-z0-9+/]{43}=";', text)) >= 6


def test_the_readers_see_the_real_blocks():
    # Guard the readers: packages the files have always listed are seen.
    _text, names = _nix_python_env()
    assert {'flask', 'aiohttp', 'sqlalchemy'} <= names
    assert 'withPackages' not in names
    assert {'aiohttp', 'dnspython', 'livekit-api'} <= _named_in(
        _read('pyproject.toml'), 'dependencies = [')
    assert {'aiohttp', 'dnspython'} <= _named_in(
        _read('setup.py'), 'install_requires = [')
    assert {'aiohttp', 'requests', 'pyjwt'} <= set(_requirement_pins())
