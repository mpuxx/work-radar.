"""Connectors: one function per hiring platform / feed. Each returns a list of raw job dicts:

  source_job_id, title, description, location, employment_type, hours, workplace, pay_text,
  published_at, published_precision ('time'|'date'|None), updated_at, job_url, apply_url,
  employer_name, agency_name

Connectors raise on an invalid structure, so a half-parsed feed never changes listing status.
Preference order (see discovery.py): documented public API > partner feed > RSS/XML > JobPosting JSON-LD.
"""
import base64
import datetime as dt
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode, urljoin

from . import http
from .extract import strip_html


class InvalidFeed(Exception):
    pass


def _iso(v, precision="time"):
    if v in (None, ""):
        return None, None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).isoformat(), "time"
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(v))
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1])).isoformat(), "date"
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if not t.tzinfo:
            t = t.replace(tzinfo=dt.timezone.utc)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
            return str(v), "date"
        return t.isoformat(), precision
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(str(v)).isoformat(), "time"
        except Exception:
            return None, None


def _need(cond, msg):
    if not cond:
        raise InvalidFeed(msg)


def _job(**kw):
    base = dict(source_job_id=None, title="", description="", location="", employment_type="", hours="", workplace="",
                pay_text="", published_at=None, published_precision=None, updated_at=None, job_url=None, apply_url=None,
                employer_name=None, agency_name=None)
    base.update(kw)
    _need(base["source_job_id"], "job without id")
    _need(base["title"], "job without title")
    _need(base["job_url"] and str(base["job_url"]).startswith("http"), "job without https link")
    base["apply_url"] = base["apply_url"] or base["job_url"]
    return base


# ---------------- documented public ATS APIs ----------------
def greenhouse(src, get=http.get_json):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{quote(src['board_id'])}/jobs?content=true")
    _need(isinstance(d.get("jobs"), list), "greenhouse: no jobs list")
    out = []
    for j in d["jobs"]:
        pub, prec = _iso(j.get("first_published"))
        out.append(_job(source_job_id=str(j["id"]), title=j.get("title", "").strip(), description=strip_html(j.get("content")),
                        location=(j.get("location") or {}).get("name", ""), published_at=pub, published_precision=prec,
                        updated_at=_iso(j.get("updated_at"))[0], job_url=j.get("absolute_url"),
                        employer_name=j.get("company_name") or src.get("org_name")))
    return out


def lever(src, get=http.get_json):
    base = "https://api.eu.lever.co" if src.get("region") == "eu" else "https://api.lever.co"
    out, skip = [], 0
    while True:
        batch = get(f"{base}/v0/postings/{quote(src['board_id'])}?mode=json&skip={skip}&limit=100")
        _need(isinstance(batch, list), "lever: response is not a list")
        for j in batch:
            c = j.get("categories") or {}
            text = "\n".join([j.get("descriptionPlain", ""), *[(l.get("text", "") + "\n" + strip_html(l.get("content"))) for l in j.get("lists", [])],
                              j.get("additionalPlain", "")])
            pub, prec = _iso(j.get("createdAt"))
            sal = j.get("salaryRange") or {}
            pay = f"£{sal.get('min')} - £{sal.get('max')} per {str(sal.get('interval', '')).replace('per-', '')}" if sal.get("currency") == "GBP" else ""
            out.append(_job(source_job_id=j["id"], title=j.get("text", "").strip(), description=text,
                            location="; ".join(c.get("allLocations") or [c.get("location", "")]) + (f" ({j['country']})" if j.get("country") else ""),
                            employment_type=c.get("commitment", ""), workplace=j.get("workplaceType", ""), pay_text=pay,
                            published_at=pub, published_precision=prec, job_url=j.get("hostedUrl"), apply_url=j.get("applyUrl"),
                            employer_name=src.get("org_name")))
        if len(batch) < 100:
            return out
        skip += 100
        _need(skip < 5000, "lever: pagination runaway")


