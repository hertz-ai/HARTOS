"""An unverifiable beacon signature must not read as a valid one.

MEASURED 2026-09-22, live A/B on the real _parse_beacon with a real packet:

    OLD (committed) -> ADMITTED 'attacker-node-0001'
    NEW             -> REFUSED {}

The signature check ended in ``except Exception: pass``, so a public key the
verifier could not parse RAISED, the exception was swallowed, and execution
fell through to ``return payload`` -- admitted, unverified, and with no log
line saying so. The key and signature both come from the packet, so that
input is attacker-controlled: sending a deliberately malformed key was enough
to skip verification entirely.

It became load-bearing because of my own change. d6c686197 demoted the beacon
code-hash gate from an admission GATE to a trust SIGNAL -- correctly, it was
refusing honest peers for running a build this node did not recognise -- which
left this signature check as the ONLY thing in front of admission. The silence
went from bad to the last line of defence. Found by hartos-14.

The fix keeps the two kinds of failure apart, because they want opposite
answers and folding them is how this class of defect is born:

  * the PACKET's key/signature makes the verifier raise -> the sender's
    malformed input, evidence of badness -> REFUSE
  * OUR verifier is missing (ImportError) -> a local packaging fault, absence
    of evidence -> admit unverified and SAY SO, because refusing here would
    partition this node off its own LAN over a missing dependency

    python -m pytest tests/unit/test_beacon_signature_cannot_be_skipped.py -q
"""
import json
import os
import sys
import time
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.social import peer_discovery as pd  # noqa: E402
import security.node_integrity as ni  # noqa: E402


@pytest.fixture
def disc():
    """A real AutoDiscovery, without running its __init__ (sockets, threads)."""
    d = pd.AutoDiscovery.__new__(pd.AutoDiscovery)
    d._gossip = types.SimpleNamespace(node_id='me-local')
    return d


def packet(**over):
    """A real beacon packet of the shape the UDP socket receives."""
    payload = {'type': 'hevolve-discovery', 'node_id': 'attacker-node-0001',
               'timestamp': time.time(),
               'public_key': 'k', 'signature': 's'}
    payload.update(over)
    return pd.AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')


def _verifier(monkeypatch, behaviour):
    monkeypatch.setattr(ni, 'verify_json_signature', behaviour)


class TestTheBypassThisFixes:
    def test_a_raising_verifier_refuses_the_beacon(self, disc, monkeypatch):
        """THE regression. A malformed key from the packet used to be admitted."""
        def boom(*a, **k):
            raise ValueError('malformed public key supplied by the packet')
        _verifier(monkeypatch, boom)
        assert disc._parse_beacon(packet()) == {}

    def test_it_says_which_kind_of_no_it_is(self, disc, monkeypatch, caplog):
        def boom(*a, **k):
            raise ValueError('malformed public key')
        _verifier(monkeypatch, boom)
        with caplog.at_level('WARNING'):
            disc._parse_beacon(packet())
        joined = ' '.join(r.message for r in caplog.records).lower()
        assert 'raised' in joined, 'a silent skip is what the defect WAS'
        assert 'refus' in joined

    def test_an_invalid_signature_still_refuses(self, disc, monkeypatch):
        """The pre-existing path, so the fix cannot be read as replacing it."""
        _verifier(monkeypatch, lambda *a, **k: False)
        assert disc._parse_beacon(packet()) == {}

    def test_a_valid_signature_is_admitted(self, disc, monkeypatch):
        """Prove the instrument sees the POSITIVE case: this must not refuse
        everything and pass for the wrong reason."""
        _verifier(monkeypatch, lambda *a, **k: True)
        out = disc._parse_beacon(packet())
        assert out.get('node_id') == 'attacker-node-0001'


class TestOurOwnFaultMustNotPartitionUs:
    def test_a_missing_verifier_admits_unverified_and_logs_loudly(
            self, disc, monkeypatch, caplog):
        """Security must not partition the system: refuse on EVIDENCE of
        badness, never on absence of evidence. A missing local dependency is
        our fault, not the peer's, and must not cut this node off its LAN."""
        real_import = __builtins__['__import__'] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def no_verifier(name, *a, **k):
            if name == 'security.node_integrity':
                raise ImportError('simulated: cryptography not installed')
            return real_import(name, *a, **k)

        monkeypatch.setattr('builtins.__import__', no_verifier)
        with caplog.at_level('ERROR'):
            out = disc._parse_beacon(packet())
        monkeypatch.undo()

        assert out.get('node_id') == 'attacker-node-0001', (
            'refusing on a local packaging fault partitions this node')
        joined = ' '.join(r.message for r in caplog.records).lower()
        assert 'not verified' in joined or 'unavailable' in joined, (
            'admitting unverified in SILENCE is the same defect again')


def test_the_gulp_has_not_come_back():
    """Source guard, labelled as such and not the only test here.

    A behavioural test cannot catch the gulp being reintroduced somewhere
    ELSE in this function, and the defect's whole nature was silence.
    """
    import inspect
    src = inspect.getsource(pd.AutoDiscovery._parse_beacon)
    assert 'except Exception:\n                pass' not in src, (
        'a bare pass returned to _parse_beacon; an unverifiable signature '
        'would read as a valid one again')
