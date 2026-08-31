"""An extension with a BAD signature must not load.

THE BUG. `core/platform/bootstrap.py::_verify_extension_signatures` opened
with:

    from security.master_key import verify_release

`verify_release` HAS NEVER EXISTED. There is no definition anywhere in the
tree. So the import raised ImportError for every extension on every boot, and
the handler was:

    except ImportError:
        logger.debug("master_key not available - skipping signature verification")

DEBUG level, then continue. The signature gate has never verified a signature.

What made it a hole rather than merely absent checking is that it was
ASYMMETRIC. The missing-signature branch runs BEFORE that import and still
works, so an extension with no manifest.sig was quarantined, while an
extension shipping ANY manifest.sig, including sixty-four bytes of garbage,
was never checked and loaded, even under HART_REQUIRE_SIGNED_EXTENSIONS=1.
Supplying a fake signature was strictly safer for an attacker than supplying
none, which is the exact inverse of what the gate is for.

These tests drive the real function against real Ed25519 signatures on real
files in a temp directory. The master public key is monkeypatched to a test
key so the positive path can be exercised without the production private key,
which lives only in CI and must never be needed to run a test.

Run:
  pytest tests/unit/test_extension_signature_gate.py -v
"""

import hashlib
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.platform.bootstrap import _verify_extension_signatures  # noqa: E402


@pytest.fixture
def test_key(monkeypatch):
    """Stand a known keypair in for the hardcoded master key."""
    priv = Ed25519PrivateKey.generate()
    import security.master_key as mk
    monkeypatch.setattr(mk, 'get_master_public_key', lambda: priv.public_key())
    return priv


def make_ext(root, name, manifest=None, sig_hex=None):
    """Write an extension dir; return its path."""
    d = os.path.join(str(root), name)
    os.makedirs(d, exist_ok=True)
    mpath = os.path.join(d, 'manifest.json')
    with open(mpath, 'w', encoding='utf-8') as f:
        json.dump(manifest if manifest is not None else {'name': name}, f)
    if sig_hex is not None:
        with open(os.path.join(d, 'manifest.sig'), 'w', encoding='utf-8') as f:
            f.write(sig_hex)
    return d


def sign_file(priv, path):
    """Sign exactly what the verifier checks: Ed25519 over sha256(file)."""
    with open(path, 'rb') as f:
        digest = hashlib.sha256(f.read()).digest()
    return priv.sign(digest).hex()


def names(root):
    return sorted(os.listdir(str(root)))


# -- the hole ---------------------------------------------------------------

def test_a_forged_signature_is_quarantined(tmp_path, test_key, monkeypatch):
    """THE REGRESSION TEST. Garbage in manifest.sig used to sail straight
    through. Under require_signed it must be quarantined."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    make_ext(tmp_path, 'evil', sig_hex='de' * 64)

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['evil.badsig'], (
        'an extension with a forged signature was left loadable')


def test_a_signature_from_the_wrong_key_is_quarantined(tmp_path, test_key,
                                                       monkeypatch):
    """Well-formed, but verifies against a DIFFERENT key. The realistic
    attack: the attacker signs with a key they control."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    d = make_ext(tmp_path, 'wrongkey', sig_hex='00')
    attacker = Ed25519PrivateKey.generate()
    sig = sign_file(attacker, os.path.join(d, 'manifest.json'))
    with open(os.path.join(d, 'manifest.sig'), 'w', encoding='utf-8') as f:
        f.write(sig)

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['wrongkey.badsig']


def test_a_tampered_manifest_breaks_its_own_signature(tmp_path, test_key,
                                                      monkeypatch):
    """Signed correctly, then the manifest is edited. The signature no longer
    covers the bytes on disk."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    d = make_ext(tmp_path, 'tampered')
    mpath = os.path.join(d, 'manifest.json')
    sig = sign_file(test_key, mpath)
    with open(os.path.join(d, 'manifest.sig'), 'w', encoding='utf-8') as f:
        f.write(sig)
    with open(mpath, 'w', encoding='utf-8') as f:
        json.dump({'name': 'tampered', 'permissions': ['all']}, f)

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['tampered.badsig']


# -- the gate must not break honest extensions ------------------------------

def test_a_genuine_signature_loads(tmp_path, test_key, monkeypatch):
    """The gate is worthless if it rejects real ones."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    d = make_ext(tmp_path, 'genuine')
    sig = sign_file(test_key, os.path.join(d, 'manifest.json'))
    with open(os.path.join(d, 'manifest.sig'), 'w', encoding='utf-8') as f:
        f.write(sig + '\n')       # a trailing newline is normal in a sig file

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['genuine'], 'a valid signature was rejected'


def test_advisory_mode_warns_but_still_loads(tmp_path, test_key, monkeypatch):
    """Without the opt-in flag the gate is advisory: it must NOT quarantine."""
    monkeypatch.delenv('HART_REQUIRE_SIGNED_EXTENSIONS', raising=False)
    make_ext(tmp_path, 'tolerated', sig_hex='de' * 64)

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['tolerated']


# -- the asymmetry itself ---------------------------------------------------

def test_a_forged_signature_is_no_safer_than_no_signature(tmp_path, test_key,
                                                          monkeypatch):
    """The shape of the old bug, stated directly. An extension with NO
    signature was quarantined; one with a FORGED signature was not. Both must
    now be treated the same."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    make_ext(tmp_path, 'nosig')                     # no manifest.sig at all
    make_ext(tmp_path, 'forged', sig_hex='de' * 64)

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['forged.badsig', 'nosig.unsigned'], (
        'carrying a forged signature must not be safer than carrying none')


def test_a_missing_verifier_fails_closed_when_signatures_are_required(
        tmp_path, monkeypatch):
    """Absence of a verifier is not evidence of a good signature. This is the
    precise handler that swallowed the bug for its whole life."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    make_ext(tmp_path, 'unverifiable', sig_hex='ab' * 64)

    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if 'extension_sandbox' in name:
            raise ImportError('no module named extension_sandbox')
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', blocked)
    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['unverifiable.badsig']


def test_a_binary_signature_file_fails_closed(tmp_path, test_key, monkeypatch):
    """The hole I nearly shipped in the FIX. manifest.sig is read as UTF-8
    text (hex). Raw bytes raise UnicodeDecodeError, which is not a False
    verdict -- it is a failure to verify. If that landed in a warn-only arm,
    an attacker shipping a BINARY signature would skip the check that a hex
    one fails, rebuilding the exact asymmetry this gate exists to remove."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    d = make_ext(tmp_path, 'binarysig')
    with open(os.path.join(d, 'manifest.sig'), 'wb') as f:
        f.write(bytes(range(256)) * 2)        # not decodable as UTF-8

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['binarysig.badsig']


def test_an_empty_signature_file_fails_closed(tmp_path, test_key, monkeypatch):
    """Zero bytes is not a signature."""
    monkeypatch.setenv('HART_REQUIRE_SIGNED_EXTENSIONS', '1')
    make_ext(tmp_path, 'emptysig', sig_hex='')

    _verify_extension_signatures(str(tmp_path))

    assert names(tmp_path) == ['emptysig.badsig']
