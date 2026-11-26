-- Work Radar schema v1. Applied once by workradar.db.migrate(); recorded in schema_migrations.

CREATE TABLE IF NOT EXISTS organisations (
  org_id        TEXT PRIMARY KEY,          -- slug of the legal/trading name, e.g. "bbc"
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('employer','agency','job_board','public_sector')),
  domain        TEXT,                      -- official domain, e.g. bbc.co.uk
  parent_org_id TEXT REFERENCES organisations(org_id),  -- subsidiaries keep their own identity
  former_names  TEXT DEFAULT '[]',         -- JSON list, for rebrands
  sectors       TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sources (
  source_id          TEXT PRIMARY KEY,     -- connector:board, e.g. greenhouse:monzo
  org_id             TEXT NOT NULL REFERENCES organisations(org_id),
  connector          TEXT NOT NULL,        -- greenhouse|lever|ashby|smartrecruiters|workable|recruitee|personio|reed|jooble|jsonld|rss|contractsfinder
  board_id           TEXT,                 -- external board identifier or feed URL
  region             TEXT DEFAULT 'global',
  careers_url        TEXT,
  host               TEXT,                 -- API host, for per-host concurrency
  discovery_origin   TEXT,                 -- e.g. "web search: 'outside IR35 business analyst' 2026-11-26"
  discovered_at      TEXT,
  access_conditions  TEXT,                 -- documented API / robots / quota notes
  check_interval_min INTEGER NOT NULL DEFAULT 15,
  daily_request_cap  INTEGER,              -- e.g. Jooble 100/day
  status             TEXT NOT NULL DEFAULT 'review'
                     CHECK (status IN ('active','paused','review','unsupported','rejected')),
  status_reason      TEXT,
  relevance          TEXT,                 -- JSON: {score, relevant_titles, uk_jobs, checked_at}
  last_attempt_at    TEXT,
  last_success_at    TEXT,
  next_check_at      TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  health             TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (health IN ('unknown','ok','empty','degraded','failing','rate_limited')),
  last_error         TEXT,
  last_job_count     INTEGER,
  lease_until        TEXT,                 -- prevents overlapping runs for the same source
  check_now_requested_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id           TEXT PRIMARY KEY,       -- sha1(source_id + source_job_id): stable across runs
  source_id        TEXT NOT NULL REFERENCES sources(source_id),
  source_job_id    TEXT NOT NULL,
  employer_org     TEXT,                   -- hiring employer, when known
  employer_name    TEXT,                   -- as advertised; NULL when an agency hides the client
  agency_org       TEXT,                   -- recruitment agency, when the advert is an agency's
  agency_name      TEXT,
  title            TEXT NOT NULL,
  description      TEXT,
  location         TEXT,
  country_eligibility TEXT,                -- e.g. "UK", "Unknown"
  attendance       TEXT,                   -- Remote | Hybrid, N days onsite | On-site | Not stated
  engagement       TEXT,                   -- Day-rate contract | Fixed-term employment | Temporary | Permanent | Contract, type not stated | Not stated
  hours            TEXT,
  rate_min REAL, rate_max REAL, currency TEXT, pay_period TEXT,
  ir35             TEXT,
  duration         TEXT,
  start            TEXT,
  job_url          TEXT,
  apply_url        TEXT,
  published_at     TEXT,                   -- source publication date (NULL if unknown)
  published_precision TEXT,                -- time | date | NULL
  source_updated_at TEXT,                  -- updated/republished: never treated as published
  first_detected_at TEXT NOT NULL,
  last_observed_at  TEXT NOT NULL,
  listing_status   TEXT NOT NULL DEFAULT 'listed'
                   CHECK (listing_status IN ('listed','no_longer_listed','closed_confirmed')),
  stale            INTEGER NOT NULL DEFAULT 0,   -- 1 when the source failed at the last attempt
  apply_check_status TEXT,                 -- ok | broken | blocked | unchecked
  apply_checked_at TEXT,
  content_hash     TEXT,
  provenance       TEXT,                   -- JSON {field: {value, method: explicit|feed_field|inferred, quote}}
  issues           TEXT,                   -- JSON list of missing/ambiguous/conflicting notes
  skills           TEXT,                   -- JSON list
  family           TEXT,
  dup_group        TEXT,                   -- probable cross-source duplicate group
  repost_of        TEXT,                   -- job_id this looks like a repost of
  is_fixture       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_key ON jobs(source_id, source_job_id);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(listing_status);

CREATE TABLE IF NOT EXISTS job_changes (
  change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL REFERENCES jobs(job_id),
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,    -- detected | rate | attendance | start | engagement | no_longer_listed | reappeared | repost
  old_value   TEXT,
  new_value   TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log (
  fetch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id   TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  ok          INTEGER,
  http_status INTEGER,
  requests    INTEGER DEFAULT 0,
  jobs_seen   INTEGER,
  new_jobs    INTEGER,
  duration_ms INTEGER,
  error       TEXT,
  run_id      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,           -- collect | discover | test
  trigger     TEXT,                    -- schedule | manual | test
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  sources_due INTEGER, sources_ok INTEGER, sources_failed INTEGER,
  new_jobs    INTEGER, notifications INTEGER,
  host        TEXT
);

CREATE TABLE IF NOT EXISTS users (
  user_id     TEXT PRIMARY KEY,
  name        TEXT,
  email       TEXT,
  is_test     INTEGER NOT NULL DEFAULT 0,
  quiet_start TEXT DEFAULT '21:00',
  quiet_end   TEXT DEFAULT '07:00',
  timezone    TEXT DEFAULT 'Europe/London',
  paused      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS searches (
  search_id   TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(user_id),
  name        TEXT,
  rules       TEXT NOT NULL,           -- JSON, see matching.py
  delivery    TEXT NOT NULL DEFAULT 'daily' CHECK (delivery IN ('immediate','daily','weekly')),
  paused      INTEGER NOT NULL DEFAULT 0,
  baseline_at TEXT,                    -- jobs matched before this are not alerted
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  notification_id TEXT PRIMARY KEY,    -- sha1(search_id + job dup group): idempotent
  user_id     TEXT NOT NULL,
  search_id   TEXT NOT NULL,
  job_id      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  reasons     TEXT,                    -- JSON list of why it matched
  channel     TEXT NOT NULL DEFAULT 'in_app',
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','held_quiet_hours','delivered','failed','read')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  last_error  TEXT
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
  candidate_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT,
  domain      TEXT,
  careers_url TEXT,
  origin      TEXT,
  found_at    TEXT,
  detected_connector TEXT,
  detected_board TEXT,
  state       TEXT NOT NULL DEFAULT 'new'
              CHECK (state IN ('new','assessed','activated','review','unsupported','rejected')),
  reason      TEXT,
  assessed_at TEXT
);

CREATE TABLE IF NOT EXISTS usage (
  day         TEXT NOT NULL,
  bucket      TEXT NOT NULL,           -- requests:<host> | ai_tokens | search_calls
  amount      REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (day, bucket)
);
