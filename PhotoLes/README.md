# Photobooth Telegram Bot

PhotoLes lets customers browse FAQs, reserve live available time slots, review or
change their upcoming reservations, and receive an automatic reminder one day before
their visit.
Questions, slots, and reservations are stored in SQLite so the shop owner can manage
them without editing the bot's source code.

## 1. Create the Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, then follow its instructions.
3. Keep the token private. Anyone with this token can control the bot.

## 2. Install and configure

From this project folder, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and replace `replace-with-your-bot-token` with the BotFather token.
The default shop timezone is `Asia/Singapore`. To use another timezone, change
`SHOP_TIMEZONE` in `.env` to a valid timezone name before creating slots.

## 3. Set up your personal Telegram as online customer service

This feature uses the bot as a private relay. Customers do not message your personal
account directly, so your phone number stays private. Their questions appear in your
own private chat with the PhotoLes bot, and your replies are sent back by the bot.

1. Start the bot once with `python bot.py`.
2. In your **own personal Telegram account**, open the bot and press **Start**. Telegram
   does not allow a bot to initiate a private conversation before you do this.
3. Send `/adminid` to the bot. It will reply with the numeric ID of that private chat.
4. Open `.env` and set the value it gave you:

   ```text
   CUSTOMER_SERVICE_CHAT_ID=123456789
   ```

5. Stop and restart `python bot.py` so the new setting is loaded.

Customers can now tap **Online Customer Service** in the main menu or send `/support`.
When a question arrives in your private Telegram chat, use Telegram's **Reply** action
on that exact ticket message and type your answer. Do not send the answer as an
unattached new message—the reply is how the bot knows which customer should receive it.

To put a direct customer-service link on a website, social profile, or QR code, replace
`YOUR_BOT_USERNAME` with the username created in BotFather (without the `@`):

```text
https://t.me/YOUR_BOT_USERNAME?start=support
```

Telegram shows a **Start** button when a customer opens a bot deep link. After they tap
it, PhotoLes immediately opens the customer-service question prompt. The customer's
question is sent to you only after they type it and press Send.

Keep `CUSTOMER_SERVICE_CHAT_ID` private. Anyone using the Telegram account for that
private chat can answer customer tickets. The bot must be running for new questions and
answers to be relayed.

Customers can change an upcoming booking from **My reservations**. After they choose
an available replacement date and time and confirm it, the reservation keeps its
contact details, receives the booking ID for its new slot, has its reminder rescheduled,
and the original place is immediately returned to availability. If the replacement becomes unavailable before
confirmation, the original reservation is left unchanged.

## 4. Manually input questions and answers in the backend

The easiest option is the guided command:

```bash
python manage_faq.py add
```

It prompts for the question and answer. Repeat it for every Q&A. For example:

```text
Question: How early should I arrive?
Answer: Please arrive 10 minutes before your reserved time.
Added Q&A #1.
```

You can also enter everything in one command:

```bash
python manage_faq.py add \
  --question "How many people fit in the booth?" \
  --answer "Up to six people can use the booth at once." \
  --category "During your visit" \
  --order 2
```

The lower the `--order`, the earlier the question appears. The category is stored for
future grouping and defaults to `General`.

Useful backend commands:

```bash
# See every Q&A and its numeric ID
python manage_faq.py list --all

# Change Q&A number 1 (only supplied fields are changed)
python manage_faq.py update 1 --answer "Please arrive 15 minutes early."

# Temporarily hide or show it
python manage_faq.py disable 1
python manage_faq.py enable 1

# Permanently remove it (asks for confirmation)
python manage_faq.py delete 1
```

Changes appear the next time a customer opens or returns to the FAQ menu; the bot does
not need to be restarted. The data is saved in `data/photobooth.db` by default.

## 5. Customize reservation time slots in the backend

All times entered below use the `SHOP_TIMEZONE` from `.env`.

The bot automatically maintains the standard PhotoLes booking calendar. Every date
from today through 28 days later contains 20-minute appointment starts from **11:00 AM
through 8:40 PM**; 9:00 PM is closing time. A slot remains bookable only until four
hours before its start time. For example, on 4 September the final date
is 2 October, and on 5 September it moves forward to 3 October. Missing slots are
created at midnight and whenever the bot starts. Existing slots are never overwritten,
so their bookings, capacity changes, and disabled status remain intact.

