from __future__ import annotations

import base64
import hashlib
import logging

from app.core.config import settings

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dependency declared in requirements.
    Fernet = None
    InvalidToken = Exception


logger = logging.getLogger(__name__)

MASKED_SECRET = "********"


class SecretError(Exception):
    """Raised when FinScan cannot encrypt or decrypt a user secret."""


def is_masked_secret(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text) and set(text) == {"*"} and len(text) >= 6


def _encryption_secret() -> str:
    secret = (settings.email_encryption_secret or "").strip()
    if secret:
        return secret
    logger.warning("EMAIL_ENCRYPTION_SECRET is not configured; using development fallback for user secret encryption.")
    return f"{settings.app_name}:{settings.mongodb_database}:development-email-secret"


def _fernet() -> Fernet:
    if Fernet is None:
        raise SecretError("cryptography is not installed. Install backend dependencies with pip install -r requirements.txt.")
    key = base64.urlsafe_b64encode(hashlib.sha256(_encryption_secret().encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str, label: str = "secret") -> str:
    if not value:
        raise SecretError(f"{label} is required")
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted_value: str | None, label: str = "secret") -> str:
    if not encrypted_value:
        raise SecretError(f"{label} has not been saved")
    try:
        return _fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError(f"Saved {label} could not be decrypted. Re-save the settings.") from exc
