"""
Build Script: Compile HevolveAI for Nunba bundling.

Steps:
  1. Find installed HevolveAI package
  2. Compile all .py → .pyc (compileall)
  3. Generate SHA-256 manifest of compiled files
  4. Sign manifest with node's Ed25519 key
  5. Strip .py source files (production mode)

Usage:
  python scripts/compile_hevolveai.py [--strip-source] [--output-dir DIR]

This is a CI/CD build step, NOT run at runtime.
"""
import argparse
import compileall
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


def find_hevolveai_root() -> Path:
    """Find the installed HevolveAI package root."""
    try:
        spec = importlib.util.find_spec('hevolveai')
    except (ModuleNotFoundError, ValueError):
        print("ERROR: HevolveAI not installed", file=sys.stderr)
        sys.exit(1)

    if spec is None or not spec.submodule_search_locations:
        print("ERROR: cannot locate HevolveAI package", file=sys.stderr)
        sys.exit(1)

    return Path(list(spec.submodule_search_locations)[0])


def compile_package(pkg_root: Path, output_dir: Path = None) -> Path:
    """Compile all .py files to .pyc."""
    target = output_dir or pkg_root

    if output_dir and output_dir != pkg_root:
        # Copy package to output dir first
        if output_dir.exists():
            shutil.rmtree(output_dir)
        shutil.copytree(pkg_root, output_dir)
        target = output_dir

    success = compileall.compile_dir(
        str(target),
        quiet=1,
        force=True,
        optimize=2,  # Remove docstrings + asserts
    )
    if not success:
        print("WARNING: some files failed to compile", file=sys.stderr)

    return target


def generate_manifest(pkg_root: Path) -> dict:
    """Generate SHA-256 hash manifest of all files."""
    manifest = {
        'version': '',
        'build_node': os.environ.get('HEVOLVE_NODE_ID', 'unknown'),
        'build_timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'files': {},
    }

    # Get version
    try:
        from importlib.metadata import version
        manifest['version'] = version('hevolveai')
    except Exception:
        pass

    for path in sorted(pkg_root.rglob('*')):
        if path.is_file() and not path.name.startswith('.'):
            rel = str(path.relative_to(pkg_root)).replace('\\', '/')
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    h.update(chunk)
            manifest['files'][rel] = h.hexdigest()

    return manifest


def sign_manifest(manifest: dict) -> dict:
    """Sign the manifest with the node's Ed25519 key."""
    try:
        from security.node_integrity import sign_json_payload
        manifest_str = json.dumps(manifest['files'], sort_keys=True)
        manifest['signature'] = sign_json_payload(
            {'manifest_hash': hashlib.sha256(
                manifest_str.encode()).hexdigest()}
        )
    except ImportError:
        print("WARNING: Cannot sign manifest (node_integrity not available)",
              file=sys.stderr)
    return manifest


def strip_source(pkg_root: Path):
    """Remove .py source files, keeping only .pyc."""
    removed = 0
    for py_file in pkg_root.rglob('*.py'):
        # Keep __init__.py stubs (needed for package discovery)
        if py_file.name == '__init__.py':
            # Truncate to minimal stub
            py_file.write_text('# Compiled\n')
        else:
            py_file.unlink()
            removed += 1
    print(f"Stripped {removed} .py source files")


def main():
    parser = argparse.ArgumentParser(
        description='Compile HevolveAI for Nunba bundling')
    parser.add_argument('--strip-source', action='store_true',
                        help='Remove .py source after compilation')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: in-place)')
    parser.add_argument('--manifest-out', type=str,
                        default='security/hevolveai_manifest.json',
                        help='Manifest output path')
    args = parser.parse_args()

    pkg_root = find_hevolveai_root()
    print(f"Found HevolveAI at: {pkg_root}")

    output = Path(args.output_dir) if args.output_dir else None
    target = compile_package(pkg_root, output)
    print(f"Compiled to: {target}")

    manifest = generate_manifest(target)
    manifest = sign_manifest(manifest)

    manifest_path = Path(args.manifest_out)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {manifest_path} ({len(manifest['files'])} files)")

    if args.strip_source:
        strip_source(target)

    print("Done.")


if __name__ == '__main__':
    main()
