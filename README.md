# Ski Valet: pickup queue

The counter enters the tag numbers guests bring. Staff see them as a queue on the back-room screen, find the
equipment and, once it is delivered, clear the numbers from the queue.

## Screens

- `/entry` **Counter:** numeric keypad. One tag: number → *Send*. Several tags for the same guest: number →
  *Add* → number → … → *Send*; they arrive together on one card.
- `/staff` **Back room:** cards in order of arrival, with large numbers. *Delivered* clears the card. If a card
  has several tags, tapping a number clears only that one. The card border turns orange after 5 minutes and red
  after 10 (`WARN_MIN` and `LATE_MIN` in both pages).

Both screens refresh every 2 seconds. If the connection is lost, a red warning appears.

## Rules that prevent confusion

- A tag cannot be in the queue twice: the counter warns about it and does not send it.
- Clearing a tag shows *Undo* for 12 seconds, in case of an accidental tap. The tag returns to its place in the queue.
- The queue empties itself at midnight (`HOTEL_TZ`).
- Tags are numbers only, up to 6 digits (`TAG_RE` in `app.py`, `MAX_DIGITS` in `static/entry.html`).

## Code layout

`app.py` is the Flask backend. Each page in `static/` holds its own CSS and JavaScript, so the code shared by
`entry.html` and `staff.html` (API calls, PIN sign-in, polling, Undo) exists in both: change it in both.

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
STAFF_PIN=1234 COOKIE_SECURE=0 .venv/bin/python app.py     # http://localhost:5057
```

## Deploying on Railway

1. Create a service from this repository.
2. Variables: `STAFF_PIN` (required; the PIN each screen asks for once). Optional: `HOTEL_TZ`
   (default `Asia/Tokyo`), `SESSION_DAYS` (30), `SECRET_KEY`.
3. Add a **volume** mounted at `/data` and set `DB_PATH=/data/queue.db`. Without a volume the app still works, but
   every deploy empties the queue and asks for the PIN again.
4. Generate a public domain and open `/entry` and `/staff` on each screen.

Use a single gunicorn worker (`--workers 1`, already configured): the PIN attempt limit lives in memory.
Changing `STAFF_PIN` signs out every screen.
