#!/usr/bin/env python3
"""
Data Security - Encryption and decryption for user data.

Provides Fernet symmetric encryption with PBKDF2HMAC-SHA256 key derivation.
Keys are derived from the user's login password and never written to disk —
they live only in AuthSystem._user_keys for the duration of the session.

Graceful fallback: if decryption fails (legacy plaintext data), the original
bytes are returned unchanged. This allows transparent migration of existing
unencrypted data — old entries are served as-is and will be re-encrypted on
next write.
"""

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


@dataclass
class EncryptionConfig:
    """Configuration for data encryption."""
    enabled: bool = False
    algorithm: str = "Fernet/AES-128-CBC"
    key_derivation: str = "PBKDF2HMAC-SHA256"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "algorithm": self.algorithm,
            "key_derivation": self.key_derivation,
        }


class DataEncryptor:
    """
    Handles encryption/decryption of user data using Fernet symmetric encryption.

    Key is derived from the user's login password via PBKDF2HMAC-SHA256 and
    lives in memory only — never written to disk.

    Graceful fallback: decrypting plaintext (legacy) data returns it unchanged.
    """

    def __init__(self, user_key: Optional[bytes] = None):
        """
        Args:
            user_key: URL-safe base64-encoded 32-byte Fernet key.
                      None means encryption is disabled (plaintext mode).
        """
        self.user_key = user_key
        self.config = EncryptionConfig(enabled=(user_key is not None))
        self._fernet = Fernet(user_key) if user_key else None

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt bytes. Returns data unchanged if encryption disabled."""
        if not self._fernet:
            return data
        return self._fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        """
        Decrypt bytes. Falls back to returning data unchanged if it is
        legacy plaintext (InvalidToken) or encryption is disabled.
        """
        if not self._fernet:
            return data
        try:
            return self._fernet.decrypt(data)
        except (InvalidToken, Exception):
            # Legacy plaintext — return as-is for graceful migration
            return data

    def encrypt_str(self, s: str) -> str:
        """Encrypt a UTF-8 string, returning an encrypted string."""
        if not self._fernet:
            return s
        return self.encrypt(s.encode()).decode()

    def decrypt_str(self, s: str) -> str:
        """Decrypt an encrypted string. Falls back to returning s if not encrypted."""
        if not self._fernet:
            return s
        return self.decrypt(s.encode()).decode()

    def encrypt_json(self, data: Any) -> bytes:
        """Serialize to JSON and encrypt."""
        json_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
        return self.encrypt(json_bytes)

    def decrypt_json(self, data: bytes) -> Any:
        """Decrypt and deserialize JSON data."""
        decrypted = self.decrypt(data)
        return json.loads(decrypted.decode('utf-8'))

    def encrypt_file(self, path: Path, data: Any):
        """Encrypt and write JSON-serializable data to a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._fernet:
            encrypted = self.encrypt_json(data)
            path.write_bytes(encrypted)
        else:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)

    def decrypt_file(self, path: Path) -> Any:
        """Read and decrypt data from a file."""
        if not path.exists():
            return None
        if self._fernet:
            encrypted = path.read_bytes()
            return self.decrypt_json(encrypted)
        else:
            with open(path) as f:
                return json.load(f)


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    """
    Derive a Fernet-compatible key from a user's password using PBKDF2HMAC-SHA256.

    Args:
        password: User's plaintext password
        salt: Random salt (16 bytes) stored per-user in users.json

    Returns:
        URL-safe base64-encoded 32-byte key suitable for Fernet
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    raw_key = kdf.derive(password.encode('utf-8'))
    return base64.urlsafe_b64encode(raw_key)


def generate_salt() -> bytes:
    """Generate a random 16-byte salt for key derivation."""
    return os.urandom(16)


class SecureUserData:
    """
    Wrapper for per-user data with encryption support.

    Handles reading/writing user data files with optional Fernet encryption.
    """

    def __init__(self, user_id: str, user_key: Optional[bytes] = None):
        """
        Args:
            user_id: Unique user identifier
            user_key: Fernet key derived from user's password (None = plaintext)
        """
        self.user_id = user_id
        self.encryptor = DataEncryptor(user_key)
        self.base_path = Path.home() / ".local/share/3am/users" / user_id

    def save(self, filename: str, data: Any):
        """Save data to a user file (encrypted if key provided)."""
        path = self.base_path / filename
        self.encryptor.encrypt_file(path, data)

    def load(self, filename: str, default: Any = None) -> Any:
        """Load data from a user file (decrypted if key provided)."""
        path = self.base_path / filename
        try:
            result = self.encryptor.decrypt_file(path)
            return result if result is not None else default
        except Exception:
            return default

    def exists(self, filename: str) -> bool:
        """Check if a user data file exists."""
        return (self.base_path / filename).exists()

    def delete(self, filename: str) -> bool:
        """Delete a user data file."""
        path = self.base_path / filename
        if path.exists():
            path.unlink()
            return True
        return False

    def list_files(self, pattern: str = "*") -> list[Path]:
        """List files in user's data directory."""
        if not self.base_path.exists():
            return []
        return list(self.base_path.glob(pattern))
