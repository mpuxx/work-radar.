"""Cross-source duplicates and reposts. Conservative: when unsure, keep adverts separate.

* Employer known: same employer + same normalised title + same town = one probable group.
* Agency advert with the client hidden: grouped only with the SAME agency's adverts (same title,
  town and rate). Two agencies with "Business Analyst, London" are never merged: they may be
  different clients.
* Repost: same source, new advert id, same employer/agency + title + town, appearing within
  30 days of the earlier one leaving the listing.
"""
import re

from . import db as D

STOP = re.compile(r"\b(senior|snr|sr|junior|jr|lead|principal|contract|contractor|interim|freelance|remote|hybrid|"
                  r"outside ir35|inside ir35|ir35|\d+ months?|urgent|immediate start)\b|[^a-z0-9 ]", re.I)


def norm_title(t):
    t = STOP.sub(" ", (t or "").lower())
    return " ".join(t.split())


def norm_org(n):
    n = re.sub(r"\b(ltd|limited|plc|llp|group|uk|inc|the)\b|[^a-z0-9 ]", " ", (n or "").lower())
    return " ".join(n.split())


def town(loc):
    loc = (loc or "").lower()
    if "remote" in loc:
        return "remote"
    return re.split(r"[,(/|-]", loc)[0].strip()


def key(j):
    emp = norm_org(j["employer_name"]) if j["employer_name"] else ""
    if emp:
        return "e:" + D.sha(emp, norm_title(j["title"]), town(j["location"]))
    ag = norm_org(j["agency_name"]) or j["source_id"]
    return "a:" + D.sha(ag, norm_title(j["title"]), town(j["location"]), j["rate_min"], j["rate_max"])


def assign(db):
    rows = db.execute("SELECT job_id, source_id, title, location, employer_name, agency_name, rate_min, rate_max, "
                      "first_detected_at, listing_status, dup_group FROM jobs").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    changed = 0
    for k, members in groups.items():
        gid = k if len(members) > 1 else None
        for m in members:
            if m["dup_group"] != gid:
                db.execute("UPDATE jobs SET dup_group=? WHERE job_id=?", (gid, m["job_id"]))
                changed += 1
        # reposts: within one source, a newer advert after an older one stopped being listed
        by_src = {}
        for m in sorted(members, key=lambda x: x["first_detected_at"]):
            by_src.setdefault(m["source_id"], []).append(m)
        for lst in by_src.values():
            for older, newer in zip(lst, lst[1:]):
                if older["listing_status"] != "listed" and newer["listing_status"] == "listed":
                    gap = (D.parse(newer["first_detected_at"]) - D.parse(older["first_detected_at"])).days
                    if gap <= 60:
                        cur = db.execute("UPDATE jobs SET repost_of=? WHERE job_id=? AND repost_of IS NULL", (older["job_id"], newer["job_id"]))
                        if cur.rowcount:
                            db.execute("INSERT INTO job_changes(job_id,at,kind,old_value,new_value) VALUES(?,?,?,?,?)",
                                       (newer["job_id"], D.iso(D.now()), "repost", older["job_id"], newer["job_id"]))
    return changed
