"""Ski Valet pickup queue: the counter sends tag numbers, the back room sees them and clears them.

The queue only matters for the current day, so it lives in one SQLite file and
is emptied at midnight (hotel time). See README.md for setup and deployment.
"""
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

STAFF_PIN = os.environ.get("STAFF_PIN")
if not STAFF_PIN:
    raise RuntimeError("STAFF_PIN is not set (the PIN every counter and back-room screen signs in with)")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "queue.db")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"      # set 0 only for local http
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
HOTEL_TZ = os.environ.get("HOTEL_TZ", "Asia/Tokyo")              # the queue empties at midnight here

TAG_RE = re.compile(r"^\d{1,6}$")
GROUP_RE = re.compile(r"^[0-9a-f]{8,32}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_TAGS_PER_SEND = 20
MAX_IDS_PER_CALL = 50
REMOVED_KEEP_SECONDS = 3600      # removed tags stay this long so "Undo" can bring them back
PURGE_EVERY_SECONDS = 60
LOGIN_MAX_FAILS = 10             # failed PINs per IP, per window
LOGIN_WINDOW_SECONDS = 600


# --- Database ----------------------------------------------------------------

@contextmanager
def tx():
    """One write transaction on its own connection; requests run in several threads."""
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def init_db():
    """Create the tables and return the session key (generated once, then kept in the database)."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    with tx() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS pickup_tags (
            id TEXT PRIMARY KEY,
            tag TEXT NOT NULL,
            group_id TEXT NOT NULL,
            pos INTEGER NOT NULL,
            created_at REAL NOT NULL,
            removed_at REAL)""")
        # A tag can only wait in the queue once; this also holds when two screens send it together.
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS pickup_tags_active ON pickup_tags(tag) WHERE removed_at IS NULL")
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('secret_key', ?)", (secrets.token_hex(32),))
        return db.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()["value"]


_last_purge = 0.0


def purge_old():
    """Each day starts empty, and removed tags are dropped once Undo no longer makes sense."""
    global _last_purge
    now = time.time()
    if now - _last_purge < PURGE_EVERY_SECONDS:
        return
    _last_purge = now
    day_start = dt.datetime.now(ZoneInfo(HOTEL_TZ)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with tx() as db:
        db.execute("DELETE FROM pickup_tags WHERE created_at < ? OR removed_at < ?",
                   (day_start, now - REMOVED_KEEP_SECONDS))


# --- App and sign-in ---------------------------------------------------------

app = Flask(__name__, static_folder="static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or init_db(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=SESSION_DAYS),
    SEND_FILE_MAX_AGE_DEFAULT=0,
)
init_db()

# Changing STAFF_PIN signs every screen out.
PIN_FINGERPRINT = hashlib.sha256(STAFF_PIN.encode()).hexdigest()[:16]

_login_fails = {}
_login_lock = threading.Lock()


def login_blocked(ip):
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    with _login_lock:
        recent = [t for t in _login_fails.get(ip, []) if t > cutoff]
        _login_fails[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILS


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("pin") != PIN_FINGERPRINT:
            return jsonify(error="Sign in required"), 401
        return view(*args, **kwargs)
    return wrapped


@app.after_request
def no_store_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/login")
def login():
    ip = request.remote_addr or "?"
    if login_blocked(ip):
        return jsonify(error="Too many attempts. Wait 10 minutes."), 429
    pin = (request.get_json(silent=True) or {}).get("pin")
    if not isinstance(pin, str) or not hmac.compare_digest(pin.encode(), STAFF_PIN.encode()):
        with _login_lock:
            _login_fails.setdefault(ip, []).append(time.time())
        return jsonify(error="Wrong PIN"), 403
    session.permanent = True
    session["pin"] = PIN_FINGERPRINT
    return jsonify(ok=True)


# --- Pages -------------------------------------------------------------------

@app.get("/")
def home_page():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/entry")
def entry_page():
    return send_from_directory(app.static_folder, "entry.html")


@app.get("/staff")
def staff_page():
    return send_from_directory(app.static_folder, "staff.html")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


# --- Queue API ---------------------------------------------------------------

def read_ids():
    """The tag ids of a remove/restore call, or None if the body is not valid."""
    ids = (request.get_json(silent=True) or {}).get("ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS_PER_CALL
            or not all(isinstance(i, str) and ID_RE.match(i) for i in ids)):
        return None
    return ids


@app.get("/api/queue")
@require_login
def get_queue():
    purge_old()
    with tx() as db:
        rows = db.execute("""SELECT id, tag, group_id, created_at FROM pickup_tags
                             WHERE removed_at IS NULL ORDER BY created_at, group_id, pos""").fetchall()
    return jsonify(now=time.time(), items=[dict(r) for r in rows])


@app.post("/api/queue")
@require_login
def add_tags():
    body = request.get_json(silent=True) or {}
    group_id, tags = body.get("group_id"), body.get("tags")
    if not isinstance(group_id, str) or not GROUP_RE.match(group_id):
        return jsonify(error="Invalid request"), 400
    if (not isinstance(tags, list) or not 1 <= len(tags) <= MAX_TAGS_PER_SEND or len(set(map(str, tags))) != len(tags)
            or not all(isinstance(t, str) and TAG_RE.match(t) for t in tags)):
        return jsonify(error=f"Send 1 to {MAX_TAGS_PER_SEND} different tag numbers"), 400

    now = time.time()
    with tx() as db:
        # A retry of a send whose answer got lost: it is already in, so nothing to do.
        if db.execute("SELECT 1 FROM pickup_tags WHERE group_id = ? LIMIT 1", (group_id,)).fetchone():
            return jsonify(ok=True)
        marks = ",".join("?" * len(tags))
        taken = db.execute(f"SELECT tag FROM pickup_tags WHERE removed_at IS NULL AND tag IN ({marks})", tags).fetchall()
        if taken:
            return jsonify(error="Already in the queue", duplicates=[r["tag"] for r in taken]), 409
        db.executemany("INSERT INTO pickup_tags (id, tag, group_id, pos, created_at) VALUES (?, ?, ?, ?, ?)",
                       [(secrets.token_hex(8), tag, group_id, pos, now) for pos, tag in enumerate(tags)])
    return jsonify(ok=True), 201


@app.post("/api/queue/remove")
@require_login
def remove_tags():
    ids = read_ids()
    if ids is None:
        return jsonify(error="Invalid request"), 400
    marks = ",".join("?" * len(ids))
    with tx() as db:
        db.execute(f"UPDATE pickup_tags SET removed_at = ? WHERE removed_at IS NULL AND id IN ({marks})",
                   [time.time(), *ids])
    return jsonify(ok=True)


@app.post("/api/queue/restore")
@require_login
def restore_tags():
    ids = read_ids()
    if ids is None:
        return jsonify(error="Invalid request"), 400
    skipped = []
    with tx() as db:
        for tag_id in ids:
            row = db.execute("SELECT tag FROM pickup_tags WHERE id = ?", (tag_id,)).fetchone()
            try:
                db.execute("UPDATE pickup_tags SET removed_at = NULL WHERE id = ?", (tag_id,))
            except sqlite3.IntegrityError:      # the same tag was sent again in the meantime
                skipped.append(row["tag"])
    return jsonify(ok=True, skipped=skipped)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5057")), threaded=True)
