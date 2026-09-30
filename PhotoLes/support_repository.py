"""SQLite storage for customer-service questions and staff replies."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MAX_QUESTION_LENGTH = 3000
MAX_ANSWER_LENGTH = 3000


@dataclass(frozen=True, slots=True)
class SupportTicket:
    id: int
    telegram_user_id: int
    telegram_chat_id: int
    customer_name: str
    telegram_username: str | None
    question: str
    staff_message_id: int | None
    answer: str | None
    status: str


class SupportRepository:
    """Persistent mapping between a staff ticket message and its customer."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    telegram_username TEXT,
                    question TEXT NOT NULL,
                    staff_message_id INTEGER UNIQUE,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'answered')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    answered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS support_ticket_customer
                ON support_tickets(telegram_user_id, created_at);
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> SupportTicket | None:
        if row is None:
            return None
        return SupportTicket(
            id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            telegram_chat_id=row["telegram_chat_id"],
            customer_name=row["customer_name"],
            telegram_username=row["telegram_username"],
            question=row["question"],
            staff_message_id=row["staff_message_id"],
            answer=row["answer"],
            status=row["status"],
        )

    def create_ticket(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        customer_name: str,
        telegram_username: str | None,
        question: str,
    ) -> SupportTicket:
        customer_name = customer_name.strip()
        question = question.strip()
        if not customer_name:
            raise ValueError("Customer name cannot be empty.")
        if not question:
            raise ValueError("Question cannot be empty.")
        if len(question) > MAX_QUESTION_LENGTH:
            raise ValueError(f"Question must be at most {MAX_QUESTION_LENGTH} characters.")

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO support_tickets (
                    telegram_user_id, telegram_chat_id, customer_name,
                    telegram_username, question
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    telegram_chat_id,
                    customer_name,
                    telegram_username,
                    question,
                ),
            )
            ticket_id = int(cursor.lastrowid)

        ticket = self.get(ticket_id)
        if ticket is None:
            raise RuntimeError("The support ticket could not be loaded after saving.")
        return ticket

    def get(self, ticket_id: int) -> SupportTicket | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return self._from_row(row)

    def set_staff_message_id(self, ticket_id: int, staff_message_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE support_tickets SET staff_message_id = ?
                WHERE id = ?
                """,
                (staff_message_id, ticket_id),
            )
        return cursor.rowcount > 0

    def get_by_staff_message_id(self, staff_message_id: int) -> SupportTicket | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM support_tickets WHERE staff_message_id = ?",
                (staff_message_id,),
            ).fetchone()
        return self._from_row(row)

    def mark_answered(self, ticket_id: int, answer: str) -> bool:
        answer = answer.strip()
        if not answer:
            raise ValueError("Answer cannot be empty.")
        if len(answer) > MAX_ANSWER_LENGTH:
            raise ValueError(f"Answer must be at most {MAX_ANSWER_LENGTH} characters.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE support_tickets
                SET answer = ?, status = 'answered', answered_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (answer, ticket_id),
            )
        return cursor.rowcount > 0

    def delete(self, ticket_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM support_tickets WHERE id = ?", (ticket_id,)
            )
        return cursor.rowcount > 0
