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
    """Compute the NODE-INTEGRITY code hash for a specific Git tag.

    Checks the tag out into a temporary git worktree, runs
    ``security.node_integrity.compute_code_hash`` over it, then cleans up.

    It MUST be that hash and no other: peers advertise
    ``compute_code_hash()`` of their running tree
    (peer_discovery.py:976), and admission compares that value against
    this registry.  The previous implementation returned
    ``git rev-parse <tag>^{tree}`` — a git TREE SHA, a different hash
    universe entirely — so even a fully populated registry could never
    have matched any peer, and every officially shipped version would
    still have been admitted as untrusted.  (The intent is that ALL
    shipped versions verify, not just the latest: peers legitimately
    run older official builds.)

    Returns '' on any failure; the caller skips that tag.
    """
    import shutil
    import tempfile

    from security.node_integrity import compute_code_hash

    # A stray precomputed-hash env var would make every tag "hash" to the
    # same value — computation must be real here.
    os.environ.pop('HEVOLVE_CODE_HASH_PRECOMPUTED', None)

    worktree = tempfile.mkdtemp(prefix=f'relhash_{tag.replace("/", "_")}_')
    try:
        result = subprocess.run(
            ['git', 'worktree', 'add', '--detach', worktree, tag],
            capture_output=True, text=True, timeout=300, cwd=code_root,
        )
        if result.returncode != 0:
            print(f"  {tag}: worktree checkout failed: "
                  f"{result.stderr.strip()[:200]}", file=sys.stderr)
            return ''
        return compute_code_hash(worktree)
    except Exception as e:
        print(f"  {tag}: hash computation failed: {e}", file=sys.stderr)
        return ''
    finally:
        # Cleanup must never mask the computed result.  `git worktree remove
        # --force` already deletes the directory; the rmtree is only for
        # leftovers, and even ignore_errors=True cannot contain a broken
        # stdlib (observed live: a 3.13 shutil on a 3.12 interpreter raising
        # AttributeError inside os.walk itself, which then propagated out of
        # this finally and threw away a successfully computed hash).
        try:
            subprocess.run(
                ['git', 'worktree', 'remove', '--force', worktree],
                capture_output=True, text=True, timeout=120, cwd=code_root,
            )
        except Exception:
            pass
        try:
            shutil.rmtree(worktree, ignore_errors=True)
        except Exception:
            pass


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

    # NOTE: no '_current' entry, deliberately.  Writing the registry changes
    # security/release_hash_registry.py, which is itself part of the tree
    # compute_code_hash hashes — so a "current tree" hash recorded here is
    # stale the moment this file is written, and could never match any
    # running node.  Tag hashes don't have that problem: they are computed
    # over the tag's own frozen tree.  Consequence to know: the NEWEST
    # release verifies every release before it, and becomes verifiable to
    # others one release later — inherent to any self-hashing tree, same
    # reason a git commit cannot contain its own sha.

    if hashes:
        update_registry_file(hashes, registry_path)
    else:
        print("No hashes computed — registry unchanged")


if __name__ == '__main__':
    main()
