#!/usr/bin/env python3
"""
armor_hevolveai.py - Encrypt hevolveai package into HARTOS vendor bundle.

Compiles all .py source files to .pyc bytecode, encrypts each with
AES-256-GCM, and writes to vendor/hevolveai_armored/modules/.

Usage:
    python scripts/armor_hevolveai.py                          # auto-find sibling
    python scripts/armor_hevolveai.py --source ../hevolveai    # explicit path
    python scripts/armor_hevolveai.py --key-file my.key        # custom key

The encrypted bundle is importable at runtime via the custom import hook
in vendor/hevolveai_armored/_runtime.py.  No third-party tools required --
uses only stdlib + cryptography (already a HARTOS dependency).
"""
import os
import sys
import py_compile
import marshal
import struct
import hashlib
import shutil
import importlib
import argparse
import time

# ---------------------------------------------------------------------------
# Encryption primitives (AES-256-GCM via cryptography — already a HARTOS dep)
# ---------------------------------------------------------------------------

def _get_cipher():
    """Import AES-GCM from cryptography."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def generate_key() -> bytes:
    """Generate a random 256-bit AES key."""
    return os.urandom(32)


def derive_key_from_passphrase(passphrase: str, salt: bytes = None) -> tuple:
    """Derive a 256-bit key from a passphrase using PBKDF2."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    if salt is None:
        salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    key = kdf.derive(passphrase.encode('utf-8'))
    return key, salt


def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Encrypt data with AES-256-GCM.  Returns nonce(12) + ciphertext + tag."""
    AESGCM = _get_cipher()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return nonce + ct  # 12 + len(data) + 16 (tag)


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM blob (nonce + ciphertext + tag)."""
    AESGCM = _get_cipher()
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# .pyc compilation
# ---------------------------------------------------------------------------

def compile_to_pyc_bytes(source_path: str) -> bytes:
    """Compile a .py file and return raw .pyc bytes (header + marshalled code).

    Returns the standard .pyc format: magic(4) + flags(4) + timestamp(4) +
    size(4) + marshalled code object.
    """
    # Compile to code object
    with open(source_path, 'r', encoding='utf-8') as f:
        source = f.read()

    code = compile(source, source_path, 'exec', dont_inherit=True, optimize=2)

    # Build .pyc header (PEP 552 — hash-based validation disabled)
    magic = importlib.util.MAGIC_NUMBER  # 4 bytes
    flags = struct.pack('<I', 0)         # no hash validation
    timestamp = struct.pack('<I', int(os.path.getmtime(source_path)))
    size = struct.pack('<I', os.path.getsize(source_path) & 0xFFFFFFFF)

    return magic + flags + timestamp + size + marshal.dumps(code)


# ---------------------------------------------------------------------------
# Package walker
# ---------------------------------------------------------------------------

_SKIP_DIRS = {'__pycache__', '.git', '.tox', 'tests', 'test', 'legacy',
              'dashboard', '.egg-info', 'dist', 'build'}


def walk_package(src_root: str):
    """Yield (rel_path, full_path) for every .py file in the package."""
    for dirpath, dirnames, filenames in os.walk(src_root):
        # Prune skipped directories
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not d.endswith('.egg-info')]

        for fname in sorted(filenames):
            if not fname.endswith('.py'):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, src_root)
            yield rel, full


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def armor_package(source_dir: str, output_dir: str, key: bytes,
                  verbose: bool = True) -> dict:
    """Encrypt an entire Python package tree.

    Args:
        source_dir: path to hevolveai package root (contains __init__.py)
        output_dir: path to vendor/hevolveai_armored/modules/
        key: 32-byte AES key

    Returns:
        dict with stats: {encrypted, failed, skipped, total_bytes}
    """
    stats = {'encrypted': 0, 'failed': 0, 'skipped': 0, 'total_bytes': 0}

    # Clean output
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Write package marker so the loader knows what's inside
    manifest = []

    for rel_path, full_path in walk_package(source_dir):
        # Convert path separators to forward slashes for consistency
        rel_path = rel_path.replace(os.sep, '/')

        # Output path: foo/bar.py → foo/bar.enc
        enc_rel = rel_path.replace('.py', '.enc')
        enc_full = os.path.join(output_dir, enc_rel)
        os.makedirs(os.path.dirname(enc_full), exist_ok=True)

        try:
            pyc_bytes = compile_to_pyc_bytes(full_path)
            encrypted = encrypt_bytes(pyc_bytes, key)

            with open(enc_full, 'wb') as f:
                f.write(encrypted)

            stats['encrypted'] += 1
            stats['total_bytes'] += len(encrypted)
            manifest.append(rel_path)

            if verbose:
                print(f"  [OK] {rel_path} ({len(encrypted)} bytes)")

        except SyntaxError as e:
            stats['failed'] += 1
            if verbose:
                print(f"  [FAIL] {rel_path}: SyntaxError: {e}")
        except Exception as e:
            stats['failed'] += 1
            if verbose:
                print(f"  [FAIL] {rel_path}: {type(e).__name__}: {e}")

    # Write manifest (list of encrypted modules)
    manifest_path = os.path.join(output_dir, '_manifest.txt')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(manifest))

    return stats


