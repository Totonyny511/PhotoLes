"""PhotoLes Telegram bot: FAQs, reservations, and appointment reminders."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from html import escape

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from booking_identifiers import booking_id, slot_label
from cancellation_notifications import process_cancellation_notifications
from faq_repository import FAQ, FAQRepository
from reservation_repository import (
    BookingNotification,
    DuplicateReservationError,
    MINIMUM_BOOKING_NOTICE,
    ReminderNotification,
    Reservation,
    ReservationChangeError,
    ReservationRepository,
    SlotUnavailableError,
    TimeSlot,
)
from settings import (
    admin_notification_chat_ids,
    customer_service_chat_id,
    database_path,
    load_local_env,
    shop_timezone,
)
from support_repository import MAX_ANSWER_LENGTH, MAX_QUESTION_LENGTH, SupportRepository


FAQ_PER_PAGE = 8
DATES_PER_PAGE = 7
TIMES_PER_PAGE = 7
BOOKING_WINDOW_LAST_DAY_OFFSET = 28
DAILY_OPENING_TIME = time(hour=11)
DAILY_CLOSING_TIME = time(hour=21)
SLOT_DURATION = timedelta(minutes=20)
(
    CHOOSE_SLOT,
    ENTER_NAME,
    ENTER_EMAIL,
    ENTER_PHONE,
    ENTER_PAX,
    CONFIRM,
    SUPPORT_QUESTION,
    CHANGE_CONFIRM,
) = range(8)
RESERVATION_DATA_KEYS = (
    "reservation_slot_id",
    "reservation_name",
    "reservation_email",
    "reservation_phone",
    "reservation_number_of_pax",
    "reservation_selected_date",
    "reservation_date_page",
    "reservation_time_page",
    "reservation_change_id",
    "reservation_original_slot_id",
)
MAIN_MENU_CALLBACK = "menu:main"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)


def clear_reservation_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in RESERVATION_DATA_KEYS:
        context.user_data.pop(key, None)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Make a reservation", callback_data="book:start")],
        [InlineKeyboardButton("🧾 My reservations", callback_data="book:mine")],
        [InlineKeyboardButton("❓ Frequently Asked Questions", callback_data="faq:page:0")],
        [InlineKeyboardButton("💬 Online Customer Service", callback_data="support:start")],
    ])


def back_to_main_menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("‹ Back to main menu", callback_data=MAIN_MENU_CALLBACK)]


def local_start(slot_or_reservation: TimeSlot | Reservation) -> datetime:
    return slot_or_reservation.start_at.astimezone(shop_timezone())


def date_label(value: date) -> str:
    return value.strftime("%a, %d %b %Y")


def booking_window_last_date(today: date | None = None) -> date:
    current_day = today or datetime.now(shop_timezone()).date()
    return current_day + timedelta(days=BOOKING_WINDOW_LAST_DAY_OFFSET)


def rolling_slot_starts(today: date, zone: tzinfo) -> list[datetime]:
    """Build 11:00–20:40 starts for every date in the configured window."""
    starts: list[datetime] = []
    for day_offset in range(BOOKING_WINDOW_LAST_DAY_OFFSET + 1):
        day = today + timedelta(days=day_offset)
        current = datetime.combine(day, DAILY_OPENING_TIME, tzinfo=zone)
        closing = datetime.combine(day, DAILY_CLOSING_TIME, tzinfo=zone)
        while current < closing:
            starts.append(current)
            current += SLOT_DURATION
    return starts


def visible_booking_slots(
    repository: ReservationRepository, *, now: datetime | None = None
) -> list[TimeSlot]:
    """Return slots bookable with four hours' notice in the rolling window."""
    zone = shop_timezone()
    current_time = now or datetime.now(timezone.utc)
    last_date = booking_window_last_date(current_time.astimezone(zone).date())
    return [
        slot
        for slot in repository.list_slots(future_only=True, now=current_time)
        if slot.start_at >= current_time + MINIMUM_BOOKING_NOTICE
        and slot.start_at.astimezone(zone).date() <= last_date
    ]


def reservation_summary(reservation: Reservation, heading: str) -> str:
    start = local_start(reservation)
    return (
        f"<b>{escape(heading)}</b>\n\n"
        f"<b>Booking ID:</b> {booking_id(reservation.start_at, shop_timezone())}\n"
        f"<b>Slot number:</b> {slot_label(reservation.start_at, shop_timezone())}\n"
        f"<b>Name:</b> {escape(reservation.customer_name)}\n"
        f"<b>Email:</b> {escape(reservation.email or 'Not recorded')}\n"
        f"<b>Contact:</b> {escape(reservation.phone)}\n"
        f"<b>Number of pax:</b> {reservation.number_of_pax}\n"
        f"<b>Date:</b> {start:%A, %d %B %Y}\n"
        f"<b>Time:</b> {start:%I:%M %p}\n"
        f"<b>Timezone:</b> {escape(shop_timezone().key)}\n"
        f"<b>Status:</b> Confirmed"
    )