def ashby(src, get=http.get_json):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(src['board_id'])}?includeCompensation=true")
    _need(isinstance(d.get("jobs"), list), "ashby: no jobs list")
    out = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        comp = (j.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or ""
        pub, prec = _iso(j.get("publishedAt"))
        out.append(_job(source_job_id=j["id"], title=j.get("title", "").strip(), description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
                        location="; ".join(l for l in locs if l), employment_type=j.get("employmentType", ""),
                        workplace=j.get("workplaceType") or ("Remote" if j.get("isRemote") else ""), pay_text=comp if "£" in comp else "",
                        published_at=pub, published_precision=prec, job_url=j.get("jobUrl"), apply_url=j.get("applyUrl"),
                        employer_name=src.get("org_name")))
    return out


def _sr_detail(company, pid, get):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{quote(company)}/postings/{quote(pid)}")
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    parts = []
    for k, head in (("jobDescription", ""), ("qualifications", "What you'll need"), ("additionalInformation", "More information"),
                    ("companyDescription", "About the company")):
        t = strip_html((secs.get(k) or {}).get("text") or "")
        if t:
            parts.append((head + "\n" if head else "") + t)
    return "\n\n".join(parts)


def smartrecruiters(src, get=http.get_json, detail_cache=None):
    """Public postings API. The list has no advert text, so the full advert is fetched once per relevant posting and cached."""
    from .extract import family_of
    cache = detail_cache if detail_cache is not None else {}
    left = src.get("_details", 60)
    out, offset = [], 0
    while True:
        d = get(f"https://api.smartrecruiters.com/v1/companies/{quote(src['board_id'])}/postings?limit=100&offset={offset}")
        _need(isinstance(d.get("content"), list), "smartrecruiters: no content list")
        for j in d["content"]:
            loc = j.get("location") or {}
            pub, prec = _iso(j.get("releasedDate"))
            country = (loc.get("country") or "").upper()
            key = f"sr:{j['id']}"
            if key not in cache and left > 0 and family_of(j.get("name", "")):
                left -= 1
                try:
                    cache[key] = _sr_detail(src["board_id"], j["id"], get)
                except http.RateLimited:
                    raise
                except Exception:
                    pass
            out.append(_job(source_job_id=j["id"], title=j.get("name", "").strip(), description=cache.get(key, ""),
                            location=", ".join(x for x in [loc.get("city"), "United Kingdom" if country == "GB" else country] if x),
                            employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                            workplace="Remote" if loc.get("remote") else ("Hybrid" if loc.get("hybrid") else ""),
                            published_at=pub, published_precision=prec,
                            job_url=f"https://jobs.smartrecruiters.com/{src['board_id']}/{j['id']}",
                            employer_name=(j.get("company") or {}).get("name") or src.get("org_name")))
        offset += 100
        if offset >= d.get("totalFound", 0):
            return out
        _need(offset < 3000, "smartrecruiters: pagination runaway")


def workable(src, get=http.get_json):
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{quote(src['board_id'])}?details=true")
    _need(isinstance(d.get("jobs"), list), "workable: no jobs list")
    out = []
    for j in d["jobs"]:
        pub, prec = _iso(j.get("published_on"), "date")
        out.append(_job(source_job_id=j.get("shortcode") or j.get("id"), title=j.get("title", "").strip(), description=strip_html(j.get("description")),
                        location=", ".join(x for x in [j.get("city"), j.get("country")] if x), employment_type=j.get("employment_type", ""),
                        workplace="Remote" if j.get("telecommuting") else "", published_at=pub, published_precision=prec,
                        job_url=j.get("url") or j.get("shortlink"), apply_url=j.get("application_url"), employer_name=src.get("org_name")))
    return out


def recruitee(src, get=http.get_json):
    d = get(f"https://{src['board_id']}.recruitee.com/api/offers/")
    _need(isinstance(d.get("offers"), list), "recruitee: no offers list")
    out = []
    for j in d["offers"]:
        pub, prec = _iso(j.get("published_at"))
        sal = j.get("salary") or {}
        pay = f"£{sal.get('min')} - £{sal.get('max')} per {sal.get('period')}" if sal.get("currency") == "GBP" and sal.get("min") else ""
        out.append(_job(source_job_id=str(j["id"]), title=j.get("title", "").strip(),
                        description=strip_html((j.get("description") or "") + "\n" + (j.get("requirements") or "")),
                        location=j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x),
                        employment_type=j.get("employment_type_code", ""), workplace="Remote" if j.get("remote") else ("Hybrid" if j.get("hybrid") else ""),
                        pay_text=pay, published_at=pub, published_precision=prec, updated_at=_iso(j.get("updated_at"))[0],
                        job_url=j.get("careers_url"), apply_url=j.get("careers_apply_url"), employer_name=j.get("company_name") or src.get("org_name")))
    return out


