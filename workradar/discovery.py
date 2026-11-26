"""Source discovery: candidates -> detect platform -> validate live -> identity -> relevance -> decide.

Decisions:
  activated   : valid structured feed, identity confirmed, at least one relevant UK-eligible role now
                (or a UK employer in a target sector with a working feed and >=5 UK roles: kept, checked hourly)
  review      : feed works but identity or relevance is uncertain, or the board looks moved
  unsupported : only a platform we have no compliant integration for (named in `reason`)
  rejected    : excluded (pay-to-apply / applicant-paid), not relevant, or no working feed

A guessed slug is never proof: a board counts only if its live API returns the expected structure
AND the organisation's identity checks out. Logins, CAPTCHAs and robots.txt disallows are respected.
"""
import concurrent.futures as cf
import functools
import datetime as dt
import json
import os
import re
import urllib.robotparser
from urllib.parse import urljoin, urlparse

from . import connectors, db as D, http
from .extract import UK_RX, NON_UK_RX, family_of, strip_html

EXCLUDED = re.compile(r"freelance ?radar|pay to apply|applicant[- ]paid", re.I)

ATS_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
]
UNSUPPORTED = [("Workday", r"myworkdayjobs\.com|workday\.com/.+/job"), ("SuccessFactors", r"successfactors\.(?:com|eu)|jobs\.sap\.com"),
               ("Oracle Taleo", r"taleo\.net"), ("Oracle Recruiting Cloud", r"oraclecloud\.com/hcmUI"), ("iCIMS", r"icims\.com"),
               ("Jobvite", r"jobs\.jobvite\.com"), ("Eightfold", r"eightfold\.ai"), ("Avature", r"avature\.net"),
               ("Phenom", r"phenompeople|/phenom"), ("Teamtailor", r"teamtailor\.com"), ("Pinpoint", r"pinpointhq\.com"),
               ("Broadbean/Volcanic", r"volcanic\.co\.uk|broadbean"), ("LinkedIn only", r"linkedin\.com/jobs")]
INTEGRATION = {"Workday": "Workday has no public jobs API; needs the employer's CXS endpoint permission or a partner feed",
               "Teamtailor": "Teamtailor public API needs a per-company key from the employer",
               "LinkedIn only": "LinkedIn jobs cannot be collected (terms); needs a direct feed from the organisation"}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@functools.lru_cache(maxsize=2048)
def _robots(origin):
    """Returns a parser, True (no robots file: allowed) or False (unreachable/forbidden: not allowed)."""
    rp = urllib.robotparser.RobotFileParser()
    try:
        txt, _ = http.get_text(f"{origin}/robots.txt", timeout=10, retries=0)
        rp.parse(txt.splitlines())
        return rp
    except http.HttpError as e:
        return e.status in (404, 410)
    except Exception:
        return False


def robots_ok(url):
    p = urlparse(url)
    r = _robots(f"{p.scheme}://{p.netloc}")
    return r.can_fetch(http.UA, url) if not isinstance(r, bool) else r


def scan_careers(url):
    """Fetch a careers page (robots permitting) and list platform hints found in its links."""
    if not url or not robots_ok(url):
        return [], [], "robots.txt disallows or site unreachable"
    try:
        html_text, _ = http.get_text(url, timeout=20, retries=1)
    except Exception as e:
        return [], [], f"careers page: {e}"
    hints = []
    for conn, rx in ATS_PATTERNS:
        for m in re.finditer(rx, html_text):
            slug = m.group(1)
            if slug.lower() not in ("embed", "api", "www", "jobs", "careers") and (conn, slug) not in hints:
                hints.append((conn, slug))
    unsup = [n for n, rx in UNSUPPORTED if re.search(rx, html_text, re.I)]
    if '"JobPosting"' in html_text or "'JobPosting'" in html_text:
        hints.append(("jsonld", url))
    for m in re.finditer(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)', html_text, re.I):
        feed = urljoin(url, m.group(1))
        if re.search(r"job|vacanc|career", feed, re.I):
            hints.append(("rss", feed))
    return hints, unsup, None