# ------------------------------- FAQ feature -------------------------------


def faq_keyboard(faqs: list[FAQ], page: int) -> InlineKeyboardMarkup:
    page_count = max(1, (len(faqs) + FAQ_PER_PAGE - 1) // FAQ_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    start = page * FAQ_PER_PAGE
    visible = faqs[start : start + FAQ_PER_PAGE]
    rows = [
        [InlineKeyboardButton(faq.question, callback_data=f"faq:item:{faq.id}:{page}")]
        for faq in visible
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹ Previous", callback_data=f"faq:page:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next ›", callback_data=f"faq:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append(back_to_main_menu_row())
    return InlineKeyboardMarkup(rows)


async def show_faq(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    repository: FAQRepository = context.application.bot_data["faq_repository"]
    faqs = repository.list_all()
    if faqs:
        text = "<b>Frequently Asked Questions</b>\n\nTap a question to see its answer."
        keyboard = faq_keyboard(faqs, page)
    else:
        text = "<b>Frequently Asked Questions</b>\n\nNo questions have been added yet. Please check again soon."
        keyboard = InlineKeyboardMarkup([back_to_main_menu_row()])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_faq(update, context)


async def handle_faq_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    parts = query.data.split(":")
    if len(parts) == 3 and parts[1] == "page":
        await query.answer()
        await show_faq(update, context, int(parts[2]))
        return
    if len(parts) != 4 or parts[1] != "item":
        await query.answer()
        return
    faq_id, page = int(parts[2]), int(parts[3])
    repository: FAQRepository = context.application.bot_data["faq_repository"]
    faq = repository.get(faq_id)
    if faq is None:
        await query.answer("That question is no longer available.", show_alert=True)
        await show_faq(update, context, page)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("‹ Back to all questions", callback_data=f"faq:page:{page}")]]
    )
    await query.edit_message_text(
        f"<b>{escape(faq.question)}</b>\n\n{escape(faq.answer)}",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


# --------------------------- Reservation feature ---------------------------


def date_keyboard(slots: list[TimeSlot], page: int) -> InlineKeyboardMarkup:
    zone = shop_timezone()
    dates = sorted({slot.start_at.astimezone(zone).date() for slot in slots})
    page_count = max(1, (len(dates) + DATES_PER_PAGE - 1) // DATES_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    visible = dates[page * DATES_PER_PAGE : (page + 1) * DATES_PER_PAGE]
    rows = [
        [InlineKeyboardButton(date_label(day), callback_data=f"book:date:{day.isoformat()}:{page}")]
        for day in visible
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹ Earlier", callback_data=f"book:dates:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Later ›", callback_data=f"book:dates:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append(back_to_main_menu_row())
    return InlineKeyboardMarkup(rows)


def slot_keyboard(
    slots: list[TimeSlot], date_page: int, time_page: int = 0
) -> InlineKeyboardMarkup:
    page_count = max(1, (len(slots) + TIMES_PER_PAGE - 1) // TIMES_PER_PAGE)
    time_page = max(0, min(time_page, page_count - 1))
    visible = slots[time_page * TIMES_PER_PAGE : (time_page + 1) * TIMES_PER_PAGE]
    rows: list[list[InlineKeyboardButton]] = []
    for slot in visible:
        callback_data = (
            f"book:slot:{slot.id}" if slot.remaining > 0 else f"book:full:{slot.id}"
        )
        rows.append([
            InlineKeyboardButton(
                f"Slot {slot_label(slot.start_at, shop_timezone())} · "
                f"{local_start(slot):%I:%M %p} — {slot.remaining} available",
                callback_data=callback_data,
            )
        ])
    if slots:
        selected_date = local_start(slots[0]).date().isoformat()
        navigation: list[InlineKeyboardButton] = []
        if time_page > 0:
            navigation.append(InlineKeyboardButton(
                "‹ Earlier",
                callback_data=(
                    f"book:times:{selected_date}:{date_page}:{time_page - 1}"
                ),
            ))
        if time_page + 1 < page_count:
            navigation.append(InlineKeyboardButton(
                "Next ›",
                callback_data=(
                    f"book:times:{selected_date}:{date_page}:{time_page + 1}"
                ),
            ))
        if navigation:
            rows.append(navigation)
    rows.append([InlineKeyboardButton("‹ Back to dates", callback_data=f"book:dates:{date_page}")])
    rows.append(back_to_main_menu_row())
    return InlineKeyboardMarkup(rows)


async def show_available_dates(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> int:
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    slots = visible_booking_slots(repository)
    change_id = context.user_data.get("reservation_change_id")
    if change_id is not None:
        slots = [slot for slot in slots if slot.id != context.user_data.get("reservation_original_slot_id")]
    if slots:
        heading = "Change reservation" if change_id is not None else "Make a reservation"
        text = f"<b>{heading}</b>\n\nChoose a date to view its time slots:"
        keyboard = date_keyboard(slots, page)
    else:
        text = "<b>Make a reservation</b>\n\nSorry, there are no available time slots right now."
        keyboard = InlineKeyboardMarkup([back_to_main_menu_row()])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return CHOOSE_SLOT


async def reservation_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_reservation_draft(context)
    if update.callback_query:
        await update.callback_query.answer()
    return await show_available_dates(update, context)


async def change_reservation_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or query.data is None or user is None:
        return ConversationHandler.END
    reservation_id = int(query.data.rsplit(":", 1)[1])
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    reservation = repository.get_reservation(reservation_id)
    if (
        reservation is None
        or reservation.telegram_user_id != user.id
        or reservation.status != "confirmed"
        or reservation.start_at <= datetime.now(timezone.utc)
    ):
        await query.answer("This reservation cannot be changed.", show_alert=True)
        return ConversationHandler.END

    clear_reservation_draft(context)
    context.user_data["reservation_change_id"] = reservation.id
    context.user_data["reservation_original_slot_id"] = reservation.slot_id
    await query.answer()
    return await show_available_dates(update, context)


async def show_slots_for_date(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected_date: date,
    date_page: int,
    time_page: int,
) -> int:
    query = update.callback_query
    if query is None:
        return CHOOSE_SLOT
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    zone = shop_timezone()
    slots = [
        slot
        for slot in visible_booking_slots(repository)
        if slot.start_at.astimezone(zone).date() == selected_date
        and slot.id != context.user_data.get("reservation_original_slot_id")
    ]
    if not slots:
        await query.answer("Those times were just booked. Please choose another date.", show_alert=True)
        return await show_available_dates(update, context, date_page)
    await query.answer()
    context.user_data["reservation_selected_date"] = selected_date.isoformat()
    context.user_data["reservation_date_page"] = date_page
    context.user_data["reservation_time_page"] = time_page
    await query.edit_message_text(
        f"<b>{escape(date_label(selected_date))}</b>\n\nChoose a time:",
        reply_markup=slot_keyboard(slots, date_page, time_page),
        parse_mode=ParseMode.HTML,
    )
    return CHOOSE_SLOT


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return CHOOSE_SLOT
    parts = query.data.split(":")
    return await show_slots_for_date(
        update,
        context,
        selected_date=date.fromisoformat(parts[2]),
        date_page=int(parts[3]),
        time_page=0,
    )


async def show_time_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return CHOOSE_SLOT
    parts = query.data.split(":")
    return await show_slots_for_date(
        update,
        context,
        selected_date=date.fromisoformat(parts[2]),
        date_page=int(parts[3]),
        time_page=int(parts[4]),
    )


async def show_date_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return CHOOSE_SLOT
    await query.answer()
    return await show_available_dates(update, context, int(query.data.rsplit(":", 1)[1]))


async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return CHOOSE_SLOT
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    slot_id = int(query.data.rsplit(":", 1)[1])
    slot = repository.get_slot(slot_id)
    current_time = datetime.now(timezone.utc)
    if (
        slot is None
        or not slot.is_active
        or slot.remaining < 1
        or slot.start_at < current_time + MINIMUM_BOOKING_NOTICE
    ):
        await query.answer("That time is no longer available. Please choose another.", show_alert=True)
        return await show_available_dates(update, context)
    await query.answer()
    context.user_data["reservation_slot_id"] = slot_id
    change_id = context.user_data.get("reservation_change_id")
    if change_id is not None:
        old_reservation = repository.get_reservation(int(change_id))
        if old_reservation is None:
            clear_reservation_draft(context)
            await query.edit_message_text(
                "This reservation is no longer available to change.",
                reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
            )
            return ConversationHandler.END
        old_start = local_start(old_reservation)
        new_start = local_start(slot)
        await query.edit_message_text(
            "<b>Confirm reservation change</b>\n\n"
            f"<b>Booking ID:</b> {booking_id(old_reservation.start_at, shop_timezone())}\n"
            f"<b>Current:</b> {old_start:%A, %d %B %Y at %I:%M %p}\n"
            f"<b>New booking ID:</b> {booking_id(slot.start_at, shop_timezone())}\n"
            f"<b>New:</b> Slot {slot_label(slot.start_at, shop_timezone())} · "
            f"{new_start:%A, %d %B %Y at %I:%M %p}\n\n"
            "Your current slot will be released only after this change succeeds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirm change", callback_data="book:changeconfirm")],
                [InlineKeyboardButton("Choose another time", callback_data="book:changedates:0")],
                [InlineKeyboardButton("Keep current reservation", callback_data="book:mine")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return CHANGE_CONFIRM
    selected_date = str(
        context.user_data.get(
            "reservation_selected_date", local_start(slot).date().isoformat()
        )
    )
    date_page = int(context.user_data.get("reservation_date_page", 0))
    time_page = int(context.user_data.get("reservation_time_page", 0))
    await query.edit_message_text(
        f"You selected <b>{local_start(slot):%A, %d %B %Y at %I:%M %p}</b>.\n\n"
        "Please type the full name for the reservation, or send /cancel.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "‹ Back to time slots",
                callback_data=f"book:times:{selected_date}:{date_page}:{time_page}",
            )
        ]]),
        parse_mode=ParseMode.HTML,
    )
    return ENTER_NAME


async def choose_unavailable_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Silently acknowledge a tap on a visible, fully booked time slot."""
    if update.callback_query:
        await update.callback_query.answer()
    return CHOOSE_SLOT


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return ENTER_NAME
    name = update.effective_message.text.strip()
    if not 2 <= len(name) <= 80:
        await update.effective_message.reply_text("Please enter a name between 2 and 80 characters.")
        return ENTER_NAME
    context.user_data["reservation_name"] = name
    await update.effective_message.reply_text(
        "Please type your email address, or send /cancel."
    )
    return ENTER_EMAIL


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return ENTER_EMAIL
    email = update.effective_message.text.strip().lower()
    if (
        not 3 <= len(email) <= 254
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None
    ):
        await update.effective_message.reply_text(
            "That email address does not look valid. Please try again, for example name@example.com."
        )
        return ENTER_EMAIL
    context.user_data["reservation_email"] = email
    await update.effective_message.reply_text(
        "Please type a contact phone number, including the country code if applicable.\n"
        "Example: +65 8123 4567"
    )
    return ENTER_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return ENTER_PHONE
    phone = update.effective_message.text.strip()
    digit_count = len(re.sub(r"\D", "", phone))
    if not 7 <= digit_count <= 15 or len(phone) > 30:
        await update.effective_message.reply_text(
            "That phone number does not look valid. Please enter 7–15 digits, for example +65 8123 4567."
        )
        return ENTER_PHONE

    context.user_data["reservation_phone"] = phone
    await update.effective_message.reply_text(
        "How many pax is this reservation for? Please enter a whole number from 1 to 50."
    )
    return ENTER_PAX


async def receive_pax(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return ENTER_PAX
    value = update.effective_message.text.strip()
    if not value.isdigit() or not 1 <= int(value) <= 50:
        await update.effective_message.reply_text(
            "Please enter the number of pax as a whole number from 1 to 50."
        )
        return ENTER_PAX

    number_of_pax = int(value)
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    slot = repository.get_slot(int(context.user_data["reservation_slot_id"]))
    current_time = datetime.now(timezone.utc)
    if (
        slot is None
        or not slot.is_active
        or slot.remaining < 1
        or slot.start_at < current_time + MINIMUM_BOOKING_NOTICE
    ):
        clear_reservation_draft(context)
        await update.effective_message.reply_text(
            "Sorry, that slot is no longer available. Use /reserve to choose another slot.",
            reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
        )
        return ConversationHandler.END

    context.user_data["reservation_number_of_pax"] = number_of_pax
    start = local_start(slot)
    text = (
        "<b>Please confirm your reservation</b>\n\n"
        f"<b>Booking ID:</b> {booking_id(slot.start_at, shop_timezone())}\n"
        f"<b>Slot number:</b> {slot_label(slot.start_at, shop_timezone())}\n"
        f"<b>Name:</b> {escape(str(context.user_data['reservation_name']))}\n"
        f"<b>Email:</b> {escape(str(context.user_data['reservation_email']))}\n"
        f"<b>Contact:</b> {escape(str(context.user_data['reservation_phone']))}\n"
        f"<b>Number of pax:</b> {number_of_pax}\n"
        f"<b>Date:</b> {start:%A, %d %B %Y}\n"
        f"<b>Time:</b> {start:%I:%M %p}\n"
        f"<b>Timezone:</b> {escape(shop_timezone().key)}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm reservation", callback_data="book:confirm")],
        back_to_main_menu_row(),
    ])
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return CONFIRM


async def confirm_reservation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or update.effective_user is None or update.effective_chat is None:
        return ConversationHandler.END
    await query.answer()
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    try:
        reservation = repository.create_reservation(
            slot_id=int(context.user_data["reservation_slot_id"]),
            telegram_user_id=update.effective_user.id,
            telegram_chat_id=update.effective_chat.id,
            customer_name=str(context.user_data["reservation_name"]),
            email=str(context.user_data["reservation_email"]),
            phone=str(context.user_data["reservation_phone"]),
            number_of_pax=int(context.user_data["reservation_number_of_pax"]),
            telegram_username=update.effective_user.username,
            admin_notification_chat_ids=admin_notification_chat_ids(),
        )
    except (SlotUnavailableError, DuplicateReservationError) as error:
        clear_reservation_draft(context)
        await query.edit_message_text(
            f"<b>Reservation not completed</b>\n\n{escape(str(error))}\n\nUse /reserve to try again.",
            reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    clear_reservation_draft(context)
    await query.edit_message_text(
        reservation_summary(reservation, "✅ Reservation confirmed"),
        reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def confirm_reservation_change(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    await query.answer()
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    try:
        reservation = repository.change_user_reservation(
            int(context.user_data["reservation_change_id"]),
            user.id,
            int(context.user_data["reservation_slot_id"]),
        )
    except (ReservationChangeError, SlotUnavailableError, DuplicateReservationError) as error:
        clear_reservation_draft(context)
        await query.edit_message_text(
            f"<b>Reservation not changed</b>\n\n{escape(str(error))}\n\n"
            "Your original reservation is still confirmed.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("View my reservations", callback_data="book:mine")],
                back_to_main_menu_row(),
            ]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    clear_reservation_draft(context)
    await query.edit_message_text(
        reservation_summary(reservation, "✅ Reservation changed")
        + "\n\nYour original slot is now available to other customers.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("View my reservations", callback_data="book:mine")],
            back_to_main_menu_row(),
        ]),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel_reservation_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_reservation_draft(context)
    await show_main_menu(update)
    return ConversationHandler.END


async def return_to_my_reservations(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    clear_reservation_draft(context)
    await my_reservations(update, context)
    return ConversationHandler.END


async def my_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    user = update.effective_user
    if user is None:
        return
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    reservations = repository.list_user_reservations(user.id)
    if reservations:
        text = "<b>Your upcoming reservations</b>\n\n" + "\n\n".join(
            reservation_summary(item, "Reservation") for item in reservations
        )
        rows = [
            [InlineKeyboardButton(
                f"Change {booking_id(item.start_at, shop_timezone())} — "
                f"{local_start(item):%d %b, %I:%M %p}",
                callback_data=f"book:change:{item.id}",
            )]
            for item in reservations
        ] + [
            [InlineKeyboardButton(
                f"Cancel {booking_id(item.start_at, shop_timezone())} — "
                f"{local_start(item):%d %b, %I:%M %p}",
                callback_data=f"book:cancelask:{item.id}",
            )]
            for item in reservations
        ]
        rows.append(back_to_main_menu_row())
        keyboard = InlineKeyboardMarkup(rows)
    else:
        text = "<b>Your upcoming reservations</b>\n\nYou do not have an upcoming reservation."
        keyboard = InlineKeyboardMarkup([back_to_main_menu_row()])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )


async def request_customer_cancellation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or query.data is None or user is None:
        return
    reservation_id = int(query.data.rsplit(":", 1)[1])
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    reservation = repository.get_reservation(reservation_id)
    if (
        reservation is None
        or reservation.telegram_user_id != user.id
        or reservation.status != "confirmed"
        or reservation.start_at <= datetime.now(timezone.utc)
    ):
        await query.answer("This reservation cannot be cancelled.", show_alert=True)
        return

    await query.answer()
    start = local_start(reservation)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Yes, cancel reservation",
                callback_data=f"book:cancelconfirm:{reservation.id}",
            )
        ],
        [InlineKeyboardButton("No, keep it", callback_data="book:mine")],
    ])
    await query.edit_message_text(
        "<b>Cancel this reservation?</b>\n\n"
        f"<b>Booking ID:</b> {booking_id(reservation.start_at, shop_timezone())}\n"
        f"<b>Slot number:</b> {slot_label(reservation.start_at, shop_timezone())}\n"
        f"<b>Date:</b> {start:%A, %d %B %Y}\n"
        f"<b>Time:</b> {start:%I:%M %p}\n\n"
        "This will immediately make the place available to another customer.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def confirm_customer_cancellation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or query.data is None or user is None:
        return
    reservation_id = int(query.data.rsplit(":", 1)[1])
    repository: ReservationRepository = context.application.bot_data["reservation_repository"]
    reservation = repository.get_reservation(reservation_id)
    cancelled = repository.cancel_user_reservation(reservation_id, user.id)
    if not cancelled:
        await query.answer("This reservation cannot be cancelled.", show_alert=True)
        return

    await query.answer("Reservation cancelled")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("View my reservations", callback_data="book:mine")],
        [InlineKeyboardButton("Make another reservation", callback_data="book:start")],
        back_to_main_menu_row(),
    ])
    await query.edit_message_text(
        "✅ Reservation "
        f"{booking_id(reservation.start_at, shop_timezone()) if reservation else ''} "
        "has been cancelled. Its place is available again.",
        reply_markup=keyboard,
    )


async def deliver_reservation_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    repository: ReservationRepository,
    notification: ReminderNotification,
) -> bool:
    """Attempt one reminder delivery and persist success or the next retry."""
    reservation = notification.reservation
    try:
        await context.bot.send_message(
            chat_id=reservation.telegram_chat_id,
            text=reservation_summary(reservation, "⏰ Reservation reminder")
            + "\n\nYour PhotoLes appointment is coming up in one day. We look forward to seeing you!",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as error:
        repository.record_reminder_failure(reservation.id, str(error))
        LOGGER.warning(
            "Reminder for reservation R%d failed on attempt %d; queued for retry",
            reservation.id,
            notification.attempt_count + 1,
        )
        return False

    repository.mark_reminder_sent(reservation.id)
    LOGGER.info("Sent reminder for reservation R%d", reservation.id)
    return True


async def process_reservation_reminders(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send all due durable reminders, up to a safe batch size."""
    repository: ReservationRepository = context.application.bot_data[
        "reservation_repository"
    ]
    for notification in repository.due_reminder_notifications(limit=20):
        await deliver_reservation_reminder(context, repository, notification)


def admin_booking_notification_text(reservation: Reservation) -> str:
    start = local_start(reservation)
    created = reservation.created_at.astimezone(shop_timezone())
    username = (
        f"@{escape(reservation.telegram_username)}"
        if reservation.telegram_username
        else "Not provided"
    )
    return (
        "<b>🆕 New PhotoLes reservation</b>\n\n"
        f"<b>Booking ID:</b> {booking_id(reservation.start_at, shop_timezone())}\n"
        f"<b>Slot number:</b> {slot_label(reservation.start_at, shop_timezone())}\n"
        f"<b>Name:</b> {escape(reservation.customer_name)}\n"
        f"<b>Email:</b> {escape(reservation.email or 'Not recorded')}\n"
        f"<b>Contact:</b> {escape(reservation.phone)}\n"
        f"<b>Number of pax:</b> {reservation.number_of_pax}\n"
        f"<b>Date:</b> {start:%A, %d %B %Y}\n"
        f"<b>Time:</b> {start:%I:%M %p}\n"
        f"<b>Timezone:</b> {escape(shop_timezone().key)}\n"
        f"<b>Telegram username:</b> {username}\n"
        f"<b>Telegram user ID:</b> {reservation.telegram_user_id}\n"
        f"<b>Booked at:</b> {created:%d %B %Y, %I:%M:%S %p}\n"
        f"<b>Current status:</b> {escape(reservation.status.title())}"
    )


async def deliver_admin_booking_notification(
    context: ContextTypes.DEFAULT_TYPE,
    repository: ReservationRepository,
    notification: BookingNotification,
) -> bool:
    """Attempt one admin alert and retain failures for automatic retry."""
    try:
        await context.bot.send_message(
            chat_id=notification.admin_chat_id,
            text=admin_booking_notification_text(notification.reservation),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as error:
        repository.record_booking_notification_failure(notification.id, str(error))
        LOGGER.warning(
            "Admin booking notification %d for reservation R%d failed on attempt %d; queued for retry",
            notification.id,
            notification.reservation.id,
            notification.attempt_count + 1,
        )
        return False

    repository.mark_booking_notification_sent(notification.id)
    LOGGER.info(
        "Sent admin booking notification %d for reservation R%d",
        notification.id,
        notification.reservation.id,
    )
    return True


async def process_admin_booking_notifications(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    repository: ReservationRepository = context.application.bot_data[
        "reservation_repository"
    ]
    for notification in repository.pending_booking_notifications(limit=20):
        await deliver_admin_booking_notification(context, repository, notification)


def delete_expired_reservation_data(application: Application) -> None:
    """Delete appointment data from calendar days before today in shop time."""
    zone = shop_timezone()
    local_now = datetime.now(zone)
    local_midnight = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    repository: ReservationRepository = application.bot_data["reservation_repository"]
    result = repository.delete_data_before(local_midnight)
    if result.reservations_deleted or result.slots_deleted:
        LOGGER.info(
            "Deleted %d expired reservation(s) and %d expired time slot(s)",
            result.reservations_deleted,
            result.slots_deleted,
        )


def ensure_rolling_reservation_slots(application: Application) -> None:
    """Keep the fixed daily booking calendar filled through the configured end date."""
    zone = shop_timezone()
    today = datetime.now(zone).date()
    repository: ReservationRepository = application.bot_data["reservation_repository"]
    inserted = repository.ensure_slots(rolling_slot_starts(today, zone))
    if inserted:
        LOGGER.info(
            "Created %d missing reservation slot(s) through %s",
            inserted,
            booking_window_last_date(today).isoformat(),
        )


async def daily_reservation_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    delete_expired_reservation_data(context.application)
    ensure_rolling_reservation_slots(context.application)


# -------------------------- Customer service feature -----------------------


async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    service_chat_id = customer_service_chat_id()
    if service_chat_id is None:
        text = "Online customer service is temporarily unavailable. Please try again later."
        keyboard = InlineKeyboardMarkup([back_to_main_menu_row()])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=keyboard)
        return ConversationHandler.END

    text = (
        "<b>Online Customer Service</b>\n\n"
        "Type your question below. It will be sent privately to our customer service team.\n\n"
        "Our customer service personnel will reply to your queries ASAP."
    )
    keyboard = InlineKeyboardMarkup(
        [back_to_main_menu_row()]
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    return SUPPORT_QUESTION


async def receive_support_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    service_chat_id = customer_service_chat_id()
    if message is None or message.text is None or user is None or chat is None:
        return SUPPORT_QUESTION
    if service_chat_id is None:
        await message.reply_text(
            "Online customer service is temporarily unavailable.",
            reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
        )
        return ConversationHandler.END

    question = message.text.strip()
    if not question:
        await message.reply_text("Please type a question, or send /cancel.")
        return SUPPORT_QUESTION
    if len(question) > MAX_QUESTION_LENGTH:
        await message.reply_text(
            f"Please shorten your question to {MAX_QUESTION_LENGTH} characters or fewer."
        )
        return SUPPORT_QUESTION

    repository: SupportRepository = context.application.bot_data["support_repository"]
    ticket = repository.create_ticket(
        telegram_user_id=user.id,
        telegram_chat_id=chat.id,
        customer_name=user.full_name,
        telegram_username=user.username,
        question=question,
    )
    username = f"@{user.username}" if user.username else "Not provided"
    staff_text = (
        f"🆕 Customer service question S{ticket.id}\n\n"
        f"From: {user.full_name}\n"
        f"Telegram username: {username}\n"
        f"Customer user ID: {user.id}\n\n"
        f"Question:\n{question}\n\n"
        "↩️ Reply to this message to answer the customer."
    )
    try:
        staff_message = await context.bot.send_message(chat_id=service_chat_id, text=staff_text)
    except TelegramError:
        repository.delete(ticket.id)
        LOGGER.exception("Could not deliver support ticket S%d to staff", ticket.id)
        await message.reply_text(
            "Sorry, we could not send your question right now. Please try again in a few minutes.",
            reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
        )
        return ConversationHandler.END

    repository.set_staff_message_id(ticket.id, staff_message.message_id)
    await message.reply_text(
        f"✅ Your question was sent to customer service as ticket S{ticket.id}. "
        "You will receive the answer here.",
        reply_markup=InlineKeyboardMarkup([back_to_main_menu_row()]),
    )
    return ConversationHandler.END


async def support_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_main_menu(update)
    return ConversationHandler.END


async def staff_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.reply_to_message is None or message.text is None:
        return

    repository: SupportRepository = context.application.bot_data["support_repository"]
    ticket = repository.get_by_staff_message_id(message.reply_to_message.message_id)
    if ticket is None:
        await message.reply_text(
            "I could not match that reply to a customer ticket. Reply directly to a message "
            "that starts with ‘Customer service question’."
        )
        return

    answer = message.text.strip()
    if not answer:
        await message.reply_text("Please send a text answer.")
        return
    if len(answer) > MAX_ANSWER_LENGTH:
        await message.reply_text(
            f"Please shorten the answer to {MAX_ANSWER_LENGTH} characters or fewer."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ask another question", callback_data="support:start")],
        back_to_main_menu_row(),
    ])
    try:
        await context.bot.send_message(
            chat_id=ticket.telegram_chat_id,
            text=f"💬 Customer service replied to ticket S{ticket.id}:\n\n{answer}",
            reply_markup=keyboard,
        )
    except TelegramError:
        LOGGER.exception("Could not deliver the answer for support ticket S%d", ticket.id)
        await message.reply_text(
            f"❌ The answer for S{ticket.id} could not be delivered. The customer may have blocked the bot."
        )
        return

    repository.mark_answered(ticket.id, answer)
    await message.reply_text(f"✅ Answer sent to the customer for ticket S{ticket.id}.")


async def admin_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_chat is None:
        return
    await update.effective_message.reply_text(
        f"This chat ID is: {update.effective_chat.id}\n\n"
        "To receive new-booking alerts here, copy this number into "
        "ADMIN_NOTIFICATION_CHAT_IDS in .env, then restart the bot."
    )


# ----------------------------- Bot lifecycle -------------------------------


async def show_main_menu(update: Update) -> None:
    text = "Welcome to PhotoLes! 📸\n\nHow can we help you today?"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=main_menu_keyboard()
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=main_menu_keyboard()
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update)


async def back_to_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    clear_reservation_draft(context)
    await show_main_menu(update)
    return ConversationHandler.END


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open the main menu"),
    ])
    delete_expired_reservation_data(application)
    ensure_rolling_reservation_slots(application)
    if application.job_queue is None:
        raise RuntimeError("JobQueue is unavailable. Install python-telegram-bot[job-queue].")
    application.job_queue.run_daily(
        daily_reservation_cleanup,
        time=time(hour=0, minute=0, tzinfo=shop_timezone()),
        name="daily-expired-reservation-cleanup",
    )
    application.job_queue.run_repeating(
        process_cancellation_notifications,
        interval=15,
        first=1,
        name="cancellation-notification-outbox",
    )
    application.job_queue.run_repeating(
        process_reservation_reminders,
        interval=30,
        first=1,
        name="reservation-reminder-outbox",
    )
    application.job_queue.run_repeating(
        process_admin_booking_notifications,
        interval=15,
        first=1,
        name="admin-booking-notification-outbox",
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Use /reserve to book a time, /myreservations to check bookings, /faq for common "
            "questions, or /support to contact customer service."
        )


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled error while processing an update", exc_info=context.error)


