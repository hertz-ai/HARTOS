"""
One-time master keypair generation for HevolveSocial deployment control.

Outputs:
  - Private key hex → store as GitHub Secret HEVOLVE_MASTER_PRIVATE_KEY_HEX
  - Public key hex  → embed in security/master_key.py MASTER_PUBLIC_KEY_HEX

NEVER run this in CI/CD or commit the private key anywhere.

Usage:
    python scripts/generate_master_keypair.py
"""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def main():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    print("=" * 60)
    print("HevolveSocial Master Keypair Generated")
    print("=" * 60)
    print()
    print(f"PRIVATE KEY (hex) - Store as GitHub Secret:")
    print(f"  HEVOLVE_MASTER_PRIVATE_KEY_HEX={priv_bytes.hex()}")
    print()
    print(f"PUBLIC KEY (hex) - Embed in security/master_key.py:")
    print(f"  MASTER_PUBLIC_KEY_HEX = '{pub_bytes.hex()}'")
    print()
    print("WARNING: The private key must NEVER be committed to git.")
    print("         Store it ONLY as a GitHub Actions secret.")
    print("=" * 60)


if __name__ == '__main__':
    main()