def main():
    parser = argparse.ArgumentParser(description='Encrypt hevolveai into HARTOS vendor bundle')
    parser.add_argument('--source', help='Path to hevolveai/src/hevolveai/',
                        default=None)
    parser.add_argument('--output', help='Output directory for encrypted modules',
                        default=None)
    parser.add_argument('--key-file', help='Path to save/load the encryption key',
                        default=None)
    parser.add_argument('--passphrase', help='Derive key from passphrase instead of random',
                        default=None)
    parser.add_argument('-q', '--quiet', action='store_true')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    hartos_root = os.path.dirname(script_dir)

    # Find hevolveai source
    if args.source:
        src = os.path.abspath(args.source)
    else:
        # Auto-discover: sibling directory or pip-installed
        for candidate in [
            os.path.join(hartos_root, '..', 'hevolveai', 'src', 'hevolveai'),
            os.path.join(hartos_root, '..', 'hevolveai', 'src', 'embodied_ai'),
        ]:
            if os.path.isdir(candidate) and os.path.isfile(
                    os.path.join(candidate, '__init__.py')):
                src = os.path.abspath(candidate)
                break
        else:
            print("ERROR: Cannot find hevolveai source. Use --source to specify.")
            sys.exit(1)

    # Also encrypt embodied_ai if it's a separate package under src/
    src_parent = os.path.dirname(src)
    embodied_src = os.path.join(src_parent, 'embodied_ai')

    # Output directory
    if args.output:
        out = os.path.abspath(args.output)
    else:
        out = os.path.join(hartos_root, 'vendor', 'hevolveai_armored', 'modules')

    # Key management
    key_file = args.key_file or os.path.join(
        hartos_root, 'vendor', 'hevolveai_armored', '_key.bin')

    if args.passphrase:
        if os.path.isfile(key_file):
            # Load existing salt
            with open(key_file, 'rb') as f:
                data = f.read()
            salt = data[:16]
            key, _ = derive_key_from_passphrase(args.passphrase, salt)
        else:
            key, salt = derive_key_from_passphrase(args.passphrase)
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, 'wb') as f:
                f.write(salt)  # only salt stored, not the key
    else:
        if os.path.isfile(key_file):
            with open(key_file, 'rb') as f:
                raw = f.read()
            if len(raw) == 32:
                key = raw
            else:
                # Salt-only file from passphrase mode — generate new random key
                key = generate_key()
                with open(key_file, 'wb') as f:
                    f.write(key)
        else:
            key = generate_key()
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, 'wb') as f:
                f.write(key)

    verbose = not args.quiet

    # Banner
    if verbose:
        print("=" * 60)
        print("  HevolveArmor — Encrypt hevolveai for HARTOS")
        print("=" * 60)
        print(f"  Source:  {src}")
        print(f"  Output:  {out}")
        print(f"  Key:     {key_file}")
        print(f"  Key SHA: {hashlib.sha256(key).hexdigest()[:16]}...")
        print("=" * 60)

    t0 = time.time()

    # Encrypt hevolveai package
    if verbose:
        print(f"\n  Encrypting hevolveai from {src}...")
    stats = armor_package(src, os.path.join(out, 'hevolveai'), key, verbose)

    # Encrypt embodied_ai if present as separate package
    if os.path.isdir(embodied_src) and os.path.isfile(
            os.path.join(embodied_src, '__init__.py')):
        if verbose:
            print(f"\n  Encrypting embodied_ai from {embodied_src}...")
        stats2 = armor_package(embodied_src,
                               os.path.join(out, 'embodied_ai'), key, verbose)
        stats['encrypted'] += stats2['encrypted']
        stats['failed'] += stats2['failed']
        stats['total_bytes'] += stats2['total_bytes']

    elapsed = time.time() - t0

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  Encrypted: {stats['encrypted']} modules")
        print(f"  Failed:    {stats['failed']} modules")
        print(f"  Size:      {stats['total_bytes'] / 1024:.1f} KB")
        print(f"  Time:      {elapsed:.1f}s")
        print(f"{'=' * 60}")

    if stats['failed'] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
