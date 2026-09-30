import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from reservation_repository import (
    DuplicateReservationError,
    ReservationChangeError,
    ReservationRepository,
    SlotUnavailableError,
)


class ReservationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = ReservationRepository(database)
        self.repository.initialize()
        self.now = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
        self.start = self.now + timedelta(days=3)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def reserve(self, slot_id: int, user_id: int = 10):
        return self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=user_id,
            telegram_chat_id=user_id,
            customer_name="Jamie Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            now=self.now,
        )

    def test_reservation_reduces_live_availability(self) -> None:
        slot_id = self.repository.add_slot(self.start, capacity=2)
        self.reserve(slot_id)
        slot = self.repository.get_slot(slot_id)
        self.assertEqual(slot.reserved_count, 1)
        self.assertEqual(slot.remaining, 1)

    def test_reservation_stores_required_party_and_telegram_details(self) -> None:
        slot_id = self.repository.add_slot(self.start)

        reservation = self.reserve(slot_id)

        self.assertEqual(reservation.number_of_pax, 3)
        self.assertEqual(reservation.telegram_user_id, 10)
        self.assertEqual(reservation.telegram_chat_id, 10)

    def test_successful_reservation_queues_alert_for_each_configured_admin(self) -> None:
        slot_id = self.repository.add_slot(self.start)

        reservation = self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=10,
            telegram_chat_id=10,
            customer_name="Jamie Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            admin_notification_chat_ids=(9002, 9001, 9001),
            now=self.now,
        )

        notifications = self.repository.pending_booking_notifications(now=self.now)

        self.assertEqual(
            [notification.admin_chat_id for notification in notifications],
            [9001, 9002],
        )
        self.assertTrue(
            all(notification.reservation == reservation for notification in notifications)
        )

    def test_failed_booking_alert_is_kept_for_retry(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=10,
            telegram_chat_id=10,
            customer_name="Jamie Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            admin_notification_chat_ids=(9001,),
            now=self.now,
        )
        notification = self.repository.pending_booking_notifications(now=self.now)[0]

        self.assertTrue(
            self.repository.record_booking_notification_failure(
                notification.id, "Temporary Telegram failure", now=self.now
            )
        )

        self.assertEqual(
            self.repository.pending_booking_notifications(now=self.now), []
        )
        retriable = self.repository.pending_booking_notifications(
            now=self.now + timedelta(seconds=30)
        )
        self.assertEqual([item.id for item in retriable], [notification.id])
        self.assertEqual(retriable[0].attempt_count, 1)
        self.assertIn("Temporary Telegram failure", retriable[0].last_error)

    def test_invalid_number_of_pax_is_rejected(self) -> None:
        slot_id = self.repository.add_slot(self.start)

        with self.assertRaisesRegex(ValueError, "between 1 and 50"):
            self.repository.create_reservation(
                slot_id=slot_id,
                telegram_user_id=10,
                telegram_chat_id=10,
                customer_name="Jamie Tan",
                email="jamie@example.com",
                phone="+65 8123 4567",
                number_of_pax=0,
                telegram_username="jamie",
                now=self.now,
            )

    def test_simultaneous_confirmations_cannot_double_book_a_slot(self) -> None:
        slot_id = self.repository.add_slot(self.start)

        def confirm(user_id: int):
            try:
                return self.reserve(slot_id, user_id=user_id)
            except SlotUnavailableError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, (10, 11)))

        reservations = [result for result in results if not isinstance(result, Exception)]
        rejected = [result for result in results if isinstance(result, SlotUnavailableError)]
        self.assertEqual(len(reservations), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.repository.get_slot(slot_id).reserved_count, 1)

    def test_audited_block_removes_slots_from_bot_availability(self) -> None:
        first_slot_id = self.repository.add_slot(self.start)
        second_start = self.start + timedelta(minutes=20)
        second_slot_id = self.repository.add_slot(second_start)
        blocked_at = self.now + timedelta(minutes=5)

        block = self.repository.block_slots(
            self.start,
            second_start + timedelta(minutes=20),
            reason="Shop closed for a public holiday",
            blocked_by="Morgan Lee",
            now=blocked_at,
        )

        self.assertEqual(block.affected_slot_count, 2)
        self.assertEqual(block.reason, "Shop closed for a public holiday")
        self.assertEqual(block.blocked_by, "Morgan Lee")
        self.assertEqual(block.blocked_at, blocked_at)
        self.assertFalse(self.repository.get_slot(first_slot_id).is_active)
        self.assertFalse(self.repository.get_slot(second_slot_id).is_active)
        self.assertNotIn(
            first_slot_id,
            [slot.id for slot in self.repository.list_slots(future_only=True, now=self.now)],
        )
        with self.assertRaises(SlotUnavailableError):
            self.reserve(first_slot_id)

    def test_releasing_block_restores_slots_and_keeps_audit_record(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        block = self.repository.block_slots(
            self.start,
            self.start + timedelta(minutes=20),
            reason="Private event",
            blocked_by="Morgan Lee",
            now=self.now,
        )

        released = self.repository.release_slot_block(
            block.id, released_by="Alex Tan", now=self.now + timedelta(hours=1)
        )

        self.assertTrue(released)
        self.assertTrue(self.repository.get_slot(slot_id).is_active)
        history = self.repository.list_slot_blocks()[0]
        self.assertEqual(history.released_by, "Alex Tan")
        self.assertEqual(history.released_at, self.now + timedelta(hours=1))

    def test_active_period_block_applies_to_slots_created_later(self) -> None:
        self.repository.add_slot(self.start)
        self.repository.block_slots(
            self.start,
            self.start + timedelta(hours=1),
            reason="Maintenance closure",
            blocked_by="Morgan Lee",
            now=self.now,
        )

        later_slot_id = self.repository.add_slot(self.start + timedelta(minutes=40))

        self.assertFalse(self.repository.get_slot(later_slot_id).is_active)

    def test_ensure_slots_adds_only_missing_times(self) -> None:
        existing_start = self.start
        new_start = self.start + timedelta(minutes=30)
        existing_id = self.repository.add_slot(existing_start, capacity=2)

        inserted = self.repository.ensure_slots([existing_start, new_start])

        self.assertEqual(inserted, 1)
        self.assertEqual(self.repository.get_slot(existing_id).capacity, 2)
        self.assertEqual(len(self.repository.list_slots()), 2)

    def test_full_slot_cannot_be_reserved(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        self.reserve(slot_id)
        with self.assertRaises(SlotUnavailableError):
            self.reserve(slot_id, user_id=11)

    def test_same_user_cannot_reserve_same_slot_twice(self) -> None:
        slot_id = self.repository.add_slot(self.start, capacity=2)
        self.reserve(slot_id)
        with self.assertRaises(DuplicateReservationError):
            self.reserve(slot_id)

    def test_reservation_inside_four_hour_cutoff_is_rejected(self) -> None:
        slot_id = self.repository.add_slot(
            self.now + timedelta(hours=3, minutes=59)
        )

        with self.assertRaisesRegex(SlotUnavailableError, "at least 4 hours"):
            self.reserve(slot_id)

    def test_reservation_exactly_four_hours_ahead_is_allowed(self) -> None:
        slot_id = self.repository.add_slot(self.now + timedelta(hours=4))

        reservation = self.reserve(slot_id)

        self.assertEqual(reservation.slot_id, slot_id)

    def test_invalid_email_is_rejected(self) -> None:
        slot_id = self.repository.add_slot(self.start)

        with self.assertRaisesRegex(ValueError, "valid email"):
            self.repository.create_reservation(
                slot_id=slot_id,
                telegram_user_id=10,
                telegram_chat_id=10,
                customer_name="Jamie Tan",
                email="not-an-email",
                phone="+65 8123 4567",
                number_of_pax=3,
                telegram_username="jamie",
                now=self.now,
            )

    def test_cancellation_returns_place_to_availability(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        self.assertTrue(
            self.repository.cancel_user_reservation(
                reservation.id, reservation.telegram_user_id, now=self.now
            )
        )
        self.assertEqual(self.repository.get_slot(slot_id).remaining, 1)

    def test_customer_can_atomically_change_to_an_available_slot(self) -> None:
        original_slot_id = self.repository.add_slot(self.start)
        new_slot_id = self.repository.add_slot(self.start + timedelta(hours=1))
        reservation = self.reserve(original_slot_id)
        self.repository.mark_reminder_sent(reservation.id)

        changed = self.repository.change_user_reservation(
            reservation.id, reservation.telegram_user_id, new_slot_id, now=self.now
        )

        self.assertEqual(changed.id, reservation.id)
        self.assertEqual(changed.slot_id, new_slot_id)
        self.assertEqual(self.repository.get_slot(original_slot_id).remaining, 1)
        self.assertEqual(self.repository.get_slot(new_slot_id).remaining, 0)
        reminder = self.repository.get_reminder_notification(reservation.id)
        self.assertEqual(reminder.status, "pending")
        self.assertFalse(changed.reminder_sent)
        self.assertEqual(
            reminder.next_attempt_at,
            self.start + timedelta(hours=1) - timedelta(days=1),
        )

    def test_failed_change_keeps_the_original_reservation(self) -> None:
        original_slot_id = self.repository.add_slot(self.start)
        full_slot_id = self.repository.add_slot(self.start + timedelta(hours=1))
        reservation = self.reserve(original_slot_id)
        self.reserve(full_slot_id, user_id=11)

        with self.assertRaises(SlotUnavailableError):
            self.repository.change_user_reservation(
                reservation.id, reservation.telegram_user_id, full_slot_id, now=self.now
            )

        unchanged = self.repository.get_reservation(reservation.id)
        self.assertEqual(unchanged.slot_id, original_slot_id)
        self.assertEqual(self.repository.get_slot(original_slot_id).remaining, 0)

    def test_customer_cannot_change_another_users_reservation(self) -> None:
        original_slot_id = self.repository.add_slot(self.start)
        new_slot_id = self.repository.add_slot(self.start + timedelta(hours=1))
        reservation = self.reserve(original_slot_id, user_id=10)

        with self.assertRaises(ReservationChangeError):
            self.repository.change_user_reservation(
                reservation.id, 11, new_slot_id, now=self.now
            )

        self.assertEqual(
            self.repository.get_reservation(reservation.id).slot_id,
            original_slot_id,
        )

    def test_customer_must_choose_a_different_slot_when_changing(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)

        with self.assertRaisesRegex(ReservationChangeError, "different time"):
            self.repository.change_user_reservation(
                reservation.id, reservation.telegram_user_id, slot_id, now=self.now
            )

    def test_cancellation_and_customer_notification_are_saved_together(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)

        cancellation = self.repository.cancel_future_reservation_and_queue_notification(
            reservation.id, now=self.now
        )

        self.assertIsNotNone(cancellation)
        self.assertEqual(self.repository.get_slot(slot_id).remaining, 1)
        self.assertEqual(
            self.repository.get_reservation(reservation.id).status, "cancelled"
        )
        pending = self.repository.pending_cancellation_notifications(now=self.now)
        self.assertEqual([item.id for item in pending], [cancellation.notification_id])
        self.assertEqual(pending[0].reservation.id, reservation.id)

    def test_failed_customer_notification_is_scheduled_for_retry(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        cancellation = self.repository.cancel_future_reservation_and_queue_notification(
            reservation.id, now=self.now
        )

        self.assertTrue(
            self.repository.record_cancellation_notification_failure(
                cancellation.notification_id, "Temporary Telegram failure", now=self.now
            )
        )

        notification = self.repository.get_cancellation_notification(
            cancellation.notification_id
        )
        self.assertEqual(notification.attempt_count, 1)
        self.assertEqual(notification.last_error, "Temporary Telegram failure")
        self.assertEqual(notification.next_attempt_at, self.now + timedelta(seconds=30))
        self.assertEqual(
            self.repository.pending_cancellation_notifications(now=self.now), []
        )
        retriable = self.repository.pending_cancellation_notifications(
            now=self.now + timedelta(seconds=30)
        )
        self.assertEqual([item.id for item in retriable], [cancellation.notification_id])

    def test_customer_can_cancel_own_future_reservation(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id, user_id=10)

        cancelled = self.repository.cancel_user_reservation(
            reservation.id, 10, now=self.now
        )

        self.assertTrue(cancelled)
        self.assertEqual(self.repository.get_slot(slot_id).remaining, 1)
        self.assertEqual(self.repository.get_reservation(reservation.id).status, "cancelled")

    def test_admin_cancellation_cannot_cancel_started_reservation(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id, user_id=10)

        cancelled = self.repository.cancel_future_reservation_and_queue_notification(
            reservation.id, now=self.start
        )

        self.assertIsNone(cancelled)
        self.assertEqual(self.repository.get_reservation(reservation.id).status, "confirmed")
        self.assertEqual(
            self.repository.pending_cancellation_notifications(now=self.start), []
        )

    def test_customer_cannot_cancel_another_users_reservation(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id, user_id=10)

        cancelled = self.repository.cancel_user_reservation(
            reservation.id, 11, now=self.now
        )

        self.assertFalse(cancelled)
        self.assertEqual(self.repository.get_reservation(reservation.id).status, "confirmed")

    def test_customer_cannot_cancel_after_reservation_starts(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id, user_id=10)

        cancelled = self.repository.cancel_user_reservation(
            reservation.id, 10, now=self.start
        )

        self.assertFalse(cancelled)
        self.assertEqual(self.repository.get_reservation(reservation.id).status, "confirmed")

    def test_pending_reminder_can_be_marked_sent(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        pending = self.repository.pending_reminders(now=self.now)
        self.assertEqual([item.id for item in pending], [reservation.id])
        self.assertTrue(self.repository.mark_reminder_sent(reservation.id))
        self.assertEqual(self.repository.pending_reminders(now=self.now), [])
        self.assertEqual(
            self.repository.get_reminder_notification(reservation.id).status, "sent"
        )

    def test_reminder_becomes_due_one_day_before_reservation(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        reminder_time = self.start - timedelta(days=1)

        self.assertEqual(
            self.repository.due_reminder_notifications(
                now=reminder_time - timedelta(seconds=1)
            ),
            [],
        )
        due = self.repository.due_reminder_notifications(now=reminder_time)
        self.assertEqual([item.reservation.id for item in due], [reservation.id])

    def test_failed_reminder_is_scheduled_for_retry(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        reminder_time = self.start - timedelta(days=1)

        self.assertTrue(
            self.repository.record_reminder_failure(
                reservation.id, "Temporary Telegram failure", now=reminder_time
            )
        )

        notification = self.repository.get_reminder_notification(reservation.id)
        self.assertEqual(notification.attempt_count, 1)
        self.assertEqual(notification.last_error, "Temporary Telegram failure")
        self.assertEqual(
            notification.next_attempt_at, reminder_time + timedelta(seconds=30)
        )
        self.assertEqual(
            self.repository.due_reminder_notifications(now=reminder_time), []
        )
        retriable = self.repository.due_reminder_notifications(
            now=reminder_time + timedelta(seconds=30)
        )
        self.assertEqual(
            [item.reservation.id for item in retriable], [reservation.id]
        )

    def test_initialize_backfills_missing_reminder_notification(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        with self.repository._connect() as connection:
            connection.execute(
                "DELETE FROM reminder_notifications WHERE reservation_id = ?",
                (reservation.id,),
            )

        self.repository.initialize()

        notification = self.repository.get_reminder_notification(reservation.id)
        self.assertIsNotNone(notification)
        self.assertEqual(
            notification.next_attempt_at, reservation.start_at - timedelta(days=1)
        )

    def test_database_connections_are_closed_after_each_operation(self) -> None:
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch(
            "reservation_repository.sqlite3.connect", side_effect=tracked_connect
        ):
            self.repository.list_slots()

        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_cleanup_deletes_previous_days_and_keeps_current_day(self) -> None:
        previous_day_slot = self.repository.add_slot(
            datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
        )
        current_day_slot = self.repository.add_slot(
            datetime(2030, 1, 2, 20, 0, tzinfo=timezone.utc)
        )
        previous_reservation = self.reserve(previous_day_slot)

        result = self.repository.delete_data_before(
            datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(result.reservations_deleted, 1)
        self.assertEqual(result.slots_deleted, 1)
        self.assertIsNone(self.repository.get_reservation(previous_reservation.id))
        self.assertIsNone(self.repository.get_slot(previous_day_slot))
        self.assertIsNotNone(self.repository.get_slot(current_day_slot))
        past_customers = self.repository.list_past_customers(
            now=datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len(past_customers), 1)
        self.assertEqual(past_customers[0].customer_name, "Jamie Tan")
        self.assertEqual(past_customers[0].email, "jamie@example.com")
        self.assertEqual(past_customers[0].phone, "+65 8123 4567")

    def test_cancelled_reservation_is_not_saved_as_past_customer(self) -> None:
        slot_id = self.repository.add_slot(self.start)
        reservation = self.reserve(slot_id)
        self.repository.cancel_user_reservation(
            reservation.id, reservation.telegram_user_id, now=self.now
        )

        customers = self.repository.list_past_customers(
            now=self.start + timedelta(minutes=1)
        )

        self.assertEqual(customers, [])

    def test_repeat_visits_are_combined_in_customer_history(self) -> None:
        first_slot = self.repository.add_slot(self.start)
        second_slot = self.repository.add_slot(self.start + timedelta(days=1))
        self.reserve(first_slot)
        self.reserve(second_slot)

        customers = self.repository.list_past_customers(
            now=self.start + timedelta(days=2)
        )

        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].visit_count, 2)
        self.assertEqual(customers[0].first_visit_at, self.start)
        self.assertEqual(customers[0].last_visit_at, self.start + timedelta(days=1))

    def test_past_slots_are_not_offered_for_booking(self) -> None:
        past_slot = self.repository.add_slot(self.now - timedelta(hours=1))
        future_slot = self.repository.add_slot(self.now + timedelta(hours=1))

        visible = self.repository.list_slots(future_only=True, now=self.now)

        self.assertEqual([slot.id for slot in visible], [future_slot])
        self.assertNotIn(past_slot, [slot.id for slot in visible])


if __name__ == "__main__":
    unittest.main()
