# Operations

## What runs, and when
| Workflow | Schedule (UTC) | What it does |
|---|---|---|
| `collect` | every 15 minutes | due sources → fetch → import → dedupe → alerts → export → Pages |
| `discover` | Monday 05:20 | re-assesses candidates, adds agencies seen on job boards, flags moved boards |
| `test` | every push | 16 fixture tests |

GitHub can start scheduled runs a few minutes late, and pauses schedules on repos with no activity for 60 days.
The state branch gets a commit every run, which counts as activity.

## Check now
Actions → collect → Run workflow. Leave "source" empty for all, or enter a source id (e.g. `greenhouse:monzo`).
The app's "Check now" button opens that page.

## How the queue protects sources
- Each source has `check_interval_min` (15 for ATS APIs, 60 Workable/JSON-LD, 120 Reed, 360 Jooble).
- A run leases each source for 15 minutes, so two runs never fetch the same source. A crashed run's leases expire.
- At most 2 requests in flight per host. `Retry-After` is obeyed; long waits move the source's next check instead of sleeping.
- Failures back off exponentially (15 → 30 → 60 min … max 24 h). 3 failures in a row = `failing`.
- Daily caps: Jooble 60 requests, Reed 900.
- One source failing never touches another source's jobs. Its own jobs stay listed but are marked `stale`.

## State
The SQLite database and SOURCE_REGISTRY.csv live on the `state` branch (one commit, force-pushed each run, so it never grows).
To start fresh: delete the `state` branch. The next run seeds and runs discovery again.

## Health states
ok · empty (feed works, 0 jobs) · degraded (1–2 failures) · failing (3+) · rate_limited · unknown (not checked yet).
See the app's **Sources** tab, or `web/data/status.json`.

## Common fixes
| Symptom | Fix |
|---|---|
| Reed or Jooble "API_KEY not set" | Add the secret (Settings → Secrets and variables → Actions) |
| Reed "HTTP 403" | Reed blocks some IP ranges; it retries with backoff. If it persists for days, ask Reed to allow GitHub Actions |
| A board returns 404 for 3 runs | Weekly discovery moves it to review; check the employer's careers page for a new board |
| Pages shows an old check | Look at the latest `collect` run; the deploy step needs Pages set to "GitHub Actions" |

## Adding a source
Add it to `research/candidates.json` (name, kind, domain, hints like `greenhouse:slug`, origin), then run `discover`.
It is activated only if the live feed works, the identity matches, and it has relevant UK roles.
