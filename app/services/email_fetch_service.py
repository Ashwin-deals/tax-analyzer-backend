from __future__ import annotations

import email
import imaplib
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.analysis_service import AnalysisError, ingest_and_analyze_statement_content
from app.services.database import repository, utc_now
from app.services.secret_service import SecretError, decrypt_secret, encrypt_secret, is_masked_secret
from src.loader import SUPPORTED_EXTENSIONS


logger = logging.getLogger(__name__)

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
FREQUENCY_INTERVALS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}
FREQUENCY_ALIASES = {
    "every 1 hour": "1h",
    "every 6 hours": "6h",
    "day": "daily",
    "daily": "daily",
    "week": "weekly",
    "weekly": "weekly",
    "1h": "1h",
    "6h": "6h",
}
WEEKDAY_OPTIONS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
STATEMENT_PASSWORD_TYPES = {"none", "fixed", "ask_every_time"}

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STARTED = False
_RUNNING_USERS: set[str] = set()


class EmailFetchError(Exception):
    """Raised when FinScan cannot save, test, or run Gmail auto-fetch."""


def normalize_fetch_frequency(value: str | None) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return FREQUENCY_ALIASES.get(normalized, "6h")


def normalize_fetch_time(value: str | None) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not match:
        return "09:00"
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def normalize_fetch_day(value: str | None) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower())
    return text if text in WEEKDAY_OPTIONS else "monday"


def normalize_statement_password_type(value: str | None) -> str:
    text = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    return text if text in STATEMENT_PASSWORD_TYPES else "none"


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    fragments: list[str] = []
    for fragment, charset in decode_header(value):
        if isinstance(fragment, bytes):
            fragments.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            fragments.append(fragment)
    return "".join(fragments).strip()


def _next_fetch_at(
    frequency: str,
    fetch_time: str | None = None,
    fetch_day: str | None = None,
    from_dt: datetime | None = None,
) -> str:
    frequency = normalize_fetch_frequency(frequency)
    now_utc = from_dt or datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    if frequency in {"1h", "6h"}:
        return (now_utc + FREQUENCY_INTERVALS[frequency]).isoformat()

    hour, minute = [int(part) for part in normalize_fetch_time(fetch_time).split(":", 1)]
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "weekly":
        target_weekday = WEEKDAY_OPTIONS[normalize_fetch_day(fetch_day)]
        days_ahead = (target_weekday - now_local.weekday()) % 7
        target = target + timedelta(days=days_ahead)
        if target <= now_local:
            target = target + timedelta(days=7)
    elif target <= now_local:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


