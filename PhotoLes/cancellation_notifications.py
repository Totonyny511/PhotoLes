"""Durable delivery of reservation-cancellation messages to customers."""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from booking_identifiers import booking_id, slot_label
from reservation_repository import (
    CancellationNotification,
    Reservation,
    ReservationRepository,
)
from settings import shop_timezone


LOGGER = logging.getLogger(__name__)


def customer_cancellation_text(reservation: Reservation) -> str:
    start_at = reservation.start_at.astimezone(shop_timezone())
    return (
        "<b>❌ Reservation cancelled by PhotoLes</b>\n\n"
        f"<b>Booking ID:</b> {booking_id(reservation.start_at, shop_timezone())}\n"
        f"<b>Slot number:</b> {slot_label(reservation.start_at, shop_timezone())}\n"
        f"<b>Name:</b> {escape(reservation.customer_name)}\n"
        f"<b>Date:</b> {start_at:%A, %d %B %Y}\n"
        f"<b>Time:</b> {start_at:%I:%M %p}\n\n"
        "Our team has cancelled this reservation and the time slot is no longer reserved for you. "
        "If you believe this was unexpected, please contact PhotoLes customer service here."
    )


def customer_cancellation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Make another reservation", callback_data="book:start")],
            [InlineKeyboardButton("Contact customer service", callback_data="support:start")],
        ]
    )


async def deliver_cancellation_notification(
    context: ContextTypes.DEFAULT_TYPE,
    repository: ReservationRepository,
    notification: CancellationNotification,
) -> bool:
    """Attempt one delivery and persist either success or the next retry time."""
    try:
        await context.bot.send_message(
            chat_id=notification.reservation.telegram_chat_id,
            text=customer_cancellation_text(notification.reservation),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_cancellation_keyboard(),
        )
    except TelegramError as error:
        repository.record_cancellation_notification_failure(
            notification.id, str(error)
        )
        LOGGER.warning(
            "Cancellation notification %d for reservation R%d failed on attempt %d; queued for retry",
            notification.id,
            notification.reservation.id,
            notification.attempt_count + 1,
        )
        return False

    repository.mark_cancellation_notification_sent(notification.id)
    LOGGER.info(
        "Sent cancellation notification %d for reservation R%d",
        notification.id,
        notification.reservation.id,
    )
    return True


async def process_cancellation_notifications(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send all cancellation messages that are currently due, up to a safe batch size."""
    repository: ReservationRepository = context.application.bot_data[
        "reservation_repository"
    ]
    notifications = repository.pending_cancellation_notifications(limit=20)
    for notification in notifications:
        await deliver_cancellation_notification(context, repository, notification)
