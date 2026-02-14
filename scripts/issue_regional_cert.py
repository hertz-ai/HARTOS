"""
Issue a regional host certificate signed by the master key.

Usage:
    python scripts/issue_regional_cert.py \
        --node-id <regional_node_id> \
        --public-key <hex_public_key> \
        --region us-east-1 \
        --output agent_data/node_certificate.json

Requires MASTER_PRIVATE_KEY_HEX environment variable.
"""
import os
import sys
import json
import argparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from security.key_delegation import create_child_certificate


def main():
    parser = argparse.ArgumentParser(
        description='Issue a signed certificate for a regional host')
    parser.add_argument('--node-id', required=True,
                        help='Node ID of the regional host')
    parser.add_argument('--public-key', required=True,
                        help='Hex-encoded Ed25519 public key of the regional host')
    parser.add_argument('--region', required=True,
                        help='Region name (e.g. us-east-1)')
    parser.add_argument('--capabilities', default='registry,gossip_hub,agent_host',
                        help='Comma-separated capabilities (default: registry,gossip_hub,agent_host)')
    parser.add_argument('--validity-days', type=int, default=365,
                        help='Certificate validity in days (default: 365)')
    parser.add_argument('--output', default='agent_data/node_certificate.json',
                        help='Output certificate file path')
    args = parser.parse_args()

    # Load master private key
    priv_hex = os.environ.get('MASTER_PRIVATE_KEY_HEX', '')
    if not priv_hex:
        print("ERROR: MASTER_PRIVATE_KEY_HEX environment variable not set",
              file=sys.stderr)
        sys.exit(1)

    try:
        priv_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    except (ValueError, Exception) as e:
        print(f"ERROR: Invalid private key: {e}", file=sys.stderr)
        sys.exit(1)

    capabilities = [c.strip() for c in args.capabilities.split(',')]

    cert = create_child_certificate(
        parent_private_key=priv_key,
        child_public_key_hex=args.public_key,
        node_id=args.node_id,
        tier='regional',
        region_name=args.region,
        capabilities=capabilities,
        validity_days=args.validity_days,
    )

    # Write certificate
    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(cert, f, indent=2)

    print(f"Regional certificate issued and saved to {args.output}")
    print(f"  node_id:     {args.node_id}")
    print(f"  region:      {args.region}")
    print(f"  public_key:  {args.public_key[:16]}...")
    print(f"  expires_at:  {cert['expires_at']}")
    print(f"  signature:   {cert['parent_signature'][:32]}...")


if __name__ == '__main__':
    main()
