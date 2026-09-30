"""Authenticated, server-rendered administration dashboard for PhotoLes."""

from __future__ import annotations

import csv
import hmac
import io
import json
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from typing import Callable, TypeVar

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from booking_identifiers import booking_id, slot_label
from faq_repository import FAQRepository
from reservation_repository import MINIMUM_BOOKING_NOTICE, Reservation, ReservationRepository
from settings import database_path, load_local_env, shop_timezone


View = TypeVar("View", bound=Callable[..., object])
OPENING_TIME = time(11, 0)
CLOSING_TIME = time(21, 0)


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    load_local_env()
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("ADMIN_SECRET_KEY"),
        ADMIN_ACCOUNTS=os.environ.get("ADMIN_ACCOUNTS"),
        DATABASE_PATH=str(database_path()),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("ADMIN_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes"},
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("ADMIN_SECRET_KEY is required to run the admin dashboard.")
    admin_accounts = parse_admin_accounts(app.config.get("ADMIN_ACCOUNTS"))

    reservations = ReservationRepository(app.config["DATABASE_PATH"])
    faqs = FAQRepository(app.config["DATABASE_PATH"])
    reservations.initialize()
    faqs.initialize()
    app.extensions["reservation_repository"] = reservations
    app.extensions["faq_repository"] = faqs

    def reservation_repository() -> ReservationRepository:
        return app.extensions["reservation_repository"]

    def faq_repository() -> FAQRepository:
        return app.extensions["faq_repository"]

    def login_required(view: View) -> View:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            if not session.get("admin_authenticated") or not session.get("admin_name"):
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return str(token)

    @app.context_processor
    def inject_helpers() -> dict[str, object]:
        return {
            "csrf_token": csrf_token,
            "shop_zone": shop_timezone(),
            "local_datetime": lambda value: value.astimezone(shop_timezone()),
            "booking_id": lambda value: booking_id(value.start_at, shop_timezone()),
            "slot_label": lambda value: slot_label(value.start_at, shop_timezone()),
            "today": datetime.now(shop_timezone()).date(),
        }

    @app.before_request
    def validate_csrf() -> None:
        if request.method != "POST":
            return
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(str(expected), supplied):
            abort(400, "Invalid or expired form token. Refresh the page and try again.")

    @app.get("/health")
    def health():
        """Report that the production web process initialized successfully."""
        return {"status": "ok"}

    @app.get("/login")
    def login():
        if session.get("admin_authenticated") and session.get("admin_name"):
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        password = request.form.get("password", "")
        username = request.form.get("username", "").strip()
        account = admin_accounts.get(username.casefold())
        # Perform a password check even for unknown usernames so the response does not
        # reveal registered accounts through an obvious timing difference.
        account_to_check = account or next(iter(admin_accounts.values()))
        valid = account is not None and account_password_matches(account_to_check, password)
        if not valid:
            flash("Incorrect username or password.", "error")
            return render_template("login.html"), 401
        session.clear()
        session["admin_authenticated"] = True
        session["admin_name"] = account["username"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        destination = request.args.get("next", "")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = url_for("dashboard")
        return redirect(destination)

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        now = datetime.now(timezone.utc)
        local_today = now.astimezone(shop_timezone()).date()
        upcoming = [
            item
            for item in reservation_repository().list_reservations()
            if item.start_at > now
        ]
        todays = [
            item
            for item in upcoming
            if item.start_at.astimezone(shop_timezone()).date() == local_today
        ]
        future_slots = [
            slot
            for slot in reservation_repository().list_slots(future_only=True, now=now)
            if slot.start_at >= now + MINIMUM_BOOKING_NOTICE
        ]
        days: dict[date, list[Reservation]] = defaultdict(list)
        for item in upcoming:
            local_day = item.start_at.astimezone(shop_timezone()).date()
            if local_day <= local_today + timedelta(days=13):
                days[local_day].append(item)
        calendar_days = [
            (local_today + timedelta(days=offset), days[local_today + timedelta(days=offset)])
            for offset in range(14)
        ]
        return render_template(
            "dashboard.html",
            upcoming=upcoming[:8],
            today_count=len(todays),
            upcoming_count=len(upcoming),
            upcoming_pax=sum(item.number_of_pax for item in upcoming),
            available_slots=sum(slot.remaining for slot in future_slots),
            calendar_days=calendar_days,
        )

    @app.get("/reservations")
    @login_required
    def reservation_list():
        selected_date = parse_optional_date(request.args.get("date"), "date")
        status = request.args.get("status", "confirmed")
        if status not in {"confirmed", "cancelled", "all"}:
            status = "confirmed"
        query = request.args.get("q", "").strip().casefold()
        items = reservation_repository().list_reservations(
            include_cancelled=status != "confirmed"
        )
        if status == "cancelled":
            items = [item for item in items if item.status == "cancelled"]
        if selected_date:
            items = [
                item
                for item in items
                if item.start_at.astimezone(shop_timezone()).date() == selected_date
            ]
        if query:
            items = [item for item in items if reservation_matches(item, query)]
        return render_template(
            "reservations.html",
            reservations=items,
            selected_date=selected_date,
            status=status,
            query=request.args.get("q", "").strip(),
        )

    @app.get("/reservations/<int:reservation_id>")
    @login_required
    def reservation_detail(reservation_id: int):
        item = reservation_repository().get_reservation(reservation_id)
        if item is None:
            abort(404)
        return render_template("reservation_detail.html", reservation=item)

    @app.post("/reservations/<int:reservation_id>/cancel")
    @login_required
    def reservation_cancel(reservation_id: int):
        result = reservation_repository().cancel_future_reservation_and_queue_notification(
            reservation_id
        )
        if result is None:
            flash("That reservation is no longer eligible for cancellation.", "error")
        else:
            flash(
                f"Reservation {booking_id(result.reservation.start_at, shop_timezone())} "
                "was cancelled and the customer notification was queued.",
                "success",
            )
        return redirect(url_for("reservation_detail", reservation_id=reservation_id))

    @app.get("/slots")
    @login_required
    def slot_list():
        selected_date = parse_optional_date(request.args.get("date"), "date")
        show_all = request.args.get("show") == "all"
        slots = reservation_repository().list_slots(
            future_only=not show_all, include_inactive=show_all
        )
        if selected_date:
            slots = [
                slot
                for slot in slots
                if slot.start_at.astimezone(shop_timezone()).date() == selected_date
            ]
        return render_template(
            "slots.html",
            slots=slots,
            selected_date=selected_date,
            show_all=show_all,
            blocks=reservation_repository().list_slot_blocks(),
        )

    @app.post("/slots")
    @login_required
    def slot_create():
        try:
            start_at = parse_local_start(request.form.get("date"), request.form.get("time"))
            validate_operating_hours(start_at)
            capacity = parse_positive_int(request.form.get("capacity"), "Capacity")
            reservation_repository().add_slot(start_at, capacity)
        except ValueError as error:
            flash(str(error), "error")
        else:
            flash("Time slot created.", "success")
        return redirect(url_for("slot_list", date=request.form.get("date", "")))

    @app.post("/slots/<int:slot_id>/update")
    @login_required
    def slot_update(slot_id: int):
        try:
            start_at = parse_local_start(request.form.get("date"), request.form.get("time"))
            validate_operating_hours(start_at)
            capacity = parse_positive_int(request.form.get("capacity"), "Capacity")
            if not reservation_repository().update_slot(
                slot_id, start_at=start_at, capacity=capacity
            ):
                abort(404)
        except ValueError as error:
            flash(str(error), "error")
        else:
            flash(f"Slot #{slot_id} updated.", "success")
        return redirect(url_for("slot_list", date=request.form.get("date", ""), show="all"))

    @app.post("/slot-blocks")
    @login_required
    def slot_block_create():
        selected_date = request.form.get("date", "")
        try:
            start_at = parse_local_start(selected_date, request.form.get("start_time"))
            end_at = parse_local_start(selected_date, request.form.get("end_time"))
            validate_block_period(start_at, end_at)
            block = reservation_repository().block_slots(
                start_at,
                end_at,
                reason=request.form.get("reason", ""),
                blocked_by=str(session["admin_name"]),
            )
        except ValueError as error:
            flash(str(error), "error")
        else:
            message = f"Blocked {block.affected_slot_count} slot(s)."
            if block.confirmed_reservation_count:
                message += (
                    f" Warning: {block.confirmed_reservation_count} existing confirmed "
                    "reservation(s) are within this period and were not cancelled."
                )
            flash(message, "success")
        return redirect(url_for("slot_list", date=selected_date, show="all"))

    @app.post("/slot-blocks/<int:block_id>/release")
    @login_required
    def slot_block_release(block_id: int):
        if reservation_repository().release_slot_block(
            block_id, released_by=str(session["admin_name"])
        ):
            flash("Slot block released. Eligible slots are available to the Telegram bot again.", "success")
        else:
            flash("That block was already released or no longer exists.", "error")
        return redirect(url_for("slot_list", show="all"))

    @app.get("/customers")
    @login_required
    def customer_list():
        query = request.args.get("q", "").strip().casefold()
        customers = reservation_repository().list_past_customers()
        if query:
            customers = [
                customer
                for customer in customers
                if query
                in " ".join(
                    (
                        customer.customer_name,
                        customer.email or "",
                        customer.phone,
                        str(customer.telegram_user_id),
                    )
                ).casefold()
            ]
        return render_template(
            "customers.html", customers=customers, query=request.args.get("q", "").strip()
        )

    @app.get("/faqs")
    @login_required
    def faq_list():
        return render_template(
            "faqs.html", faqs=faq_repository().list_all(include_inactive=True)
        )

    @app.route("/faqs/new", methods=["GET", "POST"])
    @login_required
    def faq_new():
        if request.method == "POST":
            try:
                faq_repository().add(
                    request.form.get("question", ""),
                    request.form.get("answer", ""),
                    request.form.get("category", "General"),
                    parse_integer(request.form.get("sort_order"), "Sort order", default=0),
                )
            except ValueError as error:
                flash(str(error), "error")
            else:
                flash("FAQ created.", "success")
                return redirect(url_for("faq_list"))
        return render_template("faq_form.html", faq=None)

    @app.route("/faqs/<int:faq_id>/edit", methods=["GET", "POST"])
    @login_required
    def faq_edit(faq_id: int):
        faq = faq_repository().get(faq_id, include_inactive=True)
        if faq is None:
            abort(404)
        if request.method == "POST":
            try:
                faq_repository().update(
                    faq_id,
                    question=request.form.get("question", ""),
                    answer=request.form.get("answer", ""),
                    category=request.form.get("category", "General"),
                    sort_order=parse_integer(
                        request.form.get("sort_order"), "Sort order", default=0
                    ),
                )
            except ValueError as error:
                flash(str(error), "error")
            else:
                flash("FAQ updated.", "success")
                return redirect(url_for("faq_list"))
        return render_template("faq_form.html", faq=faq)

    @app.post("/faqs/<int:faq_id>/toggle")
    @login_required
    def faq_toggle(faq_id: int):
        faq = faq_repository().get(faq_id, include_inactive=True)
        if faq is None:
            abort(404)
        faq_repository().set_active(faq_id, not faq.is_active)
        flash(f"FAQ {'enabled' if not faq.is_active else 'disabled'}.", "success")
        return redirect(url_for("faq_list"))

    @app.post("/faqs/<int:faq_id>/delete")
    @login_required
    def faq_delete(faq_id: int):
        if not faq_repository().delete(faq_id):
            abort(404)
        flash("FAQ permanently deleted.", "success")
        return redirect(url_for("faq_list"))

    @app.get("/exports/reservations.csv")
    @login_required
    def reservation_export():
        date_from = parse_optional_date(request.args.get("from"), "from")
        date_to = parse_optional_date(request.args.get("to"), "to")
        if date_from and date_to and date_from > date_to:
            abort(400, "The start date must not be after the end date.")
        status = request.args.get("status", "all")
        if status not in {"confirmed", "cancelled", "all"}:
            abort(400, "Unknown reservation status.")
        items = reservation_repository().list_reservations(include_cancelled=True)
        if status != "all":
            items = [item for item in items if item.status == status]
        if date_from:
            items = [
                item
                for item in items
                if item.start_at.astimezone(shop_timezone()).date() >= date_from
            ]
        if date_to:
            items = [
                item
                for item in items
                if item.start_at.astimezone(shop_timezone()).date() <= date_to
            ]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "Booking ID",
                "Slot number",
                "Date",
                "Time",
                "Name",
                "Phone",
                "Email",
                "Pax",
                "Telegram user ID",
                "Telegram username",
                "Status",
                "Created at (UTC)",
            ]
        )
        for item in items:
            local_start = item.start_at.astimezone(shop_timezone())
            writer.writerow(
                [
                    booking_id(item.start_at, shop_timezone()),
                    slot_label(item.start_at, shop_timezone()),
                    local_start.date().isoformat(),
                    local_start.strftime("%H:%M"),
                    csv_safe(item.customer_name),
                    csv_safe(item.phone),
                    csv_safe(item.email or ""),
                    item.number_of_pax,
                    item.telegram_user_id,
                    csv_safe(item.telegram_username or ""),
                    item.status,
                    item.created_at.isoformat(),
                ]
            )
        filename = f"photoles-reservations-{datetime.now(shop_timezone()):%Y%m%d}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


