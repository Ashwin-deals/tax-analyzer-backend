"""
src/email_fetcher.py
────────────────────
Connects to Gmail via IMAP, finds emails likely containing bank statements,
downloads statement attachments, and returns their local paths.

No credentials are hardcoded. All secrets are loaded from .env via
utils/email_utils.py.

Usage:
    from src.email_fetcher import fetch_statements
    paths = fetch_statements(email_addr, app_password, output_dir)
"""

import email
import imaplib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from email.header import decode_header
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
IMAP_HOST        = "imap.gmail.com"
IMAP_PORT        = 993
VALID_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}
TEMP_FILE_TTL_SECONDS = int(os.getenv("TEMP_FILE_TTL_SECONDS", "3600"))

# Subject keywords used to identify statement emails (case-insensitive OR)
SUBJECT_KEYWORDS = [
    "statement",
    "bank statement",
    "account statement",
    "monthly statement",
    "e-statement",
    "eStatement",
]


# ── Public API ────────────────────────────────────────────────────────────────

class GmailFetchError(Exception):
    """Raised when Gmail connection or authentication fails."""


def fetch_statements(
    email_address: str,
    app_password: str,
    output_dir: Path,
    max_emails: int = 20,
) -> list[Path]:
    """
    Connect to Gmail, search for statement emails, download statement attachments.

    Args:
        email_address: Gmail address (from .env).
        app_password:  Gmail App Password — NOT the regular account password.
        output_dir:    Directory where attachments will be saved.
        max_emails:    Maximum number of recent matched emails to inspect.

    Returns:
        List of Paths to downloaded files (may be empty if none found).

    Raises:
        GmailFetchError: on connection / authentication failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_downloads(output_dir)
    downloaded: list[Path] = []

    try:
        conn = _connect(email_address, app_password)
    except imaplib.IMAP4.error as exc:
        raise GmailFetchError(
            f"Gmail login failed for {email_address}. "
            "Make sure IMAP is enabled and you are using an App Password, "
            f"not your regular Gmail password. Detail: {exc}"
        ) from exc

    try:
        msg_ids = _search_statement_emails(conn, max_emails)
        logger.info("Found %d candidate statement email(s).", len(msg_ids))

        for mid in msg_ids:
            paths = _download_attachments(conn, mid, output_dir)
            downloaded.extend(paths)

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    logger.info("Total attachments downloaded: %d", len(downloaded))
    return downloaded


@contextmanager
def temporary_fetch_statements(
    email_address: str,
    app_password: str,
    output_dir: Path,
    max_emails: int = 20,
) -> Iterator[list[Path]]:
    """
    Fetch statement attachments for immediate processing and delete them after use.
    """
    paths = fetch_statements(email_address, app_password, output_dir, max_emails=max_emails)
    try:
        yield paths
    finally:
        cleanup_downloaded_statements(paths)


def cleanup_downloaded_statements(paths: Iterable[Path | str]) -> None:
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            if path.exists() and path.is_file():
                path.unlink()
                logger.info("Removed temporary email statement: %s", path.name)
        except OSError as exc:
            logger.warning("Could not remove temporary email statement %s: %s", path, exc)


def cleanup_stale_downloads(output_dir: Path, ttl_seconds: int = TEMP_FILE_TTL_SECONDS) -> None:
    if ttl_seconds <= 0 or not output_dir.exists():
        return

    cutoff = time.time() - ttl_seconds
    for child in output_dir.iterdir():
        if child.name == ".gitkeep":
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            logger.info("Removed stale email statement temp path: %s", child)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not remove stale email statement temp path %s: %s", child, exc)


# ── Private helpers ───────────────────────────────────────────────────────────

def _connect(email_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    """Open an SSL IMAP connection to Gmail and select INBOX."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(email_address, app_password)
    conn.select("INBOX")
    return conn


def _search_statement_emails(
    conn: imaplib.IMAP4_SSL,
    limit: int,
) -> list[bytes]:
    """
    Search INBOX for UNREAD emails whose subject matches any SUBJECT_KEYWORD.

    Only unseen (unread) messages are returned. Once an email is fetched
    via RFC822, Gmail automatically marks it as \\Seen, so subsequent runs
    will not re-download the same statement.

    Returns the `limit` most-recent matched message IDs (descending order).
    """
    matched: set[bytes] = set()

    for keyword in SUBJECT_KEYWORDS:
        try:
            # UNSEEN restricts to unread emails only
            _, data = conn.search(None, f'UNSEEN SUBJECT "{keyword}"')
            if data and data[0]:
                for mid in data[0].split():
                    matched.add(mid)
        except imaplib.IMAP4.error as exc:
            logger.warning("IMAP search failed for keyword '%s': %s", keyword, exc)

    logger.info("Unread statement emails found: %d", len(matched))
    # Sort descending so most-recent emails come first
    sorted_ids = sorted(matched, key=lambda x: int(x), reverse=True)
    return sorted_ids[:limit]


def _decode_mime_words(raw: str) -> str:
    """Decode an RFC-2047 encoded header value (e.g. encoded filename) to str."""
    parts = decode_header(raw)
    result = ""
    for part, charset in parts:
        if isinstance(part, bytes):
            result += part.decode(charset or "utf-8", errors="replace")
        else:
            result += part
    return result.strip()


def _unique_dest(filename: str, output_dir: Path) -> Path:
    """
    Sanitize filename and return a unique path inside output_dir.
    Appends a timestamp suffix if the file already exists.
    """
    safe_name = re.sub(r'[^\w\s.\-]', '_', filename).strip()
    dest = output_dir / safe_name

    if dest.exists():
        stem   = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest   = output_dir / f"{stem}_{ts}{suffix}"

    return dest


def _download_attachments(
    conn: imaplib.IMAP4_SSL,
    msg_id: bytes,
    output_dir: Path,
) -> list[Path]:
    """
    Fetch a single email, extract valid statement attachments,
    and write them to output_dir. Returns list of saved Paths.
    """
    try:
        _, msg_data = conn.fetch(msg_id, "(RFC822)")
    except imaplib.IMAP4.error as exc:
        logger.warning("Failed to fetch email ID %s: %s", msg_id, exc)
        return []

    if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
        return []

    raw_bytes = msg_data[0][1]
    msg       = email.message_from_bytes(raw_bytes)
    subject   = _decode_mime_words(msg.get("Subject", "(no subject)"))
    logger.debug("Inspecting email: %s", subject)

    saved: list[Path] = []

    for part in msg.walk():
        disposition = part.get("Content-Disposition", "")
        if "attachment" not in disposition.lower():
            continue

        raw_filename = part.get_filename()
        if not raw_filename:
            continue

        filename = _decode_mime_words(raw_filename)
        ext      = Path(filename).suffix.lower()

        if ext not in VALID_EXTENSIONS:
            logger.debug("Skipping unsupported attachment type: %s", filename)
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            logger.warning("Empty payload for attachment: %s", filename)
            continue

        dest = _unique_dest(filename, output_dir)
        dest.write_bytes(payload)
        logger.info("Saved: %s (%d bytes)", dest.name, len(payload))
        saved.append(dest)

    return saved