def personio(src, get=None):
    text, _ = http.get_text(f"https://{src['board_id']}.jobs.personio.de/xml?language=en", accept="application/xml")
    root = ET.fromstring(text)
    _need(root.tag == "workzag-jobs", "personio: unexpected XML root")
    out = []
    for p in root.findall("position"):
        jid = p.findtext("id")
        desc = "\n\n".join(strip_html((d.findtext("name") or "") + "\n" + (d.findtext("value") or "")) for d in p.findall("jobDescriptions/jobDescription"))
        pub, prec = _iso(p.findtext("createdAt"))
        out.append(_job(source_job_id=jid, title=(p.findtext("name") or "").strip(), description=desc, location=p.findtext("office") or "",
                        employment_type=p.findtext("employmentType") or "", hours=p.findtext("schedule") or "",
                        published_at=pub, published_precision=prec,
                        job_url=f"https://{src['board_id']}.jobs.personio.de/job/{jid}", employer_name=src.get("org_name")))
    return out


def _jsonld_postings(html_text):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("@type") == "JobPosting" or (isinstance(x.get("@type"), list) and "JobPosting" in x["@type"]):
                    yield x
                stack.extend(v for k, v in x.items() if k in ("@graph", "itemListElement", "item"))
                if isinstance(x.get("@graph"), list):
                    stack.extend(x["@graph"])
            elif isinstance(x, list):
                stack.extend(x)


def jsonld(src, get=None):
    """Public schema.org JobPosting data on a careers listing page and the detail pages it links to (same site only)."""
    page, _ = http.get_text(src["board_id"])
    found = list(_jsonld_postings(page))
    if not found:
        links = sorted(set(re.findall(r'href="([^"#]+/(?:jobs?|vacanc(?:y|ies)|roles?|careers?)/[^"#?]+)"', page)))[:40]
        for href in links:
            u = urljoin(src["board_id"], href)
            try:
                found.extend(_jsonld_postings(http.get_text(u)[0]))
            except Exception:
                continue
    _need(isinstance(found, list), "jsonld: parse failure")
    out = []
    for j in found:
        loc = j.get("jobLocation") or {}
        loc = loc[0] if isinstance(loc, list) and loc else loc
        addr = (loc or {}).get("address") or {}
        addr = addr if isinstance(addr, dict) else {}
        sal = j.get("baseSalary") or {}
        val = sal.get("value") or {} if isinstance(sal, dict) else {}
        pay = ""
        if isinstance(sal, dict) and sal.get("currency") == "GBP" and isinstance(val, dict) and (val.get("minValue") or val.get("value")):
            pay = f"£{val.get('minValue') or val.get('value')} - £{val.get('maxValue') or val.get('value')} per {str(val.get('unitText', '')).lower()}"
        pub, prec = _iso(j.get("datePosted"), "date")
        url = j.get("url") or src["board_id"]
        org = j.get("hiringOrganization") or {}
        out.append(_job(source_job_id=str(j.get("identifier", {}).get("value") if isinstance(j.get("identifier"), dict) else j.get("identifier") or url),
                        title=strip_html(j.get("title", "")), description=strip_html(j.get("description", "")),
                        location=", ".join(x for x in [addr.get("addressLocality"), addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else ""] if x),
                        employment_type=" ".join(j["employmentType"]) if isinstance(j.get("employmentType"), list) else j.get("employmentType", ""),
                        workplace="Remote" if j.get("jobLocationType") == "TELECOMMUTE" else "", pay_text=pay,
                        published_at=pub, published_precision=prec, job_url=url,
                        employer_name=org.get("name") if isinstance(org, dict) else None))
    return out


def _page_advert(url):
    """Advert text from a job page's schema.org JobPosting: the structured data sites publish for job search engines."""
    from .discovery import robots_ok
    if not url or not robots_ok(url):
        return ""
    page, _ = http.get_text(url, timeout=20, retries=0)
    for j in _jsonld_postings(page):
        t = strip_html(j.get("description") or "")
        if t:
            return t
    return ""


