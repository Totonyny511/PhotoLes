import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.error import TelegramError
from telegram.ext import ConversationHandler

from booking_identifiers import booking_id, slot_label, slot_number
from bot import (
    ENTER_EMAIL,
    ENTER_PAX,
    ENTER_PHONE,
    back_to_main_menu,
    admin_booking_notification_text,
    date_keyboard,
    deliver_admin_booking_notification,
    deliver_reservation_reminder,
    faq_keyboard,
    main_menu_keyboard,
    receive_email,
    receive_pax,
    receive_phone,
    receive_support_question,
    rolling_slot_starts,
    slot_keyboard,
    staff_reply,
    visible_booking_slots,
)
from reservation_repository import ReservationRepository, TimeSlot
from support_repository import SupportRepository


class SupportBotFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = SupportRepository(database)
        self.repository.initialize()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.context = SimpleNamespace(
            bot=self.bot,
            application=SimpleNamespace(
                bot_data={"support_repository": self.repository}
            ),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_customer_question_and_staff_reply_are_relayed(self) -> None:
        customer_message = SimpleNamespace(
            text="Can I change my backdrop?", reply_text=AsyncMock()
        )
        customer_update = SimpleNamespace(
            effective_message=customer_message,
            effective_user=SimpleNamespace(
                id=101, full_name="Jamie Tan", username="jamie"
            ),
            effective_chat=SimpleNamespace(id=202),
        )
        self.bot.send_message.return_value = SimpleNamespace(message_id=303)

        with patch("bot.customer_service_chat_id", return_value=999):
            result = asyncio.run(
                receive_support_question(customer_update, self.context)
            )

        self.assertEqual(result, ConversationHandler.END)
        staff_delivery = self.bot.send_message.await_args
        self.assertEqual(staff_delivery.kwargs["chat_id"], 999)
        ticket = self.repository.get_by_staff_message_id(303)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.telegram_chat_id, 202)

        self.bot.send_message.reset_mock()
        staff_message = SimpleNamespace(
            text="Yes—tell us your preferred colour.",
            reply_to_message=SimpleNamespace(message_id=303),
            reply_text=AsyncMock(),
        )
        staff_update = SimpleNamespace(effective_message=staff_message)

        asyncio.run(staff_reply(staff_update, self.context))

        customer_delivery = self.bot.send_message.await_args
        self.assertEqual(customer_delivery.kwargs["chat_id"], 202)
        self.assertIn("preferred colour", customer_delivery.kwargs["text"])
        self.assertEqual(self.repository.get(ticket.id).status, "answered")


