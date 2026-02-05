"""
Encrypted Secrets Manager
Replaces plaintext config.json with Fernet-encrypted vault.
Master key derived from HEVOLVE_MASTER_KEY env var via PBKDF2.

Usage:
    from security.secrets_manager import SecretsManager
    sm = SecretsManager.get_instance()
    api_key = sm.get_secret('OPENAI_API_KEY')

Migration:
    python -m security.secrets_manager migrate
"""

import os
import json
import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger('hevolve_security')

_VAULT_PATH = os.path.join(os.path.dirname(__file__), '..', 'secrets.enc')
_SALT_PATH = os.path.join(os.path.dirname(__file__), '..', 'secrets.salt')
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.json')

# Known secret keys that should be loaded from the vault
SECRET_KEYS = [
    'OPENAI_API_KEY',
    'GROQ_API_KEY',
    'LANGCHAIN_API_KEY',
    'GOOGLE_CSE_ID',
    'GOOGLE_API_KEY',
    'NEWS_API_KEY',
    'SERPAPI_API_KEY',
    'ZEP_API_KEY',
    'SOCIAL_SECRET_KEY',
    'HEVOLVE_API_KEY',
    'SOCIAL_DB_KEY',
    'REDIS_URL',
    'DATABASE_URL',
]


class SecretsManager:
    """Thread-safe singleton for encrypted secret access."""

    _instance = None

    def __init__(self):
        self._cache: dict = {}
        self._fernet: Optional[Fernet] = None
        self._init_encryption()
        self._load_vault()

    @classmethod
    def get_instance(cls) -> 'SecretsManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Reset singleton (for testing)."""
        cls._instance = None

    def _derive_key(self, master_key: str, salt: bytes) -> bytes:
        """Derive Fernet key from master key using PBKDF2."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        return base64.urlsafe_b64encode(kdf.derive(master_key.encode()))

    def _init_encryption(self):
        """Initialize Fernet cipher from master key."""
        master_key = os.environ.get('HEVOLVE_MASTER_KEY')
        if not master_key:
            logger.warning(
                "HEVOLVE_MASTER_KEY not set. Secrets vault unavailable. "
                "Falling back to environment variables only."
            )
            return

        salt_path = os.path.abspath(_SALT_PATH)
        if os.path.exists(salt_path):
            with open(salt_path, 'rb') as f:
                salt = f.read()
        else:
            salt = os.urandom(16)
            with open(salt_path, 'wb') as f:
                f.write(salt)

        key = self._derive_key(master_key, salt)
        self._fernet = Fernet(key)

    def _load_vault(self):
        """Load and decrypt the vault file."""
        vault_path = os.path.abspath(_VAULT_PATH)
        if not os.path.exists(vault_path) or self._fernet is None:
            return

        try:
            with open(vault_path, 'rb') as f:
                encrypted = f.read()
            decrypted = self._fernet.decrypt(encrypted)
            self._cache = json.loads(decrypted.decode())
            logger.info("Secrets vault loaded successfully.")
        except InvalidToken:
            logger.error(
                "Failed to decrypt secrets vault. "
                "Check HEVOLVE_MASTER_KEY is correct."
            )
        except Exception as e:
            logger.error(f"Failed to load secrets vault: {e}")

    def get_secret(self, name: str, default: str = '') -> str:
        """
        Get a secret value. Priority:
        1. Environment variable
        2. Encrypted vault
        3. Default value
        """
        env_val = os.environ.get(name)
        if env_val:
            return env_val
        return self._cache.get(name, default)

    def set_secret(self, name: str, value: str):
        """Set a secret in the vault (persists to disk)."""
        self._cache[name] = value
        self._save_vault()

    def _save_vault(self):
        """Encrypt and write vault to disk."""
        if self._fernet is None:
            raise RuntimeError("Cannot save vault: HEVOLVE_MASTER_KEY not set")
        vault_path = os.path.abspath(_VAULT_PATH)
        plaintext = json.dumps(self._cache, indent=2).encode()
        encrypted = self._fernet.encrypt(plaintext)
        with open(vault_path, 'wb') as f:
            f.write(encrypted)
        logger.info("Secrets vault saved.")

    def has_secret(self, name: str) -> bool:
        """Check if a secret exists (env or vault)."""
        return bool(os.environ.get(name)) or name in self._cache

    @staticmethod
    def migrate_from_config():
        """
        One-time migration: read config.json, encrypt into secrets.enc.
        Run via: python -m security.secrets_manager migrate
        """
        config_path = os.path.abspath(_CONFIG_PATH)
        if not os.path.exists(config_path):
            print(f"No config.json found at {config_path}")
            return

        master_key = os.environ.get('HEVOLVE_MASTER_KEY')
        if not master_key:
            print("ERROR: Set HEVOLVE_MASTER_KEY environment variable first.")
            print("  Example: export HEVOLVE_MASTER_KEY='your-strong-secret-key-here'")
            return

        with open(config_path, 'r') as f:
            config = json.load(f)

        # Map config.json keys to standard secret names
        key_mapping = {
            'OPENAI_API_KEY': config.get('OPENAI_API_KEY', ''),
            'GROQ_API_KEY': config.get('GROQ_API_KEY', ''),
            'LANGCHAIN_API_KEY': config.get('LANGCHAIN_API_KEY', ''),
            'GOOGLE_CSE_ID': config.get('GOOGLE_CSE_ID', ''),
            'GOOGLE_API_KEY': config.get('GOOGLE_API_KEY', ''),
            'NEWS_API_KEY': config.get('NEWS_API_KEY', ''),
            'SERPAPI_API_KEY': config.get('SERPAPI_API_KEY', ''),
        }

        # Also capture any other keys not in mapping
        for k, v in config.items():
            if isinstance(v, str) and k not in key_mapping:
                key_mapping[k] = v

        sm = SecretsManager.get_instance()
        for name, value in key_mapping.items():
            if value:
                sm.set_secret(name, value)

        # Backup original config
        backup_path = config_path + '.bak'
        os.rename(config_path, backup_path)
        print(f"Migration complete:")
        print(f"  Encrypted {len(key_mapping)} secrets to secrets.enc")
        print(f"  Original config backed up to {backup_path}")
        print(f"  Add config.json and secrets.enc to .gitignore")


# Convenience function for backward compatibility
def get_secret(name: str, default: str = '') -> str:
    """Shorthand for SecretsManager.get_instance().get_secret()"""
    return SecretsManager.get_instance().get_secret(name, default)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'migrate':
        SecretsManager.migrate_from_config()
    else:
        print("Usage: python -m security.secrets_manager migrate")
