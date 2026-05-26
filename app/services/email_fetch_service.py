from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.analysis_service import AnalysisError, cleanup_paths, ingest_local_statement
from src import email_fetcher


logger = logging.getLogger(__name__)


class EmailFetchError(Exception):
    """Raised when Gmail auto-fetch cannot run for a user."""


def run_email_fetch_now(
    *,
    user_id: str,
    business_id: str,
    gmail_address: str,
    gmail_app_password: str,
) -> dict[str, Any]:
    if not gmail_address:
        raise EmailFetchError("Gmail address is required. Save Email Auto Fetch Settings first.")
    if not gmail_app_password:
        raise EmailFetchError("Gmail app password is required. Re-enter and save it before running fetch.")
    if not business_id:
        raise EmailFetchError("Business profile is required before fetching statements.")

    output_dir = settings.email_statement_dir / user_id
    try:
        attachments = email_fetcher.fetch_statement_attachments(
            gmail_address,
            gmail_app_password,
            output_dir,
        )
    except email_fetcher.GmailFetchError as exc:
        raise EmailFetchError(str(exc)) from exc

    processed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    successful_message_ids: set[str] = set()
    downloaded_paths = [Path(item["path"]) for item in attachments if item.get("path")]

    try:
        for attachment in attachments:
            path = Path(attachment["path"])
            message_id = str(attachment.get("message_id") or "")
            try:
                result = ingest_local_statement(path, business_id=business_id, user_id=user_id)
                processed.append(result)
                if message_id:
                    successful_message_ids.add(message_id)
            except AnalysisError as exc:
                failed.append({"filename": path.name, "error": str(exc)})
                logger.warning("Could not process fetched statement %s: %s", path.name, exc)

        if successful_message_ids:
            email_fetcher.mark_messages_seen(gmail_address, gmail_app_password, successful_message_ids)

    finally:
        cleanup_paths(downloaded_paths)

    return {
        "processedAttachments": len(processed),
        "failedAttachments": len(failed),
        "statements": processed,
        "errors": failed,
        "message": _result_message(len(processed), len(failed), len(attachments)),
    }


def _result_message(processed_count: int, failed_count: int, attachment_count: int) -> str:
    if attachment_count == 0:
        return "No unread Gmail statement attachments were found."
    if failed_count and processed_count:
        return f"Processed {processed_count} statement attachment(s); {failed_count} failed."
    if failed_count:
        return f"Found {failed_count} statement attachment(s), but none could be processed."
    return f"Processed {processed_count} statement attachment(s)."
