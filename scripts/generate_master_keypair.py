"""
One-time master keypair generation for HART deployment control.

FOR DEVELOPMENT ONLY. In production, generate the key directly inside
the HSM (Google Cloud KMS, Azure Key Vault, or HashiCorp Vault) so
the private key NEVER exists outside hardware.

HSM key generation:
  - GCP:   gcloud kms keys create hart-master --keyring=hart --location=global --purpose=asymmetric-signing --default-algorithm=ec-sign-ed25519
  - Azure: az keyvault key create --vault-name hart-vault --name hart-master --kty OKP-HSM --curve Ed25519
  - Vault: vault write transit/keys/hart-master type=ed25519

Dev fallback (this script):
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

    print("=" * 70)
    print("HART Master Keypair Generated (DEV ONLY)")
    print("=" * 70)
    print()
    print("  In production, generate keys INSIDE the HSM so the private")
    print("  key never exists outside hardware. See docstring for commands.")
    print()
    print(f"PRIVATE KEY (hex) - Store as GitHub Secret:")
    print(f"  HEVOLVE_MASTER_PRIVATE_KEY_HEX={priv_bytes.hex()}")
    print()
    print(f"PUBLIC KEY (hex) - Embed in security/master_key.py:")
    print(f"  MASTER_PUBLIC_KEY_HEX = '{pub_bytes.hex()}'")
    print()
    print("WARNING: The private key must NEVER be committed to git.")
    print("         In production, use HSM (GCP KMS / Azure Key Vault / Vault).")
    print("=" * 70)


if __name__ == '__main__':
    main()
