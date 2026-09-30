"""Shared local configuration for the bot and backend commands."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BASE_DIR = Path(__file__).parent
DEFAULT_DATABASE = BASE_DIR / "data" / "photobooth.db"


def load_local_env(path: Path = BASE_DIR / ".env") -> None:
    """Load simple KEY=VALUE entries without requiring another package."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def database_path() -> Path:
    # FAQ_DATABASE_PATH remains supported for existing PhotoLes installations.
    configured = os.environ.get("DATABASE_PATH") or os.environ.get("FAQ_DATABASE_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_DATABASE


def shop_timezone() -> ZoneInfo:
    timezone_name = os.environ.get("SHOP_TIMEZONE", "Asia/Singapore")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown SHOP_TIMEZONE: {timezone_name}") from error


def customer_service_chat_id() -> int | None:
    """Return the private operator chat that receives customer questions."""
    configured = os.environ.get("CUSTOMER_SERVICE_CHAT_ID", "").strip()
    if not configured:
        return None
    try:
        return int(configured)
    except ValueError as error:
        raise ValueError("CUSTOMER_SERVICE_CHAT_ID must be a Telegram numeric chat ID.") from error

def admin_notification_chat_ids() -> frozenset[int]:
    """Return private chats that receive successful-booking notifications."""
    configured = os.environ.get("ADMIN_NOTIFICATION_CHAT_IDS", "").strip()
    if not configured:
        return frozenset()

    chat_ids: set[int] = set()
    for raw_value in configured.split(","):
        value = raw_value.strip()
        try:
            chat_id = int(value)
        except ValueError as error:
            raise ValueError(
                "ADMIN_NOTIFICATION_CHAT_IDS must be a comma-separated list of numeric Telegram chat IDs."
            ) from error
        if chat_id <= 0:
            raise ValueError(
                "ADMIN_NOTIFICATION_CHAT_IDS must contain positive private-chat IDs."
            )
        chat_ids.add(chat_id)
    return frozenset(chat_ids)