class BookingKeyboardTests(unittest.TestCase):
    def test_slot_number_and_booking_id_follow_daily_schedule(self) -> None:
        zone = ZoneInfo("Asia/Singapore")
        first = datetime(2026, 9, 16, 11, 0, tzinfo=zone)
        twentieth = datetime(2026, 9, 16, 17, 20, tzinfo=zone)

        self.assertEqual(slot_number(first, zone), 1)
        self.assertEqual(slot_label(twentieth, zone), "020")
        self.assertEqual(booking_id(first, zone), "PL260916001")

    def test_main_menu_links_to_every_customer_section(self) -> None:
        callbacks = [
            row[0].callback_data for row in main_menu_keyboard().inline_keyboard
        ]

        self.assertEqual(
            callbacks,
            ["book:start", "book:mine", "faq:page:0", "support:start"],
        )

    def test_top_level_pages_have_a_back_to_main_menu_button(self) -> None:
        faq = SimpleNamespace(id=1, question="What should I bring?")
        slot = TimeSlot(
            id=1,
            start_at=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            capacity=1,
            is_active=True,
            reserved_count=0,
        )

        faq_back = faq_keyboard([faq], page=0).inline_keyboard[-1][0]
        dates_back = date_keyboard([slot], page=0).inline_keyboard[-1][0]

        self.assertEqual(faq_back.callback_data, "menu:main")
        self.assertEqual(dates_back.callback_data, "menu:main")

    def test_back_to_main_menu_exits_a_flow_and_clears_its_draft(self) -> None:
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_message=query)
        context = SimpleNamespace(
            user_data={"reservation_name": "Jamie", "unrelated": "keep"}
        )

        state = asyncio.run(back_to_main_menu(update, context))

        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("reservation_name", context.user_data)
        self.assertEqual(context.user_data["unrelated"], "keep")
        query.answer.assert_awaited_once()
        self.assertEqual(
            query.edit_message_text.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .callback_data,
            "book:start",
        )

    def test_fully_booked_slot_remains_visible_but_uses_inactive_callback(self) -> None:
        slot = TimeSlot(
            id=7,
            start_at=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            capacity=1,
            is_active=True,
            reserved_count=1,
        )

        keyboard = slot_keyboard([slot], date_page=0)
        button = keyboard.inline_keyboard[0][0]

        self.assertIn("0 available", button.text)
        self.assertEqual(button.callback_data, "book:full:7")

    def test_time_slots_are_paginated_with_earlier_and_next_buttons(self) -> None:
        slots = [
            TimeSlot(
                id=index + 1,
                start_at=datetime(2030, 1, 2, 3, 0, tzinfo=timezone.utc)
                + timedelta(minutes=30 * index),
                capacity=1,
                is_active=True,
                reserved_count=0,
            )
            for index in range(20)
        ]

        first_page = slot_keyboard(slots, date_page=2, time_page=0)
        middle_page = slot_keyboard(slots, date_page=2, time_page=1)

        first_slot_buttons = [
            row[0] for row in first_page.inline_keyboard
            if row[0].callback_data.startswith("book:slot:")
        ]
        self.assertEqual(len(first_slot_buttons), 7)
        self.assertEqual(first_slot_buttons[-1].callback_data, "book:slot:7")
        self.assertEqual(
            first_page.inline_keyboard[7][0].callback_data,
            "book:times:2030-01-02:2:1",
        )

        navigation = middle_page.inline_keyboard[7]
        self.assertEqual(navigation[0].text, "‹ Earlier")
        self.assertEqual(navigation[1].text, "Next ›")

    def test_rolling_slots_follow_requested_date_examples(self) -> None:
        starts = rolling_slot_starts(
            datetime(2026, 9, 4).date(), timezone.utc
        )

        self.assertEqual(len(starts), 29 * 30)
        self.assertEqual(starts[0], datetime(2026, 9, 4, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(starts[1], datetime(2026, 9, 4, 11, 20, tzinfo=timezone.utc))
        self.assertEqual(starts[-1], datetime(2026, 10, 2, 20, 40, tzinfo=timezone.utc))

    def test_slots_are_hidden_inside_four_hour_booking_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = ReservationRepository(Path(temporary_directory) / "test.db")
            repository.initialize()
            now = datetime(2030, 1, 2, 7, 0, tzinfo=timezone.utc)
            too_late = repository.add_slot(now + timedelta(hours=3, minutes=59))
            boundary = repository.add_slot(now + timedelta(hours=4))

            with patch("bot.shop_timezone", return_value=timezone.utc):
                visible = visible_booking_slots(repository, now=now)

            self.assertNotIn(too_late, [slot.id for slot in visible])
            self.assertIn(boundary, [slot.id for slot in visible])


class ReservationEmailTests(unittest.TestCase):
    def test_valid_email_is_normalized_and_saved_in_draft(self) -> None:
        message = SimpleNamespace(
            text="  Jamie@Example.COM  ", reply_text=AsyncMock()
        )
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(user_data={})

        state = asyncio.run(receive_email(update, context))

        self.assertEqual(state, ENTER_PHONE)
        self.assertEqual(context.user_data["reservation_email"], "jamie@example.com")

    def test_invalid_email_keeps_customer_on_email_step(self) -> None:
        message = SimpleNamespace(text="not-an-email", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(user_data={})

        state = asyncio.run(receive_email(update, context))

        self.assertEqual(state, ENTER_EMAIL)
        self.assertNotIn("reservation_email", context.user_data)


class ReservationPartySizeTests(unittest.TestCase):
    def test_valid_phone_advances_to_required_pax_step(self) -> None:
        message = SimpleNamespace(text="+65 8123 4567", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(user_data={})

        state = asyncio.run(receive_phone(update, context))

        self.assertEqual(state, ENTER_PAX)
        self.assertEqual(context.user_data["reservation_phone"], "+65 8123 4567")

    def test_invalid_pax_keeps_customer_on_pax_step(self) -> None:
        message = SimpleNamespace(text="0", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(user_data={})

        state = asyncio.run(receive_pax(update, context))

        self.assertEqual(state, ENTER_PAX)
        self.assertNotIn("reservation_number_of_pax", context.user_data)


class ReminderDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = ReservationRepository(database)
        self.repository.initialize()
        self.now = datetime.now(timezone.utc)
        slot_id = self.repository.add_slot(self.now + timedelta(hours=12))
        self.reservation = self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=101,
            telegram_chat_id=202,
            customer_name="Jamie Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            now=self.now,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def context(self, *, error: TelegramError | None = None):
        send_message = AsyncMock(side_effect=error) if error else AsyncMock()
        return SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

    def test_successful_reminder_is_marked_sent(self) -> None:
        notification = self.repository.due_reminder_notifications(now=self.now)[0]
        context = self.context()

        delivered = asyncio.run(
            deliver_reservation_reminder(context, self.repository, notification)
        )

        self.assertTrue(delivered)
        self.assertTrue(
            self.repository.get_reservation(self.reservation.id).reminder_sent
        )
        self.assertEqual(
            self.repository.get_reminder_notification(self.reservation.id).status,
            "sent",
        )

    def test_failed_reminder_remains_pending_for_retry(self) -> None:
        notification = self.repository.due_reminder_notifications(now=self.now)[0]
        context = self.context(error=TelegramError("Temporary failure"))

        delivered = asyncio.run(
            deliver_reservation_reminder(context, self.repository, notification)
        )

        self.assertFalse(delivered)
        saved = self.repository.get_reminder_notification(self.reservation.id)
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.attempt_count, 1)
        self.assertIn("Temporary failure", saved.last_error)


class AdminBookingNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "test.db"
        self.repository = ReservationRepository(database)
        self.repository.initialize()
        self.now = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
        slot_id = self.repository.add_slot(self.now + timedelta(days=3))
        self.reservation = self.repository.create_reservation(
            slot_id=slot_id,
            telegram_user_id=101,
            telegram_chat_id=202,
            customer_name="Jamie & Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie_tan",
            admin_notification_chat_ids=(999,),
            now=self.now,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_alert_contains_complete_booking_details(self) -> None:
        with patch("bot.shop_timezone", return_value=ZoneInfo("UTC")):
            text = admin_booking_notification_text(self.reservation)

        for expected in (
            "Booking ID:</b> PL300104001",
            "Slot number:</b> 001",
            "Jamie &amp; Tan",
            "jamie@example.com",
            "+65 8123 4567",
            "Number of pax:</b> 3",
            "Friday, 04 January 2030",
            "08:00 AM",
            "@jamie_tan",
            "Telegram user ID:</b> 101",
            "01 January 2030, 08:00:00 AM",
            "Confirmed",
        ):
            self.assertIn(expected, text)

    def test_successful_alert_delivery_is_marked_sent(self) -> None:
        notification = self.repository.pending_booking_notifications(now=self.now)[0]
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(bot=bot)

        delivered = asyncio.run(
            deliver_admin_booking_notification(
                context, self.repository, notification
            )
        )

        self.assertTrue(delivered)
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 999)
        self.assertEqual(
            self.repository.pending_booking_notifications(now=self.now), []
        )

    def test_failed_alert_delivery_remains_pending_for_retry(self) -> None:
        notification = self.repository.pending_booking_notifications(now=self.now)[0]
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=TelegramError("Temporary failure"))
        )
        context = SimpleNamespace(bot=bot)

        delivered = asyncio.run(
            deliver_admin_booking_notification(
                context, self.repository, notification
            )
        )

        self.assertFalse(delivered)
        saved = self.repository.pending_booking_notifications(
            now=self.now + timedelta(seconds=30)
        )[0]
        self.assertEqual(saved.attempt_count, 1)
        self.assertIn("Temporary failure", saved.last_error)


if __name__ == "__main__":
    unittest.main()
