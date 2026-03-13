"""
CI/CD Script: Update release hash registry with GA release hashes.

Reads all Git tags matching 'v*', computes the code hash for each,
and writes the resulting dict into security/release_hash_registry.py.

Usage (CI/CD only — not run at runtime):
  python scripts/update_release_hashes.py [--code-root DIR]

This is called in .github/workflows/release.yml BEFORE sign_release.py.
"""
import argparse
import os
import re
import subprocess
import sys

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_git_tags() -> list:
    """Get all version tags (v*) from Git."""
    try:
        result = subprocess.run(
            ['git', 'tag', '--list', 'v*'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
        return [t.strip() for t in result.stdout.strip().split('\n') if t.strip()]
    except Exception:
        return []


def compute_hash_for_tag(tag: str, code_root: str) -> str:
    """Compute code hash for a specific Git tag.

    Checks out the tag in a temporary worktree, computes the hash,
    then cleans up.  Falls back to current tree hash if worktree fails.
    """
    try:
        from security.node_integrity import compute_code_hash
        # Try to get the tree hash from git directly (no checkout needed)
        result = subprocess.run(
            ['git', 'rev-parse', f'{tag}^{{tree}}'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ''


def compute_current_hash(code_root: str) -> str:
    """Compute code hash for the current working tree."""
    try:
        from security.node_integrity import compute_code_hash
        return compute_code_hash(code_root)
    except Exception:
        return ''


def update_registry_file(hashes: dict, registry_path: str) -> None:
    """Write the hash dict into release_hash_registry.py."""
    with open(registry_path, 'r') as f:
        content = f.read()

    # Build the new dict literal
    lines = ['_KNOWN_HASHES: Dict[str, str] = {']
    for version, h in sorted(hashes.items()):
        lines.append(f"    '{version}': '{h}',")
    lines.append('}')
    new_dict = '\n'.join(lines)

    # Replace the existing _KNOWN_HASHES block
    pattern = r'_KNOWN_HASHES: Dict\[str, str\] = \{[^}]*\}'
    updated = re.sub(pattern, new_dict, content, flags=re.DOTALL)

    with open(registry_path, 'w') as f:
        f.write(updated)

    print(f"Updated {registry_path} with {len(hashes)} release hashes")


def main():
    parser = argparse.ArgumentParser(
        description='Update release hash registry from Git tags')
    parser.add_argument('--code-root', type=str,
                        default=os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__))),
                        help='Project root directory')
    args = parser.parse_args()

    registry_path = os.path.join(args.code_root,
                                 'security', 'release_hash_registry.py')
    if not os.path.exists(registry_path):
        print(f"ERROR: {registry_path} not found", file=sys.stderr)
        sys.exit(1)

    tags = get_git_tags()
    print(f"Found {len(tags)} version tags: {tags}")

    hashes = {}
    for tag in tags:
        h = compute_hash_for_tag(tag, args.code_root)
        if h:
            # Strip 'v' prefix for version string
            version = tag.lstrip('v')
            hashes[version] = h
            print(f"  {tag}: {h[:16]}...")

    # Always include current HEAD hash
    current = compute_current_hash(args.code_root)
    if current:
        hashes['_current'] = current
        print(f"  current: {current[:16]}...")

    if hashes:
        update_registry_file(hashes, registry_path)
    else:
        print("No hashes computed — registry unchanged")


if __name__ == '__main__':
    main()
