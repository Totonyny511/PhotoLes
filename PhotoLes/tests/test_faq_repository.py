import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from faq_repository import FAQRepository


class FAQRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = FAQRepository(database)
        self.repository.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_add_and_list_faq(self) -> None:
        faq_id = self.repository.add("What should I bring?", "Bring your booking email.")

        faqs = self.repository.list_all()

        self.assertEqual(len(faqs), 1)
        self.assertEqual(faqs[0].id, faq_id)
        self.assertEqual(faqs[0].question, "What should I bring?")

    def test_disabled_faq_is_hidden_from_customers(self) -> None:
        faq_id = self.repository.add("Can I reschedule?", "Yes, contact us one day before.")

        self.assertTrue(self.repository.set_active(faq_id, False))

        self.assertEqual(self.repository.list_all(), [])
        self.assertIsNone(self.repository.get(faq_id))
        self.assertIsNotNone(self.repository.get(faq_id, include_inactive=True))

    def test_update_and_delete_faq(self) -> None:
        faq_id = self.repository.add("Old question", "Old answer")

        self.assertTrue(self.repository.update(faq_id, question="New question"))
        self.assertEqual(self.repository.get(faq_id).question, "New question")
        self.assertTrue(self.repository.delete(faq_id))
        self.assertIsNone(self.repository.get(faq_id, include_inactive=True))

    def test_rejects_empty_question(self) -> None:
        with self.assertRaisesRegex(ValueError, "Question cannot be empty"):
            self.repository.add("  ", "An answer")

    def test_database_connections_are_closed_after_each_operation(self) -> None:
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("faq_repository.sqlite3.connect", side_effect=tracked_connect):
            self.repository.list_all()

        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
