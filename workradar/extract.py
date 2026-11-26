"""Turn advert text + structured feed fields into labelled fields with provenance.

Rules: structured fields first; text second; every label keeps the words it came from.
Unknown stays unknown. Conflicts are recorded, not resolved by guessing. No AI is used here;
an optional AI pass (ai.py) may only fill fields that are still unknown and must keep its quote.
"""
import html
import re

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"

UK_RX = re.compile(r"\b(london|manchester|edinburgh|glasgow|bristol|leeds|birmingham|cambridge|oxford|belfast|cardiff|reading|"
                   r"newcastle|sheffield|nottingham|brighton|milton keynes|liverpool|leicester|salford|mediacity|staines|"
                   r"guildford|bath|york|swindon|coventry|aberdeen|dundee|southampton|portsmouth|exeter|norwich|"
                   r"uk|u\.k\.|united kingdom|great britain|england|scotland|wales|northern ireland|\(gb\)|, gb\b)\b", re.I)
NON_UK_RX = re.compile(r"\b(united states|usa|u\.s\.|canada|india|germany|france|spain|ireland|dublin|netherlands|amsterdam|"
                       r"poland|portugal|singapore|australia|new york|san francisco|toronto|berlin|paris|madrid|lisbon)\b", re.I)

FAMILIES = [
    ("Business analysis", r"business analyst|business analysis|\bBA\b|requirements analyst|process analyst|systems analyst|"
                          r"functional analyst|business systems analyst|technical analyst"),
    ("Digital & CMS", r"\bCMS\b|headless|content management|contentful|sitecore|adobe experience manager|\bAEM\b|drupal|"
                      r"wordpress|umbraco|storyblok|sanity|digital producer|digital product|web producer|content designer|content platform"),
    ("Delivery & programme", r"programme manag|program manag|project manag|delivery lead|delivery manag|\bPMO\b|scrum master|"
                             r"agile coach|portfolio manag|release manag|product owner|head of delivery"),
    ("ProductOps, DesignOps & service design", r"product ?ops|product operations|design ?ops|design operations|service design|"
                                                r"service designer|research ?ops|ux research|user research"),
    ("AI, automation & transformation", r"\bAI\b|artificial intelligence|machine learning|\bML\b|\bLLM|genai|generative|automation|"
                                        r"\bRPA\b|workflow|transformation|change manag|change lead|change analyst|operating model|"
                                        r"process improvement|continuous improvement|operational excellence|adoption|data scien"),
]
FAMILY_RX = [(n, re.compile(p, re.I)) for n, p in FAMILIES]
EXCLUDE_RX = re.compile(r"\bintern\b|internship|graduate|apprentice|recruiter\b|talent acquisition|account executive|sales development|"
                        r"\bSDR\b|\bBDR\b|legal counsel|nurse|teacher|driver|tutor|warehouse|chef|cleaner|electrician", re.I)

SKILLS = ["requirements", "user stories", "process mapping", "BPMN", "stakeholder", "Jira", "Confluence", "Agile", "Scrum", "SAFe",
          "SQL", "Power BI", "Tableau", "Visio", "Salesforce", "SAP", "Workday", "ServiceNow", "Dynamics", "CRM", "ERP",
          "data migration", "API", "integration", "UAT", "Python", "LLM", "machine learning", "Azure", "AWS", "GCP", "Copilot",
          "Power Automate", "UiPath", "RPA", "regulatory", "GDPR", "banking", "insurance", "payments", "public sector", "NHS",
          "media", "broadcast", "publishing", "streaming", "utilities", "energy", "telecoms", "retail", "operating model", "Lean",
          "Six Sigma", "PRINCE2", "MSP", "governance", "roadmap", "product discovery", "service design", "GDS", "accessibility",
          "Contentful", "Sitecore", "AEM", "Drupal", "WordPress", "headless", "SC clearance", "DV clearance"]