Add one slot with one available place:

```bash
python manage_reservations.py add-slot --start "2026-09-10 11:20"
```

To allow two separate reservations at the same time, set its capacity to 2:

```bash
python manage_reservations.py add-slot --start "2026-09-10 11:40" --capacity 2
```

Generate a full day quickly. This example creates starts at 11:00, 11:20, 11:40,
and so on, with the final start at 20:40:

```bash
python manage_reservations.py generate-slots \
  --date 2026-09-10 \
  --from 11:00 \
  --to 21:00 \
  --every 20 \
  --capacity 1
```

Change a slot using its numeric ID:

```bash
python manage_reservations.py update-slot 1 --start "2026-09-10 11:00"
python manage_reservations.py update-slot 1 --capacity 2
python manage_reservations.py disable-slot 1
python manage_reservations.py enable-slot 1
```

Disabling a slot hides it from customers without erasing its history. A slot with any
reservation history cannot be deleted; disable it instead.

## 6. Check availability and reservations in the backend

Check all upcoming slots:

```bash
python manage_reservations.py list-slots
```

Check one date or include past and disabled slots:

```bash
python manage_reservations.py list-slots --date 2026-09-10
python manage_reservations.py list-slots --all
```

The output shows the daily slot number, internal maintenance ID, booked places, total
capacity, and remaining availability:

```text
[slot 001; internal #1] Thu, 10 Sep 2026 11:00 | booked 1/2 | available 1 | active
```

Check customer reservations:

```bash
python manage_reservations.py list-reservations
python manage_reservations.py list-reservations --date 2026-09-10
```

Cancel the reservation whose internal maintenance ID is `3` and return its place to
availability:

```bash
python manage_reservations.py cancel-reservation 3
```

This uses the same atomic cancellation-and-notification queue as the web dashboard. The
terminal does not send Telegram messages itself; the running PhotoLes customer bot
normally delivers the queued cancellation within 15 seconds and retries temporary
failures automatically.

Availability updates automatically after every confirmation or cancellation. The
database rechecks current slot availability inside a locked transaction when the user
taps **Confirm reservation**, so two customers cannot take the final available place.

Fully booked times remain visible in the customer menu with a **0 available** label.
Tapping one leaves the customer on the same screen and does not start a reservation.
Each day's time slots are split into pages of seven. Customers use **Earlier** and
**Next** to browse the day's remaining times instead of scrolling through one long
column.

## 7. Customer reservation journey

Customers send `/reserve` or tap **Make a reservation**, then:

1. Choose an available date and time.
2. Enter their full name, email address, phone number, and number of pax.
3. Review and confirm the details.
4. Receive a confirmation summary with a booking ID, slot number, date, time, and status.

Slots are numbered from `001` in chronological order from the 11:00 opening time.
Booking IDs use `PL` + the appointment date in `YYMMDD` format + the three-digit slot
number. For example, slot `001` on 16 September 2026 is `PL260916001`.

Customers can use `/myreservations` to see upcoming bookings. Names, email addresses,
phone numbers, number of pax, and Telegram account details are saved locally in
`data/photobooth.db`; protect this file and only give trusted staff access to it.
The saved Telegram account details include the numeric Telegram user ID used to tie
future reservation changes and cancellations to the customer who made the booking.

Each upcoming reservation has a **Cancel** button. The customer must confirm before
anything changes. A successful cancellation stops its reminder and immediately returns
the place to the available capacity for that time slot. Customers can cancel only their
own future reservations.

## 8. Reminder behavior

The bot stores a durable Telegram reminder for 24 hours before each confirmed
reservation. If a customer books less than 24 hours before the appointment, the
reminder is sent shortly after confirmation. Failed deliveries are retried after 30
seconds with increasing delays capped at one hour. The queue is stored in SQLite, so
unsent reminders resume automatically when the bot restarts.

The bot must be running on an internet-connected, always-on computer or server for
reminders to arrive on time. If it was offline at the reminder time, it sends the
overdue reminder when it starts again, provided the appointment has not passed.

