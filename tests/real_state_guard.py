"""Fail a test session that changed the owner's real node identity or keys.

On 2026-09-23 a test run replaced the owner's desktop node_id.json (46329c87,
the id central had verified). Nothing failed: the suite passed and the damage
was found by a person. core.platform_paths.get_identity_data_dir now keeps
tests off the real root; this is the tripwire for whatever gets past it.

The session is hashed at start and end under the REAL platform data root
(never an override: an override is not the owner's install). A changed,
created or deleted file fails the session and is named.
"""
import glob
import hashlib
import os

# Relative to the data root. data/ is where a Nunba process keeps its keys
# (dirname of its HEVOLVE_DB_PATH); agent_data/ is the stable fallback.
_KEY_FILES = ('node_private_key.pem', 'node_public_key.pem',
              'node_x25519_private.key', 'node_x25519_public.key',
              'node_identity.json', 'node_identity.superseded.*.json')
_SUBDIRS = ('', 'data', 'agent_data')


def watched_root():
    """The owner's real data root, or HARTOS_REAL_STATE_ROOT to point at a decoy."""
    override = os.environ.get('HARTOS_REAL_STATE_ROOT', '').strip()
    if override:
        return override
    from core.platform_paths import _platform_default_data_dir
    return _platform_default_data_dir()


def snapshot(root=None):
    """{relative path: sha256} for every identity or key file under root."""
    root = root or watched_root()
    patterns = [os.path.join(root, 'node_id.json')]
    for sub in _SUBDIRS:
        patterns += [os.path.join(root, sub, name) for name in _KEY_FILES]
    found = {}
    for pattern in patterns:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                with open(path, 'rb') as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                found[os.path.relpath(path, root)] = digest
    return found


def changes(before, after):
    """Human-readable lines for every file created, changed or deleted."""
    lines = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            lines.append(f'created  {rel}')
        elif rel not in after:
            lines.append(f'deleted  {rel}')
        elif before[rel] != after[rel]:
            lines.append(f'changed  {rel}')
    return lines
