"""Write the static data the web app reads (web/data/*.json) and SOURCE_REGISTRY.csv. No secrets are ever written."""
import csv
import datetime as dt
import json
from pathlib import Path

from . import db as D
from .taxonomy import classify

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "data"

JOB_FIELDS = ["job_id", "source_id", "employer_name", "agency_name", "title", "description", "location", "country_eligibility",
              "attendance", "engagement", "hours", "rate_min", "rate_max", "currency", "pay_period", "ir35", "duration", "start",
              "job_url", "apply_url", "published_at", "published_precision", "source_updated_at", "first_detected_at",
              "last_observed_at", "listing_status", "stale", "apply_check_status", "provenance", "issues", "skills", "family",
              "dup_group", "repost_of"]


def jobs(db, days=30):
    since = D.iso(D.now() - dt.timedelta(days=days))
    rows = db.execute(f"""SELECT {','.join('j.' + f for f in JOB_FIELDS)}, s.connector, o.kind AS source_kind FROM jobs j
                          JOIN sources s USING(source_id) JOIN organisations o ON o.org_id=s.org_id
                          WHERE j.is_fixture=0 AND (j.listing_status='listed' OR j.last_observed_at > ?)
                          AND j.country_eligibility != 'Outside UK' ORDER BY j.first_detected_at DESC""", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("provenance", "issues", "skills"):
            d[k] = json.loads(d[k] or "null")
        d["description"] = (d["description"] or "")[:6000]
        d["family"] = classify(d["title"])[0] or d["family"]   # always the current families, even for jobs stored under old names
        d["changes"] = [dict(c) for c in db.execute("SELECT at, kind, old_value, new_value FROM job_changes WHERE job_id=? AND kind!='detected' ORDER BY at", (d["job_id"],))]
        out.append(d)
    return out


def sources(db):
    q = """SELECT s.*, o.name AS org_name, o.kind AS org_kind, o.domain FROM sources s JOIN organisations o USING(org_id) ORDER BY o.kind, o.name"""
    return [dict(r) for r in db.execute(q)]


def status(db):
    last = db.execute("SELECT * FROM runs WHERE kind='collect' AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1").fetchone()
    srcs = db.execute("SELECT health, COUNT(*) n FROM sources WHERE status='active' GROUP BY health").fetchall()
    failing = [dict(r) for r in db.execute("""SELECT s.source_id, o.name, s.health, s.last_error, s.last_success_at FROM sources s
                                              JOIN organisations o USING(org_id) WHERE s.status='active' AND s.health IN ('degraded','failing','rate_limited')""")]
    nxt = db.execute("SELECT MIN(next_check_at) FROM sources WHERE status='active'").fetchone()[0]
    cands = {r[0]: r[1] for r in db.execute("SELECT state, COUNT(*) FROM discovery_candidates GROUP BY state")}
    recent = [dict(r) for r in db.execute("SELECT run_id, trigger, started_at, finished_at, sources_due, sources_ok, sources_failed, new_jobs FROM runs WHERE kind='collect' ORDER BY started_at DESC LIMIT 12")]
    return dict(generated_at=D.iso(D.now()), last_run=dict(last) if last else None, next_check_at=nxt,
                health={r["health"]: r["n"] for r in srcs}, partial_failures=failing, candidates=cands, recent_runs=recent,
                active_sources=db.execute("SELECT COUNT(*) FROM sources WHERE status='active'").fetchone()[0],
                listed_jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE listing_status='listed' AND is_fixture=0").fetchone()[0],
                usage_today={r["bucket"]: r["amount"] for r in db.execute("SELECT bucket, amount FROM usage WHERE day=?", (D.now().date().isoformat(),))})


def notifications(db):
    q = """SELECT n.notification_id, n.user_id, n.search_id, n.job_id, n.created_at, n.status, n.delivered_at, n.reasons, j.title, j.apply_url
           FROM notifications n JOIN jobs j USING(job_id) JOIN users u ON u.user_id=n.user_id ORDER BY n.created_at DESC LIMIT 500"""
    return [dict(r, reasons=json.loads(r["reasons"] or "[]")) for r in db.execute(q)]


REG_COLS = ["source_id", "org_name", "org_kind", "connector", "board_id", "region", "careers_url", "host", "status", "status_reason",
            "discovery_origin", "discovered_at", "access_conditions", "check_interval_min", "daily_request_cap", "health",
            "last_success_at", "last_job_count", "consecutive_failures", "last_error", "relevance"]


def registry_csv(db, path=ROOT / "SOURCE_REGISTRY.csv"):
    rows = sources(db)
    cands = [dict(r) for r in db.execute("""SELECT * FROM discovery_candidates c WHERE NOT EXISTS
                (SELECT 1 FROM sources s WHERE s.connector=c.detected_connector AND s.board_id=c.detected_board) ORDER BY state, name""")]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REG_COLS)
        for r in rows:
            w.writerow([r.get(c) for c in REG_COLS])
        for c in cands:
            w.writerow(["", c["name"], c["kind"], c["detected_connector"], c["detected_board"], "", c["careers_url"], "", c["state"],
                        c["reason"], c["origin"], c["found_at"], "", "", "", "", "", "", "", "", ""])
    return len(rows) + len(cands)


def write_all(db):
    WEB.mkdir(parents=True, exist_ok=True)
    all_jobs = jobs(db)
    # Light list for the results page; the long text loads only when a job is opened (details/<2 hex>.json).
    heavy = ("description", "provenance", "changes")
    light = [{k: v for k, v in j.items() if k not in heavy} for j in all_jobs]
    det_dir = WEB / "details"
    det_dir.mkdir(parents=True, exist_ok=True)
    buckets = {}
    for j in all_jobs:
        buckets.setdefault(j["job_id"][:2], {})[j["job_id"]] = {k: j.get(k) for k in heavy}
    for old in det_dir.glob("*.json"):
        if old.stem not in buckets:
            old.unlink()
    for b, d in buckets.items():
        (det_dir / f"{b}.json").write_text(json.dumps(d, ensure_ascii=False, default=str))
    for name, data in (("jobs", light), ("sources", sources(db)), ("status", status(db)), ("notifications", notifications(db))):
        (WEB / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")))
    from .taxonomy import FAMILIES
    (WEB / "taxonomy.json").write_text(json.dumps([{"family": f, "terms": t} for f, t in FAMILIES], ensure_ascii=False))
    n = registry_csv(db)
    return dict(registry_rows=n)
