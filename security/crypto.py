"""
Encryption for Data at Rest and Agent-to-Agent E2E Communication
Uses Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256).
"""

import os
import json
import logging
from typing import Optional, Union

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger('hevolve_security')

# Fernet-encrypted data always starts with 'gAAAAA'
_FERNET_PREFIX = b'gAAAAA'


def _get_data_key() -> Optional[Fernet]:
    """Get Fernet cipher for data-at-rest encryption."""
    key = os.environ.get('HEVOLVE_DATA_KEY')
    if not key:
        try:
            from security.secrets_manager import get_secret
            key = get_secret('HEVOLVE_DATA_KEY')
        except Exception:
            pass

    if not key:
        return None

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        logger.error("Invalid HEVOLVE_DATA_KEY format. Must be a Fernet key.")
        return None


def generate_data_key() -> str:
    """Generate a new Fernet key for data encryption. Store this securely."""
    return Fernet.generate_key().decode()


def encrypt_data(plaintext: Union[str, bytes]) -> bytes:
    """
    Encrypt data using Fernet.
    Returns encrypted bytes, or original data if no key is configured.
    """
    fernet = _get_data_key()
    if fernet is None:
        if isinstance(plaintext, str):
            return plaintext.encode()
        return plaintext

    if isinstance(plaintext, str):
        plaintext = plaintext.encode()

    return fernet.encrypt(plaintext)


def decrypt_data(ciphertext: Union[str, bytes]) -> bytes:
    """
    Decrypt Fernet-encrypted data.
    Auto-detects if data is encrypted (Fernet prefix) or plaintext.
    Returns decrypted bytes.
    """
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode()

    # Auto-detect: if not Fernet-encrypted, return as-is
    if not ciphertext.startswith(_FERNET_PREFIX):
        return ciphertext

    fernet = _get_data_key()
    if fernet is None:
        logger.warning("Encrypted data found but HEVOLVE_DATA_KEY not set")
        return ciphertext

    try:
        return fernet.decrypt(ciphertext)
    except InvalidToken:
        logger.error("Failed to decrypt data - wrong key or corrupted data")
        return ciphertext


def encrypt_json_file(filepath: str, data: dict):
    """
    Write JSON data to an encrypted file.
    Falls back to plaintext if encryption key not configured.
    """
    plaintext = json.dumps(data, indent=2).encode()
    encrypted = encrypt_data(plaintext)

    with open(filepath, 'wb') as f:
        f.write(encrypted)


def decrypt_json_file(filepath: str) -> Optional[dict]:
    """
    Read and decrypt a JSON file.
    Auto-detects encrypted vs plaintext files.
    """
    if not os.path.exists(filepath):
        return None

    with open(filepath, 'rb') as f:
        raw = f.read()

    decrypted = decrypt_data(raw)

    try:
        return json.loads(decrypted.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Try reading as regular text file (legacy plaintext JSON)
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception:
            logger.error(f"Failed to read JSON file: {filepath}")
            return None


class A2ACrypto:
    """
    End-to-end encryption for agent-to-agent communication.
    Each session gets a unique symmetric key.
    """

    def __init__(self, session_key: Optional[bytes] = None):
        if session_key:
            self._fernet = Fernet(session_key)
            self._key = session_key
        else:
            self._key = Fernet.generate_key()
            self._fernet = Fernet(self._key)

    @property
    def session_key(self) -> bytes:
        """The session key (share with peer agent for decryption)."""
        return self._key

    def encrypt_message(self, plaintext: str) -> str:
        """Encrypt a message for transmission."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt_message(self, ciphertext: str) -> str:
        """Decrypt a received message."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            logger.error("Failed to decrypt A2A message - invalid key or corrupted")
            raise ValueError("Decryption failed: invalid key or corrupted message")

    def encrypt_payload(self, payload: dict) -> str:
        """Encrypt a dict payload as JSON."""
        plaintext = json.dumps(payload)
        return self.encrypt_message(plaintext)

    def decrypt_payload(self, ciphertext: str) -> dict:
        """Decrypt a JSON payload."""
        plaintext = self.decrypt_message(ciphertext)
        return json.loads(plaintext)