At midnight in `SHOP_TIMEZONE`, the bot saves the name, email address, phone number,
visit time, and visit count for each completed, non-cancelled customer. It then deletes
the expired reservation internals and time slots. Cancelled reservations are never
added to the past-customer directory. The same cleanup runs when the bot starts, so
expired data is still handled if the bot was offline at midnight. Slots are only shown
when their start time is still in the future, so customers cannot select a slot from a
previous day—or an earlier time on the current day.

## 9. Run the customer bot

```bash
source .venv/bin/activate
python bot.py
```

Keep that terminal running. In Telegram, open the new bot and send `/start`. Telegram's
command menu shows only `/start`; use the buttons in the main menu for reservations,
FAQs, and online customer service. The direct commands remain available as hidden
fallbacks if they are typed manually.
For a real shop, run the process on an always-on server rather than a personal laptop.

## 10. Run the admin dashboard

The authenticated web dashboard provides an operations overview, a 14-day booking
calendar, reservation search and cancellation, slot management, past-customer search,
FAQ management, and CSV reservation exports. It uses the same database and repository
logic as the PhotoLes customer bot.

The **Slots** page can block one 20-minute slot, a time period, or a complete operating
day. Each block permanently records the affected date and period, reason, administrator
name, and exact creation time. Active blocks immediately disappear from the customer
bot's availability and are checked again during final reservation confirmation. The
history also records who releases a block and when. If a blocked period already has
confirmed reservations, the dashboard warns the administrator; it does not silently
cancel those customers' bookings.

### Receive new-booking alerts in Telegram

The PhotoLes customer bot can notify one or more administrators after every
successful consumer booking. Each alert shows
the booking ID, slot number, customer name, email, phone, pax, appointment date and time,
Telegram username and numeric user ID, booking timestamp, and current status.

1. Open the regular PhotoLes bot from each administrator's private Telegram account and
   send `/start`, then `/adminid`.
2. Copy the returned chat ID into `.env`:

   ```env
   ADMIN_NOTIFICATION_CHAT_IDS=123456789
   ```

   For multiple administrators, use comma-separated IDs.
3. Restart `bot.py`.

The booking and its pending admin alerts are committed to SQLite in the same
transaction. The bot checks the queue every 15 seconds. Failed Telegram deliveries are
retried with increasing delays, so a temporary connection problem does not lose the
alert. Each administrator must start the regular PhotoLes bot before Telegram permits
it to send messages to their private chat.

Copy the admin settings from `.env.example` into `.env`. Replace
`ADMIN_SECRET_KEY` with a long random value and configure `ADMIN_ACCOUNTS` as a JSON
object. Every authorized username must have its own `password` (local use) or
`password_hash` (recommended for deployment). Usernames not present in this setting are
rejected, even if they submit another administrator's valid password. The registered
username is also used for the slot-blocking and release audit trail. Then run:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python admin_dashboard.py
```

Open `http://127.0.0.1:8000`. The default host keeps the dashboard accessible only on
the same computer. For remote access, deploy it behind HTTPS and a production WSGI
server, set `ADMIN_COOKIE_SECURE=true`, and do not expose Flask's development server
directly to the internet.

## Run both services in production

`main.py` is the production entry point for hosts such as Railway. It starts the admin
dashboard with Gunicorn and starts the Telegram polling bot in the same container so
both processes can safely use the same SQLite volume. If either process exits, the
launcher stops the other and exits so the hosting platform can restart the service.

The dashboard listens on the host's `PORT` variable and exposes an unauthenticated
`/health` endpoint for deployment health checks. To exercise the production setup
locally, run:

```bash
source .venv/bin/activate
pip install -r requirements.txt
PORT=8000 python main.py
```

For Railway, set the service root directory to `/PhotoLes`. Railpack detects `main.py`
as the Python start file, so the resulting start command is `python main.py`. Configure
the health-check path as `/health`, use one replica, and keep Serverless disabled.

## Check the local database code

The repository tests do not need Telegram or an internet connection:

```bash
python -m unittest discover -s tests -v
```