def _open_gmail_mailbox(gmail_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    if not gmail_address or "@" not in gmail_address:
        raise EmailFetchError("A valid Gmail address is required")
    if not app_password:
        raise EmailFetchError("Gmail app password is required")
    try:
        client = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
        client.login(gmail_address, app_password)
        return client
    except imaplib.IMAP4.error as exc:
        raise EmailFetchError(f"Gmail IMAP login failed: {exc}") from exc
    except OSError as exc:
        raise EmailFetchError(f"Could not connect to Gmail IMAP: {exc}") from exc


def _business_for_setting(user_id: str, business_id: str | None) -> dict[str, Any]:
    if business_id:
        business = repository.get_business(business_id)
        if business and business.get("userId") == user_id:
            return business
        raise EmailFetchError("Selected business profile is not available for this user")
    businesses = repository.list_businesses(user_id)
    if businesses:
        return businesses[0]
    raise EmailFetchError("Create a business profile before enabling email auto fetch")


def save_email_settings(
    *,
    user_id: str,
    gmail_address: str | None,
    app_password: str | None,
    auto_fetch_enabled: bool,
    fetch_frequency: str | None,
    fetch_time: str | None,
    fetch_day: str | None,
    statement_password_type: str | None,
    statement_password: str | None,
    business_id: str | None,
) -> dict[str, Any]:
    existing = repository.get_email_settings_record(user_id) or {}
    business = _business_for_setting(user_id, business_id or existing.get("business_id"))

    encrypted_password = existing.get("encrypted_gmail_app_password") or existing.get("encrypted_app_password")
    if app_password and not is_masked_secret(app_password):
        try:
            encrypted_password = encrypt_secret(app_password, label="Gmail app password")
        except SecretError as exc:
            raise EmailFetchError(str(exc)) from exc
    if auto_fetch_enabled and not encrypted_password:
        raise EmailFetchError("Gmail app password is required before enabling auto fetch")

    address = (gmail_address or existing.get("gmail_address") or "").strip().lower()
    if auto_fetch_enabled and ("@" not in address):
        raise EmailFetchError("A valid Gmail address is required before enabling auto fetch")

    frequency = normalize_fetch_frequency(fetch_frequency)
    normalized_time = normalize_fetch_time(fetch_time or existing.get("fetch_time"))
    normalized_day = normalize_fetch_day(fetch_day or existing.get("fetch_day"))
    next_fetch_at = _next_fetch_at(frequency, normalized_time, normalized_day) if auto_fetch_enabled else None

    password_type = normalize_statement_password_type(
        statement_password_type if statement_password_type is not None else existing.get("statement_password_type")
    )
    encrypted_statement_password = existing.get("encrypted_statement_password")
    if password_type == "fixed":
        if statement_password and not is_masked_secret(statement_password):
            try:
                encrypted_statement_password = encrypt_secret(statement_password, label="statement password")
            except SecretError as exc:
                raise EmailFetchError(str(exc)) from exc
        if not encrypted_statement_password:
            raise EmailFetchError("Statement password is required when Fixed Password is selected")
    else:
        encrypted_statement_password = None

    doc = repository.upsert_email_settings(
        user_id,
        {
            "auto_fetch_enabled": bool(auto_fetch_enabled),
            "gmail_address": address,
            "encrypted_gmail_app_password": encrypted_password,
            "encrypted_app_password": encrypted_password,
            "fetch_frequency": frequency,
            "fetch_time": normalized_time,
            "fetch_day": normalized_day,
            "next_fetch_at": next_fetch_at,
            "business_id": business["businessId"],
            "business_name": business.get("name"),
            "statement_password_type": password_type,
            "encrypted_statement_password": encrypted_statement_password,
        },
    )
    return repository.public_email_settings(doc)


def test_email_connection(
    *,
    user_id: str,
    gmail_address: str | None = None,
    app_password: str | None = None,
) -> dict[str, Any]:
    existing = repository.get_email_settings_record(user_id) or {}
    address = (gmail_address or existing.get("gmail_address") or "").strip().lower()
    if app_password and not is_masked_secret(app_password):
        password = app_password
    else:
        try:
            password = decrypt_secret(
                existing.get("encrypted_gmail_app_password") or existing.get("encrypted_app_password"),
                label="Gmail app password",
            )
        except SecretError as exc:
            raise EmailFetchError(str(exc)) from exc

    client = _open_gmail_mailbox(address, password)
    try:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise EmailFetchError("Gmail inbox could not be opened")
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    repository.update_email_fetch_status(user_id, {"last_test_at": utc_now(), "last_fetch_error": None})
    return {"connected": True, "message": "Gmail connection successful"}


def _message_attachments(message: Message) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        raw_filename = part.get_filename()
        if not raw_filename:
            continue
        filename = _decode_header_value(raw_filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        content = part.get_payload(decode=True)
        if not content:
            continue
        attachments.append(
            {
                "filename": filename,
                "contentType": part.get_content_type(),
                "content": content,
            }
        )
    return attachments


def _fetch_message(client: imaplib.IMAP4_SSL, message_id: bytes) -> Message:
    status, payload = client.fetch(message_id, "(RFC822)")
    if status != "OK" or not payload:
        raise EmailFetchError("Could not fetch unread email from Gmail")
    for item in payload:
        if isinstance(item, tuple) and item[1]:
            return email.message_from_bytes(item[1])
    raise EmailFetchError("Unread email did not contain a readable message body")


def run_email_fetch_for_user(user_id: str) -> dict[str, Any]:
    user = repository.get_user(user_id)
    if not user:
        raise EmailFetchError("Authenticated user was not found")
    setting = repository.get_email_settings_record(user_id)
    if not setting:
        raise EmailFetchError("Email auto fetch settings have not been saved")

    business = _business_for_setting(user_id, setting.get("business_id"))
    address = setting.get("gmail_address") or ""
    try:
        password = decrypt_secret(
            setting.get("encrypted_gmail_app_password") or setting.get("encrypted_app_password"),
            label="Gmail app password",
        )
    except SecretError as exc:
        raise EmailFetchError(str(exc)) from exc

    result: dict[str, Any] = {
        "processedMessages": 0,
        "processedAttachments": 0,
        "skippedMessages": 0,
        "createdStatements": [],
        "errors": [],
    }

    client = _open_gmail_mailbox(address, password)
    try:
        status, _ = client.select("INBOX")
        if status != "OK":
            raise EmailFetchError("Gmail inbox could not be opened")
        status, search_data = client.search(None, "UNSEEN")
        if status != "OK":
            raise EmailFetchError("Could not search unread Gmail messages")

        message_ids = search_data[0].split() if search_data and search_data[0] else []
        for message_id in message_ids:
            try:
                message = _fetch_message(client, message_id)
                attachments = _message_attachments(message)
                if not attachments:
                    result["skippedMessages"] += 1
                    continue

                subject = _decode_header_value(message.get("Subject"))
                sender = _decode_header_value(message.get("From"))
                message_date = _decode_header_value(message.get("Date"))
                email_message_id = _decode_header_value(message.get("Message-ID"))
                created_for_message: list[dict[str, Any]] = []

                for attachment in attachments:
                    processed = ingest_and_analyze_statement_content(
                        content=attachment["content"],
                        filename=attachment["filename"],
                        content_type=attachment["contentType"],
                        business_id=business["businessId"],
                        user_id=user_id,
                        source="email",
                        source_metadata={
                            "gmailAddress": address,
                            "emailSubject": subject,
                            "emailFrom": sender,
                            "emailDate": message_date,
                            "emailMessageId": email_message_id,
                        },
                    )
                    statement = processed["statement"]
                    created_for_message.append(
                        {
                            "statementId": statement.get("statementId"),
                            "filename": statement.get("filename"),
                            "s3Key": statement.get("s3Key"),
                        }
                    )
                    result["processedAttachments"] += 1

                client.store(message_id, "+FLAGS", "\\Seen")
                result["processedMessages"] += 1
                result["createdStatements"].extend(created_for_message)
            except (AnalysisError, EmailFetchError, imaplib.IMAP4.error, OSError) as exc:
                logger.warning("Gmail auto fetch skipped message for user %s: %s", user_id, exc)
                result["errors"].append(str(exc))
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    status_text = "completed_with_errors" if result["errors"] else "completed"
    repository.update_email_fetch_status(
        user_id,
        {
            "last_fetch_at": utc_now(),
            "last_fetch_status": status_text,
            "last_fetch_error": "; ".join(result["errors"])[:1000] if result["errors"] else None,
            "next_fetch_at": (
                _next_fetch_at(setting.get("fetch_frequency"), setting.get("fetch_time"), setting.get("fetch_day"))
                if setting.get("auto_fetch_enabled")
                else None
            ),
        },
    )
    return result


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _setting_is_due(setting: dict[str, Any]) -> bool:
    next_fetch_at = _parse_utc(setting.get("next_fetch_at"))
    if not next_fetch_at:
        return True
    return datetime.now(timezone.utc) >= next_fetch_at


def _scheduler_loop() -> None:
    while True:
        try:
            for setting in repository.list_enabled_email_settings():
                user_id = setting.get("user_id")
                if not user_id or not _setting_is_due(setting):
                    continue
                with _SCHEDULER_LOCK:
                    if user_id in _RUNNING_USERS:
                        continue
                    _RUNNING_USERS.add(user_id)
                try:
                    run_email_fetch_for_user(user_id)
                except EmailFetchError as exc:
                    logger.warning("Scheduled Gmail auto fetch failed for user %s: %s", user_id, exc)
                    repository.update_email_fetch_status(
                        user_id,
                        {
                            "last_fetch_at": utc_now(),
                            "last_fetch_status": "failed",
                            "last_fetch_error": str(exc)[:1000],
                            "next_fetch_at": _next_fetch_at(
                                setting.get("fetch_frequency"),
                                setting.get("fetch_time"),
                                setting.get("fetch_day"),
                            ),
                        },
                    )
                finally:
                    with _SCHEDULER_LOCK:
                        _RUNNING_USERS.discard(user_id)
        except Exception as exc:  # pragma: no cover - keeps background thread alive.
            logger.warning("Gmail auto fetch scheduler error: %s", exc)

        time.sleep(max(15, settings.email_fetch_scheduler_interval_seconds))


def start_email_fetch_scheduler() -> None:
    global _SCHEDULER_STARTED
    if not settings.email_fetch_scheduler_enabled:
        logger.info("Gmail auto fetch scheduler is disabled.")
        return
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return
        _SCHEDULER_STARTED = True
    thread = threading.Thread(target=_scheduler_loop, name="finscan-email-fetch-scheduler", daemon=True)
    thread.start()
    logger.info("Gmail auto fetch scheduler started.")