def parse_admin_accounts(configured: object) -> dict[str, dict[str, str]]:
    """Return configured administrator accounts keyed by normalized username."""
    if isinstance(configured, str):
        try:
            configured = json.loads(configured)
        except json.JSONDecodeError as error:
            raise RuntimeError("ADMIN_ACCOUNTS must be valid JSON.") from error
    if not isinstance(configured, dict) or not configured:
        raise RuntimeError(
            "ADMIN_ACCOUNTS must define at least one username with its own password."
        )

    accounts: dict[str, dict[str, str]] = {}
    for raw_username, raw_credentials in configured.items():
        username = str(raw_username).strip()
        if not 2 <= len(username) <= 80:
            raise RuntimeError("Each ADMIN_ACCOUNTS username must be 2 to 80 characters.")
        if not isinstance(raw_credentials, dict):
            raise RuntimeError(
                f"ADMIN_ACCOUNTS entry for {username!r} must contain password or password_hash."
            )
        password = raw_credentials.get("password")
        password_hash = raw_credentials.get("password_hash")
        if bool(password) == bool(password_hash):
            raise RuntimeError(
                f"ADMIN_ACCOUNTS entry for {username!r} must contain exactly one of "
                "password or password_hash."
            )
        normalized = username.casefold()
        if normalized in accounts:
            raise RuntimeError("ADMIN_ACCOUNTS usernames must be unique ignoring case.")
        accounts[normalized] = {
            "username": username,
            "password": str(password or ""),
            "password_hash": str(password_hash or ""),
        }
    return accounts


