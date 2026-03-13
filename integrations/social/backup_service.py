"""
HevolveSocial - Encrypted Backup & Restore Service
Zero-knowledge backup: Fernet encryption (AES-128-CBC + HMAC-SHA256)
Key derived from user passphrase via PBKDF2 (600K iterations).
Server stores only opaque ciphertext - cannot read user data.
"""
import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from .models import (
    User, Post, Comment, Vote, BackupMetadata,
)

logger = logging.getLogger('hevolve_social')

PBKDF2_ITERATIONS = 600_000
BACKUP_DIR_NAME = 'backups'


def _get_backup_dir():
    try:
        from core.platform_paths import get_db_dir
        base = os.path.join(get_db_dir(), BACKUP_DIR_NAME)
    except ImportError:
        base = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', BACKUP_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet key from passphrase + salt using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
    return key


def create_backup(db, user_id: str, passphrase: str) -> dict:
    """
    Bundle user data → JSON → Fernet encrypt → write file.
    Returns { backup_id, content_hash, size_bytes }.
    """
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise ValueError("User not found")

    # Collect data
    posts = db.query(Post).filter_by(author_id=user_id).all()
    comments = db.query(Comment).filter_by(author_id=user_id).all()
    votes = db.query(Vote).filter_by(user_id=user_id).all()

    bundle = {
        'version': 1,
        'user_id': user_id,
        'profile': user.to_dict(include_token=False),
        'posts': [p.to_dict() for p in posts],
        'comments': [c.to_dict() for c in comments],
        'votes': [{'target_type': v.target_type, 'target_id': v.target_id,
                    'value': v.value} for v in votes],
    }

    # Optional: memory graph data
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        mg = MemoryGraph(f"{user_id}_default")
        memories = mg.get_session_memories(limit=1000)
        bundle['memories'] = [m.to_dict() for m in memories]
    except Exception:
        bundle['memories'] = []

    plaintext = json.dumps(bundle, default=str).encode()

    # Encrypt
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    f = Fernet(key)
    ciphertext = f.encrypt(plaintext)

    # Prepend salt (16 bytes) to ciphertext
    blob = salt + ciphertext
    content_hash = hashlib.sha256(blob).hexdigest()

    # Write to file
    backup_dir = _get_backup_dir()
    from .models import _uuid
    backup_id = _uuid()
    filepath = os.path.join(backup_dir, f"{user_id}_{backup_id}.enc")
    with open(filepath, 'wb') as fp:
        fp.write(blob)

    # Record metadata
    meta = BackupMetadata(
        id=backup_id,
        user_id=user_id,
        backup_version=1,
        content_hash=content_hash,
        size_bytes=len(blob),
    )
    db.add(meta)
    db.commit()

    return {
        'backup_id': backup_id,
        'content_hash': content_hash,
        'size_bytes': len(blob),
    }


def restore_backup(db, user_id: str, passphrase: str, backup_id: str = None) -> dict:
    """
    Read latest (or specified) backup → decrypt → upsert data.
    Returns { restored_items }.
    """
    backup_dir = _get_backup_dir()

    if backup_id:
        filepath = os.path.join(backup_dir, f"{user_id}_{backup_id}.enc")
    else:
        # Find latest backup for user
        metas = (db.query(BackupMetadata)
                 .filter_by(user_id=user_id)
                 .order_by(BackupMetadata.created_at.desc())
                 .first())
        if not metas:
            raise ValueError("No backups found")
        filepath = os.path.join(backup_dir, f"{user_id}_{metas.id}.enc")

    if not os.path.exists(filepath):
        raise ValueError("Backup file not found")

    with open(filepath, 'rb') as fp:
        blob = fp.read()

    # Extract salt (first 16 bytes) and ciphertext
    salt = blob[:16]
    ciphertext = blob[16:]

    key = derive_key(passphrase, salt)
    f = Fernet(key)
    try:
        plaintext = f.decrypt(ciphertext)
    except Exception:
        raise ValueError("Invalid passphrase - decryption failed")

    bundle = json.loads(plaintext.decode())
    restored = {'profile': False, 'posts': 0, 'comments': 0, 'votes': 0}

    # Restore profile fields
    user = db.query(User).filter_by(id=user_id).first()
    if user and bundle.get('profile'):
        profile = bundle['profile']
        user.display_name = profile.get('display_name', user.display_name)
        user.bio = profile.get('bio', user.bio)
        user.avatar_url = profile.get('avatar_url', user.avatar_url)
        restored['profile'] = True

    # Restore posts (upsert by id)
    for p_data in bundle.get('posts', []):
        existing = db.query(Post).filter_by(id=p_data.get('id')).first()
        if not existing:
            post = Post(
                id=p_data.get('id'),
                author_id=user_id,
                title=p_data.get('title', ''),
                content=p_data.get('content', ''),
                content_type=p_data.get('content_type', 'text'),
            )
            db.add(post)
            restored['posts'] += 1

    # Restore comments (upsert by id)
    for c_data in bundle.get('comments', []):
        existing = db.query(Comment).filter_by(id=c_data.get('id')).first()
        if not existing:
            comment = Comment(
                id=c_data.get('id'),
                post_id=c_data.get('post_id'),
                author_id=user_id,
                content=c_data.get('content', ''),
            )
            db.add(comment)
            restored['comments'] += 1

    db.commit()
    return restored


def list_backups(db, user_id: str) -> list:
    """List all backup metadata for a user."""
    metas = (db.query(BackupMetadata)
             .filter_by(user_id=user_id)
             .order_by(BackupMetadata.created_at.desc())
             .all())
    return [m.to_dict() for m in metas]
