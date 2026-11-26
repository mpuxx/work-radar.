"""Look for public job feeds on agency and job-board sites. Read-only: robots.txt, the home/jobs page and a few common feed paths.
Run on the GitHub runner (python tools/probe.py domain ...). Prints one summary block per domain."""
import re
import sys
import urllib.request
import urllib.robotparser

UA = "WorkRadarBot/1.0 (+https://mpuxx.github.io/work-radar./)"
PATHS = ["/", "/jobs", "/jobs/", "/vacancies", "/feed", "/jobs/feed", "/rss", "/jobs/rss", "/jobs.rss", "/feed/jobs", "/job-feed", "/rss/jobs",
         "/jobs.xml", "/jobs/rss.xml", "/sitemap.xml", "/sitemap_index.xml", "/api/jobs"]
PLATFORMS = ["volcanic", "firefish", "jobadder", "bullhorn", "broadbean", "idibu", "eploy", "workable", "greenhouse", "lever.co", "ashbyhq",
             "pinpointhq", "recruitee", "teamtailor", "vincere", "itris", "mercury", "access recruitment", "jobsoid", "recruitcrm", "zoho",
             "wp-job-manager", "job_listing", "JobPosting", "smartrecruiters", "jazzhr", "manatal", "loxo", "tracker-rms", "staffing"]


def get(url, n=200_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("content-type", ""), r.read(n).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", "", url
    except Exception as e:
        return 0, type(e).__name__, "", url


def probe(domain):
    base = "https://" + domain
    out = [f"=== {domain}"]
    st, _, robots, _ = get(base + "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots.splitlines() if st == 200 else [])
    out.append(f"robots {st}; bot may fetch /jobs: {rp.can_fetch(UA, base + '/jobs')}; sitemaps: {re.findall(r'(?im)^sitemap:\s*(\S+)', robots)[:4]}")
    for p in PATHS:
        if not rp.can_fetch(UA, base + p):
            out.append(f"  {p}: disallowed by robots")
            continue
        st, ct, body, final = get(base + p)
        if st != 200:
            if p == "/":
                out.append(f"  /: {st} {ct}")
            continue
        feedish = ("xml" in ct or "rss" in ct or "json" in ct or body.lstrip().startswith("<?xml") or "<rss" in body[:500])
        items = len(re.findall(r"<item>|<entry>|<url>", body))
        links = re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', body)[:3]
        plat = sorted({k for k in PLATFORMS if k.lower() in body.lower()})
        jl = body.count('"JobPosting"')
        if p in ("/", "/jobs", "/jobs/", "/vacancies") or feedish:
            out.append(f"  {p} -> {final} {st} {ct[:30]} items={items} jsonld_jobs={jl} feeds={links} platforms={plat}")
            jobsm = [u for u in re.findall(r"<loc>([^<]+)</loc>", body) if re.search(r"job|vacan", u, re.I)][:3]
            if jobsm:
                out.append(f"     job urls in sitemap: {jobsm}")
    print("\n".join(out), flush=True)


if __name__ == "__main__":
    for d in sys.argv[1:]:
        probe(d)
