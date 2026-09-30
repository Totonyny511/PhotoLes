"""Backend commands for PhotoLes time slots and reservations."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta

from booking_identifiers import booking_id, slot_label
from reservation_repository import ReservationRepository
from settings import database_path, load_local_env, shop_timezone


DATE_TIME_FORMAT = "%Y-%m-%d %H:%M"


def local_datetime(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, DATE_TIME_FORMAT)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use YYYY-MM-DD HH:MM, for example 2026-09-10 14:30."
        ) from error
    return parsed.replace(tzinfo=shop_timezone())


def date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD, for example 2026-09-10.") from error


def clock_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use HH:MM, for example 09:30.") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage PhotoLes availability and reservations.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add-slot", help="Add one available time slot.")
    add.add_argument("--start", required=True, type=local_datetime)
    add.add_argument("--capacity", type=int, default=1)

    generate = commands.add_parser("generate-slots", help="Create evenly spaced slots for one day.")
    generate.add_argument("--date", required=True, type=date_value)
    generate.add_argument("--from", dest="from_time", required=True, type=clock_time)
    generate.add_argument("--to", dest="to_time", required=True, type=clock_time)
    generate.add_argument("--every", type=int, default=20, help="Minutes between slot start times.")
    generate.add_argument("--capacity", type=int, default=1)

    list_slots = commands.add_parser("list-slots", help="Check slot capacity and availability.")
    list_slots.add_argument("--date", type=date_value)
    list_slots.add_argument("--all", action="store_true", help="Include old and disabled slots.")

    update = commands.add_parser("update-slot", help="Change a slot's time or capacity.")
    update.add_argument("id", type=int)
    update.add_argument("--start", type=local_datetime)
    update.add_argument("--capacity", type=int)

    for command, help_text in (
        ("enable-slot", "Make a slot visible to customers."),
        ("disable-slot", "Hide a slot from customers."),
        ("delete-slot", "Permanently delete a slot with no reservation history."),
    ):
        command_parser = commands.add_parser(command, help=help_text)
        command_parser.add_argument("id", type=int)
        if command == "delete-slot":
            command_parser.add_argument("--yes", action="store_true")

    reservations = commands.add_parser("list-reservations", help="View customer reservations.")
    reservations.add_argument("--date", type=date_value)
    reservations.add_argument("--all", action="store_true", help="Include cancelled reservations.")

    cancel = commands.add_parser("cancel-reservation", help="Cancel a reservation and free its place.")
    cancel.add_argument("id", type=int)
    return parser


def print_slots(repository: ReservationRepository, selected_date: date | None, show_all: bool) -> None:
    zone = shop_timezone()
    slots = repository.list_slots(future_only=not show_all, include_inactive=show_all)
    slots = [
        slot
        for slot in slots
        if selected_date is None or slot.start_at.astimezone(zone).date() == selected_date
    ]
    if not slots:
        print("No matching slots.")
        return
    for slot in slots:
        local_start = slot.start_at.astimezone(zone)
        status = "active" if slot.is_active else "disabled"
        print(
            f"[slot {slot_label(slot.start_at, zone)}; internal #{slot.id}] "
            f"{local_start:%a, %d %b %Y %H:%M} | "
            f"booked {slot.reserved_count}/{slot.capacity} | "
            f"available {slot.remaining} | {status}"
        )


def print_reservations(
    repository: ReservationRepository, selected_date: date | None, show_all: bool
) -> None:
    zone = shop_timezone()
    reservations = repository.list_reservations(include_cancelled=show_all)
    reservations = [
        reservation
        for reservation in reservations
        if selected_date is None or reservation.start_at.astimezone(zone).date() == selected_date
    ]
    if not reservations:
        print("No matching reservations.")
        return
    for reservation in reservations:
        local_start = reservation.start_at.astimezone(zone)
        username = f"@{reservation.telegram_username}" if reservation.telegram_username else "no username"
        reminder = "sent" if reservation.reminder_sent else "pending"
        print(
            f"[{booking_id(reservation.start_at, zone)}; internal #{reservation.id}] "
            f"{local_start:%a, %d %b %Y %H:%M} | "
            f"{reservation.customer_name} | {reservation.email or 'no email'} | "
            f"{reservation.phone} | {reservation.number_of_pax} pax | {username} | "
            f"{reservation.status} | reminder {reminder}"
        )


def main() -> int:
    load_local_env()
    args = build_parser().parse_args()
    repository = ReservationRepository(database_path())
    repository.initialize()
    zone = shop_timezone()

    try:
        if args.command == "add-slot":
            slot_id = repository.add_slot(args.start, args.capacity)
            print(f"Added slot #{slot_id}: {args.start:%a, %d %b %Y %H:%M} ({zone.key}).")
        elif args.command == "generate-slots":
            if args.every < 1:
                raise ValueError("--every must be at least 1 minute.")
            current = datetime.combine(args.date, args.from_time, tzinfo=zone)
            end = datetime.combine(args.date, args.to_time, tzinfo=zone)
            if end <= current:
                raise ValueError("--to must be later than --from on the same day.")
            added = 0
            skipped = 0
            while current < end:
                try:
                    repository.add_slot(current, args.capacity)
                    added += 1
                except ValueError as error:
                    if "already exists" not in str(error):
                        raise
                    skipped += 1
                current += timedelta(minutes=args.every)
            print(f"Added {added} slot(s); skipped {skipped} duplicate(s).")
        elif args.command == "list-slots":
            print_slots(repository, args.date, args.all)
        elif args.command == "update-slot":
            if args.start is None and args.capacity is None:
                raise ValueError("Provide --start and/or --capacity.")
            if not repository.update_slot(args.id, start_at=args.start, capacity=args.capacity):
                raise ValueError(f"Slot #{args.id} does not exist.")
            print(f"Updated slot #{args.id}.")
        elif args.command in {"enable-slot", "disable-slot"}:
            active = args.command == "enable-slot"
            if not repository.set_slot_active(args.id, active):
                raise ValueError(f"Slot #{args.id} does not exist.")
            print(f"{'Enabled' if active else 'Disabled'} slot #{args.id}.")
        elif args.command == "delete-slot":
            if not args.yes:
                confirmation = input(f"Permanently delete slot #{args.id}? Type yes: ")
                if confirmation.strip().lower() != "yes":
                    print("Delete cancelled.")
                    return 0
            if not repository.delete_slot(args.id):
                raise ValueError(f"Slot #{args.id} does not exist.")
            print(f"Deleted slot #{args.id}.")
        elif args.command == "list-reservations":
            print_reservations(repository, args.date, args.all)
        elif args.command == "cancel-reservation":
            cancellation = repository.cancel_future_reservation_and_queue_notification(
                args.id
            )
            if cancellation is None:
                raise ValueError(
                    f"Upcoming confirmed reservation record #{args.id} does not exist."
                )
            print(
                f"Cancelled {booking_id(result.reservation.start_at, zone)}; "
                "its place is available again. "
                "The customer notification is queued for the PhotoLes bot."
            )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