def rss(src, get=None, detail_cache=None):
    from .extract import family_of
    cache = detail_cache if detail_cache is not None else {}
    left = src.get("_details", 25)
    text, _ = http.get_text(src["board_id"], accept="application/rss+xml, application/xml")
    root = ET.fromstring(text)
    items = root.findall(".//item")
    _need(root.tag in ("rss", "{http://www.w3.org/2005/Atom}feed") or items, "rss: not a feed")
    out = []
    for it in items:
        link = it.findtext("link")
        pub, prec = _iso(it.findtext("pubDate"))
        title = strip_html(it.findtext("title") or "")
        desc = strip_html(it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or "")
        key = f"page:{link}"
        if len(desc) < 600 and link and family_of(title):
            if key not in cache and left > 0:
                left -= 1
                try:
                    cache[key] = _page_advert(link)
                except Exception:
                    cache[key] = ""
            if len(cache.get(key) or "") > len(desc):
                desc = cache[key]
        out.append(_job(source_job_id=it.findtext("guid") or link, title=title,
                        description=desc, published_at=pub, published_precision=prec,
                        job_url=link, agency_name=src.get("org_name") if src.get("org_kind") == "agency" else None,
                        employer_name=src.get("org_name") if src.get("org_kind") == "employer" else None))
    return out


def xmlfeed(src, get=None):
    """Publisher job XML feed in the common job-board syndication format (<source><job>...), as agencies publish for job boards."""
    text, _ = http.get_text(src["board_id"], accept="application/xml, text/xml")
    root = ET.fromstring(text)
    jobs = root.findall(".//job")
    _need(root.tag == "source" or jobs, "xmlfeed: not a job feed")
    out = []
    for it in jobs:
        f = lambda k: (it.findtext(k) or "").strip()
        pub, prec = _iso(f("date") or None)
        place = ", ".join(x for x in [f("city"), f("country")] if x)
        out.append(_job(source_job_id=f("referencenumber") or f("requisitionid") or f("url"), title=strip_html(f("title")),
                        description=strip_html(f("description")), location=place, employment_type=f("jobtype"),
                        pay_text=f("salary"), published_at=pub, published_precision=prec, job_url=re.sub(r"[?&]source=Indeed$", "", f("url")),
                        agency_name=src.get("org_name") if src.get("org_kind") == "agency" else None,
                        employer_name=src.get("org_name") if src.get("org_kind") == "employer" else (f("company") if src.get("org_kind") != "agency" else None)))
    return out


# ---------------- job boards with keys (agencies advertise here) ----------------
from .taxonomy import SEARCH_TERMS as SEARCHES, batch as _batch


