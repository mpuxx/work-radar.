"""Strict matching. Each rule returns pass / fail / unknown. A job is an exact match only if every rule passes.
Unknown is never silently treated as pass, and rules are never broadened without the user choosing to.

rules = {
  "query": "business analyst",          # phrase match on the title
  "related": false,                     # opt-in: also match titles in the same family
  "country": "UK",                      # UK eligibility
  "where": "remote" | "hybrid" | "any", # remote = no office attendance; hybrid = remote or hybrid
  "engagement": ["Day-rate contract"],  # allowed engagement labels
  "posted_days": 1,                     # by source publication date; unknown date = unknown
  "rate_min": 500,                      # GBP per day
  "ir35": "any" | "outside"
}
"""
import datetime as dt
import re

from . import db as D
from .extract import family_of

ENG_ALIASES = {"day": "Day-rate contract", "fixed": "Fixed-term employment", "temp": "Temporary", "perm": "Permanent"}


def _norm(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "


def r_title(job, rules):
    q = rules.get("query")
    if not q:
        return "pass", None
    if _norm(q) in _norm(job["title"]):
        return "pass", f"Title contains “{q}”"
    if rules.get("related") and family_of(job["title"]) and family_of(job["title"]) == family_of(q):
        return "pass", f"Related title ({family_of(q)})"
    return "fail", None


def r_country(job, rules):
    if rules.get("country", "UK") != "UK":
        return "pass", None
    c = job["country_eligibility"]
    if c in ("UK", "UK and elsewhere"):
        return "pass", "Open to UK"
    if c == "Outside UK":
        return "fail", None
    return "unknown", "Country eligibility not stated"


def r_where(job, rules):
    w = rules.get("where", "any")
    a = job["attendance"] or "Not stated"
    if w == "any":
        return "pass", None
    if a == "Not stated":
        return "unknown", "Office attendance not stated"
    if a.startswith("Remote"):
        return "pass", a
    if w == "hybrid" and a.startswith("Hybrid"):
        return "pass", a
    return "fail", None


def r_engagement(job, rules):
    allowed = [ENG_ALIASES.get(e, e) for e in rules.get("engagement") or []]
    if not allowed:
        return "pass", None
    e = job["engagement"] or "Not stated"
    if e in allowed:
        return "pass", e
    if e in ("Not stated", "Contract, type not stated") and any(x in ("Day-rate contract", "Fixed-term employment") for x in allowed):
        return "unknown", f"Engagement: {e}"
    return "fail", None


def r_posted(job, rules, now=None):
    days = rules.get("posted_days")
    if not days:
        return "pass", None
    now = now or D.now()
    if not job["published_at"]:
        return "unknown", "Found {}; posting date unknown".format(D.parse(job["first_detected_at"]).date().isoformat())
    p = D.parse(job["published_at"])
    if job["published_precision"] == "date":
        ok = (now.date() - p.date()).days < days
    else:
        ok = now - p <= dt.timedelta(days=days)
    return ("pass", f"Posted {p.date().isoformat()}") if ok else ("fail", None)


def r_rate(job, rules):
    lo = rules.get("rate_min")
    if not lo:
        return "pass", None
    if job["rate_max"] is None:
        return "unknown", "Rate not stated"
    if job["pay_period"] != "day" or job["currency"] != "GBP":
        return "unknown", f"Pay is {job['currency']} per {job['pay_period']}"
    return ("pass", f"Up to £{job['rate_max']:g}/day") if job["rate_max"] >= lo else ("fail", None)


def r_ir35(job, rules):
    if rules.get("ir35", "any") != "outside":
        return "pass", None
    v = job["ir35"] or "Not stated"
    if v == "Outside IR35":
        return "pass", v
    if v == "Inside IR35":
        return "fail", None
    return "unknown", f"IR35: {v}"


RULES = [("title", r_title), ("country", r_country), ("where", r_where), ("engagement", r_engagement),
         ("posted", r_posted), ("rate", r_rate), ("ir35", r_ir35)]


def evaluate(job, rules, now=None):
    """Returns (verdict, reasons, unknowns). verdict: 'match' | 'unknown' | 'fail'."""
    job = dict(job)
    if job.get("listing_status", "listed") != "listed":
        return "fail", [], []
    reasons, unknowns = [], []
    for name, fn in RULES:
        st, why = fn(job, rules, now) if fn is r_posted else fn(job, rules)
        if st == "fail":
            return "fail", [], []
        if st == "unknown":
            unknowns.append(why)
        elif why:
            reasons.append(why)
    return ("match" if not unknowns else "unknown"), reasons, unknowns


def search(db, rules, now=None):
    out = {"match": [], "unknown": []}
    for j in db.execute("SELECT * FROM jobs WHERE listing_status='listed'"):
        v, reasons, unknowns = evaluate(j, rules, now)
        if v != "fail":
            out[v].append((dict(j), reasons, unknowns))
    return out