FEED_PATHS = ["/jobs/rss", "/jobs.rss", "/jobs/feed", "/feed/jobs", "/rss/jobs", "/job-feed", "/jobs/rss.xml", "/vacancies/rss", "/jobs?format=rss"]


def probe_feeds(domain):
    """Agencies: look for a published jobs RSS feed at the usual paths (robots permitting)."""
    if not domain:
        return []
    found = []
    for path in FEED_PATHS:
        url = f"https://www.{domain.removeprefix('www.')}{path}"
        if not robots_ok(url):
            return found
        try:
            text, h = http.get_text(url, accept="application/rss+xml, application/xml", timeout=12, retries=0)
        except Exception:
            continue
        if re.search(r"<rss|<feed", text[:500], re.I) and "<item" in text:
            found.append(("rss", url))
            break
    return found


def probe(conn, board, region="global"):
    """Live check. Returns (jobs, meta) or raises."""
    src = {"connector": conn, "board_id": board, "region": region, "org_name": None}
    meta = {}
    if conn == "greenhouse":
        try:
            meta["board_name"] = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}", retries=0).get("name")
        except Exception:
            pass
    jobs = connectors.CONNECTORS[conn](src)
    return jobs, meta


def identity_ok(cand, jobs, meta):
    name = cand["name"].lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", name) if len(t) > 2 and t not in ("the", "ltd", "group", "plc", "and", "bank", "energy")]
    toks = toks or [re.sub(r"[^a-z0-9]", "", name)]
    bn = (meta.get("board_name") or "").lower()
    if bn and any(t in bn for t in toks):
        return True, f"board name '{meta['board_name']}'"
    if not jobs:
        return False, "no jobs to check identity against"
    dom = (cand.get("domain") or "").lower().split(".")[0]
    hits = 0
    for j in jobs[:40]:
        blob = " ".join([j.get("description", "")[:3000], j.get("job_url") or "", j.get("apply_url") or "", j.get("employer_name") or ""]).lower()
        if any(t in blob for t in toks) or (dom and dom in blob):
            hits += 1
    share = hits / min(len(jobs), 40)
    return share >= 0.3, f"{hits}/{min(len(jobs), 40)} adverts name the organisation"


def relevance(jobs):
    rel = [j for j in jobs if family_of(j["title"])]
    uk = [j for j in jobs if UK_RX.search(j.get("location") or "") or re.search(r"remote", j.get("location") or "", re.I)
          and not NON_UK_RX.search(j.get("location") or "")]
    rel_uk = [j for j in rel if j in uk]
    return dict(total=len(jobs), relevant_titles=len(rel), uk_jobs=len(uk), relevant_uk=len(rel_uk),
                examples=[j["title"] for j in rel_uk[:5]], checked_at=D.iso(D.now()))


