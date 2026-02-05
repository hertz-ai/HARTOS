"""
Release signing script for HevolveSocial CI/CD.
Computes manifest, signs with master private key, outputs release_manifest.json.

Usage:
    python scripts/sign_release.py --version v1.0.0 --git-sha abc123 \
        --code-hash <hash> --manifest-hash <hash> --output release_manifest.json

Requires MASTER_PRIVATE_KEY_HEX environment variable (GitHub Actions secret).
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def main():
    parser = argparse.ArgumentParser(description='Sign a HevolveSocial release')
    parser.add_argument('--version', required=True, help='Release version (e.g. v1.0.0)')
    parser.add_argument('--git-sha', required=True, help='Git commit SHA')
    parser.add_argument('--code-hash', required=True, help='SHA-256 code manifest hash')
    parser.add_argument('--manifest-hash', required=True, help='SHA-256 file manifest hash')
    parser.add_argument('--output', default='release_manifest.json', help='Output file path')
    args = parser.parse_args()

    # Load master private key from environment
    priv_hex = os.environ.get('MASTER_PRIVATE_KEY_HEX', '')
    if not priv_hex:
        print("ERROR: MASTER_PRIVATE_KEY_HEX environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        priv_bytes = bytes.fromhex(priv_hex)
        priv_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    except (ValueError, Exception) as e:
        print(f"ERROR: Invalid private key: {e}", file=sys.stderr)
        sys.exit(1)

    # Build manifest payload
    pub_bytes = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    manifest = {
        'version': args.version,
        'git_sha': args.git_sha,
        'code_hash': args.code_hash,
        'file_manifest_hash': args.manifest_hash,
        'built_at': datetime.now(timezone.utc).isoformat(),
        'master_public_key': pub_bytes.hex(),
    }

    # Sign: canonicalize and sign with Ed25519
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    signature = priv_key.sign(canonical.encode('utf-8'))
    manifest['master_signature'] = signature.hex()

    # Write output
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    print(f"Release manifest signed and written to {args.output}")
    print(f"  version:   {args.version}")
    print(f"  code_hash: {args.code_hash[:16]}...")
    print(f"  signature: {manifest['master_signature'][:32]}...")


if __name__ == '__main__':
    main()
