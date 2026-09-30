"""SQLite time-slot and reservation storage."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator


MINIMUM_BOOKING_NOTICE = timedelta(hours=4)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_database_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Reservation times must include a timezone.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_database_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class TimeSlot:
    id: int
    start_at: datetime
    capacity: int
    is_active: bool
    reserved_count: int

    @property
    def remaining(self) -> int:
        return max(0, self.capacity - self.reserved_count)


@dataclass(frozen=True, slots=True)
class Reservation:
    id: int
    slot_id: int
    start_at: datetime
    telegram_user_id: int
    telegram_chat_id: int
    customer_name: str
    email: str | None
    phone: str
    number_of_pax: int
    telegram_username: str | None
    status: str
    reminder_sent: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CancellationResult:
    reservation: Reservation
    notification_id: int


@dataclass(frozen=True, slots=True)
class CancellationNotification:
    id: int
    reservation: Reservation
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReminderNotification:
    reservation: Reservation
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class BookingNotification:
    id: int
    admin_chat_id: int
    reservation: Reservation
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    reservations_deleted: int
    slots_deleted: int


@dataclass(frozen=True, slots=True)
class SlotBlock:
    id: int
    start_at: datetime
    end_at: datetime
    reason: str
    blocked_by: str
    blocked_at: datetime
    released_by: str | None
    released_at: datetime | None
    affected_slot_count: int
    confirmed_reservation_count: int

    @property
    def is_active(self) -> bool:
        return self.released_at is None


@dataclass(frozen=True, slots=True)
class PastCustomer:
    telegram_user_id: int
    customer_name: str
    email: str | None
    phone: str
    first_visit_at: datetime
    last_visit_at: datetime
    visit_count: int


class SlotUnavailableError(ValueError):
    """Raised when a customer attempts to take an unavailable slot."""


class DuplicateReservationError(ValueError):
    """Raised when a customer already holds the selected slot."""


class ReservationChangeError(ValueError):
    """Raised when a reservation cannot be changed by the requesting customer."""


class ReservationRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS time_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_at_utc TEXT NOT NULL UNIQUE,
                    capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity > 0),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id INTEGER NOT NULL REFERENCES time_slots(id) ON DELETE RESTRICT,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    email TEXT,
                    phone TEXT NOT NULL,
                    number_of_pax INTEGER NOT NULL DEFAULT 1
                        CHECK (number_of_pax > 0),
                    telegram_username TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed', 'cancelled')),
                    reminder_sent INTEGER NOT NULL DEFAULT 0
                        CHECK (reminder_sent IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_user_slot
                ON reservations(slot_id, telegram_user_id)
                WHERE status = 'confirmed';

                CREATE INDEX IF NOT EXISTS reservation_reminders
                ON reservations(status, reminder_sent, slot_id);

                CREATE TABLE IF NOT EXISTS slot_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_at_utc TEXT NOT NULL,
                    end_at_utc TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    blocked_by TEXT NOT NULL,
                    blocked_at_utc TEXT NOT NULL,
                    released_by TEXT,
                    released_at_utc TEXT,
                    CHECK (end_at_utc > start_at_utc)
                );

                CREATE INDEX IF NOT EXISTS active_slot_blocks
                ON slot_blocks(released_at_utc, start_at_utc, end_at_utc);

                CREATE TABLE IF NOT EXISTS reminder_notifications (
                    reservation_id INTEGER PRIMARY KEY
                        REFERENCES reservations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at_utc TEXT NOT NULL,
                    last_attempt_at_utc TEXT,
                    last_error TEXT,
                    sent_at_utc TEXT,
                    created_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS pending_reminder_notifications
                ON reminder_notifications(status, next_attempt_at_utc);

                CREATE TABLE IF NOT EXISTS booking_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id INTEGER NOT NULL
                        REFERENCES reservations(id) ON DELETE CASCADE,
                    admin_chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at_utc TEXT NOT NULL,
                    last_attempt_at_utc TEXT,
                    last_error TEXT,
                    sent_at_utc TEXT,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE (reservation_id, admin_chat_id)
                );

                CREATE INDEX IF NOT EXISTS pending_booking_notifications
                ON booking_notifications(status, next_attempt_at_utc);

                CREATE TABLE IF NOT EXISTS cancellation_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id INTEGER NOT NULL UNIQUE
                        REFERENCES reservations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at_utc TEXT NOT NULL,
                    last_attempt_at_utc TEXT,
                    last_error TEXT,
                    sent_at_utc TEXT,
                    created_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS pending_cancellation_notifications
                ON cancellation_notifications(status, next_attempt_at_utc);

                CREATE TABLE IF NOT EXISTS completed_customer_visits (
                    reservation_id INTEGER PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    email TEXT,
                    phone TEXT NOT NULL,
                    visited_at_utc TEXT NOT NULL,
                    archived_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS completed_customer_names
                ON completed_customer_visits(customer_name COLLATE NOCASE);
                """
            )
            reservation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(reservations)")
            }
            if "email" not in reservation_columns:
                connection.execute("ALTER TABLE reservations ADD COLUMN email TEXT")
            if "number_of_pax" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN number_of_pax "
                    "INTEGER NOT NULL DEFAULT 1 CHECK (number_of_pax > 0)"
                )
            existing = connection.execute(
                """
                SELECT r.id, r.created_at, s.start_at_utc
                FROM reservations AS r
                JOIN time_slots AS s ON s.id = r.slot_id
                WHERE r.status = 'confirmed' AND r.reminder_sent = 0
                """
            ).fetchall()
            connection.executemany(
                """
                INSERT OR IGNORE INTO reminder_notifications (
                    reservation_id, next_attempt_at_utc, created_at_utc
                ) VALUES (?, ?, ?)
                """,
                [
                    (
                        row["id"],
                        to_database_time(
                            from_database_time(row["start_at_utc"])
                            - timedelta(days=1)
                        ),
                        row["created_at"],
                    )
                    for row in existing
                ],
            )

    @staticmethod
    def _slot_from_row(row: sqlite3.Row | None) -> TimeSlot | None:
        if row is None:
            return None
        return TimeSlot(
            id=row["id"],
            start_at=from_database_time(row["start_at_utc"]),
            capacity=row["capacity"],
            is_active=bool(row["is_active"]),
            reserved_count=row["reserved_count"],
        )

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row | None) -> Reservation | None:
        if row is None:
            return None
        return Reservation(
            id=row["id"],
            slot_id=row["slot_id"],
            start_at=from_database_time(row["start_at_utc"]),
            telegram_user_id=row["telegram_user_id"],
            telegram_chat_id=row["telegram_chat_id"],
            customer_name=row["customer_name"],
            email=row["email"],
            phone=row["phone"],
            number_of_pax=row["number_of_pax"],
            telegram_username=row["telegram_username"],
            status=row["status"],
            reminder_sent=bool(row["reminder_sent"]),
            created_at=from_database_time(row["created_at"]),
        )

    @classmethod
    def _cancellation_notification_from_row(
        cls, row: sqlite3.Row | None
    ) -> CancellationNotification | None:
        if row is None:
            return None
        reservation = cls._reservation_from_row(row)
        if reservation is None:
            return None
        return CancellationNotification(
            id=row["notification_id"],
            reservation=reservation,
            status=row["notification_status"],
            attempt_count=row["attempt_count"],
            next_attempt_at=from_database_time(row["next_attempt_at_utc"]),
            last_error=row["last_error"],
            sent_at=(
                from_database_time(row["sent_at_utc"])
                if row["sent_at_utc"] is not None
                else None
            ),
        )

    @classmethod
    def _reminder_notification_from_row(
        cls, row: sqlite3.Row | None
    ) -> ReminderNotification | None:
        if row is None:
            return None
        reservation = cls._reservation_from_row(row)
        if reservation is None:
            return None
        return ReminderNotification(
            reservation=reservation,
            status=row["notification_status"],
            attempt_count=row["attempt_count"],
            next_attempt_at=from_database_time(row["next_attempt_at_utc"]),
            last_error=row["last_error"],
            sent_at=(
                from_database_time(row["sent_at_utc"])
                if row["sent_at_utc"] is not None
                else None
            ),
        )

    @classmethod
    def _booking_notification_from_row(
        cls, row: sqlite3.Row | None
    ) -> BookingNotification | None:
        if row is None:
            return None
        reservation = cls._reservation_from_row(row)
        if reservation is None:
            return None
        return BookingNotification(
            id=row["notification_id"],
            admin_chat_id=row["admin_chat_id"],
            reservation=reservation,
            status=row["notification_status"],
            attempt_count=row["attempt_count"],
            next_attempt_at=from_database_time(row["next_attempt_at_utc"]),
            last_error=row["last_error"],
            sent_at=(
                from_database_time(row["sent_at_utc"])
                if row["sent_at_utc"] is not None
                else None
            ),
        )

    @staticmethod
    def _slot_select() -> str:
        return """
            SELECT s.id, s.start_at_utc, s.capacity,
                   CASE WHEN s.is_active = 1 AND NOT EXISTS (
                       SELECT 1 FROM slot_blocks AS b
                       WHERE b.released_at_utc IS NULL
                         AND s.start_at_utc >= b.start_at_utc
                         AND s.start_at_utc < b.end_at_utc
                   ) THEN 1 ELSE 0 END AS is_active,
                   COUNT(r.id) AS reserved_count
            FROM time_slots AS s
            LEFT JOIN reservations AS r
              ON r.slot_id = s.id AND r.status = 'confirmed'
        """

    @staticmethod
    def _reservation_select() -> str:
        return """
            SELECT r.id, r.slot_id, s.start_at_utc, r.telegram_user_id,
                   r.telegram_chat_id, r.customer_name, r.email, r.phone,
                   r.number_of_pax, r.telegram_username, r.status,
                   r.reminder_sent, r.created_at
            FROM reservations AS r
            JOIN time_slots AS s ON s.id = r.slot_id
        """

    @staticmethod
    def _cancellation_notification_select() -> str:
        return """
            SELECT n.id AS notification_id, n.status AS notification_status,
                   n.attempt_count, n.next_attempt_at_utc, n.last_error,
                   n.sent_at_utc, r.id, r.slot_id, s.start_at_utc,
                   r.telegram_user_id, r.telegram_chat_id, r.customer_name,
                   r.email, r.phone, r.number_of_pax, r.telegram_username,
                   r.status, r.reminder_sent, r.created_at
            FROM cancellation_notifications AS n
            JOIN reservations AS r ON r.id = n.reservation_id
            JOIN time_slots AS s ON s.id = r.slot_id
        """

    @staticmethod
    def _reminder_notification_select() -> str:
        return """
            SELECT n.status AS notification_status, n.attempt_count,
                   n.next_attempt_at_utc, n.last_error, n.sent_at_utc,
                   r.id, r.slot_id, s.start_at_utc, r.telegram_user_id,
                   r.telegram_chat_id, r.customer_name, r.email, r.phone,
                   r.number_of_pax, r.telegram_username, r.status,
                   r.reminder_sent, r.created_at
            FROM reminder_notifications AS n
            JOIN reservations AS r ON r.id = n.reservation_id
            JOIN time_slots AS s ON s.id = r.slot_id
        """

    @staticmethod
    def _booking_notification_select() -> str:
        return """
            SELECT n.id AS notification_id, n.admin_chat_id,
                   n.status AS notification_status, n.attempt_count,
                   n.next_attempt_at_utc, n.last_error, n.sent_at_utc,
                   r.id, r.slot_id, s.start_at_utc, r.telegram_user_id,
                   r.telegram_chat_id, r.customer_name, r.email, r.phone,
                   r.number_of_pax, r.telegram_username, r.status,
                   r.reminder_sent, r.created_at
            FROM booking_notifications AS n
            JOIN reservations AS r ON r.id = n.reservation_id
            JOIN time_slots AS s ON s.id = r.slot_id
        """

    def add_slot(self, start_at: datetime, capacity: int = 1) -> int:
        if capacity < 1:
            raise ValueError("Capacity must be at least 1.")
        if start_at.tzinfo is None:
            raise ValueError("Slot start time must include a timezone.")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO time_slots (start_at_utc, capacity) VALUES (?, ?)",
                    (to_database_time(start_at), capacity),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("A time slot already exists at that date and time.") from error

    def ensure_slots(self, start_times: Iterable[datetime], capacity: int = 1) -> int:
        """Create missing slots without changing slots that already exist."""
        if capacity < 1:
            raise ValueError("Capacity must be at least 1.")
        normalized: list[str] = []
        for start_at in start_times:
            if start_at.tzinfo is None:
                raise ValueError("Slot start time must include a timezone.")
            normalized.append(to_database_time(start_at))

        inserted = 0
        with self._connect() as connection:
            for start_at_utc in normalized:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO time_slots (start_at_utc, capacity)
                    VALUES (?, ?)
                    """,
                    (start_at_utc, capacity),
                )
                inserted += cursor.rowcount
        return inserted

    def get_slot(self, slot_id: int) -> TimeSlot | None:
        with self._connect() as connection:
            row = connection.execute(
                self._slot_select() + " WHERE s.id = ? GROUP BY s.id", (slot_id,)
            ).fetchone()
        return self._slot_from_row(row)

    def list_slots(
        self,
        *,
        future_only: bool = False,
        available_only: bool = False,
        include_inactive: bool = False,
        now: datetime | None = None,
    ) -> list[TimeSlot]:
        clauses: list[str] = []
        parameters: list[object] = []
        if future_only:
            clauses.append("s.start_at_utc > ?")
            parameters.append(to_database_time(now or utc_now()))
        if not include_inactive:
            clauses.append("s.is_active = 1")
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM slot_blocks AS b "
                "WHERE b.released_at_utc IS NULL "
                "AND s.start_at_utc >= b.start_at_utc "
                "AND s.start_at_utc < b.end_at_utc)"
            )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        having = " HAVING COUNT(r.id) < s.capacity" if available_only else ""
        query = self._slot_select() + where + " GROUP BY s.id" + having + " ORDER BY s.start_at_utc ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [slot for row in rows if (slot := self._slot_from_row(row)) is not None]

    def update_slot(
        self,
        slot_id: int,
        *,
        start_at: datetime | None = None,
        capacity: int | None = None,
    ) -> bool:
        current = self.get_slot(slot_id)
        if current is None:
            return False
        new_capacity = capacity if capacity is not None else current.capacity
        if new_capacity < 1:
            raise ValueError("Capacity must be at least 1.")
        if new_capacity < current.reserved_count:
            raise ValueError(
                f"Capacity cannot be below the {current.reserved_count} confirmed reservation(s)."
            )
        new_start = start_at if start_at is not None else current.start_at
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE time_slots
                    SET start_at_utc = ?, capacity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (to_database_time(new_start), new_capacity, slot_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("A time slot already exists at that date and time.") from error
        return True

    def set_slot_active(self, slot_id: int, active: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE time_slots SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(active), slot_id),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _slot_block_from_row(row: sqlite3.Row | None) -> SlotBlock | None:
        if row is None:
            return None
        return SlotBlock(
            id=row["id"],
            start_at=from_database_time(row["start_at_utc"]),
            end_at=from_database_time(row["end_at_utc"]),
            reason=row["reason"],
            blocked_by=row["blocked_by"],
            blocked_at=from_database_time(row["blocked_at_utc"]),
            released_by=row["released_by"],
            released_at=(
                from_database_time(row["released_at_utc"])
                if row["released_at_utc"] is not None
                else None
            ),
            affected_slot_count=row["affected_slot_count"],
            confirmed_reservation_count=row["confirmed_reservation_count"],
        )

    @staticmethod
    def _slot_block_select() -> str:
        return """
            SELECT b.id, b.start_at_utc, b.end_at_utc, b.reason,
                   b.blocked_by, b.blocked_at_utc, b.released_by,
                   b.released_at_utc,
                   (SELECT COUNT(*) FROM time_slots AS s
                    WHERE s.start_at_utc >= b.start_at_utc
                      AND s.start_at_utc < b.end_at_utc) AS affected_slot_count,
                   (SELECT COUNT(*) FROM reservations AS r
                    JOIN time_slots AS s ON s.id = r.slot_id
                    WHERE s.start_at_utc >= b.start_at_utc
                      AND s.start_at_utc < b.end_at_utc
                      AND r.status = 'confirmed') AS confirmed_reservation_count
            FROM slot_blocks AS b
        """

    def block_slots(
        self,
        start_at: datetime,
        end_at: datetime,
        *,
        reason: str,
        blocked_by: str,
        now: datetime | None = None,
    ) -> SlotBlock:
        """Create an audited block that immediately removes matching slots from availability."""
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("Block start and end times must include a timezone.")
        if end_at <= start_at:
            raise ValueError("Block end time must be later than its start time.")
        reason = reason.strip()
        blocked_by = blocked_by.strip()
        if not 3 <= len(reason) <= 500:
            raise ValueError("Block reason must be between 3 and 500 characters.")
        if not 2 <= len(blocked_by) <= 80:
            raise ValueError("Administrator name must be between 2 and 80 characters.")
        start_value = to_database_time(start_at)
        end_value = to_database_time(end_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            affected = connection.execute(
                """
                SELECT COUNT(*) AS count FROM time_slots
                WHERE start_at_utc >= ? AND start_at_utc < ?
                """,
                (start_value, end_value),
            ).fetchone()["count"]
            if affected < 1:
                raise ValueError("No existing time slots fall within that period.")
            cursor = connection.execute(
                """
                INSERT INTO slot_blocks (
                    start_at_utc, end_at_utc, reason, blocked_by, blocked_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    start_value,
                    end_value,
                    reason,
                    blocked_by,
                    to_database_time(now or utc_now()),
                ),
            )
            block_id = int(cursor.lastrowid)
            row = connection.execute(
                self._slot_block_select() + " WHERE b.id = ?", (block_id,)
            ).fetchone()
        block = self._slot_block_from_row(row)
        if block is None:
            raise RuntimeError("The slot block could not be loaded after saving.")
        return block

    def list_slot_blocks(self, *, active_only: bool = False) -> list[SlotBlock]:
        where = " WHERE b.released_at_utc IS NULL" if active_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                self._slot_block_select()
                + where
                + " ORDER BY b.blocked_at_utc DESC, b.id DESC"
            ).fetchall()
        return [
            block for row in rows if (block := self._slot_block_from_row(row)) is not None
        ]

    def release_slot_block(
        self,
        block_id: int,
        *,
        released_by: str,
        now: datetime | None = None,
    ) -> bool:
        released_by = released_by.strip()
        if not 2 <= len(released_by) <= 80:
            raise ValueError("Administrator name must be between 2 and 80 characters.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE slot_blocks
                SET released_by = ?, released_at_utc = ?
                WHERE id = ? AND released_at_utc IS NULL
                """,
                (released_by, to_database_time(now or utc_now()), block_id),
            )
        return cursor.rowcount == 1

    def delete_slot(self, slot_id: int) -> bool:
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM time_slots WHERE id = ?", (slot_id,))
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "This slot has reservation history. Disable it instead of deleting it."
            ) from error
        return cursor.rowcount > 0

    def create_reservation(
        self,
        *,
        slot_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        customer_name: str,
        email: str,
        phone: str,
        number_of_pax: int,
        telegram_username: str | None,
        admin_notification_chat_ids: Iterable[int] = (),
        now: datetime | None = None,
    ) -> Reservation:
        customer_name = customer_name.strip()
        email = email.strip().lower()
        phone = phone.strip()
        if not 2 <= len(customer_name) <= 80:
            raise ValueError("Customer name must be between 2 and 80 characters.")
        if not 7 <= len(phone) <= 30:
            raise ValueError("Phone number must be between 7 and 30 characters.")
        if not isinstance(number_of_pax, int) or isinstance(number_of_pax, bool):
            raise ValueError("Number of pax must be a whole number.")
        if not 1 <= number_of_pax <= 50:
            raise ValueError("Number of pax must be between 1 and 50.")
        if (
            not 3 <= len(email) <= 254
            or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None
        ):
            raise ValueError("Please provide a valid email address.")
        current_time = now or utc_now()
        notification_chat_ids = sorted(set(admin_notification_chat_ids))
        if any(
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id <= 0
            for chat_id in notification_chat_ids
        ):
            raise ValueError("Admin notification chat IDs must be positive integers.")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            slot_row = connection.execute(
                self._slot_select() + " WHERE s.id = ? GROUP BY s.id", (slot_id,)
            ).fetchone()
            slot = self._slot_from_row(slot_row)
            if slot is None or not slot.is_active or slot.remaining < 1:
                raise SlotUnavailableError("That time slot is no longer available.")
            if slot.start_at < current_time + MINIMUM_BOOKING_NOTICE:
                raise SlotUnavailableError(
                    "Reservations must be made at least 4 hours before the time slot."
                )

            duplicate = connection.execute(
                """
                SELECT 1 FROM reservations
                WHERE slot_id = ? AND telegram_user_id = ? AND status = 'confirmed'
                """,
                (slot_id, telegram_user_id),
            ).fetchone()
            if duplicate:
                raise DuplicateReservationError("You already reserved this time slot.")

            created_at = to_database_time(current_time)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO reservations (
                        slot_id, telegram_user_id, telegram_chat_id, customer_name,
                        email, phone, number_of_pax, telegram_username, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slot_id,
                        telegram_user_id,
                        telegram_chat_id,
                        customer_name,
                        email,
                        phone,
                        number_of_pax,
                        telegram_username,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateReservationError("You already reserved this time slot.") from error
            reservation_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reservation_id, next_attempt_at_utc, created_at_utc
                ) VALUES (?, ?, ?)
                """,
                (
                    reservation_id,
                    to_database_time(slot.start_at - timedelta(days=1)),
                    created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO booking_notifications (
                    reservation_id, admin_chat_id, next_attempt_at_utc, created_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (reservation_id, chat_id, created_at, created_at)
                    for chat_id in notification_chat_ids
                ],
            )

        reservation = self.get_reservation(reservation_id)
        if reservation is None:
            raise RuntimeError("The reservation could not be loaded after saving.")
        return reservation

    def get_reservation(self, reservation_id: int) -> Reservation | None:
        with self._connect() as connection:
            row = connection.execute(
                self._reservation_select() + " WHERE r.id = ?", (reservation_id,)
            ).fetchone()
        return self._reservation_from_row(row)

    def list_reservations(self, *, include_cancelled: bool = False) -> list[Reservation]:
        where = "" if include_cancelled else " WHERE r.status = 'confirmed'"
        with self._connect() as connection:
            rows = connection.execute(
                self._reservation_select() + where + " ORDER BY s.start_at_utc ASC"
            ).fetchall()
        return [item for row in rows if (item := self._reservation_from_row(row)) is not None]

    def list_user_reservations(
        self, telegram_user_id: int, *, now: datetime | None = None
    ) -> list[Reservation]:
        with self._connect() as connection:
            rows = connection.execute(
                self._reservation_select()
                + """
                  WHERE r.telegram_user_id = ? AND r.status = 'confirmed'
                    AND s.start_at_utc > ? ORDER BY s.start_at_utc ASC
                """,
                (telegram_user_id, to_database_time(now or utc_now())),
            ).fetchall()
        return [item for row in rows if (item := self._reservation_from_row(row)) is not None]

    def change_user_reservation(
        self,
        reservation_id: int,
        telegram_user_id: int,
        new_slot_id: int,
        *,
        now: datetime | None = None,
    ) -> Reservation:
        """Atomically move a customer's future reservation to an available slot."""
        current_time = now or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation_row = connection.execute(
                self._reservation_select()
                + """
                  WHERE r.id = ? AND r.telegram_user_id = ?
                    AND r.status = 'confirmed' AND s.start_at_utc > ?
                """,
                (
                    reservation_id,
                    telegram_user_id,
                    to_database_time(current_time),
                ),
            ).fetchone()
            reservation = self._reservation_from_row(reservation_row)
            if reservation is None:
                raise ReservationChangeError(
                    "This reservation cannot be changed. It may no longer be upcoming."
                )
            if reservation.slot_id == new_slot_id:
                raise ReservationChangeError(
                    "Please choose a different time from your current reservation."
                )

            slot_row = connection.execute(
                self._slot_select() + " WHERE s.id = ? GROUP BY s.id",
                (new_slot_id,),
            ).fetchone()
            new_slot = self._slot_from_row(slot_row)
            if new_slot is None or not new_slot.is_active or new_slot.remaining < 1:
                raise SlotUnavailableError("That time slot is no longer available.")
            if new_slot.start_at < current_time + MINIMUM_BOOKING_NOTICE:
                raise SlotUnavailableError(
                    "Reservations must be changed at least 4 hours before the new time slot."
                )

            duplicate = connection.execute(
                """
                SELECT 1 FROM reservations
                WHERE slot_id = ? AND telegram_user_id = ? AND status = 'confirmed'
                  AND id != ?
                """,
                (new_slot_id, telegram_user_id, reservation_id),
            ).fetchone()
            if duplicate:
                raise DuplicateReservationError("You already reserved this time slot.")

            try:
                connection.execute(
                    """
                    UPDATE reservations
                    SET slot_id = ?, reminder_sent = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_slot_id, reservation_id),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateReservationError(
                    "You already reserved this time slot."
                ) from error

            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reservation_id, status, attempt_count, next_attempt_at_utc,
                    last_attempt_at_utc, last_error, sent_at_utc, created_at_utc
                ) VALUES (?, 'pending', 0, ?, NULL, NULL, NULL, ?)
                ON CONFLICT(reservation_id) DO UPDATE SET
                    status = 'pending', attempt_count = 0,
                    next_attempt_at_utc = excluded.next_attempt_at_utc,
                    last_attempt_at_utc = NULL, last_error = NULL, sent_at_utc = NULL
                """,
                (
                    reservation_id,
                    to_database_time(new_slot.start_at - timedelta(days=1)),
                    to_database_time(current_time),
                ),
            )

        changed = self.get_reservation(reservation_id)
        if changed is None:
            raise RuntimeError("The changed reservation could not be loaded.")
        return changed

    def cancel_future_reservation_and_queue_notification(
        self, reservation_id: int, *, now: datetime | None = None
    ) -> CancellationResult | None:
        """Atomically cancel an upcoming booking and persist its customer notification."""
        current_time = now or utc_now()
        current_time_value = to_database_time(current_time)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._reservation_select()
                + """
                  WHERE r.id = ? AND r.status = 'confirmed'
                    AND s.start_at_utc > ?
                """,
                (reservation_id, current_time_value),
            ).fetchone()
            reservation = self._reservation_from_row(row)
            if reservation is None:
                return None

            cursor = connection.execute(
                """
                UPDATE reservations
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'confirmed'
                """,
                (reservation_id,),
            )
            if cursor.rowcount != 1:
                return None

            notification_cursor = connection.execute(
                """
                INSERT INTO cancellation_notifications (
                    reservation_id, next_attempt_at_utc, created_at_utc
                ) VALUES (?, ?, ?)
                """,
                (reservation_id, current_time_value, current_time_value),
            )
            notification_id = int(notification_cursor.lastrowid)
        return CancellationResult(
            reservation=reservation,
            notification_id=notification_id,
        )

    def cancel_user_reservation(
        self,
        reservation_id: int,
        telegram_user_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Cancel a future reservation only when it belongs to the requesting user."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reservations
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND telegram_user_id = ? AND status = 'confirmed'
                  AND EXISTS (
                      SELECT 1 FROM time_slots AS s
                      WHERE s.id = reservations.slot_id AND s.start_at_utc > ?
                  )
                """,
                (
                    reservation_id,
                    telegram_user_id,
                    to_database_time(now or utc_now()),
                ),
            )
        return cursor.rowcount > 0

    def pending_reminders(self, *, now: datetime | None = None) -> list[Reservation]:
        with self._connect() as connection:
            rows = connection.execute(
                self._reservation_select()
                + """
                  WHERE r.status = 'confirmed' AND r.reminder_sent = 0
                    AND s.start_at_utc > ? ORDER BY s.start_at_utc ASC
                """,
                (to_database_time(now or utc_now()),),
            ).fetchall()
        return [item for row in rows if (item := self._reservation_from_row(row)) is not None]

    def mark_reminder_sent(self, reservation_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE reservations SET reminder_sent = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'confirmed' AND reminder_sent = 0
                """,
                (reservation_id,),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE reminder_notifications
                    SET status = 'sent', sent_at_utc = ?, last_error = NULL
                    WHERE reservation_id = ? AND status = 'pending'
                    """,
                    (to_database_time(utc_now()), reservation_id),
                )
        return cursor.rowcount > 0

    def get_reminder_notification(
        self, reservation_id: int
    ) -> ReminderNotification | None:
        with self._connect() as connection:
            row = connection.execute(
                self._reminder_notification_select()
                + " WHERE n.reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        return self._reminder_notification_from_row(row)

    def pending_booking_notifications(
        self, *, now: datetime | None = None, limit: int = 20
    ) -> list[BookingNotification]:
        if limit < 1:
            raise ValueError("Notification limit must be at least 1.")
        with self._connect() as connection:
            rows = connection.execute(
                self._booking_notification_select()
                + """
                  WHERE n.status = 'pending' AND n.next_attempt_at_utc <= ?
                  ORDER BY n.next_attempt_at_utc ASC, n.id ASC
                  LIMIT ?
                """,
                (to_database_time(now or utc_now()), limit),
            ).fetchall()
        return [
            notification
            for row in rows
            if (notification := self._booking_notification_from_row(row)) is not None
        ]

    def mark_booking_notification_sent(
        self, notification_id: int, *, now: datetime | None = None
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE booking_notifications
                SET status = 'sent', sent_at_utc = ?, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (to_database_time(now or utc_now()), notification_id),
            )
        return cursor.rowcount == 1

    def record_booking_notification_failure(
        self,
        notification_id: int,
        error_message: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM booking_notifications
                WHERE id = ? AND status = 'pending'
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                return False
            new_attempt_count = int(row["attempt_count"]) + 1
            retry_delay_seconds = min(3600, 30 * (2 ** min(new_attempt_count - 1, 7)))
            cursor = connection.execute(
                """
                UPDATE booking_notifications
                SET attempt_count = ?, last_attempt_at_utc = ?, last_error = ?,
                    next_attempt_at_utc = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    new_attempt_count,
                    to_database_time(current_time),
                    error_message[:500],
                    to_database_time(
                        current_time + timedelta(seconds=retry_delay_seconds)
                    ),
                    notification_id,
                ),
            )
        return cursor.rowcount == 1

    def due_reminder_notifications(
        self, *, now: datetime | None = None, limit: int = 20
    ) -> list[ReminderNotification]:
        if limit < 1:
            raise ValueError("Reminder limit must be at least 1.")
        current_time = to_database_time(now or utc_now())
        with self._connect() as connection:
            rows = connection.execute(
                self._reminder_notification_select()
                + """
                  WHERE n.status = 'pending' AND n.next_attempt_at_utc <= ?
                    AND r.status = 'confirmed' AND r.reminder_sent = 0
                    AND s.start_at_utc > ?
                  ORDER BY n.next_attempt_at_utc ASC, r.id ASC
                  LIMIT ?
                """,
                (current_time, current_time, limit),
            ).fetchall()
        return [
            notification
            for row in rows
            if (notification := self._reminder_notification_from_row(row))
            is not None
        ]

    def record_reminder_failure(
        self,
        reservation_id: int,
        error_message: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed reminder and schedule an exponential retry."""
        current_time = now or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM reminder_notifications
                WHERE reservation_id = ? AND status = 'pending'
                """,
                (reservation_id,),
            ).fetchone()
            if row is None:
                return False
            new_attempt_count = int(row["attempt_count"]) + 1
            retry_delay_seconds = min(3600, 30 * (2 ** min(new_attempt_count - 1, 7)))
            cursor = connection.execute(
                """
                UPDATE reminder_notifications
                SET attempt_count = ?, last_attempt_at_utc = ?, last_error = ?,
                    next_attempt_at_utc = ?
                WHERE reservation_id = ? AND status = 'pending'
                """,
                (
                    new_attempt_count,
                    to_database_time(current_time),
                    error_message[:500],
                    to_database_time(
                        current_time + timedelta(seconds=retry_delay_seconds)
                    ),
                    reservation_id,
                ),
            )
        return cursor.rowcount > 0

    def get_cancellation_notification(
        self, notification_id: int
    ) -> CancellationNotification | None:
        with self._connect() as connection:
            row = connection.execute(
                self._cancellation_notification_select() + " WHERE n.id = ?",
                (notification_id,),
            ).fetchone()
        return self._cancellation_notification_from_row(row)

    def pending_cancellation_notifications(
        self, *, now: datetime | None = None, limit: int = 20
    ) -> list[CancellationNotification]:
        if limit < 1:
            raise ValueError("Notification limit must be at least 1.")
        with self._connect() as connection:
            rows = connection.execute(
                self._cancellation_notification_select()
                + """
                  WHERE n.status = 'pending' AND n.next_attempt_at_utc <= ?
                  ORDER BY n.next_attempt_at_utc ASC, n.id ASC
                  LIMIT ?
                """,
                (to_database_time(now or utc_now()), limit),
            ).fetchall()
        return [
            notification
            for row in rows
            if (notification := self._cancellation_notification_from_row(row))
            is not None
        ]

    def mark_cancellation_notification_sent(
        self, notification_id: int, *, now: datetime | None = None
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE cancellation_notifications
                SET status = 'sent', sent_at_utc = ?, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (to_database_time(now or utc_now()), notification_id),
            )
        return cursor.rowcount > 0

    def record_cancellation_notification_failure(
        self,
        notification_id: int,
        error_message: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed attempt and schedule an exponential retry, capped at one hour."""
        current_time = now or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM cancellation_notifications
                WHERE id = ? AND status = 'pending'
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                return False
            new_attempt_count = int(row["attempt_count"]) + 1
            retry_delay_seconds = min(3600, 30 * (2 ** min(new_attempt_count - 1, 7)))
            cursor = connection.execute(
                """
                UPDATE cancellation_notifications
                SET attempt_count = ?, last_attempt_at_utc = ?, last_error = ?,
                    next_attempt_at_utc = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    new_attempt_count,
                    to_database_time(current_time),
                    error_message[:500],
                    to_database_time(
                        current_time + timedelta(seconds=retry_delay_seconds)
                    ),
                    notification_id,
                ),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _archive_completed_customer_visits(
        connection: sqlite3.Connection, cutoff_value: str
    ) -> int:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO completed_customer_visits (
                reservation_id, telegram_user_id, customer_name, email, phone,
                visited_at_utc, archived_at_utc
            )
            SELECT r.id, r.telegram_user_id, r.customer_name, r.email, r.phone,
                   s.start_at_utc, CURRENT_TIMESTAMP
            FROM reservations AS r
            JOIN time_slots AS s ON s.id = r.slot_id
            WHERE r.status = 'confirmed' AND s.start_at_utc < ?
            """,
            (cutoff_value,),
        )
        return cursor.rowcount

    def archive_completed_customer_visits(
        self, *, now: datetime | None = None
    ) -> int:
        """Preserve past confirmed bookings without retaining reservation internals."""
        cutoff_value = to_database_time(now or utc_now())
        with self._connect() as connection:
            return self._archive_completed_customer_visits(connection, cutoff_value)

    def list_past_customers(
        self, *, now: datetime | None = None
    ) -> list[PastCustomer]:
        """Return one alphabetical entry per customer with completed visits."""
        cutoff_value = to_database_time(now or utc_now())
        with self._connect() as connection:
            self._archive_completed_customer_visits(connection, cutoff_value)
            rows = connection.execute(
                """
                SELECT telegram_user_id, customer_name, email, phone, visited_at_utc
                FROM completed_customer_visits
                ORDER BY telegram_user_id, visited_at_utc
                """
            ).fetchall()

        grouped: dict[int, PastCustomer] = {}
        for row in rows:
            visited_at = from_database_time(row["visited_at_utc"])
            previous = grouped.get(row["telegram_user_id"])
            grouped[row["telegram_user_id"]] = PastCustomer(
                telegram_user_id=row["telegram_user_id"],
                customer_name=row["customer_name"],
                email=row["email"],
                phone=row["phone"],
                first_visit_at=(
                    previous.first_visit_at if previous is not None else visited_at
                ),
                last_visit_at=visited_at,
                visit_count=(previous.visit_count + 1 if previous is not None else 1),
            )
        return sorted(
            grouped.values(),
            key=lambda customer: (customer.customer_name.casefold(), customer.telegram_user_id),
        )

    def delete_data_before(self, cutoff: datetime) -> CleanupResult:
        """Archive completed customers, then remove expired reservation internals."""
        if cutoff.tzinfo is None:
            raise ValueError("Cleanup cutoff must include a timezone.")
        cutoff_value = to_database_time(cutoff)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._archive_completed_customer_visits(connection, cutoff_value)
            reservation_cursor = connection.execute(
                """
                DELETE FROM reservations
                WHERE slot_id IN (
                    SELECT id FROM time_slots WHERE start_at_utc < ?
                )
                """,
                (cutoff_value,),
            )
            slot_cursor = connection.execute(
                "DELETE FROM time_slots WHERE start_at_utc < ?",
                (cutoff_value,),
            )
        return CleanupResult(
            reservations_deleted=reservation_cursor.rowcount,
            slots_deleted=slot_cursor.rowcount,
        )
