"""Human-facing PhotoLes slot numbers and booking IDs."""

from __future__ import annotations

from datetime import datetime, time, tzinfo


OPENING_TIME = time(11, 0)
SLOT_MINUTES = 20


def slot_number(start_at: datetime, zone: tzinfo) -> int:
    """Return the one-based daily slot number for a scheduled start time.

    Current slots are validated onto the 20-minute grid. Rounding keeps legacy
    records displayable if they were created before that validation existed.
    """
    if start_at.tzinfo is None:
        raise ValueError("Slot start time must include a timezone.")
    local_start = start_at.astimezone(zone)
    minutes_from_opening = (
        local_start.hour * 60
        + local_start.minute
        - (OPENING_TIME.hour * 60 + OPENING_TIME.minute)
    )
    return max(1, (minutes_from_opening + SLOT_MINUTES // 2) // SLOT_MINUTES + 1)


def slot_label(start_at: datetime, zone: tzinfo) -> str:
    return f"{slot_number(start_at, zone):03d}"


def booking_id(start_at: datetime, zone: tzinfo) -> str:
    """Build PL + YYMMDD + three-digit slot number."""
    local_start = start_at.astimezone(zone)
    return f"PL{local_start:%y%m%d}{slot_label(start_at, zone)}"