def _slot(hours):
    import time
    return int(time.time() // (hours * 3600))


def _key(name):
    v = os.environ.get(f"{name.upper()}_API_KEY")
    if v:
        return v.strip()
    p = os.path.expanduser(f"~/.config/plainroles/{name}.key")
    return open(p).read().strip() if os.path.exists(p) else None


def _reed_pay(r):
    """Pay from the search result when the full advert wasn't fetched. Reed gives numbers without a period: day rates are small."""
    lo, hi = r.get("minimumSalary"), r.get("maximumSalary")
    if not lo and not hi:
        return ""
    lo, hi = lo or hi, hi or lo
    return f"£{lo:,.0f} - £{hi:,.0f} " + ("per day" if hi <= 2500 else "per annum")


def reed(src, get=http.get_json, detail_cache=None):
    k = _key("reed")
    _need(k, "reed: REED_API_KEY not set")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{k}:".encode()).decode()}
    found, kind = {}, {}
    # ~219 terms, 45 per 2-hourly run: a full sweep about every 10 h. Each term is searched as contract, temporary and permanent,
    # so the job type comes from Reed's own filter even when the advert text doesn't say.
    terms = SEARCHES if os.environ.get("WORKRADAR_FULL_SWEEP") == "1" else _batch(SEARCHES, src.get("_batch", 45), _slot(2))
    for q in terms:
        for flag in ("contract", "temp", "permanent"):
            d = get("https://www.reed.co.uk/api/1.0/search?" + urlencode({"keywords": q, flag: "true", "resultsToTake": 100}), headers=auth)
            _need(isinstance(d.get("results"), list), "reed: no results list")
            for r in d["results"]:
                found.setdefault(r["jobId"], r)
                kind.setdefault(r["jobId"], flag)
    from .extract import family_of
    out = []
    cache = detail_cache if detail_cache is not None else {}
    details_left = src.get("_details", 250)  # full advert text, fetched once per job and cached; the search snippet until then
    for i, r in found.items():
        if not family_of(r["jobTitle"]):
            continue
        d = cache.get(str(i))
        if d is None and details_left <= 0:
            d = {}
        elif d is None:
            details_left -= 1
            try:
                d = get(f"https://www.reed.co.uk/api/1.0/jobs/{i}", headers=auth)
                cache[str(i)] = d
            except http.RateLimited:
                raise
            except Exception:
                d = {}
        pub, prec = _iso(r.get("date"))
        out.append(_job(source_job_id=str(i), title=r["jobTitle"].strip(), description=strip_html(d.get("jobDescription") or r.get("jobDescription")),
                        location=f"{r.get('locationName', '')}, UK",
                        employment_type=d.get("contractType") or {"contract": "Contract", "temp": "Temporary", "permanent": "Permanent"}[kind[i]],
                        hours="Part-time" if d.get("partTime") else "Full-time" if d.get("fullTime") else "",
                        pay_text=d.get("salary") or _reed_pay(r), published_at=pub, published_precision=prec,
                        job_url=r.get("jobUrl"), apply_url=d.get("externalUrl") or r.get("jobUrl"),
                        agency_name=(r.get("employerName") or "").strip() or None))
    return out


def jooble(src, get=http.get_json, budget=22):
    k = _key("jooble")
    _need(k, "jooble: JOOBLE_API_KEY not set")
    out = []
    for q in _batch(SEARCHES, 22, _slot(6))[:budget]:  # 4 runs a day x 22 = 88 of the 100 daily requests
        d = get(f"https://jooble.org/api/{k}", data={"keywords": q, "location": "United Kingdom"})
        _need(isinstance(d.get("jobs"), list), "jooble: no jobs list")
        for j in d["jobs"]:
            if "$" in (j.get("salary") or ""):
                continue
            out.append(_job(source_job_id=str(j["id"]), title=strip_html(j.get("title")), description=strip_html(j.get("snippet")),
                            location=j.get("location", ""), employment_type=j.get("type", ""), pay_text=j.get("salary", ""),
                            updated_at=_iso(j.get("updated"))[0], job_url=j.get("link"), agency_name=j.get("company") or None))
    return out


def contractsfinder(src, get=http.get_json):
    """UK public-sector tenders (bids, not jobs). Kept in their own source so the app can show them apart."""
    d = get("https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?stages=tender&limit=100", timeout=90)
    _need(isinstance(d.get("releases"), list), "contractsfinder: no releases list")
    out = []
    for r in d["releases"]:
        t = r.get("tender") or {}
        if not t.get("title") or not r.get("id"):
            continue
        end = (t.get("tenderPeriod") or {}).get("endDate")
        if end and _iso(end)[0] and _iso(end)[0] < dt.datetime.now(dt.timezone.utc).isoformat():
            continue
        pub, prec = _iso(r.get("date"))
        val = (t.get("value") or {}).get("amount")
        out.append(_job(source_job_id=r["id"], title=t["title"].strip(), location="UK",
                        description="Public-sector tender (a bid for a supplier, not a job). "
                                    + (f"Value: £{val:,.0f}. " if isinstance(val, (int, float)) else "")
                                    + (f"Closes {end[:10]}. " if end else "") + strip_html(t.get("description"))[:3000],
                        employment_type="Tender", published_at=pub, published_precision=prec,
                        job_url="https://www.contractsfinder.service.gov.uk/Notice/" + r["id"].rsplit("-", 1)[0],
                        employer_name=(r.get("buyer") or {}).get("name")))
    return out


def pinpoint(src, get=http.get_json):
    d = get(f"https://{src['board_id']}.pinpointhq.com/postings.json")
    _need(isinstance(d.get("data"), list), "pinpoint: no data list")
    out = []
    for j in d["data"]:
        loc = j.get("location") or {}
        pub, prec = _iso(j.get("published_at") or j.get("created_at"))
        out.append(_job(source_job_id=str(j["id"]), title=(j.get("title") or "").strip(),
                        description=strip_html((j.get("description") or "") + "\n" + (j.get("key_responsibilities") or "")),
                        location=", ".join(x for x in [loc.get("city"), loc.get("province"), loc.get("name")] if x),
                        employment_type=j.get("employment_type_text") or j.get("employment_type") or "",
                        workplace=j.get("workplace_type_text") or j.get("workplace_type") or "",
                        pay_text=(j.get("compensation") or "") if "£" in str(j.get("compensation") or "") else "",
                        published_at=pub, published_precision=prec, job_url=j.get("url"), employer_name=src.get("org_name")))
    return out


# ---------------- open job-board APIs (no key; attribution: we always link to the original advert) ----------------
REMOTE_TERMS = ["business analyst", "project manager", "product owner", "delivery manager", "transformation", "service design", "operations"]


def remotive(src, get=http.get_json):
    out, seen = [], set()
    for q in REMOTE_TERMS[:4]:
        d = get("https://remotive.com/api/remote-jobs?" + urlencode({"search": q, "limit": 100}))
        _need(isinstance(d.get("jobs"), list), "remotive: no jobs list")
        for j in d["jobs"]:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            pub, prec = _iso(j.get("publication_date"))
            out.append(_job(source_job_id=str(j["id"]), title=j.get("title", "").strip(), description=strip_html(j.get("description")),
                            location="Remote, " + (j.get("candidate_required_location") or "location not stated"),
                            employment_type=j.get("job_type", ""), workplace="Remote", pay_text=j.get("salary") or "",
                            published_at=pub, published_precision=prec, job_url=j.get("url"), employer_name=j.get("company_name")))
    return out


def himalayas(src, get=http.get_json):
    out = []
    for offset in (0, 100, 200):
        d = get(f"https://himalayas.app/jobs/api?limit=100&offset={offset}")
        _need(isinstance(d.get("jobs"), list), "himalayas: no jobs list")
        for j in d["jobs"]:
            locs = j.get("locationRestrictions") or []
            pub, prec = _iso((j.get("pubDate") or 0) * 1000 if isinstance(j.get("pubDate"), (int, float)) else j.get("pubDate"))
            out.append(_job(source_job_id=str(j.get("guid") or j.get("applicationLink")), title=(j.get("title") or "").strip(),
                            description=strip_html(j.get("description") or j.get("excerpt")),
                            location="Remote, " + (", ".join(locs) if locs else "worldwide"), employment_type=j.get("employmentType") or "",
                            workplace="Remote", published_at=pub, published_precision=prec,
                            job_url=j.get("applicationLink") or j.get("guid"), employer_name=j.get("companyName")))
    return out


def remoteok(src, get=http.get_json):
    d = get("https://remoteok.com/api")
    _need(isinstance(d, list), "remoteok: not a list")
    out = []
    for j in d[1:]:  # first item is the API's legal notice
        if not isinstance(j, dict) or not j.get("id"):
            continue
        pub, prec = _iso(j.get("date"))
        out.append(_job(source_job_id=str(j["id"]), title=(j.get("position") or "").strip(), description=strip_html(j.get("description")),
                        location="Remote, " + (j.get("location") or "location not stated"), workplace="Remote",
                        published_at=pub, published_precision=prec, job_url=j.get("url"), apply_url=j.get("apply_url"),
                        employer_name=j.get("company")))
    return out


def jobicy(src, get=http.get_json):
    d = get("https://jobicy.com/api/v2/remote-jobs?count=100&geo=uk")
    _need(isinstance(d.get("jobs"), list), "jobicy: no jobs list")
    out = []
    for j in d["jobs"]:
        pub, prec = _iso(j.get("pubDate"))
        typ = j.get("jobType")
        out.append(_job(source_job_id=str(j["id"]), title=strip_html(j.get("jobTitle")), description=strip_html(j.get("jobDescription")),
                        location="Remote, " + (j.get("jobGeo") or "UK"), employment_type=", ".join(typ) if isinstance(typ, list) else (typ or ""),
                        workplace="Remote", published_at=pub, published_precision=prec, job_url=j.get("url"), employer_name=j.get("companyName")))
    return out


def arbeitnow(src, get=http.get_json):
    out = []
    for page in (1, 2, 3):
        d = get(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        _need(isinstance(d.get("data"), list), "arbeitnow: no data list")
        for j in d["data"]:
            pub, prec = _iso((j.get("created_at") or 0) * 1000 if isinstance(j.get("created_at"), (int, float)) else j.get("created_at"))
            out.append(_job(source_job_id=j.get("slug"), title=(j.get("title") or "").strip(), description=strip_html(j.get("description")),
                            location=j.get("location") or "", employment_type=", ".join(j.get("job_types") or []),
                            workplace="Remote" if j.get("remote") else "", published_at=pub, published_precision=prec,
                            job_url=j.get("url"), employer_name=j.get("company_name")))
    return out


# ---------------- aggregator APIs with free keys ----------------
def adzuna(src, get=http.get_json):
    app_id, key = os.environ.get("ADZUNA_APP_ID"), _key("adzuna")
    _need(app_id and key, "adzuna: ADZUNA_APP_ID / ADZUNA_API_KEY not set")
    out, seen = [], set()
    for q in SEARCHES[: src.get("_budget", 12)]:
        d = get("https://api.adzuna.com/v1/api/jobs/gb/search/1?" + urlencode(
            {"app_id": app_id, "app_key": key, "what_phrase": q, "contract_type": "contract", "results_per_page": 50, "max_days_old": 7}))
        _need(isinstance(d.get("results"), list), "adzuna: no results list")
        for j in d["results"]:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            lo, hi = j.get("salary_min"), j.get("salary_max")
            pay = f"£{lo:.0f} - £{hi:.0f} per year" if lo and hi and j.get("salary_is_predicted") in ("0", 0) else ""
            pub, prec = _iso(j.get("created"))
            out.append(_job(source_job_id=str(j["id"]), title=strip_html(j.get("title")), description=strip_html(j.get("description")),
                            location=(j.get("location") or {}).get("display_name", "") + ", UK",
                            employment_type=j.get("contract_type") or "", hours=j.get("contract_time") or "", pay_text=pay,
                            published_at=pub, published_precision=prec, job_url=j.get("redirect_url"),
                            agency_name=(j.get("company") or {}).get("display_name")))
    return out


def careerjet(src, get=http.get_json):
    aff = _key("careerjet")
    _need(aff, "careerjet: CAREERJET_API_KEY (affiliate id) not set")
    out, seen = [], set()
    for q in SEARCHES[: src.get("_budget", 12)]:
        d = get("https://public.api.careerjet.net/search?" + urlencode(
            {"keywords": q, "location": "United Kingdom", "locale_code": "en_GB", "contracttype": "c", "pagesize": 50, "affid": aff,
             "user_ip": "1.1.1.1", "user_agent": http.UA, "url": "https://mpuxx.github.io/work-radar./"}))
        _need(d.get("type") in ("JOBS", "LOCATIONS") , "careerjet: unexpected response")
        for j in d.get("jobs") or []:
            if j["url"] in seen:
                continue
            seen.add(j["url"])
            pub, _ = _iso(j.get("date"))
            out.append(_job(source_job_id=D_sha(j["url"]), title=strip_html(j.get("title")), description=strip_html(j.get("description")),
                            location=(j.get("locations") or "") + ", UK", pay_text=j.get("salary") or "", updated_at=pub,
                            job_url=j["url"], agency_name=j.get("company") or None))
    return out


def D_sha(x):
    import hashlib
    return hashlib.sha1(x.encode()).hexdigest()[:16]


def themuse(src, get=http.get_json):
    out = []
    for loc in ("London, United Kingdom", "Manchester, United Kingdom", "Edinburgh, United Kingdom", "Flexible / Remote"):
        for page in (0, 1, 2):
            d = get("https://www.themuse.com/api/public/jobs?" + urlencode({"location": loc, "page": page}))
            _need(isinstance(d.get("results"), list), "themuse: no results list")
            for j in d["results"]:
                pub, prec = _iso(j.get("publication_date"))
                out.append(_job(source_job_id=str(j["id"]), title=(j.get("name") or "").strip(), description=strip_html(j.get("contents")),
                                location="; ".join(l.get("name", "") for l in j.get("locations") or []),
                                workplace="Remote" if loc.startswith("Flexible") else "", published_at=pub, published_precision=prec,
                                job_url=(j.get("refs") or {}).get("landing_page"), employer_name=(j.get("company") or {}).get("name")))
            if page + 1 >= d.get("page_count", 0):
                break
    return out


def workingnomads(src, get=http.get_json):
    d = get("https://www.workingnomads.com/api/exposed_jobs/")
    _need(isinstance(d, list), "workingnomads: not a list")
    out = []
    for j in d:
        pub, prec = _iso(j.get("pub_date"))
        out.append(_job(source_job_id=j.get("url"), title=(j.get("title") or "").strip(), description=strip_html(j.get("description")),
                        location="Remote, " + (j.get("location") or "location not stated"), workplace="Remote",
                        published_at=pub, published_precision=prec, job_url=j.get("url"), employer_name=j.get("company_name")))
    return out


def findatender(src, get=http.get_json):
    """UK Find a Tender (above-threshold public contracts). Bids, not jobs, like Contracts Finder."""
    d = get("https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?stages=tender&limit=100", timeout=90)
    _need(isinstance(d.get("releases"), list), "findatender: no releases list")
    out = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for r in d["releases"]:
        t = r.get("tender") or {}
        end = (t.get("tenderPeriod") or {}).get("endDate")
        if not t.get("title") or not r.get("id") or (end and (_iso(end)[0] or "9") < now):
            continue
        pub, prec = _iso(r.get("date"))
        val = (t.get("value") or {}).get("amount")
        notice = r["id"].split("-")[0] + "-" + r["id"].split("-")[1] if r["id"].count("-") >= 2 else r["id"]
        out.append(_job(source_job_id=r["id"], title=t["title"].strip(), location="UK",
                        description="Public-sector tender (a bid for a supplier, not a job). "
                                    + (f"Value: £{val:,.0f}. " if isinstance(val, (int, float)) else "")
                                    + (f"Closes {end[:10]}. " if end else "") + strip_html(t.get("description"))[:3000],
                        employment_type="Tender", published_at=pub, published_precision=prec,
                        job_url=f"https://www.find-tender.service.gov.uk/Notice/{notice}", employer_name=(r.get("buyer") or {}).get("name")))
    return out


CONNECTORS = {"contractsfinder": contractsfinder, "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "workable": workable,
              "recruitee": recruitee, "personio": personio, "pinpoint": pinpoint,
              "remotive": remotive, "himalayas": himalayas, "remoteok": remoteok, "jobicy": jobicy, "arbeitnow": arbeitnow,
              "adzuna": adzuna, "careerjet": careerjet, "themuse": themuse, "workingnomads": workingnomads, "findatender": findatender, "jsonld": jsonld, "rss": rss, "xmlfeed": xmlfeed, "reed": reed, "jooble": jooble}

API_HOST = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com",
            "smartrecruiters": "api.smartrecruiters.com", "workable": "apply.workable.com", "reed": "www.reed.co.uk", "jooble": "jooble.org",
            "contractsfinder": "www.contractsfinder.service.gov.uk", "remotive": "remotive.com", "himalayas": "himalayas.app",
            "remoteok": "remoteok.com", "jobicy": "jobicy.com", "arbeitnow": "www.arbeitnow.com", "adzuna": "api.adzuna.com",
            "careerjet": "public.api.careerjet.net", "themuse": "www.themuse.com", "workingnomads": "www.workingnomads.com",
            "findatender": "www.find-tender.service.gov.uk"}

# Documented limits and conditions, recorded in the registry.
ACCESS = {
    "greenhouse": "Public Job Board API, no key (docs.greenhouse.io/job-board.html)",
    "lever": "Public Postings API, no key (github.com/lever/postings-api)",
    "ashby": "Public Job Posting API, no key (developers.ashbyhq.com)",
    "smartrecruiters": "Public Posting API, no key (developers.smartrecruiters.com)",
    "workable": "Public careers widget API, no key; rate limits often (429) so checked hourly",
    "recruitee": "Public Careers Site API, no key (docs.recruitee.com)",
    "personio": "Public XML job feed, no key",
    "jsonld": "Public schema.org JobPosting on careers pages; robots.txt checked",
    "rss": "Published RSS job feed",
    "xmlfeed": "Published job XML feed that the agency syndicates to job boards",
    "reed": "Reed Jobseeker API, free key, basic auth (reed.co.uk/developers)",
    "jooble": "Jooble REST API, free key, 100 requests/day",
    "pinpoint": "Public Pinpoint postings JSON, no key",
    "remotive": "Remotive public API, no key; terms ask for a link back to the advert (we always link to it)",
    "himalayas": "Himalayas public jobs API, no key; link back required",
    "remoteok": "Remote OK public API, no key; terms require a link back and attribution",
    "jobicy": "Jobicy public API, no key, UK filter; link back required",
    "arbeitnow": "Arbeitnow public job board API, no key",
    "adzuna": "Adzuna API, free key (developer.adzuna.com), contract filter",
    "careerjet": "Careerjet Partner API, affiliate id",
    "themuse": "The Muse public jobs API, no key (500 requests/hour without one)",
    "workingnomads": "Working Nomads public jobs API, no key; link back to the advert",
    "findatender": "UK Find a Tender OCDS API, open data, no key",
    "contractsfinder": "UK Government Contracts Finder OCDS API, open data, no key (Open Government Licence)",
}
INTERVAL = {"workable": 60, "reed": 120, "jooble": 360, "jsonld": 60, "rss": 30, "xmlfeed": 60, "contractsfinder": 60, "remotive": 60, "himalayas": 60,
            "remoteok": 60, "jobicy": 60, "arbeitnow": 120, "adzuna": 120, "careerjet": 120, "pinpoint": 30, "themuse": 120, "workingnomads": 60, "findatender": 120}
DAILY_CAP = {"jooble": 95, "reed": 3000, "adzuna": 240, "careerjet": 300, "remoteok": 48}