SKILL_RX = [(k, re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", re.I)) for k in SKILLS]


def strip_html(s):
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(br|p|li|div|h\d|tr)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def family_of(title):
    if not title or EXCLUDE_RX.search(title):
        return None
    for name, rx in FAMILY_RX:
        if rx.search(title):
            return name
    return None


def quote(text, m, width=60):
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    q = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("…" if a else "") + q + ("…" if b < len(text) else "")


def find(rx, text):
    m = re.search(rx, text, re.I)
    return (m, quote(text, m)) if m else (None, None)


MONEY = r"£\s?(\d{2,3}(?:,\d{3})+|\d{2,4}(?:\.\d+)?\s?[kK]?)"


def num(s):
    s = s.replace(",", "").replace(" ", "")
    return float(s[:-1]) * 1000 if s[-1:] in "kK" else float(s)


class Labeller:
    def __init__(self, raw):
        self.raw = raw
        self.text = (raw.get("title", "") + ".\n" + (raw.get("description") or ""))
        self.prov, self.issues = {}, []

    def put(self, field, value, method, q):
        self.prov[field] = {"value": value, "method": method, "quote": q}
        return value

    # Employment arrangement (engagement) is separate from hours.
    def engagement(self):
        raw = (self.raw.get("employment_type") or "").lower()
        t = self.text
        m, q = find(r"fixed[- ]term|\bftc\b|maternity (cover|leave)|parental leave cover|secondment", t)
        if m:
            v = self.put("engagement", "Fixed-term employment", "explicit", q)
        else:
            m, q = find(r"outside ir35|inside ir35|\bir35\b|day rate|per day|\bp/d\b|\bumbrella\b|ltd company|limited company|"
                        r"\bcontractor\b|contract role|freelance|interim|initial \d+[- ]month contract|\d+[- ]months? contract", t)
            if m:
                v = self.put("engagement", "Day-rate contract", "explicit", q)
            elif any(x in raw for x in ("contract", "freelance", "contractor")):
                v = self.put("engagement", "Contract, type not stated", "feed_field", f"Feed field: {self.raw.get('employment_type')}")
            elif "temp" in raw or "intern" in raw:
                v = self.put("engagement", "Temporary", "feed_field", f"Feed field: {self.raw.get('employment_type')}")
            elif "permanent" in raw:
                v = self.put("engagement", "Permanent", "feed_field", f"Feed field: {self.raw.get('employment_type')}")
            else:
                m, q = find(r"\bpermanent\b", t)
                if m:
                    v = self.put("engagement", "Permanent", "explicit", q)
                elif raw in ("fulltime", "full-time", "full time", "parttime", "part-time") and self.raw.get("source_kind") == "employer_board":
                    v = self.put("engagement", "Permanent", "inferred", f"Employer board lists '{self.raw.get('employment_type')}' with no contract wording")
                else:
                    v = "Not stated"
        if v in ("Day-rate contract", "Fixed-term employment") and "permanent" in raw:
            self.issues.append(f"Conflict: feed says {self.raw.get('employment_type')}, advert says {v.lower()}")
        return v

    def hours(self):
        raw = (self.raw.get("hours") or self.raw.get("employment_type") or "").lower()
        m, q = find(r"(?<!onsite )(?<!office )\b([1-4])\s*(?:days?|d)\s*(?:a|per|/)\s*week\b(?![^.]{0,25}(?:office|onsite|on-site|on site|in person))", self.text)
        if m:
            return self.put("hours", f"{m.group(1)} days a week", "explicit", q)
        if "part" in raw:
            return self.put("hours", "Part-time", "feed_field", f"Feed field: {raw}")
        if "full" in raw:
            return self.put("hours", "Full-time", "feed_field", f"Feed field: {raw}")
        m, q = find(r"\bpart[- ]time\b", self.text)
        if m:
            return self.put("hours", "Part-time", "explicit", q)
        m, q = find(r"\bfull[- ]time\b", self.text)
        if m:
            return self.put("hours", "Full-time", "explicit", q)
        return "Not stated"

    def pay(self):
        src = (self.raw.get("pay_text") or "") + " . " + self.text
        m = re.search(MONEY + r"(?:\s?(?:-|–|to)\s?" + MONEY + r")?\s?(?:per|a|/|p/?)\s?(day|d\b|hour|hr|annum|year|pa\b|month)", src, re.I)
        if m:
            lo = num(m.group(1)); hi = num(m.group(2)) if m.group(2) else lo
            u = m.group(3).lower()
            unit = "day" if u.startswith("d") else "hour" if u.startswith("h") else "month" if u.startswith("m") else "year"
            if unit == "day" and not (100 <= lo <= 3000):
                return None
            self.put("rate", {"min": lo, "max": hi, "currency": "GBP", "period": unit}, "explicit", quote(src, m))
            return lo, hi, "GBP", unit
        m = re.search(MONEY + r"\s?(?:-|–|to)\s?" + MONEY, src)
        if m:
            lo, hi = num(m.group(1)), num(m.group(2))
            if 15000 <= lo <= 500000 and hi >= lo:
                self.put("rate", {"min": lo, "max": hi, "currency": "GBP", "period": "year"}, "inferred", quote(src, m) + " (period inferred from amount)")
                return lo, hi, "GBP", "year"
            if 150 <= lo <= 2500 and hi >= lo:
                self.issues.append("Pay period not stated; amount looks like a day rate but is not labelled")
        if re.search(r"[$€]\s?\d", src):
            self.issues.append("Pay quoted in a non-GBP currency")
        return None

    def ir35(self):
        m, q = find(r"outside (of )?ir35", self.text)
        m2, q2 = find(r"inside (of )?ir35", self.text)
        if m and m2:
            self.issues.append("Conflict: advert mentions both inside and outside IR35")
            self.put("ir35", "Conflicting", "explicit", q + " / " + q2)
            return "Conflicting"
        if m:
            return self.put("ir35", "Outside IR35", "explicit", q)
        if m2:
            return self.put("ir35", "Inside IR35", "explicit", q2)
        m, q = find(r"\bir35\b", self.text)
        if m:
            self.put("ir35", "Mentioned, status unclear", "explicit", q)
            return "Mentioned, status unclear"
        return "Not stated"

    def duration(self):
        m, q = find(r"\b(\d{1,2})[- ]?(month|week)s?\b(?=[^.]{0,40}(contract|initial|fixed|engagement|assignment|ftc|duration))|"
                    r"(contract|duration|initial(?:ly)?)[^.]{0,30}?\b(\d{1,2})[- ]?(month|week)s?", self.text)
        if not m:
            return "Not stated"
        n = m.group(1) or m.group(5); u = m.group(2) or m.group(6)
        return self.put("duration", f"{n} {u.lower()}{'s' if n != '1' else ''}", "explicit", q)

    def start(self):
        m, q = find(r"immediate start|start(?:ing)? (?:asap|immediately)|asap start|start(?: date)?\s*[:\-]\s*asap|available to start immediately", self.text)
        if m:
            return self.put("start", "ASAP", "explicit", q)
        m, q = find(r"start(?:ing)?(?: date)?\s*[:\-]?\s*(?:in|on|from|by|early|mid|late|beginning of|end of|w/c)?\s*((?:early |mid |late )?(?:"
                    + MONTHS + r")\b(?: \d{4})?|\d{1,2}(?:st|nd|rd|th)? (?:" + MONTHS + r")|\d{1,2}/\d{1,2}/\d{2,4}|the new year|q[1-4])", self.text)
        if m:
            return self.put("start", "Starts " + m.group(1).strip(), "explicit", q)
        return "Not stated"

    def attendance(self):
        w = ((self.raw.get("workplace") or "") + " " + (self.raw.get("location") or "")).lower()
        m, q = find(r"\b([1-5])\s*days?\s*(?:a|per)\s*(?:week|month)\s*(?:in|on|at)[- ]?(?:the )?(?:office|site|client)|"
                    r"\b(?:onsite|on-site|on site|in the office|in office)\s*([1-5])\s*days?", self.text)
        if m:
            n = m.group(1) or m.group(2)
            return self.put("attendance", f"Hybrid, {n} day{'s' if n != '1' else ''} onsite", "explicit", q)
        if "remote" in w:
            self.put("attendance", "Remote", "feed_field", f"Feed: {self.raw.get('workplace') or self.raw.get('location')}")
            m, q = find(r"occasional (?:travel|visits?)|ad[- ]hoc (?:travel|visits?)|travel to (?:the )?(?:office|client)", self.text)
            if m:
                self.issues.append("Remote, but advert mentions office or client visits: " + q)
            return "Remote"
        if "hybrid" in w:
            return self.put("attendance", "Hybrid", "feed_field", f"Feed: {self.raw.get('workplace') or self.raw.get('location')}")
        m, q = find(r"\bfully remote\b|\bremote[- ]first\b|\b100% remote\b|\bremote \(uk\)|\bremote,? uk\b|\bwork from home\b", self.text)
        if m:
            return self.put("attendance", "Remote", "explicit", q)
        m, q = find(r"\bhybrid\b", self.text)
        if m:
            return self.put("attendance", "Hybrid", "explicit", q)
        if "onsite" in w or "on-site" in w:
            return self.put("attendance", "On-site", "feed_field", f"Feed: {self.raw.get('workplace')}")
        return "Not stated"

    def country(self):
        loc = self.raw.get("location") or ""
        if UK_RX.search(loc) and not NON_UK_RX.search(loc):
            return "UK"
        if UK_RX.search(loc) and NON_UK_RX.search(loc):
            return "UK and elsewhere"
        if NON_UK_RX.search(loc):
            return "Outside UK"
        if re.search(r"remote", loc, re.I):
            m, q = find(r"(based|located|resident|right to work) in (the )?(uk|united kingdom)", self.text)
            if m:
                self.put("country_eligibility", "UK", "explicit", q)
                return "UK"
            self.issues.append("Remote role; country eligibility not stated")
        return "Unknown"


def label(raw):
    L = Labeller(raw)
    out = dict(engagement=L.engagement(), hours=L.hours(), ir35=L.ir35(), duration=L.duration(), start=L.start(),
               attendance=L.attendance(), country_eligibility=L.country())
    pay = L.pay()
    out.update(rate_min=pay[0], rate_max=pay[1], currency=pay[2], pay_period=pay[3]) if pay else \
        out.update(rate_min=None, rate_max=None, currency=None, pay_period=None)
    out["skills"] = [k for k, rx in SKILL_RX if rx.search(L.text)]
    out["family"] = family_of(raw.get("title", ""))
    for f, v in (("rate", pay), ("ir35", out["ir35"] if out["ir35"] != "Not stated" else None),
                 ("attendance", out["attendance"] if out["attendance"] != "Not stated" else None),
                 ("start", out["start"] if out["start"] != "Not stated" else None)):
        if not v:
            L.issues.append(f"{f.capitalize()} not stated")
    out["provenance"], out["issues"] = L.prov, L.issues
    return out
