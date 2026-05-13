"""
utils/email_utils.py
────────────────────
Helper utilities for Gmail credential loading, validation, and safe display.
Credentials are NEVER hardcoded — always loaded from the .env file or
from OS environment variables.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Credential loading ────────────────────────────────────────────────────────

def load_credentials(env_path: Path | None = None) -> tuple[str, str]:
    """
    Load Gmail credentials from environment variables.

    Resolution order:
      1. Reads .env file from project root (via python-dotenv, if installed).
      2. Falls back to OS environment variables (e.g. set via shell / CI).

    Args:
        env_path: Optional explicit path to the .env file.
                  Defaults to <project_root>/.env.

    Returns:
        (email_address, app_password) tuple.

    Raises:
        ValueError: if EMAIL_ADDRESS or EMAIL_PASSWORD is missing.
    """
    # Load .env if python-dotenv is available
    try:
        from dotenv import load_dotenv
        env_file = env_path or (Path(__file__).resolve().parent.parent / ".env")
        if env_file.exists():
            load_dotenv(env_file, override=False)
            logger.debug("Loaded .env from: %s", env_file)
        else:
            logger.debug(".env not found at %s — reading OS environment.", env_file)
    except ImportError:
        logger.warning(
            "python-dotenv not installed. Reading EMAIL_ADDRESS / EMAIL_PASSWORD "
            "from OS environment only."
        )

    email_addr   = os.environ.get("EMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("EMAIL_PASSWORD", "").strip()

    missing = []
    if not email_addr:
        missing.append("EMAIL_ADDRESS")
    if not app_password:
        missing.append("EMAIL_PASSWORD")

    if missing:
        raise ValueError(
            f"Missing required credential(s): {', '.join(missing)}.\n"
            "Create a .env file in the project root with:\n"
            "  EMAIL_ADDRESS=your_email@gmail.com\n"
            "  EMAIL_PASSWORD=your_16_char_app_password\n\n"
            "See .env.example for the template. "
            "Use a Gmail App Password — NOT your regular Gmail password."
        )

    return email_addr, app_password


def is_configured(env_path: Path | None = None) -> bool:
    """
    Returns True if both EMAIL_ADDRESS and EMAIL_PASSWORD are set in .env
    or OS environment. Safe to call without raising.
    """
    try:
        load_credentials(env_path)
        return True
    except ValueError:
        return False


def mask_email(email_addr: str) -> str:
    """
    Return a safely masked version of an email address for display/logging.
    E.g.: "ashwin@gmail.com" → "as****@gmail.com"
    """
    parts = email_addr.split("@")
    if len(parts) != 2:
        return "***@***"
    local, domain = parts
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[:2] + "*" * (len(local) - 2)
    return f"{masked_local}@{domain}"
