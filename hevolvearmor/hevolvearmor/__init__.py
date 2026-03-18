"""
HevolveArmor — Encrypted Python module loader for HART OS.

Build-time:
    from hevolvearmor import encrypt_package
    encrypt_package('/path/to/hevolveai/src/hevolveai', '/path/to/output', key)

Runtime:
    from hevolvearmor import install_loader
    install_loader('/path/to/modules', key)
    import hevolveai  # transparently decrypted from .enc files
"""

__version__ = "0.1.0"

from hevolvearmor._native import (
    armor_encrypt,
    armor_decrypt,
    armor_generate_key,
    armor_derive_key_ed25519,
    armor_derive_key_passphrase,
    armor_derive_key_raw,
    armor_self_hash,
    armor_load_module,
    armor_read_manifest,
    armor_encrypt_package,
    KEY_SIZE,
    NONCE_SIZE,
)
from hevolvearmor._loader import ArmoredFinder, install_loader, uninstall_loader
from hevolvearmor._keygen import derive_runtime_key

__all__ = [
    "armor_encrypt",
    "armor_decrypt",
    "armor_generate_key",
    "armor_derive_key_ed25519",
    "armor_derive_key_passphrase",
    "armor_derive_key_raw",
    "armor_self_hash",
    "armor_load_module",
    "armor_read_manifest",
    "armor_encrypt_package",
    "ArmoredFinder",
    "install_loader",
    "uninstall_loader",
    "derive_runtime_key",
    "encrypt_package",
    "KEY_SIZE",
    "NONCE_SIZE",
]


def encrypt_package(source_dir: str, output_dir: str, key: bytes = None,
                    passphrase: str = None, verbose: bool = True) -> dict:
    """High-level API: encrypt an entire Python package.

    Compiles .py → .pyc → AES-256-GCM encrypted .enc blobs.
    Uses the Rust native encrypt for each file.

    Args:
        source_dir: path to package root (contains __init__.py)
        output_dir: path to write encrypted modules
        key: 32-byte AES key (mutually exclusive with passphrase)
        passphrase: derive key from passphrase
        verbose: print progress

    Returns:
        dict with {encrypted, failed, total_bytes, key}
    """
    if key is None and passphrase is None:
        key = armor_generate_key()
    elif passphrase is not None:
        key = armor_derive_key_passphrase(passphrase)

    from hevolvearmor._builder import build_encrypted_package
    stats = build_encrypted_package(source_dir, output_dir, key, verbose)
    stats['key'] = key
    return stats
