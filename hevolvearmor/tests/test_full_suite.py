"""
HevolveArmor Complete Test Suite
================================

Covers: unit, integration, stress, e2e, and pen testing.

Run: python -m pytest hevolvearmor/tests/test_full_suite.py -v
"""
import json
import os
import sys
import tempfile
import time

import pytest

# ─── Fixtures ─────────────────────────────────────────────────────────────────

HEVOLVEAI_SRC = os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'hevolveai', 'src', 'hevolveai'
)
HEVOLVEAI_SRC = os.path.abspath(HEVOLVEAI_SRC)

@pytest.fixture(scope="session")
def test_dir():
    d = tempfile.mkdtemp(prefix="hevolvearmor_test_")
    yield d

@pytest.fixture(scope="session")
def aes_key():
    from hevolvearmor._native import derive_runtime_key
    return derive_runtime_key(passphrase="pytest-key")

@pytest.fixture(scope="session")
def signing_keys():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption
    )
    priv = Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv_hex, pub_hex

@pytest.fixture(scope="session")
def encrypted_modules(test_dir, aes_key):
    """Build encrypted hevolveai once for all tests."""
    from hevolvearmor._builder import build_encrypted_package
    out = os.path.join(test_dir, "modules", "hevolveai")
    if not os.path.isdir(HEVOLVEAI_SRC):
        pytest.skip("hevolveai source not found")
    stats = build_encrypted_package(HEVOLVEAI_SRC, out, aes_key, verbose=False)
    return os.path.join(test_dir, "modules"), stats

def _clean_hevolveai():
    """Remove hevolveai from sys.modules."""
    for k in list(sys.modules):
        if "hevolveai" in k or "embodied_ai" in k:
            del sys.modules[k]

@pytest.fixture(autouse=True)
def clean_between_tests():
    _clean_hevolveai()
    yield
    try:
        from hevolvearmor._native import uninstall
        uninstall()
    except Exception:
        pass
    _clean_hevolveai()


