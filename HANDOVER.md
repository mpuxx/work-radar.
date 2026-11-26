# Handover

## What this is
Work Radar collects UK roles in business analysis, delivery and programme, digital and CMS, ProductOps/DesignOps/service design,
and AI/automation/transformation, from official public feeds. It labels each advert with evidence, removes duplicates,
tracks changes, runs strict saved searches and raises in-app alerts. It runs itself on GitHub Actions every 15 minutes.

- Repo: https://github.com/mpuxx/work-radar.  (the name ends with a dot; renaming it to `work-radar` is fine, update the links in web/index.html)
- App: GitHub Pages for this repo (Find work · My workspace · Sources)
- State: `state` branch (database + latest SOURCE_REGISTRY.csv)

## Code map
| File | Job |
|---|---|
| `workradar/connectors.py` | one function per platform: Greenhouse, Lever (incl. EU), Ashby, SmartRecruiters, Workable, Recruitee, Personio XML, JSON-LD JobPosting, RSS, Reed, Jooble, Contracts Finder |
| `workradar/http.py` | polite HTTP: 2 per host, Retry-After, backoff, request counts |
| `workradar/extract.py` | labels with provenance: engagement, hours, rate, IR35, duration, start, attendance, UK eligibility, skills, family |
| `workradar/collector.py` | queue, leases, caps, atomic per-source import, change tracking, stale marking, link checks |
| `workradar/dedupe.py` | cross-source groups (agency adverts with a hidden client are never merged across agencies), reposts |
| `workradar/matching.py` | strict rules: pass / fail / unknown. Unknown never counts as a match |
| `workradar/notify.py` | baseline, idempotent ids, quiet hours, pause, immediate/daily/weekly, delivery tracking |
| `workradar/discovery.py` | detect platform, validate live, identity, relevance, then activate / review / unsupported / rejected |
| `workradar/export.py` | `web/data/*.json` and SOURCE_REGISTRY.csv |
| `migrations/` | schema, applied automatically |
| `web/index.html` | the app |
| `tests/` | fixture tests |

## Accuracy rules built in
- Structured feed fields first, advert text second; every label keeps the words it came from (`provenance`).
- Unknown stays unknown. Conflicts (e.g. inside and outside IR35 both stated) are recorded, not guessed.
- "Updated" dates are never shown as posting dates. First detection is stored separately.
- A failed or malformed fetch never marks jobs as gone. "No longer listed" is not "closed".
- Agencies and employers are kept apart; an agency advert without a client name has no employer.

## Accounts and secrets
- `REED_API_KEY`, `JOOBLE_API_KEY`: GitHub Actions secrets. Not in any file.
- Test account: `test-user` with two Business Analyst searches. No real user receives anything.

## Known limits
- Most recruitment agencies run their own sites (often Broadbean/Volcanic). They are scanned on the runner for a
  public feed or JobPosting data; where none exists they are covered through Reed and Jooble adverts instead.
- Workday, SuccessFactors, Taleo, iCIMS and similar have no public jobs API. These employers are marked
  `unsupported` with the integration needed.
- LinkedIn is not collected (terms).
- Coverage is the sources in the registry, not the whole UK market.
