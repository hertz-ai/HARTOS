"""An unconfigured node does not accept unsigned handshakes.

``security.master_key.get_enforcement_mode`` is the one resolver for this
node's posture and it defaults to 'hard', documented as the correct default
for Sybil resistance.  Both PeerLink handshake checks read the environment
variable directly instead -- ``os.environ.get('HEVOLVE_ENFORCEMENT_MODE') ==
'hard'`` -- which inverts that default: unset means the comparison is False,
so an unsigned handshake was accepted on exactly the nodes nobody had
configured.

Measured 2026-09-21: the variable is unset in all three scopes on the owner's
main desktop, and unset on the laptop at 192.168.0.15 whose node accepted the
owner's phone.  Every other caller of get_enforcement_mode() on those same
machines was reading 'hard' at the time.

    python -m pytest tests/unit/test_peer_link_enforcement_default.py -q
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from core.peer_link import link as link_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    """The state both real machines were in: nothing set anywhere."""
    monkeypatch.delenv('HEVOLVE_ENFORCEMENT_MODE', raising=False)


def test_an_unconfigured_node_is_hard():
    assert link_mod._enforcement_mode() == 'hard'
    assert link_mod._enforcement_is_hard() is True, (
        'with nothing configured the node must take the documented default, '
        'not the weakest posture')


@pytest.mark.parametrize('mode,hard', [('hard', True), ('soft', False),
                                       ('warn', False), ('off', False)])
def test_an_explicit_mode_is_honoured(monkeypatch, mode, hard):
    monkeypatch.setenv('HEVOLVE_ENFORCEMENT_MODE', mode)
    assert link_mod._enforcement_is_hard() is hard


def test_a_nonsense_mode_is_hard(monkeypatch):
    monkeypatch.setenv('HEVOLVE_ENFORCEMENT_MODE', 'banana')
    assert link_mod._enforcement_is_hard() is True


def test_an_unreadable_resolver_is_hard():
    """Fail-CLOSED: a node that cannot read its own posture must not assume
    the weakest one.  The opposite of the capacity checks in dispatch, and
    deliberately so -- skipping a tick is cheap, admitting a stranger is
    not."""
    with patch.dict(sys.modules, {'security.master_key': None}):
        assert link_mod._enforcement_is_hard() is True


def test_it_asks_the_canonical_resolver_not_the_environment(monkeypatch):
    """The whole defect was a second reading of one setting.  If the resolver
    says 'soft', this module must say soft even when the variable says
    otherwise, because the resolver is where the default and the validation
    live."""
    monkeypatch.setenv('HEVOLVE_ENFORCEMENT_MODE', 'hard')
    with patch('security.master_key.get_enforcement_mode',
               return_value='soft'):
        assert link_mod._enforcement_is_hard() is False


def test_source_guard_no_direct_environment_read_remains():
    """Labelled source guard, beside the behavioural tests above rather than
    instead of them: the defect was two call sites reading the variable
    directly, and a behavioural test cannot see a third one being added.

    Parsed, not grepped.  A string count flagged this module's own docstring,
    which explains the defect by quoting it -- the guard would have banned
    writing down what went wrong.  An ast walk sees CALLS.
    """
    import ast
    src = os.path.join(_ROOT, 'core', 'peer_link', 'link.py')
    with open(src, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        reads_env = (
            isinstance(fn, ast.Attribute) and fn.attr in ('get', 'getenv')
            and 'environ' in ast.dump(fn.value))
        if not reads_env:
            continue
        for arg in node.args:
            if (isinstance(arg, ast.Constant)
                    and arg.value == 'HEVOLVE_ENFORCEMENT_MODE'):
                offenders.append(node.lineno)

    assert offenders == [], (
        f'link.py reads HEVOLVE_ENFORCEMENT_MODE directly at line(s) '
        f'{offenders}; ask _enforcement_mode() instead, so the documented '
        f"'hard' default and the value validation apply")
