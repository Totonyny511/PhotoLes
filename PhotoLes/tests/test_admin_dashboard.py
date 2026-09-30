import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from admin_dashboard import create_app
from booking_identifiers import booking_id, slot_label
from settings import shop_timezone


class AdminDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "dashboard.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key",
                "ADMIN_ACCOUNTS": {
                    "Morgan Lee": {"password": "correct horse battery staple"},
                    "Taylor Lim": {"password": "taylor's distinct password"},
                },
                "DATABASE_PATH": str(self.database),
            }
        )
        self.client = self.app.test_client()
        self.repository = self.app.extensions["reservation_repository"]
        self.faq_repository = self.app.extensions["faq_repository"]
        self.now = datetime.now(timezone.utc)
        self.slot_id = self.repository.add_slot(self.now + timedelta(days=2))
        self.reservation = self.repository.create_reservation(
            slot_id=self.slot_id,
            telegram_user_id=123456,
            telegram_chat_id=123456,
            customer_name="Jamie Tan",
            email="jamie@example.com",
            phone="+65 8123 4567",
            number_of_pax=3,
            telegram_username="jamie",
            now=self.now,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def csrf_token(self) -> str:
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def login(self):
        self.client.get("/login")
        return self.client.post(
            "/login",
            data={
                "username": "Morgan Lee",
                "password": "correct horse battery staple",
                "csrf_token": self.csrf_token(),
            },
        )

    def test_dashboard_requires_login(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_health_check_does_not_require_login(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_login_and_dashboard_summary(self) -> None:
        response = self.login()
        self.assertEqual(response.status_code, 302)

        dashboard = self.client.get("/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Upcoming bookings", dashboard.data)
        self.assertIn(b"Jamie Tan", dashboard.data)
        self.assertIn(b"Upcoming pax", dashboard.data)

    def test_incorrect_password_is_rejected(self) -> None:
        self.client.get("/login")

        response = self.client.post(
            "/login",
            data={
                "username": "Morgan Lee",
                "password": "wrong",
                "csrf_token": self.csrf_token(),
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Incorrect username or password", response.data)

    def test_unregistered_username_is_rejected_with_a_valid_password(self) -> None:
        self.client.get("/login")

        response = self.client.post(
            "/login",
            data={
                "username": "Unregistered Person",
                "password": "correct horse battery staple",
                "csrf_token": self.csrf_token(),
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Incorrect username or password", response.data)

    def test_password_from_another_account_is_rejected(self) -> None:
        self.client.get("/login")

        response = self.client.post(
            "/login",
            data={
                "username": "Taylor Lim",
                "password": "correct horse battery staple",
                "csrf_token": self.csrf_token(),
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_post_without_csrf_token_is_rejected(self) -> None:
        self.login()

        response = self.client.post("/logout")

        self.assertEqual(response.status_code, 400)

    def test_admin_can_cancel_reservation_and_queue_notification(self) -> None:
        self.login()

        response = self.client.post(
            f"/reservations/{self.reservation.id}/cancel",
            data={"csrf_token": self.csrf_token()},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"customer notification was queued", response.data)
        self.assertEqual(
            self.repository.get_reservation(self.reservation.id).status, "cancelled"
        )
        self.assertEqual(len(self.repository.pending_cancellation_notifications()), 1)

    def test_admin_can_create_operating_hours_slot(self) -> None:
        self.login()
        selected_date = (datetime.now().date() + timedelta(days=5)).isoformat()

        response = self.client.post(
            "/slots",
            data={
                "csrf_token": self.csrf_token(),
                "date": selected_date,
                "time": "14:20",
                "capacity": "2",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Time slot created", response.data)
        matching = [
            slot
            for slot in self.repository.list_slots()
            if slot.start_at.astimezone(shop_timezone()).date().isoformat() == selected_date
        ]
        self.assertTrue(matching)
        self.assertEqual(matching[0].capacity, 2)

    def test_admin_can_block_period_with_audited_identity(self) -> None:
        self.login()
        local_start = (datetime.now(shop_timezone()) + timedelta(days=5)).replace(
            hour=14, minute=20, second=0, microsecond=0
        )
        slot_id = self.repository.add_slot(local_start)

        response = self.client.post(
            "/slot-blocks",
            data={
                "csrf_token": self.csrf_token(),
                "date": local_start.date().isoformat(),
                "start_time": local_start.strftime("%H:%M"),
                "end_time": (local_start + timedelta(minutes=20)).strftime("%H:%M"),
                "reason": "Private event",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Blocked 1 slot", response.data)
        self.assertFalse(self.repository.get_slot(slot_id).is_active)
        block = self.repository.list_slot_blocks()[0]
        self.assertEqual(block.reason, "Private event")
        self.assertEqual(block.blocked_by, "Morgan Lee")
        self.assertIsNotNone(block.blocked_at)

    def test_admin_can_release_block(self) -> None:
        local_start = self.repository.get_slot(self.slot_id).start_at
        block = self.repository.block_slots(
            local_start,
            local_start + timedelta(minutes=20),
            reason="Public holiday",
            blocked_by="Morgan Lee",
        )
        self.login()

        response = self.client.post(
            f"/slot-blocks/{block.id}/release",
            data={"csrf_token": self.csrf_token()},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.repository.get_slot(self.slot_id).is_active)
        released = self.repository.list_slot_blocks()[0]
        self.assertEqual(released.released_by, "Morgan Lee")
        self.assertIsNotNone(released.released_at)

    def test_admin_can_create_and_hide_faq(self) -> None:
        self.login()

        created = self.client.post(
            "/faqs/new",
            data={
                "csrf_token": self.csrf_token(),
                "question": "Can I bring a guest?",
                "answer": "Yes, within the booked pax count.",
                "category": "Bookings",
                "sort_order": "5",
            },
            follow_redirects=True,
        )

        self.assertEqual(created.status_code, 200)
        self.assertIn(b"Can I bring a guest?", created.data)
        faq = self.faq_repository.list_all()[0]
        hidden = self.client.post(
            f"/faqs/{faq.id}/toggle",
            data={"csrf_token": self.csrf_token()},
            follow_redirects=True,
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertFalse(self.faq_repository.get(faq.id, include_inactive=True).is_active)

    def test_csv_export_contains_booking_fields(self) -> None:
        self.login()

        response = self.client.get("/exports/reservations.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(b"Booking ID,Slot number", response.data)
        self.assertIn(
            booking_id(self.reservation.start_at, shop_timezone()).encode(),
            response.data,
        )
        self.assertIn(
            slot_label(self.reservation.start_at, shop_timezone()).encode(),
            response.data,
        )
        self.assertIn(b"Telegram user ID", response.data)
        self.assertIn(b"Jamie Tan", response.data)
        self.assertIn(b",3,123456,", response.data)


if __name__ == "__main__":
    unittest.main()
