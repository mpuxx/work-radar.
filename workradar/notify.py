"""In-app alerts. Email is optional and only goes to users marked is_test unless WORKRADAR_EMAIL_LIVE=1.

* Baseline: a new or edited search alerts only on jobs first detected after baseline_at.
* Idempotent: notification_id = sha(search_id, dup_group or job_id). The same job seen on three
  sources, or in ten runs, alerts once.
* Strict: only exact matches alert. Matches with unknowns are shown in the app, not pushed.
* Quiet hours and pause: held, then released after quiet hours end. Paused users get nothing new.
* Delivery: immediate = this run; daily = 07:00 local; weekly = Monday 07:00 local.
"""
import datetime as dt
import os
import smtplib
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import db as D, matching

MAX_ATTEMPTS = 5


def in_quiet(user, at):
    tz = ZoneInfo(user["timezone"] or "Europe/London")
    t = at.astimezone(tz).strftime("%H:%M")
    s, e = user["quiet_start"] or "21:00", user["quiet_end"] or "07:00"
    return (s <= t or t < e) if s > e else (s <= t < e)


def digest_due(search, user, at, last):
    tz = ZoneInfo(user["timezone"] or "Europe/London")
    local = at.astimezone(tz)
    if search["delivery"] == "immediate":
        return True
    if local.hour < 7:
        return False
    if search["delivery"] == "weekly" and local.weekday() != 0:
        return False
    if last is None:
        return True
    return D.parse(last).astimezone(tz).date() < local.date()


def create(db, at=None):
    at = at or D.now()
    made = 0
    q = """SELECT s.*, u.paused AS user_paused FROM searches s JOIN users u USING(user_id) WHERE s.paused=0 AND u.paused=0"""
    for s in db.execute(q).fetchall():
        rules = __import__("json").loads(s["rules"])
        base = s["baseline_at"] or s["created_at"]
        for j in db.execute("SELECT * FROM jobs WHERE listing_status='listed' AND first_detected_at > ? AND repost_of IS NULL", (base,)):
            verdict, reasons, _ = matching.evaluate(j, rules, at)
            if verdict != "match":
                continue
            nid = D.sha(s["search_id"], j["dup_group"] or j["job_id"])
            cur = db.execute("INSERT OR IGNORE INTO notifications(notification_id,user_id,search_id,job_id,created_at,reasons) VALUES(?,?,?,?,?,?)",
                             (nid, s["user_id"], s["search_id"], j["job_id"], D.iso(at), D.jdump(reasons)))
            made += cur.rowcount
    return made


def _send_email(user, items):
    host = os.environ.get("SMTP_HOST")
    if not host or not user["email"]:
        raise RuntimeError("email not configured")
    if not user["is_test"] and os.environ.get("WORKRADAR_EMAIL_LIVE") != "1":
        raise RuntimeError("live email disabled: only test users receive email")
    m = EmailMessage()
    m["From"], m["To"] = os.environ.get("SMTP_FROM", "alerts@example.invalid"), user["email"]
    m["Subject"] = f"{len(items)} new role{'s' if len(items) != 1 else ''} that match your search"
    m.set_content("\n\n".join(f"{i['title']}\n{i['apply_url']}" for i in items))
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as c:
        c.starttls()
        c.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        c.send_message(m)


def deliver(db, at=None, email=None):
    """In-app delivery = making the notification visible in the exported feed. Marked delivered with a time."""
    at = at or D.now()
    sent = held = 0
    for u in db.execute("SELECT * FROM users").fetchall():
        pend = db.execute("""SELECT n.*, s.delivery, s.search_id AS sid, j.title, j.apply_url FROM notifications n
                             JOIN searches s ON s.search_id=n.search_id JOIN jobs j ON j.job_id=n.job_id
                             WHERE n.user_id=? AND n.status IN ('pending','held_quiet_hours','failed') AND n.attempts < ?""",
                          (u["user_id"], MAX_ATTEMPTS)).fetchall()
        if not pend:
            continue
        if u["paused"]:
            continue
        if in_quiet(u, at):
            for n in pend:
                if n["status"] == "pending":
                    db.execute("UPDATE notifications SET status='held_quiet_hours' WHERE notification_id=?", (n["notification_id"],))
                    held += 1
            continue
        by_search = {}
        for n in pend:
            by_search.setdefault(n["sid"], []).append(n)
        for sid, items in by_search.items():
            s = db.execute("SELECT * FROM searches WHERE search_id=?", (sid,)).fetchone()
            last = db.execute("SELECT MAX(delivered_at) FROM notifications WHERE search_id=? AND status IN ('delivered','read')", (sid,)).fetchone()[0]
            if not digest_due(s, u, at, last):
                continue
            err = None
            if email or (email is None and os.environ.get("SMTP_HOST")):
                try:
                    _send_email(u, items)
                except Exception as e:
                    err = str(e)[:200]
            for n in items:
                if err and "not configured" not in err and "disabled" not in err:
                    db.execute("UPDATE notifications SET status='failed', attempts=attempts+1, last_error=? WHERE notification_id=?",
                               (err, n["notification_id"]))
                else:
                    db.execute("UPDATE notifications SET status='delivered', attempts=attempts+1, delivered_at=?, channel=?, last_error=? WHERE notification_id=?",
                               (D.iso(at), "in_app", err, n["notification_id"]))
                    sent += 1
    return dict(delivered=sent, held=held)


def set_baseline(db, search_id, at=None):
    db.execute("UPDATE searches SET baseline_at=? WHERE search_id=?", (D.iso(at or D.now()), search_id))