def main() -> None:
    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token == "replace-with-your-bot-token":
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and add the token from @BotFather."
        )

    faq_repository = FAQRepository(database_path())
    faq_repository.initialize()
    reservation_repository = ReservationRepository(database_path())
    reservation_repository.initialize()
    support_repository = SupportRepository(database_path())
    support_repository.initialize()

    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["faq_repository"] = faq_repository
    application.bot_data["reservation_repository"] = reservation_repository
    application.bot_data["support_repository"] = support_repository

    reservation_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("reserve", reservation_start),
            CallbackQueryHandler(reservation_start, pattern=r"^book:start$"),
            CallbackQueryHandler(change_reservation_start, pattern=r"^book:change:\d+$"),
        ],
        states={
            CHOOSE_SLOT: [
                CallbackQueryHandler(choose_date, pattern=r"^book:date:\d{4}-\d{2}-\d{2}:\d+$"),
                CallbackQueryHandler(show_date_page, pattern=r"^book:dates:\d+$"),
                CallbackQueryHandler(
                    show_time_page,
                    pattern=r"^book:times:\d{4}-\d{2}-\d{2}:\d+:\d+$",
                ),
                CallbackQueryHandler(choose_slot, pattern=r"^book:slot:\d+$"),
                CallbackQueryHandler(choose_unavailable_slot, pattern=r"^book:full:\d+$"),
            ],
            ENTER_NAME: [
                CallbackQueryHandler(
                    show_time_page,
                    pattern=r"^book:times:\d{4}-\d{2}-\d{2}:\d+:\d+$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name),
            ],
            ENTER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            ENTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            ENTER_PAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pax)],
            CONFIRM: [CallbackQueryHandler(confirm_reservation, pattern=r"^book:confirm$")],
            CHANGE_CONFIRM: [
                CallbackQueryHandler(
                    confirm_reservation_change, pattern=r"^book:changeconfirm$"
                ),
                CallbackQueryHandler(
                    show_date_page, pattern=r"^book:changedates:\d+$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_reservation_flow),
            CallbackQueryHandler(cancel_reservation_flow, pattern=r"^book:cancel$"),
            CallbackQueryHandler(return_to_my_reservations, pattern=r"^book:mine$"),
            CallbackQueryHandler(back_to_main_menu, pattern=r"^menu:main$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(reservation_conversation)
    support_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("support", support_start),
            CallbackQueryHandler(support_start, pattern=r"^support:start$"),
            MessageHandler(
                filters.Regex(r"^/start(?:@\w+)?\s+support\s*$"), support_start
            ),
        ],
        states={
            SUPPORT_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_support_question)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", support_cancel),
            CallbackQueryHandler(support_cancel, pattern=r"^support:cancel$"),
            CallbackQueryHandler(back_to_main_menu, pattern=r"^menu:main$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(support_conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myreservations", my_reservations))
    application.add_handler(CommandHandler("faq", faq_command))
    application.add_handler(CommandHandler("adminid", admin_id_command))
    application.add_handler(
        CallbackQueryHandler(back_to_main_menu, pattern=r"^menu:main$")
    )
    application.add_handler(
        CallbackQueryHandler(request_customer_cancellation, pattern=r"^book:cancelask:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(confirm_customer_cancellation, pattern=r"^book:cancelconfirm:\d+$")
    )
    application.add_handler(CallbackQueryHandler(my_reservations, pattern=r"^book:mine$"))
    application.add_handler(CallbackQueryHandler(handle_faq_button, pattern=r"^faq:"))
    service_chat_id = customer_service_chat_id()
    if service_chat_id is not None:
        application.add_handler(
            MessageHandler(
                filters.Chat(chat_id=service_chat_id)
                & filters.REPLY
                & filters.TEXT
                & ~filters.COMMAND,
                staff_reply,
            )
        )
    else:
        LOGGER.warning(
            "CUSTOMER_SERVICE_CHAT_ID is not configured; /support will remain unavailable"
        )
    if not admin_notification_chat_ids():
        LOGGER.warning(
            "ADMIN_NOTIFICATION_CHAT_IDS is not configured; new-booking admin alerts are disabled"
        )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    application.add_error_handler(log_error)

    LOGGER.info("Starting PhotoLes bot in timezone %s", shop_timezone().key)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
