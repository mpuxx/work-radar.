"""SQLite storage, migrations and small helpers. All state lives in one file (WORKRADAR_DB)."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "workradar.sqlite3"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat() if isinstance(t, dt.datetime) else t


def parse(t):
    if not t:
        return None
    if isinstance(t, dt.datetime):
        return t
    x = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)


def sha(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def connect(path=None):
    path = Path(path or os.environ.get("WORKRADAR_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; use `with tx(db)` for transactions
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    migrate(db)
    return db


class tx:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on error. Keeps each source's import atomic."""
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, et, ev, tb):
        self.db.execute("ROLLBACK" if et else "COMMIT")
        return False


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    done = {r[0] for r in db.execute("SELECT name FROM schema_migrations")}
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        if f.name in done:
            continue
        db.executescript(f.read_text())
        db.execute("INSERT INTO schema_migrations VALUES (?,?)", (f.name, iso(now())))


def jdump(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)


def add_usage(db, bucket, amount=1, day=None):
    day = day or now().date().isoformat()
    db.execute("INSERT INTO usage(day,bucket,amount) VALUES(?,?,?) ON CONFLICT(day,bucket) DO UPDATE SET amount=amount+excluded.amount",
               (day, bucket, amount))


def usage_today(db, bucket):
    r = db.execute("SELECT amount FROM usage WHERE day=? AND bucket=?", (now().date().isoformat(), bucket)).fetchone()
    return r[0] if r else 0
