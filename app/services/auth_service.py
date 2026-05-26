from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_ROOT, settings


class AuthError(Exception):
    """Raised when authentication or user management fails."""


_STORE_LOCK = threading.RLock()
_STORE_PATH = BACKEND_ROOT / "data" / "auth_store.json"
_PBKDF2_ITERATIONS = 260_000
_SECRET_PREFIX = "enc:v1:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "business"


def _empty_store() -> dict[str, Any]:
    return {
        "users": [],
        "businesses": [],
        "emailSettings": {},
    }


def _read_store() -> dict[str, Any]:
    if not _STORE_PATH.exists():
        return _empty_store()
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    return {
        **_empty_store(),
        **data,
    }


def _write_store(store: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _STORE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(_STORE_PATH)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _encryption_key() -> bytes:
    secret = os.getenv("EMAIL_ENCRYPTION_SECRET") or os.getenv("SECRET_KEY") or "finscan-local-dev-secret"
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < length:
        blocks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def _encrypt_secret(value: str | None) -> str:
    plaintext = (value or "").encode("utf-8")
    key = _encryption_key()
    nonce = secrets.token_bytes(16)
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream))
    signature = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return _SECRET_PREFIX + urlsafe_b64encode(nonce + signature + ciphertext).decode("ascii")


def _decrypt_secret(value: str | None) -> str:
    if not value or not value.startswith(_SECRET_PREFIX):
        return ""
    key = _encryption_key()
    try:
        raw = urlsafe_b64decode(value.removeprefix(_SECRET_PREFIX).encode("ascii"))
        nonce, signature, ciphertext = raw[:16], raw[16:48], raw[48:]
    except Exception:
        return ""
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return ""
    stream = _keystream(key, nonce, len(ciphertext))
    plaintext = bytes(left ^ right for left, right in zip(ciphertext, stream))
    return plaintext.decode("utf-8", errors="replace")


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "userId": user["userId"],
        "name": user.get("name") or user.get("username") or "User",
        "username": user.get("username"),
        "email": user.get("email"),
    }


def _public_business(business: dict[str, Any] | None) -> dict[str, Any] | None:
    if not business:
        return None
    return {
        "businessId": business["businessId"],
        "userId": business["userId"],
        "name": business.get("name") or "Business",
        "industry": business.get("industry") or "Business",
    }


