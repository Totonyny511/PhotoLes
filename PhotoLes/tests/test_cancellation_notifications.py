import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TelegramError

from cancellation_notifications import (
    customer_cancellation_text,
    deliver_cancellation_notification,
)
from reservation_repository import ReservationRepository


class CancellationNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = ReservationRepository(database)
        self.repository.initialize()
        self.now = datetime.now(timezone.utc)
        slot_id = self.repository.add_slot(self.now + timedelta(days=2))
        self.reservation = self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=101,
            telegram_chat_id=202,
            customer_name="Jamie <Tan>",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            now=self.now,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_customer_cancellation_message_escapes_customer_name(self) -> None:
        text = customer_cancellation_text(self.reservation)

        self.assertIn("Reservation cancelled by PhotoLes", text)
        self.assertIn("Jamie &lt;Tan&gt;", text)
        self.assertIn("Booking ID:</b> PL", text)
        self.assertIn("Slot number:</b>", text)

    def test_customer_bot_delivers_and_marks_queued_notification_sent(self) -> None:
        cancellation = self.repository.cancel_future_reservation_and_queue_notification(
            self.reservation.id, now=self.now
        )
        notification = self.repository.get_cancellation_notification(
            cancellation.notification_id
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        delivered = asyncio.run(
            deliver_cancellation_notification(context, self.repository, notification)
        )

        self.assertTrue(delivered)
        self.assertEqual(context.bot.send_message.await_args.kwargs["chat_id"], 202)
        saved = self.repository.get_cancellation_notification(
            cancellation.notification_id
        )
        self.assertEqual(saved.status, "sent")
        self.assertIsNotNone(saved.sent_at)

    def test_temporary_delivery_failure_remains_queued_for_retry(self) -> None:
        cancellation = self.repository.cancel_future_reservation_and_queue_notification(
            self.reservation.id, now=self.now
        )
        notification = self.repository.get_cancellation_notification(
            cancellation.notification_id
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=TelegramError("Temporary failure"))
            )
        )

        delivered = asyncio.run(
            deliver_cancellation_notification(context, self.repository, notification)
        )

        self.assertFalse(delivered)
        saved = self.repository.get_cancellation_notification(
            cancellation.notification_id
        )
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.attempt_count, 1)
        self.assertIn("Temporary failure", saved.last_error)


if __name__ == "__main__":
    unittest.main()
