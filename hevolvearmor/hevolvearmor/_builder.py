"""
Build-time encryption of Python packages.

Compiles .py → .pyc (bytecode) → AES-256-GCM encrypted .enc blobs.
The .pyc compilation happens in Python (marshal), encryption in Rust.
"""
import importlib.util
import marshal
import os
import py_compile
import shutil
import struct
import sys

_SKIP_DIRS = frozenset({
    '__pycache__', '.git', '.tox', 'tests', 'test', 'legacy',
    'dashboard', '.egg-info', 'dist', 'build', '.mypy_cache',
    '.pytest_cache', '.ruff_cache',
})


def compile_to_pyc_bytes(source_path: str) -> bytes:
    """Compile a .py file to .pyc bytes (header + marshalled code)."""
    with open(source_path, 'r', encoding='utf-8') as f:
        source = f.read()

    code = compile(source, source_path, 'exec', dont_inherit=True, optimize=2)

    magic = importlib.util.MAGIC_NUMBER
    flags = struct.pack('<I', 0)
    timestamp = struct.pack('<I', int(os.path.getmtime(source_path)))
    size = struct.pack('<I', os.path.getsize(source_path) & 0xFFFFFFFF)

    return magic + flags + timestamp + size + marshal.dumps(code)


def build_encrypted_package(source_dir: str, output_dir: str,
                            key: bytes, verbose: bool = True) -> dict:
    """Encrypt an entire Python package tree.

    For each .py file:
      1. Compile to .pyc (bytecode, optimize=2)
      2. Encrypt .pyc with AES-256-GCM via Rust native
      3. Write as .enc to output_dir

    Args:
        source_dir: path to package root (must contain __init__.py)
        output_dir: output directory for .enc files
        key: 32-byte AES key
        verbose: print per-file progress

    Returns:
        dict with {encrypted, failed, skipped, total_bytes}
    """
    from hevolvearmor._native import armor_encrypt

    stats = {'encrypted': 0, 'failed': 0, 'skipped': 0, 'total_bytes': 0}

    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    manifest = []

    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.endswith('.egg-info')]

        for fname in sorted(filenames):
            if not fname.endswith('.py'):
                continue

            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, source_dir).replace(os.sep, '/')

            enc_rel = rel_path.replace('.py', '.enc')
            enc_full = os.path.join(output_dir, enc_rel)
            os.makedirs(os.path.dirname(enc_full), exist_ok=True)

            try:
                pyc_bytes = compile_to_pyc_bytes(full_path)
                encrypted = bytes(armor_encrypt(pyc_bytes, key))

                with open(enc_full, 'wb') as f:
                    f.write(encrypted)

                stats['encrypted'] += 1
                stats['total_bytes'] += len(encrypted)
                manifest.append(rel_path)

                if verbose:
                    print(f"  [OK] {rel_path} ({len(encrypted):,} bytes)")

            except SyntaxError as e:
                stats['failed'] += 1
                if verbose:
                    print(f"  [FAIL] {rel_path}: SyntaxError: {e}")
            except Exception as e:
                stats['failed'] += 1
                if verbose:
                    print(f"  [FAIL] {rel_path}: {type(e).__name__}: {e}")

    # Write manifest
    manifest_path = os.path.join(output_dir, '_manifest.txt')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(manifest))

    return stats