def account_password_matches(account: dict[str, str], supplied_password: str) -> bool:
    if account["password_hash"]:
        return check_password_hash(account["password_hash"], supplied_password)
    return hmac.compare_digest(account["password"], supplied_password)


def parse_optional_date(value: str | None, field_name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        abort(400, f"Invalid {field_name} date.")


def parse_local_start(date_value: str | None, time_value: str | None) -> datetime:
    if not date_value or not time_value:
        raise ValueError("Date and time are required.")
    try:
        return datetime.combine(
            date.fromisoformat(date_value), time.fromisoformat(time_value), tzinfo=shop_timezone()
        )
    except ValueError as error:
        raise ValueError("Enter a valid date and time.") from error


def validate_operating_hours(start_at: datetime) -> None:
    if not OPENING_TIME <= start_at.timetz().replace(tzinfo=None) < CLOSING_TIME:
        raise ValueError("Slots must start within operating hours, from 11:00 to 20:40.")
    if start_at.minute not in {0, 20, 40} or start_at.second or start_at.microsecond:
        raise ValueError("Slot start times must follow the 20-minute schedule.")


def validate_block_period(start_at: datetime, end_at: datetime) -> None:
    if end_at <= start_at:
        raise ValueError("Block end time must be later than its start time.")
    start_clock = start_at.timetz().replace(tzinfo=None)
    end_clock = end_at.timetz().replace(tzinfo=None)
    if start_clock < OPENING_TIME or end_clock > CLOSING_TIME:
        raise ValueError("Blocks must fall within operating hours, from 11:00 to 21:00.")
    for value in (start_at, end_at):
        if value.minute not in {0, 20, 40} or value.second or value.microsecond:
            raise ValueError("Block times must follow the 20-minute schedule.")


def parse_positive_int(value: str | None, label: str) -> int:
    parsed = parse_integer(value, label)
    if parsed < 1:
        raise ValueError(f"{label} must be at least 1.")
    return parsed


def parse_integer(value: str | None, label: str, *, default: int | None = None) -> int:
    if (value is None or value == "") and default is not None:
        return default
    try:
        return int(str(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a whole number.") from error


def reservation_matches(reservation: Reservation, query: str) -> bool:
    haystack = " ".join(
        (
            booking_id(reservation.start_at, shop_timezone()),
            slot_label(reservation.start_at, shop_timezone()),
            reservation.customer_name,
            reservation.email or "",
            reservation.phone,
            str(reservation.telegram_user_id),
            reservation.telegram_username or "",
        )
    ).casefold()
    return query in haystack


def csv_safe(value: str) -> str:
    """Prevent spreadsheet programs from interpreting exported customer text as formulas."""
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def main() -> None:
    app = create_app()
    app.run(
        host=os.environ.get("ADMIN_HOST", "127.0.0.1"),
        port=int(os.environ.get("ADMIN_PORT", "8000")),
        debug=False,
    )


if __name__ == "__main__":
    main()
