"""Collector: picks due sources, leases them, fetches politely, imports atomically, tracks changes.

One run = one pass over the due queue. Safe to start every 15 minutes:
  * a lease (sources.lease_until) stops two runs fetching the same source;
  * a crashed run's leases simply expire, so the next run recovers by itself;
  * one source failing never touches another source's jobs.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import socket
import time

from . import connectors, db as D, dedupe, http
from .extract import EXCLUDE_RX, family_of, label

LEASE_MIN = 15
MAX_BACKOFF_MIN = 24 * 60
WORKERS = int(os.environ.get("WORKRADAR_WORKERS", "6"))
TRACKED = ("rate", "attendance", "start", "engagement")


# ---------------- queue ----------------
def due_sources(db, at=None, only=None):
    at = D.iso(at or D.now())
    q = ("SELECT * FROM sources WHERE status='active' AND (lease_until IS NULL OR lease_until < ?) "
         "AND (check_now_requested_at IS NOT NULL OR next_check_at IS NULL OR next_check_at <= ?)")
    args = [at, at]
    if only:
        q += " AND source_id IN (%s)" % ",".join("?" * len(only))
        args += list(only)
    q += " ORDER BY check_now_requested_at IS NULL, next_check_at"
    return [dict(r) for r in db.execute(q, args)]


def lease(db, source_id, at=None):
    at = at or D.now()
    cur = db.execute("UPDATE sources SET lease_until=?, last_attempt_at=? WHERE source_id=? AND (lease_until IS NULL OR lease_until < ?)",
                     (D.iso(at + dt.timedelta(minutes=LEASE_MIN)), D.iso(at), source_id, D.iso(at)))
    return cur.rowcount == 1


def request_check_now(db, source_id=None):
    if source_id in (None, "all"):
        db.execute("UPDATE sources SET check_now_requested_at=? WHERE status='active'", (D.iso(D.now()),))
    else:
        db.execute("UPDATE sources SET check_now_requested_at=? WHERE source_id=?", (D.iso(D.now()), source_id))


# ---------------- fetch (no DB access in worker threads) ----------------
def fetch(src, detail_cache=None):
    fn = connectors.CONNECTORS[src["connector"]]
    if src["connector"] in ("reed", "smartrecruiters", "rss"):
        return fn(src, detail_cache=detail_cache)
    if src["connector"] == "jooble":
        return fn(src, budget=src.get("_budget", 22))
    return fn(src)


def relevant(raw):
    return bool(family_of(raw["title"])) and not EXCLUDE_RX.search(raw["title"])


# ---------------- import ----------------
def _row_from(raw, src, lab, at, is_fixture):
    pub = raw.get("published_at")
    return dict(
        job_id=D.sha(src["source_id"], raw["source_job_id"]), source_id=src["source_id"], source_job_id=raw["source_job_id"],
        employer_org=src["org_id"] if src.get("org_kind") in ("employer", "public_sector") else None,
        employer_name=raw.get("employer_name") or (src.get("org_name") if src.get("org_kind") in ("employer", "public_sector") else None),
        agency_org=src["org_id"] if src.get("org_kind") == "agency" else None,
        agency_name=raw.get("agency_name") or (src.get("org_name") if src.get("org_kind") == "agency" else None),
        title=raw["title"], description=(raw.get("description") or "")[:8000], location=raw.get("location"),
        country_eligibility=lab["country_eligibility"], attendance=lab["attendance"], engagement=lab["engagement"],
        hours=lab["hours"], rate_min=lab["rate_min"], rate_max=lab["rate_max"], currency=lab["currency"], pay_period=lab["pay_period"],
        ir35=lab["ir35"], duration=lab["duration"], start=lab["start"], job_url=raw["job_url"], apply_url=raw["apply_url"],
        published_at=pub, published_precision=raw.get("published_precision") if pub else None,
        source_updated_at=raw.get("updated_at"), first_detected_at=D.iso(at), last_observed_at=D.iso(at),
        content_hash=D.sha(raw["title"], raw.get("description"), raw.get("pay_text"), raw.get("location")),
        provenance=D.jdump(lab["provenance"]), issues=D.jdump(lab["issues"]), skills=D.jdump(lab["skills"]),
        family=lab["family"], is_fixture=1 if is_fixture else 0)


def _rate_str(r):
    return None if r["rate_min"] is None else f"{r['currency']} {r['rate_min']:g}-{r['rate_max']:g}/{r['pay_period']}"


ROTATING = {"reed", "jooble"}   # searched a batch of terms per run, not the whole board
ROTATING_KEEP_HOURS = 72        # a full sweep of the terms takes under a day; 72 h leaves room for a missed run


def restore_rotating(db, at=None):
    """Jobs that a rotating search wrongly marked as gone (before ROTATING existed) come back if seen in the last 72 h."""
    at = at or D.now()
    cutoff = D.iso(at - dt.timedelta(hours=ROTATING_KEEP_HOURS))
    q = """UPDATE jobs SET listing_status='listed' WHERE listing_status='no_longer_listed' AND last_observed_at >= ?
           AND source_id IN (SELECT source_id FROM sources WHERE connector IN (%s))""" % ",".join("?" * len(ROTATING))
    with D.tx(db):
        n = db.execute(q, (cutoff, *ROTATING)).rowcount
    return n


def import_jobs(db, src, raws, at=None):
    """Atomic per source. Returns (seen, new). Only called after a fully parsed, valid fetch."""
    at = at or D.now()
    is_fixture = bool(src.get("is_fixture"))
    keep = [r for r in raws if relevant(r)]
    seen_ids, new = set(), 0
    with D.tx(db):
        for raw in keep:
            row = _row_from(raw, src, label(raw), at, is_fixture)
            seen_ids.add(row["job_id"])
            old = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            if old is None:
                cols = ",".join(row)
                db.execute(f"INSERT INTO jobs({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
                db.execute("INSERT INTO job_changes(job_id,at,kind,new_value) VALUES(?,?,?,?)", (row["job_id"], D.iso(at), "detected", row["title"]))
                new += 1
                continue
            old = dict(old)
            changes = []
            for k in TRACKED:
                a = _rate_str(old) if k == "rate" else old[k]
                b = _rate_str(row) if k == "rate" else row[k]
                if a != b:
                    changes.append((k, a, b))
            if old["listing_status"] != "listed":
                changes.append(("reappeared", old["listing_status"], "listed"))
            for k, a, b in changes:
                db.execute("INSERT INTO job_changes(job_id,at,kind,old_value,new_value) VALUES(?,?,?,?,?)",
                           (row["job_id"], D.iso(at), k, a, b))
            # Never overwrite first detection; never let an "updated" date become the published date.
            row.pop("first_detected_at")
            if old["published_at"] and not row["published_at"]:
                row.pop("published_at"); row.pop("published_precision")
            row.update(listing_status="listed", stale=0)
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in row) + " WHERE job_id=?", [*row.values(), row["job_id"]])
        # Jobs this source listed before but not now: no longer listed (not "closed": we can't know that).
        # Job-board searches rotate through the search terms, so each run sees only part of the board: a job missing from
        # one run is normal there. Those jobs stay listed until nothing has seen them for ROTATING_KEEP_HOURS.
        if src.get("connector") in ROTATING:
            cutoff = D.iso(at - dt.timedelta(hours=ROTATING_KEEP_HOURS))
            gone = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE source_id=? AND listing_status='listed' AND last_observed_at < ?",
                                             (src["source_id"], cutoff)) if r[0] not in seen_ids]
        else:
            gone = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE source_id=? AND listing_status='listed'", (src["source_id"],))
                    if r[0] not in seen_ids]
        for jid in gone:
            db.execute("UPDATE jobs SET listing_status='no_longer_listed', stale=0 WHERE job_id=?", (jid,))
            db.execute("INSERT INTO job_changes(job_id,at,kind,old_value,new_value) VALUES(?,?,?,?,?)",
                       (jid, D.iso(at), "no_longer_listed", "listed", "no_longer_listed"))
        db.execute("UPDATE jobs SET stale=0 WHERE source_id=?", (src["source_id"],))
    return len(keep), new, len(raws)


# ---------------- health ----------------
def record_success(db, src, seen, total, at):
    interval = src["check_interval_min"]
    health = "ok" if total else "empty"
    db.execute("""UPDATE sources SET last_success_at=?, next_check_at=?, consecutive_failures=0, health=?, last_error=NULL,
                  last_job_count=?, lease_until=NULL, check_now_requested_at=NULL WHERE source_id=?""",
               (D.iso(at), D.iso(at + dt.timedelta(minutes=interval)), health, total, src["source_id"]))


def record_failure(db, src, err, at):
    fails = (src.get("consecutive_failures") or 0) + 1
    if isinstance(err, http.RateLimited):
        wait_min = max(src["check_interval_min"], err.retry_after / 60)
        health = "rate_limited"
    else:
        wait_min = min(MAX_BACKOFF_MIN, src["check_interval_min"] * 2 ** fails)
        health = "degraded" if fails < 3 else "failing"
    db.execute("""UPDATE sources SET next_check_at=?, consecutive_failures=?, health=?, last_error=?, lease_until=NULL,
                  check_now_requested_at=NULL WHERE source_id=?""",
               (D.iso(at + dt.timedelta(minutes=wait_min)), fails, health, str(err)[:300], src["source_id"]))
    # Keep the jobs, but say they may be out of date.
    db.execute("UPDATE jobs SET stale=1 WHERE source_id=? AND listing_status='listed'", (src["source_id"],))


# ---------------- run ----------------
def _src_view(db, s):
    o = db.execute("SELECT name, kind FROM organisations WHERE org_id=?", (s["org_id"],)).fetchone()
    s = dict(s)
    s["org_name"], s["org_kind"] = (o["name"], o["kind"]) if o else (s["org_id"], "employer")
    return s


def run(db, trigger="schedule", only=None, max_minutes=12, detail_cache_path=None, fetcher=fetch, at=None):
    started = at or D.now()
    run_id = D.sha("run", D.iso(started), os.getpid(), time.time())
    # Restart recovery: close runs a crash left open.
    db.execute("UPDATE runs SET finished_at=started_at WHERE finished_at IS NULL")
    restore_rotating(db, started)
    for conn, cap in connectors.DAILY_CAP.items():   # caps follow the code, not the value stored when the source was added
        db.execute("UPDATE sources SET daily_request_cap=? WHERE connector=?", (cap, conn))
    db.execute("INSERT INTO runs(run_id,kind,trigger,started_at,host) VALUES(?,?,?,?,?)",
               (run_id, "collect", trigger, D.iso(started), socket.gethostname()[:40]))
    due = due_sources(db, started, only)
    picked = []
    for s in due:
        cap = s.get("daily_request_cap")
        if cap and D.usage_today(db, f"requests:{s['source_id']}") >= cap:
            db.execute("UPDATE sources SET next_check_at=?, health='rate_limited', last_error=? WHERE source_id=?",
                       (D.iso(started + dt.timedelta(hours=6)), f"daily cap {cap} reached; resumes later", s["source_id"]))
            continue
        if lease(db, s["source_id"], started):
            v = _src_view(db, s)
            if cap and s["connector"] == "jooble":
                v["_budget"] = max(0, min(22, int(cap - D.usage_today(db, f"requests:{s['source_id']}"))))
            picked.append(v)

    detail_cache = {}
    if detail_cache_path and os.path.exists(detail_cache_path):
        try:
            detail_cache = json.load(open(detail_cache_path))
        except Exception:
            detail_cache = {}
    deadline = time.time() + max_minutes * 60
    ok = failed = new_total = 0

    def work(s):
        t0 = time.time()
        try:
            return s, fetcher(s, detail_cache), None, t0
        except Exception as e:  # isolation: every failure stays with its own source
            return s, None, e, t0

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, s) for s in picked]
        for f in cf.as_completed(futs):
            s, raws, err, t0 = f.result()
            at2 = D.now() if at is None else at
            reqs = sum(http.take_counts().values())  # approximate per source under concurrency
            D.add_usage(db, f"requests:{s['source_id']}", max(reqs, 1))
            entry = dict(source_id=s["source_id"], started_at=D.iso(D.now()), run_id=run_id, requests=reqs,
                         duration_ms=int((time.time() - t0) * 1000))
            if err is None:
                try:
                    seen, new, total = import_jobs(db, s, raws, at2)
                    record_success(db, s, seen, total, at2)
                    ok += 1
                    new_total += new
                    entry.update(ok=1, jobs_seen=total, new_jobs=new)
                except Exception as e:  # import rolled back by tx()
                    err = e
            if err is not None:
                record_failure(db, s, err, at2)
                failed += 1
                entry.update(ok=0, error=str(err)[:300], http_status=getattr(err, "status", None))
            entry["finished_at"] = D.iso(D.now())
            cols = ",".join(entry)
            db.execute(f"INSERT INTO fetch_log({cols}) VALUES({','.join('?' * len(entry))})", list(entry.values()))
            if time.time() > deadline:
                for g in futs:
                    g.cancel()
    # Anything cancelled keeps its lease until expiry; release now so the next run picks it up.
    db.execute("UPDATE sources SET lease_until=NULL WHERE lease_until IS NOT NULL AND source_id IN (%s)" %
               ",".join("?" * len(picked)), [s["source_id"] for s in picked]) if picked else None
    if detail_cache_path:
        json.dump(detail_cache, open(detail_cache_path, "w"))
    dedupe.assign(db)
    db.execute("UPDATE runs SET finished_at=?, sources_due=?, sources_ok=?, sources_failed=?, new_jobs=? WHERE run_id=?",
               (D.iso(D.now()), len(picked), ok, failed, new_total, run_id))
    return dict(run_id=run_id, due=len(picked), ok=ok, failed=failed, new=new_total)


# ---------------- apply-link checks ----------------
def check_links(db, limit=40):
    rows = db.execute("""SELECT job_id, apply_url FROM jobs WHERE listing_status='listed' AND is_fixture=0
                         AND (apply_check_status IS NULL OR apply_checked_at < ?) ORDER BY first_detected_at DESC LIMIT ?""",
                      (D.iso(D.now() - dt.timedelta(days=3)), limit)).fetchall()

    def one(r):
        try:
            st, _, _ = http.request(r["apply_url"], accept="text/html,*/*", timeout=15, retries=0)
            return r["job_id"], "ok" if st < 400 else "broken"
        except http.RateLimited:
            return r["job_id"], "blocked"
        except http.HttpError as e:
            return r["job_id"], "blocked" if e.status in (401, 403, 405, 429) or e.status == 0 else "broken"

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(one, rows))
    for jid, st in res:
        db.execute("UPDATE jobs SET apply_check_status=?, apply_checked_at=? WHERE job_id=?", (st, D.iso(D.now()), jid))
    return dict(checked=len(res), broken=sum(1 for _, s in res if s == "broken"))