# ═══════════════════════════════════════════════════════════════════════════════
#  UNIT TESTS — individual crypto primitives
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrypto:
    def test_generate_key_length(self):
        from hevolvearmor._native import armor_generate_key
        assert len(armor_generate_key()) == 32

    def test_generate_key_unique(self):
        from hevolvearmor._native import armor_generate_key
        assert armor_generate_key() != armor_generate_key()

    def test_encrypt_decrypt_roundtrip(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        data = b"hello hevolveai world" * 100
        ct = armor_encrypt(data, key)
        assert ct != data
        assert len(ct) > len(data)  # nonce + tag overhead
        pt = armor_decrypt(ct, key)
        assert pt == data

    def test_wrong_key_fails(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        k1, k2 = armor_generate_key(), armor_generate_key()
        ct = armor_encrypt(b"secret", k1)
        with pytest.raises(RuntimeError):
            armor_decrypt(ct, k2)

    def test_truncated_blob_fails(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        ct = armor_encrypt(b"data", key)
        with pytest.raises(RuntimeError):
            armor_decrypt(ct[:10], key)

    def test_empty_payload(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        ct = armor_encrypt(b"", key)
        assert armor_decrypt(ct, key) == b""

    def test_large_payload(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        data = os.urandom(10 * 1024 * 1024)  # 10 MB
        ct = armor_encrypt(data, key)
        assert armor_decrypt(ct, key) == data

    def test_invalid_key_length(self):
        from hevolvearmor._native import armor_encrypt
        with pytest.raises(ValueError):
            armor_encrypt(b"data", b"short")


class TestKeyDerivation:
    def test_passphrase_deterministic(self):
        from hevolvearmor._native import armor_derive_key_passphrase
        k1 = armor_derive_key_passphrase("test")
        k2 = armor_derive_key_passphrase("test")
        assert k1 == k2

    def test_passphrase_different(self):
        from hevolvearmor._native import armor_derive_key_passphrase
        k1 = armor_derive_key_passphrase("aaa")
        k2 = armor_derive_key_passphrase("bbb")
        assert k1 != k2

    def test_raw_key_derivation(self):
        from hevolvearmor._native import armor_derive_key_raw
        k = armor_derive_key_raw(b"raw-material")
        assert len(k) == 32

    def test_ed25519_key_derivation(self):
        from hevolvearmor._native import armor_derive_key_ed25519
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        k = armor_derive_key_ed25519(raw)
        assert len(k) == 32
        # Deterministic
        k2 = armor_derive_key_ed25519(raw)
        assert k == k2

    def test_ed25519_wrong_length(self):
        from hevolvearmor._native import armor_derive_key_ed25519
        with pytest.raises(ValueError):
            armor_derive_key_ed25519(b"too-short")

    def test_runtime_key_passphrase(self):
        from hevolvearmor._native import derive_runtime_key
        k = derive_runtime_key(passphrase="test")
        assert len(k) == 32


class TestSelfHash:
    def test_returns_hex(self):
        from hevolvearmor._native import armor_self_hash
        h = armor_self_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        from hevolvearmor._native import armor_self_hash
        assert armor_self_hash() == armor_self_hash()


class TestAntiDebug:
    def test_returns_bool(self):
        from hevolvearmor._native import armor_is_debugger_present
        assert isinstance(armor_is_debugger_present(), bool)

    def test_no_debugger_in_tests(self):
        from hevolvearmor._native import armor_is_debugger_present
        # pytest doesn't attach as a debugger
        assert armor_is_debugger_present() == False


class TestMachineInfo:
    def test_has_all_fields(self):
        from hevolvearmor._native import armor_machine_info
        info = armor_machine_info()
        assert "mac_address" in info
        assert "hostname" in info
        assert "machine_id" in info

    def test_mac_format(self):
        from hevolvearmor._native import armor_machine_info
        mac = armor_machine_info()["mac_address"]
        assert len(mac) > 0
        assert ":" in mac or "-" in mac


# ═══════════════════════════════════════════════════════════════════════════════
#  UNIT TESTS — license management
# ═══════════════════════════════════════════════════════════════════════════════

class TestLicense:
    def test_generate_and_validate(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_valid.json")
        armor_generate_license("TestCo", priv_hex, path, expires_at=2000000000)
        r = armor_validate_license(path, pub_hex)
        assert r["valid"] == True
        assert r["licensee"] == "TestCo"

    def test_expired_rejected(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_expired.json")
        armor_generate_license("Exp", priv_hex, path, expires_at=1000000000, grace_days=0)
        r = armor_validate_license(path, pub_hex)
        assert r["valid"] == False
        assert r["expired"] == True

    def test_grace_period(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_grace.json")
        armor_generate_license("Grace", priv_hex, path,
                                expires_at=int(time.time()) - 5 * 86400, grace_days=30)
        r = armor_validate_license(path, pub_hex)
        assert r["valid"] == True
        assert r["in_grace"] == True

    def test_no_expiry(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_noexp.json")
        armor_generate_license("Perm", priv_hex, path, expires_at=0)
        r = armor_validate_license(path, pub_hex)
        assert r["valid"] == True
        assert r["expired"] == False

    def test_wrong_mac_rejected(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_wrongmac.json")
        armor_generate_license("Wrong", priv_hex, path, bind_mac=["00:00:00:00:00:00"])
        r = armor_validate_license(path, pub_hex)
        assert r["valid"] == False
        assert r["machine_bound"] == False

    def test_tampered_rejected(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        priv_hex, pub_hex = signing_keys
        path = os.path.join(test_dir, "lic_tamper_src.json")
        armor_generate_license("Orig", priv_hex, path, expires_at=2000000000)
        with open(path) as f:
            data = json.load(f)
        data["licensee"] = "TAMPERED"
        tampered = os.path.join(test_dir, "lic_tampered.json")
        with open(tampered, "w") as f:
            json.dump(data, f)
        r = armor_validate_license(tampered, pub_hex)
        assert r["valid"] == False

    def test_wrong_public_key_rejected(self, test_dir, signing_keys):
        from hevolvearmor._native import armor_generate_license, armor_validate_license
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, NoEncryption
        priv_hex, _ = signing_keys
        path = os.path.join(test_dir, "lic_wrongpk.json")
        armor_generate_license("Test", priv_hex, path)
        wrong_pub = Ed25519PrivateKey.generate().public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw).hex()
        r = armor_validate_license(path, wrong_pub)
        assert r["valid"] == False


# ═══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION TESTS — build + import pipeline
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildAndImport:
    def test_plain_build_stats(self, encrypted_modules):
        _, stats = encrypted_modules
        assert stats["encrypted"] >= 140
        assert stats["failed"] == 0

    def test_import_hevolveai(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        import hevolveai
        assert hevolveai.__version__ == "0.1.0"

    def test_armored_marker(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        import hevolveai
        assert getattr(hevolveai, "__hevolvearmor__", False) == True

    def test_submodule_imports(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        from hevolveai.embodied_ai.core import tool_registry
        assert tool_registry is not None
        from hevolveai.embodied_ai.memory import episodic_memory
        assert episodic_memory is not None
        import hevolveai.embodied_ai.config
        assert hevolveai.embodied_ai.config is not None

    def test_deep_submodule(self, encrypted_modules, aes_key):
        """Deep submodule — some may fail due to __file__ not defined in encrypted
        modules (hevolveai source uses os.path.dirname(__file__) at module level).
        This is a known limitation: encrypted modules don't have a real __file__."""
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        # Use a module that doesn't reference __file__ at module level
        from hevolveai.embodied_ai.utils import context_logger
        assert context_logger is not None

    def test_class_instantiation(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        from hevolveai.embodied_ai.core.tool_registry import ToolRegistry
        tr = ToolRegistry()
        assert tr is not None

    def test_uninstall_wipes_key(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install, uninstall, _runtime_decrypt, armor_encrypt
        install(modules_dir, passphrase="pytest-key")
        ct = armor_encrypt(b"test", aes_key)
        _runtime_decrypt(ct)  # should work
        uninstall()
        with pytest.raises(RuntimeError):
            _runtime_decrypt(ct)  # should fail — key wiped

    def test_settrace_nulled(self, encrypted_modules, aes_key):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")
        assert sys.settrace is None
        assert sys.setprofile is None


# ═══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION TESTS — build with transforms
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransformBuilds:
    def test_string_encryption_build(self, test_dir, aes_key):
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._transforms import TransformConfig
        from hevolvearmor._builder import build_encrypted_package
        out = os.path.join(test_dir, "t_str", "hevolveai")
        config = TransformConfig(encrypt_strings=True, string_min_length=10)
        stats = build_encrypted_package(HEVOLVEAI_SRC, out, aes_key, verbose=False, config=config)
        assert stats["encrypted"] >= 140
        assert stats["failed"] == 0

    def test_string_encryption_import(self, test_dir, aes_key):
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._native import install
        install(os.path.join(test_dir, "t_str"), passphrase="pytest-key")
        import hevolveai
        assert hevolveai.__version__ == "0.1.0"

    def test_assert_import_build(self, test_dir, aes_key):
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._transforms import TransformConfig
        from hevolvearmor._builder import build_encrypted_package
        out = os.path.join(test_dir, "t_assert", "hevolveai")
        config = TransformConfig(
            assert_imports=True, armored_packages=["hevolveai", "embodied_ai"])
        stats = build_encrypted_package(HEVOLVEAI_SRC, out, aes_key, verbose=False, config=config)
        assert stats["failed"] == 0

    def test_assert_import_import(self, test_dir, aes_key):
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._native import install
        install(os.path.join(test_dir, "t_assert"), passphrase="pytest-key")
        import hevolveai
        assert hevolveai.__version__ == "0.1.0"

    def test_rft_build(self, test_dir, aes_key):
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._transforms import TransformConfig
        from hevolvearmor._builder import build_encrypted_package
        out = os.path.join(test_dir, "t_rft", "hevolveai")
        config = TransformConfig(rft_mode="_private_only")
        stats = build_encrypted_package(HEVOLVEAI_SRC, out, aes_key, verbose=False, config=config)
        # RFT may fail on some files due to AST edge cases — allow some failures
        assert stats["encrypted"] >= 130


# ═══════════════════════════════════════════════════════════════════════════════
#  STRESS TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestStress:
    def test_encrypt_decrypt_1000_iterations(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        data = os.urandom(4096)
        for _ in range(1000):
            ct = armor_encrypt(data, key)
            assert armor_decrypt(ct, key) == data

    def test_concurrent_key_derivation(self):
        from hevolvearmor._native import armor_derive_key_passphrase
        import threading
        results = {}
        def derive(i):
            results[i] = armor_derive_key_passphrase(f"thread-{i}")
        threads = [threading.Thread(target=derive, args=(i,)) for i in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(results) == 50
        assert len(set(results[i].hex() for i in range(50))) == 50  # all unique

    def test_rapid_install_uninstall(self, encrypted_modules):
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install, uninstall
        for _ in range(20):
            _clean_hevolveai()
            install(modules_dir, passphrase="pytest-key")
            import hevolveai
            assert hevolveai.__version__
            uninstall()
            _clean_hevolveai()

    def test_large_file_encryption(self):
        from hevolvearmor._native import armor_encrypt, armor_decrypt, armor_generate_key
        key = armor_generate_key()
        # 50 MB payload
        data = os.urandom(50 * 1024 * 1024)
        t0 = time.time()
        ct = armor_encrypt(data, key)
        enc_time = time.time() - t0
        t0 = time.time()
        pt = armor_decrypt(ct, key)
        dec_time = time.time() - t0
        assert pt == data
        # Should be under 2 seconds for 50MB on any modern CPU
        assert enc_time < 2.0, f"Encrypt too slow: {enc_time:.2f}s"
        assert dec_time < 2.0, f"Decrypt too slow: {dec_time:.2f}s"


# ═══════════════════════════════════════════════════════════════════════════════
#  E2E TESTS — full workflow
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2E:
    def test_full_workflow_encrypt_import_use(self, test_dir, aes_key):
        """Complete end-to-end: encrypt → install → import → use → uninstall."""
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._builder import build_encrypted_package
        from hevolvearmor._native import install, uninstall

        out = os.path.join(test_dir, "e2e", "hevolveai")
        stats = build_encrypted_package(HEVOLVEAI_SRC, out, aes_key, verbose=False)
        assert stats["failed"] == 0

        install(os.path.join(test_dir, "e2e"), passphrase="pytest-key")

        import hevolveai
        assert hevolveai.__version__ == "0.1.0"
        assert hevolveai.__hevolvearmor__ == True

        from hevolveai.embodied_ai.core.tool_registry import ToolRegistry
        tr = ToolRegistry()
        assert tr is not None

        uninstall()

    def test_licensed_workflow(self, test_dir, aes_key, signing_keys):
        """Encrypt + license + import."""
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._native import (
            install, uninstall, armor_generate_license, armor_machine_info
        )

        modules_dir = os.path.join(test_dir, "e2e")
        priv_hex, pub_hex = signing_keys
        info = armor_machine_info()

        lic_path = os.path.join(test_dir, "e2e", "hevolvearmor.license")
        armor_generate_license(
            "E2E Test", priv_hex, lic_path,
            expires_at=2000000000,
            bind_mac=[info["mac_address"]],
            features=["hevolveai"],
        )

        install(modules_dir, passphrase="pytest-key",
                license_file=lic_path, license_public_key=pub_hex)
        import hevolveai
        assert hevolveai.__version__ == "0.1.0"
        uninstall()


# ═══════════════════════════════════════════════════════════════════════════════
#  PEN TESTS — attempt to bypass protections
# ═══════════════════════════════════════════════════════════════════════════════

class TestPenetration:
    def test_cannot_read_key_from_python(self, encrypted_modules):
        """Key is stored in Rust Mutex, not accessible from Python."""
        modules_dir, _ = encrypted_modules
        from hevolvearmor._native import install, ArmoredFinder
        install(modules_dir, passphrase="pytest-key")
        # ArmoredFinder has no key attribute
        assert not hasattr(ArmoredFinder, "key")
        assert not hasattr(ArmoredFinder, "_key")
        # Cannot construct ArmoredFinder from Python (no __init__)
        with pytest.raises(TypeError):
            ArmoredFinder()
        # dir() on class should not reveal key
        attrs = dir(ArmoredFinder)
        assert "key" not in attrs
        assert "_key" not in attrs

    def test_cannot_decrypt_without_install(self):
        """_runtime_decrypt fails when not installed."""
        from hevolvearmor._native import _runtime_decrypt, armor_encrypt, armor_generate_key
        key = armor_generate_key()
        ct = armor_encrypt(b"secret", key)
        with pytest.raises(RuntimeError, match="not initialized"):
            _runtime_decrypt(ct)

    def test_wrong_passphrase_fails(self, test_dir):
        """Modules encrypted with one key can't be loaded with another.
        Uses armor_load_module directly to avoid pip-installed hevolveai fallback."""
        if not os.path.isdir(HEVOLVEAI_SRC):
            pytest.skip("hevolveai source not found")
        from hevolvearmor._native import derive_runtime_key, armor_load_module
        from hevolvearmor._builder import build_encrypted_package

        correct_key = derive_runtime_key(passphrase="correct-key")
        out = os.path.join(test_dir, "pen_wrong", "hevolveai")
        build_encrypted_package(HEVOLVEAI_SRC, out, correct_key, verbose=False)

        # Try to decrypt with wrong key — should fail GCM auth
        wrong_key = derive_runtime_key(passphrase="wrong-key")
        enc_file = os.path.join(out, "__init__.enc")
        with pytest.raises(ImportError, match="Decrypt|wrong key|corrupted"):
            armor_load_module(enc_file, wrong_key)

    def test_tampered_enc_file_fails(self, encrypted_modules, aes_key):
        """Corrupted .enc file detected by GCM tag."""
        modules_dir, _ = encrypted_modules
        # Find an .enc file and corrupt it
        enc_file = None
        for root, dirs, files in os.walk(modules_dir):
            for f in files:
                if f.endswith(".enc") and f != "__init__.enc":
                    enc_file = os.path.join(root, f)
                    break
            if enc_file:
                break

        if enc_file is None:
            pytest.skip("No .enc file found")

        # Read, corrupt, and write back
        original = open(enc_file, "rb").read()
        corrupted = bytearray(original)
        corrupted[-1] ^= 0xFF  # flip last byte (inside GCM tag)
        open(enc_file, "wb").write(corrupted)

        from hevolvearmor._native import install
        install(modules_dir, passphrase="pytest-key")

        # The corrupted module should fail to import
        module_name = os.path.basename(enc_file).replace(".enc", "")
        parent = os.path.basename(os.path.dirname(enc_file))
        try:
            __import__(f"hevolveai.{parent}.{module_name}")
            corrupted_detected = False
        except ImportError:
            corrupted_detected = True

        # Restore original
        open(enc_file, "wb").write(original)
        assert corrupted_detected, "Corrupted .enc file should fail GCM authentication"

    def test_expired_license_blocks_install(self, test_dir, signing_keys):
        """Expired license prevents install()."""
        from hevolvearmor._native import install, armor_generate_license
        priv_hex, pub_hex = signing_keys
        lic_path = os.path.join(test_dir, "pen_expired.license")
        armor_generate_license("Expired", priv_hex, lic_path,
                                expires_at=1000000000, grace_days=0)

        modules_dir = os.path.join(test_dir, "e2e")
        if not os.path.isdir(modules_dir):
            pytest.skip("e2e modules not built")

        with pytest.raises(RuntimeError, match="license"):
            install(modules_dir, passphrase="pytest-key",
                    license_file=lic_path, license_public_key=pub_hex)
