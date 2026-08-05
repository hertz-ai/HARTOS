"""Per-node HMAC secret. One owner, so nobody falls back to a shared constant.

This logic was written once, in integrations/agent_engine/federated_aggregator,
where its own comment records why:

    G8 fix -- replaces the old HART_NODE_KEY env var / Ed25519 public key
    fallback which was a hardcoded/default key vulnerability

Federation deltas were fixed. The email campaign's click-attribution token was
not, and still keyed its HMAC on `os.environ.get('HEVOLVE_TRACK_SECRET',
'hevolve-campaign')`. That default is in the public source, so the "opaque"
recipient token was not opaque at all: anyone with the repository and a
candidate address could confirm whether a token belonged to it.

Rather than importing the aggregator from integrations/channels, which is the
wrong direction and drags a federation module into a mailer, the helper moves
here and both callers delegate. Moved, not copied. A second implementation
reading the same file would be the exact failure this codebase keeps hitting.
"""
import logging
import os

logger = logging.getLogger('hevolve.node_secret')

# User-writable dir: installed builds under Program Files are read-only.
_HMAC_SECRET_PATH = os.path.join(
    os.environ.get('HEVOLVE_AGENT_DATA',
                   os.path.join(os.path.expanduser('~'), '.nunba', 'agent_data')),
    '.hmac_secret')

_NODE_HMAC_SECRET: str = ''


def load_or_create_hmac_secret() -> str:
    """Load the per-node HMAC secret from disk, or generate one on first boot.

    32 random bytes, hex-encoded, stored at agent_data/.hmac_secret with
    owner-only permissions where the platform supports it. Never transmitted.
    """
    try:
        if os.path.isfile(_HMAC_SECRET_PATH):
            with open(_HMAC_SECRET_PATH, 'r') as f:
                secret = f.read().strip()
            if len(secret) >= 32:
                return secret
    except (OSError, PermissionError) as e:
        logger.warning(f'Cannot read HMAC secret ({e}), regenerating')

    secret = os.urandom(32).hex()
    try:
        os.makedirs(os.path.dirname(_HMAC_SECRET_PATH), exist_ok=True)
        with open(_HMAC_SECRET_PATH, 'w') as f:
            f.write(secret)
        try:
            import stat
            os.chmod(_HMAC_SECRET_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, NotImplementedError):
            pass  # Windows does not honour POSIX chmod the same way
        logger.info(f'Generated per-node HMAC secret at {_HMAC_SECRET_PATH}')
    except (OSError, PermissionError) as e:
        logger.warning(f'Cannot persist HMAC secret ({e}), using ephemeral')

    return secret


def get_hmac_secret() -> str:
    """The per-node HMAC secret, lazily loaded and cached."""
    global _NODE_HMAC_SECRET
    if not _NODE_HMAC_SECRET:
        _NODE_HMAC_SECRET = load_or_create_hmac_secret()
    return _NODE_HMAC_SECRET


def get_tracking_secret() -> str:
    """Key for campaign click-attribution tokens.

    HEVOLVE_TRACK_SECRET still wins, so an operator who set one keeps their
    existing links working. Otherwise the per-node secret above is used instead
    of the old 'hevolve-campaign' literal.

    A consequence worth stating: tokens minted before this change were derived
    from the public default, so links already in inboxes will not verify
    against the new key. Attribution for those is lost, which is the correct
    trade against a token anyone could forge or de-anonymise.
    """
    env = os.environ.get('HEVOLVE_TRACK_SECRET')
    if env:
        return env
    return get_hmac_secret()