def _business_for_user(store: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    return next((business for business in store["businesses"] if business.get("userId") == user_id), None)


def _auth_response(user: dict[str, Any], business: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "token": f"session-{user['userId']}",
        "authMode": "password",
        "user": _public_user(user),
        "business": _public_business(business),
    }


def token_user_id(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("session-"):
        return None
    return token.removeprefix("session-").strip() or None


def require_user_id(authorization: str | None) -> str:
    user_id = token_user_id(authorization)
    if not user_id:
        raise AuthError("Valid user session is required")
    return user_id


def register_user(*, name: str, username: str, email: str, password: str, business_name: str) -> dict[str, Any]:
    clean_name = name.strip()
    clean_username = username.strip().lower()
    clean_email = email.strip().lower()
    clean_business_name = business_name.strip()
    if not clean_name or not clean_username or not clean_email or not password or not clean_business_name:
        raise AuthError("Name, username, email, password, and business name are required")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters")

    with _STORE_LOCK:
        store = _read_store()
        if any(user.get("email", "").lower() == clean_email for user in store["users"]):
            raise AuthError("Email is already registered")
        if any(user.get("username", "").lower() == clean_username for user in store["users"]):
            raise AuthError("Username is already registered")

        user = {
            "userId": uuid.uuid4().hex,
            "name": clean_name,
            "username": clean_username,
            "email": clean_email,
            "passwordHash": _hash_password(password),
            "createdAt": _now(),
        }
        business = {
            "businessId": uuid.uuid4().hex,
            "userId": user["userId"],
            "name": clean_business_name,
            "slug": _slug(clean_business_name),
            "industry": "Business",
            "createdAt": _now(),
        }
        store["users"].append(user)
        store["businesses"].append(business)
        _write_store(store)
        return _auth_response(user, business)


def login(identifier: str, password: str) -> dict[str, Any]:
    clean_identifier = identifier.strip().lower()
    if not clean_identifier or not password:
        raise AuthError("Username/email and password are required")

    with _STORE_LOCK:
        store = _read_store()
        user = next(
            (
                candidate
                for candidate in store["users"]
                if candidate.get("username", "").lower() == clean_identifier
                or candidate.get("email", "").lower() == clean_identifier
            ),
            None,
        )
        if not user:
            if settings.environment != "production" and not store["users"]:
                return register_user(
                    name=identifier.strip() or "Example User",
                    username=re.sub(r"[^a-z0-9_]+", "_", clean_identifier).strip("_") or "example_user",
                    email=f"{_slug(clean_identifier)}@example.com",
                    password=password,
                    business_name=identifier.strip() or "Example Business",
                )
            raise AuthError("Invalid username/email or password")
        if not _verify_password(password, user.get("passwordHash", "")):
            raise AuthError("Invalid username/email or password")
        return _auth_response(user, _business_for_user(store, user["userId"]))


def list_businesses(user_id: str) -> list[dict[str, Any]]:
    with _STORE_LOCK:
        store = _read_store()
        return [_public_business(business) for business in store["businesses"] if business.get("userId") == user_id]


def default_business_id(user_id: str) -> str | None:
    with _STORE_LOCK:
        business = _business_for_user(_read_store(), user_id)
    return business.get("businessId") if business else None


def create_business(user_id: str, name: str, industry: str | None = None) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise AuthError("Business name is required")
    with _STORE_LOCK:
        store = _read_store()
        if not any(user.get("userId") == user_id for user in store["users"]):
            raise AuthError("Authenticated user was not found")
        business = {
            "businessId": uuid.uuid4().hex,
            "userId": user_id,
            "name": clean_name,
            "slug": _slug(clean_name),
            "industry": industry or "Business",
            "createdAt": _now(),
        }
        store["businesses"].append(business)
        _write_store(store)
        return _public_business(business)


def get_email_settings(user_id: str) -> dict[str, Any]:
    with _STORE_LOCK:
        settings_doc = (_read_store().get("emailSettings") or {}).get(user_id, {})
    return {
        "enableAutoFetch": bool(settings_doc.get("enableAutoFetch")),
        "gmailAddress": settings_doc.get("gmailAddress") or "",
        "gmailAppPassword": "********" if settings_doc.get("encryptedGmailAppPassword") else "",
        "fetchFrequency": settings_doc.get("fetchFrequency") or "6h",
        "fetchTime": settings_doc.get("fetchTime") or "09:00",
        "fetchDay": settings_doc.get("fetchDay") or "monday",
        "statementPasswordType": settings_doc.get("statementPasswordType") or "none",
        "statementPassword": "********" if settings_doc.get("encryptedStatementPassword") else "",
        "businessId": settings_doc.get("businessId") or "",
    }


def get_private_email_settings(user_id: str) -> dict[str, Any]:
    with _STORE_LOCK:
        settings_doc = (_read_store().get("emailSettings") or {}).get(user_id, {})
    return {
        **get_email_settings(user_id),
        "gmailAppPassword": _decrypt_secret(settings_doc.get("encryptedGmailAppPassword")),
        "statementPassword": _decrypt_secret(settings_doc.get("encryptedStatementPassword")),
    }


def save_email_settings(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _STORE_LOCK:
        store = _read_store()
        current = (store.setdefault("emailSettings", {}).get(user_id) or {})
        next_settings = {
            **current,
            "enableAutoFetch": bool(payload.get("enableAutoFetch")),
            "gmailAddress": payload.get("gmailAddress") or "",
            "fetchFrequency": payload.get("fetchFrequency") or "6h",
            "fetchTime": payload.get("fetchTime") or "09:00",
            "fetchDay": payload.get("fetchDay") or "monday",
            "statementPasswordType": payload.get("statementPasswordType") or "none",
            "businessId": payload.get("businessId") or current.get("businessId") or "",
        }
        if payload.get("gmailAppPassword") and payload.get("gmailAppPassword") != "********":
            next_settings["encryptedGmailAppPassword"] = _encrypt_secret(payload.get("gmailAppPassword"))
            next_settings["gmailAppPasswordConfigured"] = True
        if payload.get("statementPassword") and payload.get("statementPassword") != "********":
            next_settings["encryptedStatementPassword"] = _encrypt_secret(payload.get("statementPassword"))
            next_settings["statementPasswordConfigured"] = True
        store["emailSettings"][user_id] = next_settings
        _write_store(store)
    return get_email_settings(user_id)


def verify_account_password(user_id: str, password: str) -> None:
    with _STORE_LOCK:
        store = _read_store()
        user = next((candidate for candidate in store["users"] if candidate.get("userId") == user_id), None)
        if not user:
            raise AuthError("Authenticated user was not found")
        if not _verify_password(password, user.get("passwordHash", "")):
            raise AuthError("Password is incorrect")


def delete_account(user_id: str, password: str) -> None:
    verify_account_password(user_id, password)
    with _STORE_LOCK:
        store = _read_store()
        store["users"] = [candidate for candidate in store["users"] if candidate.get("userId") != user_id]
        store["businesses"] = [business for business in store["businesses"] if business.get("userId") != user_id]
        store.get("emailSettings", {}).pop(user_id, None)
        _write_store(store)