def assess(cand):
    """Returns a decision dict; never writes. Network calls only to public, documented endpoints."""
    if EXCLUDED.search(" ".join(str(v) for v in cand.values())):
        return dict(state="rejected", reason="Excluded: applicant-paid / pay-to-apply destination")
    hints = [tuple(h.split(":", 1)) for h in cand.get("hints", [])]
    unsup, note = [], None
    if cand.get("retired"):
        h = hints[0] if hints else (None, None)
        return dict(connector=h[0], board_id=h[1], state="rejected", reason=cand["retired"])
    if cand.get("careers_url") and not hints:
        found, unsup, note = scan_careers(cand["careers_url"])
        hints += found
    if cand.get("kind") == "agency" and not hints:
        hints += probe_feeds(cand.get("domain"))
    for conn, board in hints:
        if conn in ("reed", "jooble", "adzuna", "careerjet"):  # keyed APIs: don't spend quota on discovery; the collector validates each run
            have = "key configured as a secret" if connectors._key(conn) else f"{conn.upper()}_API_KEY secret not set yet: each run reports it until it is"
            return dict(connector=conn, board_id=board, state="activated", reason=f"{connectors.ACCESS[conn]}; {have}")
    guessed = not hints
    if guessed:
        slugs = list(dict.fromkeys([slugify(cand["name"]).replace("-", ""), slugify(cand["name"])]))
        hints = [(c, s) for c in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee", "pinpoint", "personio") for s in slugs]
        hints += [("lever", s + "@eu") for s in slugs[:1]]
    tried = []
    for conn, board in hints:
        region = "eu" if conn == "lever" and board.endswith("@eu") else "global"
        board = board.split("@")[0]
        try:
            jobs, meta = probe(conn, board, region)
        except http.RateLimited as e:
            tried.append(f"{conn}:{board} rate limited")
            continue
        except Exception as e:
            tried.append(f"{conn}:{board} {str(e)[:60]}")
            continue
        if guessed and not jobs:
            tried.append(f"{conn}:{board} empty")
            continue  # an empty board at a guessed address proves nothing
        if cand.get("kind") == "job_board":  # the documented API is the organisation's own: identity is the endpoint itself
            ok, why = True, f"official {conn} API"
        else:
            ok, why = identity_ok(cand, jobs, meta)
        rel = relevance(jobs)
        base = dict(connector=conn, board_id=board, region=region, relevance=rel, identity=why)
        if not ok:
            return dict(base, state="review", reason=f"Feed works but identity unconfirmed ({why})")
        if cand.get("kind") == "agency" and conn == "rss" and rel["relevant_titles"] >= 1:
            return dict(base, state="activated", reason=f"UK agency feed: {rel['relevant_titles']} relevant roles now; identity: {why}")
        if rel["relevant_uk"] >= 1:
            return dict(base, state="activated", reason=f"{rel['relevant_uk']} relevant UK roles now; identity: {why}")
        if rel["uk_jobs"] >= 5 and cand.get("target_sector"):
            return dict(base, state="activated", reason=f"No relevant role today but {rel['uk_jobs']} UK roles in a target sector; identity: {why}",
                        interval=60)
        return dict(base, state="review", reason=f"Feed works; {rel['uk_jobs']} UK roles, none relevant today")
    if unsup:
        n = unsup[0]
        return dict(state="unsupported", reason=f"Uses {n}. " + INTEGRATION.get(n, f"No public {n} feed; needs a partner feed or employer permission"))
    transient = any(re.search(r"HTTP (0|5\d\d)|rate limited|timed out", t) for t in tried)
    detail = "No working public feed found (" + "; ".join(tried[:4]) + ")"
    if cand.get("kind") == "agency":
        return dict(state="review", reason=(note + "; " if note else "") + "No public feed on the agency's own site; "
                    "its adverts are collected through Reed and Jooble instead. " + detail)
    if note or transient:
        return dict(state="review", reason=(note + "; " if note else "") + "Temporary error, retried next week. " + detail)
    return dict(state="rejected", reason=detail)


def upsert_org(db, c):
    oid = c.get("org_id") or slugify(c["name"])
    db.execute("""INSERT INTO organisations(org_id,name,kind,domain,parent_org_id,former_names,sectors) VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(org_id) DO UPDATE SET name=excluded.name, kind=excluded.kind, domain=COALESCE(excluded.domain, domain)""",
               (oid, c["name"], c.get("kind", "employer"), c.get("domain"), c.get("parent"), D.jdump(c.get("former_names", [])),
                D.jdump(c.get("sectors", []))))
    return oid


def apply_decision(db, c, d, at=None):
    at = at or D.now()
    oid = upsert_org(db, c)
    cid = c.get("candidate_id") or D.sha("cand", oid)
    db.execute("""INSERT INTO discovery_candidates(candidate_id,name,kind,domain,careers_url,origin,found_at,detected_connector,detected_board,state,reason,assessed_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET detected_connector=excluded.detected_connector,
                  detected_board=excluded.detected_board, state=excluded.state, reason=excluded.reason, assessed_at=excluded.assessed_at""",
               (cid, c["name"], c.get("kind"), c.get("domain"), c.get("careers_url"), c.get("origin"), c.get("found_at") or D.iso(at),
                d.get("connector"), d.get("board_id"), d["state"], d["reason"], D.iso(at)))
    if not d.get("connector"):
        return None
    sid = f"{d['connector']}:{d['board_id']}"
    status = {"activated": "active", "review": "review"}.get(d["state"], d["state"])
    interval = d.get("interval") or connectors.INTERVAL.get(d["connector"], 15)
    db.execute("""INSERT INTO sources(source_id,org_id,connector,board_id,region,careers_url,host,discovery_origin,discovered_at,access_conditions,
                  check_interval_min,daily_request_cap,status,status_reason,relevance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(source_id) DO UPDATE SET status=CASE WHEN sources.status IN ('paused','rejected') THEN sources.status ELSE excluded.status END,
                  status_reason=excluded.status_reason, relevance=excluded.relevance, check_interval_min=excluded.check_interval_min""",
               (sid, oid, d["connector"], d["board_id"], d.get("region", "global"), c.get("careers_url"),
                connectors.API_HOST.get(d["connector"], urlparse(d["board_id"]).hostname if "://" in d["board_id"] else None),
                c.get("origin"), D.iso(at), connectors.ACCESS.get(d["connector"]), interval, connectors.DAILY_CAP.get(d["connector"]),
                status, d["reason"], D.jdump(d.get("relevance"))))
    return sid


def candidates_from_adverts(db, min_ads=2):
    """Agencies seen advertising relevant roles on Reed/Jooble become candidates automatically."""
    rows = db.execute("""SELECT agency_name, COUNT(*) n, MIN(source_id) src FROM jobs WHERE agency_name IS NOT NULL AND is_fixture=0
                         AND source_id IN ('reed:uk','jooble:uk') GROUP BY lower(agency_name) HAVING n >= ?""", (min_ads,)).fetchall()
    known = {r[0] for r in db.execute("SELECT lower(name) FROM organisations")}
    out = []
    for r in rows:
        if r["agency_name"].lower() in known:
            continue
        out.append(dict(name=r["agency_name"], kind="agency", origin=f"seen advertising {r['n']} relevant roles on {r['src'].split(':')[0]}",
                        found_at=D.iso(D.now())))
    return out


def recheck_moved(db):
    """Weekly: an active source whose board now returns 404 goes to review so its careers page is rescanned."""
    moved = []
    for s in db.execute("SELECT * FROM sources WHERE status='active' AND consecutive_failures>=3 AND last_error LIKE 'HTTP 404%'"):
        db.execute("UPDATE sources SET status='review', status_reason='Board returns 404: probably moved; careers page queued for rescan' WHERE source_id=?",
                   (s["source_id"],))
        moved.append(s["source_id"])
    return moved


def run(db, candidates, limit=None, assess_fn=assess):
    run_id = D.sha("discover", D.iso(D.now()))
    db.execute("INSERT INTO runs(run_id,kind,trigger,started_at) VALUES(?,?,?,?)", (run_id, "discover", "schedule", D.iso(D.now())))
    moved = recheck_moved(db)
    fresh = {r[0] for r in db.execute("SELECT candidate_id FROM discovery_candidates WHERE state='activated' AND assessed_at > ?",
                                       (D.iso(D.now() - dt.timedelta(days=3)),))}
    todo = [c for c in (candidates[:limit] if limit else candidates)
            if c.get("retired") or (c.get("candidate_id") or D.sha("cand", c.get("org_id") or slugify(c["name"]))) not in fresh]
    results = []
    # Assess in parallel (network only); write one at a time. Per-host limits in http.py still apply.
    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("WORKRADAR_DISCOVERY_WORKERS", "8"))) as ex:
        futs = {ex.submit(assess_fn, c): c for c in todo}
        for f in cf.as_completed(futs):
            c = futs[f]
            try:
                d = f.result()
            except Exception as e:
                d = dict(state="review", reason=f"Assessment error: {str(e)[:120]}")
            with D.tx(db):
                sid = apply_decision(db, c, d)
            results.append(dict(name=c["name"], state=d["state"], source=sid, reason=d["reason"]))
    db.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (D.iso(D.now()), run_id))
    return dict(results=results, moved=moved)
