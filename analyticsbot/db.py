import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            initial_since REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            post_key TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            grouped_id TEXT,
            message_ids TEXT NOT NULL,
            published_at REAL NOT NULL,
            due_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            next_attempt REAL NOT NULL DEFAULT 0,
            snapshot TEXT,
            last_error TEXT,
            sheet_row INTEGER
        );
        CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, next_attempt, due_at);
    """)
    return db


def channel_state(db, channel_id, since):
    with db:
        db.execute("INSERT OR IGNORE INTO channels(channel_id, initial_since) VALUES (?, ?)", (channel_id, since))
    return db.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()


def remember(db, channel_id, message, advance_cursor=True):
    with db:
        if getattr(message, "action", None) is None and getattr(message, "post", False):
            grouped_id = str(message.grouped_id) if message.grouped_id is not None else None
            key = f"{channel_id}:g:{grouped_id}" if grouped_id else f"{channel_id}:m:{message.id}"
            published = message.date.timestamp()
            old = db.execute("SELECT * FROM jobs WHERE post_key = ?", (key,)).fetchone()
            if old is None:
                db.execute(
                    "INSERT INTO jobs(post_key, channel_id, post_id, grouped_id, message_ids, published_at, due_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, channel_id, message.id, grouped_id, json.dumps([message.id]), published, published + 86400),
                )
            elif old["status"] == "pending" and old["snapshot"] is None:
                ids = sorted(set(json.loads(old["message_ids"])) | {message.id})
                published = min(old["published_at"], published)
                db.execute(
                    "UPDATE jobs SET post_id = ?, message_ids = ?, published_at = ?, due_at = ? WHERE post_key = ?",
                    (ids[0], json.dumps(ids), published, published + 86400, key),
                )
        if advance_cursor:
            db.execute("UPDATE channels SET last_message_id = MAX(last_message_id, ?) WHERE channel_id = ?", (message.id, channel_id))


def due_jobs(db, now, selected_keys=None):
    jobs = db.execute(
        "SELECT * FROM jobs WHERE status = 'pending' AND published_at + 86400 <= ? AND due_at <= ? AND next_attempt <= ? ORDER BY due_at",
        (now, now, now),
    ).fetchall()
    return [job for job in jobs if job["post_key"] in selected_keys] if selected_keys is not None else jobs


def save_snapshot(db, key, snapshot):
    with db:
        db.execute("UPDATE jobs SET snapshot = ? WHERE post_key = ? AND snapshot IS NULL", (json.dumps(snapshot, ensure_ascii=False), key))


def finish(db, key, status, row=None):
    with db:
        db.execute("UPDATE jobs SET status = ?, sheet_row = ?, last_error = NULL WHERE post_key = ?", (status, row, key))


def retry(db, key, when, error):
    with db:
        db.execute("UPDATE jobs SET next_attempt = ?, last_error = ? WHERE post_key = ?", (when, str(error), key))


def update_pending_layout(db, targets):
    by_sheet = {(target["spreadsheet_id"], target["worksheet"]): target for target in targets}
    with db:
        rows = db.execute("SELECT post_key, snapshot, sheet_row FROM jobs WHERE status = 'pending' AND snapshot IS NOT NULL").fetchall()
        for row in rows:
            snapshot = json.loads(row["snapshot"])
            target = snapshot["target"]
            current = by_sheet.get((target["spreadsheet_id"], target["worksheet"]))
            if current is None or current == target:
                continue
            if row["sheet_row"] is not None:
                raise RuntimeError(f"Cannot change layout for reserved row {row['sheet_row']}; check the sheet before retrying")
            snapshot["target"] = current
            db.execute("UPDATE jobs SET snapshot = ? WHERE post_key = ?", (json.dumps(snapshot, ensure_ascii=False), row["post_key"]))


@contextmanager
def single_instance(database):
    path = Path(str(Path(database).resolve()) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            lock = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise RuntimeError("Another service instance is already using this database") from exc
        try:
            yield
        finally:
            unlock()
