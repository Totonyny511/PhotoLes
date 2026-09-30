import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support_repository import SupportRepository


class SupportRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = SupportRepository(database)
        self.repository.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_ticket_is_found_from_staff_reply_message(self) -> None:
        ticket = self.repository.create_ticket(
            telegram_user_id=101,
            telegram_chat_id=202,
            customer_name="Jamie Tan",
            telegram_username="jamie",
            question="Can I bring my dog?",
        )

        self.assertTrue(self.repository.set_staff_message_id(ticket.id, 303))
        routed = self.repository.get_by_staff_message_id(303)

        self.assertIsNotNone(routed)
        self.assertEqual(routed.telegram_chat_id, 202)
        self.assertEqual(routed.question, "Can I bring my dog?")

    def test_answer_closes_ticket(self) -> None:
        ticket = self.repository.create_ticket(
            telegram_user_id=101,
            telegram_chat_id=202,
            customer_name="Jamie Tan",
            telegram_username=None,
            question="Is parking available?",
        )

        self.assertTrue(self.repository.mark_answered(ticket.id, "Yes, beside the studio."))
        answered = self.repository.get(ticket.id)

        self.assertEqual(answered.status, "answered")
        self.assertEqual(answered.answer, "Yes, beside the studio.")

    def test_rejects_empty_question(self) -> None:
        with self.assertRaisesRegex(ValueError, "Question cannot be empty"):
            self.repository.create_ticket(
                telegram_user_id=101,
                telegram_chat_id=202,
                customer_name="Jamie Tan",
                telegram_username=None,
                question="  ",
            )

    def test_database_connections_are_closed_after_each_operation(self) -> None:
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("support_repository.sqlite3.connect", side_effect=tracked_connect):
            self.repository.get(999)

        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
