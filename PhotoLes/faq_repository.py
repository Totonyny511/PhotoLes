"""SQLite storage used by both the Telegram bot and the FAQ admin tool."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MAX_QUESTION_LENGTH = 200
MAX_ANSWER_LENGTH = 3500
MAX_CATEGORY_LENGTH = 50


@dataclass(frozen=True, slots=True)
class FAQ:
    id: int
    question: str
    answer: str
    category: str
    sort_order: int
    is_active: bool


class FAQRepository:
    """Small repository with one short-lived connection per operation."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS faqs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'General',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _validate(question: str, answer: str, category: str) -> tuple[str, str, str]:
        question = question.strip()
        answer = answer.strip()
        category = category.strip() or "General"

        if not question:
            raise ValueError("Question cannot be empty.")
        if not answer:
            raise ValueError("Answer cannot be empty.")
        if len(question) > MAX_QUESTION_LENGTH:
            raise ValueError(f"Question must be at most {MAX_QUESTION_LENGTH} characters.")
        if len(answer) > MAX_ANSWER_LENGTH:
            raise ValueError(f"Answer must be at most {MAX_ANSWER_LENGTH} characters.")
        if len(category) > MAX_CATEGORY_LENGTH:
            raise ValueError(f"Category must be at most {MAX_CATEGORY_LENGTH} characters.")
        return question, answer, category

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> FAQ | None:
        if row is None:
            return None
        return FAQ(
            id=row["id"],
            question=row["question"],
            answer=row["answer"],
            category=row["category"],
            sort_order=row["sort_order"],
            is_active=bool(row["is_active"]),
        )

    def add(
        self,
        question: str,
        answer: str,
        category: str = "General",
        sort_order: int = 0,
    ) -> int:
        question, answer, category = self._validate(question, answer, category)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO faqs (question, answer, category, sort_order)
                VALUES (?, ?, ?, ?)
                """,
                (question, answer, category, sort_order),
            )
            return int(cursor.lastrowid)

    def list_all(self, *, include_inactive: bool = False) -> list[FAQ]:
        where = "" if include_inactive else "WHERE is_active = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, question, answer, category, sort_order, is_active
                FROM faqs
                {where}
                ORDER BY sort_order ASC, id ASC
                """  # The clause is selected above, not supplied by a user.
            ).fetchall()
        return [faq for row in rows if (faq := self._from_row(row)) is not None]

    def get(self, faq_id: int, *, include_inactive: bool = False) -> FAQ | None:
        active_filter = "" if include_inactive else "AND is_active = 1"
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id, question, answer, category, sort_order, is_active
                FROM faqs
                WHERE id = ? {active_filter}
                """,
                (faq_id,),
            ).fetchone()
        return self._from_row(row)

    def update(
        self,
        faq_id: int,
        *,
        question: str | None = None,
        answer: str | None = None,
        category: str | None = None,
        sort_order: int | None = None,
    ) -> bool:
        current = self.get(faq_id, include_inactive=True)
        if current is None:
            return False

        new_question, new_answer, new_category = self._validate(
            question if question is not None else current.question,
            answer if answer is not None else current.answer,
            category if category is not None else current.category,
        )
        new_order = sort_order if sort_order is not None else current.sort_order
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE faqs
                SET question = ?, answer = ?, category = ?, sort_order = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_question, new_answer, new_category, new_order, faq_id),
            )
        return True

    def set_active(self, faq_id: int, active: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE faqs
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(active), faq_id),
            )
        return cursor.rowcount > 0

    def delete(self, faq_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM faqs WHERE id = ?", (faq_id,))
        return cursor.rowcount > 0
