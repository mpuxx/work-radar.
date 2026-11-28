"""Fixture tests. Every record here is a fixture (is_fixture=1) in a throwaway database; none reach production."""
import datetime as dt
import json
import os
import tempfile
import unittest

from workradar import collector, db as D, dedupe, discovery, export, http, matching, notify

T0 = dt.datetime(2026, 11, 26, 9, 0, tzinfo=dt.timezone.utc)


def raw(i, title="Business Analyst", desc="Outside IR35. £550 per day. 6 month contract. Fully remote, UK based.", loc="Remote, UK", **kw):
    r = dict(source_job_id=str(i), title=title, description=desc, location=loc, employment_type="Contract", hours="", workplace="",
             pay_text="", published_at=None, published_precision=None, updated_at=None, job_url=f"https://example.org/jobs/{i}",
             apply_url=f"https://example.org/jobs/{i}/apply", employer_name=None, agency_name=None)
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = D.connect(os.path.join(self.tmp, "t.sqlite3"))
        for org, kind in (("acme", "employer"), ("agency-a", "agency"), ("agency-b", "agency")):
            self.db.execute("INSERT INTO organisations(org_id,name,kind) VALUES(?,?,?)", (org, org.title(), kind))
        for sid, org in (("fx:acme", "acme"), ("fx:a", "agency-a"), ("fx:b", "agency-b")):
            self.db.execute("INSERT INTO sources(source_id,org_id,connector,board_id,status,check_interval_min) VALUES(?,?,?,?,?,15)",
                            (sid, org, "fixture", sid, "active"))
        self.feeds = {}

    def fetcher(self, src, _cache=None):
        f = self.feeds.get(src["source_id"], [])
        if isinstance(f, Exception):
            raise f
        return f

    def run_(self, at=T0, only=None):
        return collector.run(self.db, trigger="test", at=at, fetcher=self.fetcher, only=only)

    def job(self, sid, i):
        return self.db.execute("SELECT * FROM jobs WHERE job_id=?", (D.sha(sid, str(i)),)).fetchone()


class Collector(Base):
    def test_01_lease_prevents_overlap(self):
        self.assertTrue(collector.lease(self.db, "fx:acme", T0))
        self.assertFalse(collector.lease(self.db, "fx:acme", T0 + dt.timedelta(minutes=5)))
        self.assertNotIn("fx:acme", [s["source_id"] for s in collector.due_sources(self.db, T0 + dt.timedelta(minutes=5))])
        self.assertTrue(collector.lease(self.db, "fx:acme", T0 + dt.timedelta(minutes=16)))  # crashed run's lease expires: recovery

    def test_02_failure_isolated_and_marked_stale(self):
        self.feeds = {"fx:acme": [raw(1)], "fx:a": [raw(2, agency_name="Agency A")]}
        self.run_()
        self.feeds["fx:acme"] = http.HttpError(500, "https://example.org")
        r = self.run_(T0 + dt.timedelta(minutes=20))
        self.assertEqual(r["failed"], 1)
        self.assertEqual(self.job("fx:acme", 1)["stale"], 1)
        self.assertEqual(self.job("fx:acme", 1)["listing_status"], "listed")  # failure never means "closed"
        self.assertEqual(self.job("fx:a", 2)["stale"], 0)
        s = self.db.execute("SELECT * FROM sources WHERE source_id='fx:acme'").fetchone()
        self.assertEqual(s["health"], "degraded")
        self.assertGreater(D.parse(s["next_check_at"]), T0 + dt.timedelta(minutes=40))  # exponential backoff

    def test_03_invalid_feed_does_not_change_listing(self):
        from workradar.connectors import InvalidFeed
        self.feeds = {"fx:acme": [raw(1), raw(2, title="Delivery Manager")]}
        self.run_()
        self.feeds["fx:acme"] = InvalidFeed("no jobs list")
        self.run_(T0 + dt.timedelta(hours=1))
        self.assertEqual(self.job("fx:acme", 2)["listing_status"], "listed")

    def test_04_retry_after_respected(self):
        self.feeds = {"fx:acme": http.RateLimited(3600, "https://example.org")}
        self.run_()
        s = self.db.execute("SELECT * FROM sources WHERE source_id='fx:acme'").fetchone()
        self.assertEqual(s["health"], "rate_limited")
        self.assertGreaterEqual(D.parse(s["next_check_at"]), T0 + dt.timedelta(minutes=60))

    def test_05_first_detected_vs_published(self):
        self.feeds = {"fx:acme": [raw(1, updated_at="2026-11-26T08:00:00+00:00")]}
        self.run_()
        j = self.job("fx:acme", 1)
        self.assertIsNone(j["published_at"])  # an "updated" date is never the posting date
        self.assertEqual(j["first_detected_at"], D.iso(T0))
        self.run_(T0 + dt.timedelta(hours=1))
        self.assertEqual(self.job("fx:acme", 1)["first_detected_at"], D.iso(T0))

    def test_06_changes_tracked_and_relisting(self):
        self.feeds = {"fx:acme": [raw(1)]}
        self.run_()
        self.feeds["fx:acme"] = [raw(1, desc="Outside IR35. £600 per day. 6 month contract. Fully remote, UK based.")]
        self.run_(T0 + dt.timedelta(hours=1))
        self.feeds["fx:acme"] = []
        self.run_(T0 + dt.timedelta(hours=2))
        self.assertEqual(self.job("fx:acme", 1)["listing_status"], "no_longer_listed")
        self.feeds["fx:acme"] = [raw(1)]
        self.run_(T0 + dt.timedelta(hours=3))
        kinds = [r[0] for r in self.db.execute("SELECT kind FROM job_changes WHERE job_id=? ORDER BY change_id", (D.sha("fx:acme", "1"),))]
        self.assertEqual(kinds[:3], ["detected", "rate", "no_longer_listed"])
        self.assertIn("reappeared", kinds)
        self.assertEqual(self.job("fx:acme", 1)["listing_status"], "listed")

    def test_06b_rotating_search_keeps_unseen_jobs_for_72h(self):
        # A job board searched in batches of terms: a job missing from one batch is not gone.
        self.db.execute("UPDATE sources SET connector='reed' WHERE source_id='fx:b'")
        self.feeds = {"fx:b": [raw(1), raw(2, title="Scrum Master")]}
        self.run_()
        self.feeds["fx:b"] = [raw(2, title="Scrum Master")]
        self.run_(T0 + dt.timedelta(hours=2))
        self.assertEqual(self.job("fx:b", 1)["listing_status"], "listed")
        self.run_(T0 + dt.timedelta(hours=73))
        self.assertEqual(self.job("fx:b", 1)["listing_status"], "no_longer_listed")
        self.assertEqual(self.job("fx:b", 2)["listing_status"], "listed")

    def test_07_daily_cap(self):
        self.db.execute("UPDATE sources SET daily_request_cap=1 WHERE source_id='fx:acme'")
        D.add_usage(self.db, "requests:fx:acme", 1)
        r = collector.run(self.db, trigger="test", fetcher=self.fetcher, only=["fx:acme"])
        self.assertEqual(r["due"], 0)


class Dedupe(Base):
    def test_08_same_employer_across_sources_grouped(self):
        self.feeds = {"fx:acme": [raw(1, employer_name="Acme Ltd")], "fx:a": [raw(9, employer_name="Acme", agency_name="Agency A")]}
        self.run_()
        self.assertIsNotNone(self.job("fx:acme", 1)["dup_group"])
        self.assertEqual(self.job("fx:acme", 1)["dup_group"], self.job("fx:a", 9)["dup_group"])

    def test_09_agencies_hiding_client_not_merged(self):
        self.feeds = {"fx:a": [raw(1, agency_name="Agency A")], "fx:b": [raw(1, agency_name="Agency B")]}
        self.run_()
        self.assertIsNone(self.job("fx:a", 1)["dup_group"])
        self.assertIsNone(self.job("fx:b", 1)["dup_group"])


class Matching(Base):
    RULES = {"query": "business analyst", "country": "UK", "where": "remote", "engagement": ["day"], "posted_days": 1, "ir35": "outside"}

    def test_10_unknowns_never_pass(self):
        self.feeds = {"fx:acme": [raw(1, published_at="2026-11-26T08:00:00+00:00", published_precision="time"),
                                  raw(2, desc="Contract. Remote.", published_at="2026-11-26T08:00:00+00:00", published_precision="time"),
                                  raw(3)]}
        self.run_()
        res = matching.search(self.db, self.RULES, now=T0 + dt.timedelta(hours=1))
        self.assertEqual([j["source_job_id"] for j, _, _ in res["match"]], ["1"])
        self.assertEqual(sorted(j["source_job_id"] for j, _, _ in res["unknown"]), ["2", "3"])

    def test_11_remote_uk_and_phrase_title(self):
        self.feeds = {"fx:acme": [raw(1, loc="Remote, United States", published_at="2026-11-26T08:00:00+00:00", published_precision="time"),
                                  raw(2, title="Business Analytics Lead", published_at="2026-11-26T08:00:00+00:00", published_precision="time")]}
        self.run_()
        res = matching.search(self.db, self.RULES, now=T0 + dt.timedelta(hours=1))
        self.assertEqual(res["match"], [])


class Alerts(Base):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO users(user_id,name,is_test) VALUES('u','Test',1)")
        self.db.execute("INSERT INTO searches(search_id,user_id,rules,delivery,baseline_at,created_at) VALUES('s','u',?, 'immediate', ?, ?)",
                        (json.dumps(Matching.RULES), D.iso(T0), D.iso(T0)))

    def test_12_baseline_and_idempotent(self):
        self.feeds = {"fx:acme": [raw(1, published_at="2026-11-26T08:00:00+00:00", published_precision="time")]}
        self.run_(T0 - dt.timedelta(minutes=5))  # before baseline: never alerted
        self.feeds["fx:acme"].append(raw(2, published_at="2026-11-26T09:00:00+00:00", published_precision="time"))
        self.feeds["fx:a"] = [raw(7, employer_name="Acme", agency_name="Agency A", published_at="2026-11-26T09:00:00+00:00", published_precision="time")]
        self.feeds["fx:acme"][1]["employer_name"] = "Acme"
        self.run_(T0 + dt.timedelta(minutes=30))
        now = T0 + dt.timedelta(minutes=40)
        self.assertEqual(notify.create(self.db, now), 1)  # job 2 and its agency copy alert once
        self.assertEqual(notify.create(self.db, now), 0)

    def test_13_quiet_hours_then_delivery(self):
        self.feeds = {"fx:acme": [raw(1, published_at="2026-11-26T22:00:00+00:00", published_precision="time")]}
        night = dt.datetime(2026, 11, 26, 22, 30, tzinfo=dt.timezone.utc)
        self.run_(night)
        notify.create(self.db, night)
        self.assertEqual(notify.deliver(self.db, night, email=False)["held"], 1)
        morning = dt.datetime(2026, 11, 27, 7, 5, tzinfo=dt.timezone.utc)
        self.assertEqual(notify.deliver(self.db, morning, email=False)["delivered"], 1)

    def test_14_pause_blocks_alerts(self):
        self.db.execute("UPDATE users SET paused=1 WHERE user_id='u'")
        self.feeds = {"fx:acme": [raw(1, published_at="2026-11-26T09:30:00+00:00", published_precision="time")]}
        self.run_(T0 + dt.timedelta(minutes=30))
        self.assertEqual(notify.create(self.db, T0 + dt.timedelta(minutes=40)), 0)


class Safety(Base):
    def test_15_excluded_destinations(self):
        self.assertEqual(discovery.assess({"name": "Freelance Radar", "hints": []})["state"], "rejected")

    def test_16_fixtures_never_exported(self):
        self.feeds = {"fx:acme": [raw(1)]}
        self.run_()
        self.assertEqual(len(export.jobs(self.db)), 1)
        self.db.execute("UPDATE jobs SET is_fixture=1")
        self.assertEqual(export.jobs(self.db), [])




class Feeds(unittest.TestCase):
    def test_17_agency_xml_feed(self):
        from unittest import mock
        from workradar import connectors
        xml = """<?xml version="1.0" encoding="utf-8"?><source><publisher><![CDATA[Example Agency]]></publisher>
          <job><title><![CDATA[Business Analyst]]></title><date><![CDATA[2026-11-20T10:00:00Z]]></date>
          <referencenumber><![CDATA[123]]></referencenumber><url><![CDATA[https://example.com/jobs/ba-123?source=Indeed]]></url>
          <company><![CDATA[Example Agency]]></company><city><![CDATA[London]]></city><country><![CDATA[GB]]></country>
          <description><![CDATA[<p>6 month contract, outside IR35, £550 per day.</p>]]></description></job></source>"""
        with mock.patch.object(connectors.http, "get_text", return_value=(xml, {})):
            jobs = connectors.xmlfeed({"board_id": "https://example.com/jobs.xml", "org_name": "Example Agency", "org_kind": "agency"})
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["title"], "Business Analyst")
        self.assertEqual(j["job_url"], "https://example.com/jobs/ba-123")
        self.assertEqual(j["agency_name"], "Example Agency")
        self.assertTrue(j["published_at"].startswith("2026-11-20"))


class FullAdverts(unittest.TestCase):
    def test_19_smartrecruiters_full_advert_cached(self):
        from workradar import connectors
        lst = {"content": [{"id": "1", "name": "Business Analyst", "location": {"city": "London", "country": "gb"}}], "totalFound": 1}
        det = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Full advert here</p>"}}}}
        calls = []
        def get(u, **k):
            calls.append(u)
            return det if "/postings/1" in u else lst
        cache = {}
        j = connectors.smartrecruiters({"board_id": "Acme", "org_name": "Acme"}, get=get, detail_cache=cache)
        self.assertEqual(j[0]["description"], "Full advert here")
        connectors.smartrecruiters({"board_id": "Acme", "org_name": "Acme"}, get=get, detail_cache=cache)
        self.assertEqual(sum("/postings/1" in u for u in calls), 1)   # fetched once, then cached

    def test_20_rss_summary_replaced_by_page_jobposting(self):
        from unittest import mock
        from workradar import connectors
        feed = """<?xml version="1.0"?><rss><channel><item><title>Business Analyst</title><link>https://ex.com/job/1</link>
                  <guid>1</guid><description>Short</description></item></channel></rss>"""
        page = '<script type="application/ld+json">{"@type":"JobPosting","description":"<p>Long full advert text</p>"}</script>'
        gt = lambda u, **k: (feed, {}) if u == "https://ex.com/feed" else (page, {})
        with mock.patch.object(connectors.http, "get_text", side_effect=gt), mock.patch("workradar.discovery.robots_ok", return_value=True):
            j = connectors.rss({"board_id": "https://ex.com/feed", "org_name": "Ex", "org_kind": "agency"}, detail_cache={})
        self.assertEqual(j[0]["description"], "Long full advert text")
        with mock.patch.object(connectors.http, "get_text", side_effect=gt), mock.patch("workradar.discovery.robots_ok", return_value=False):
            j = connectors.rss({"board_id": "https://ex.com/feed", "org_name": "Ex", "org_kind": "agency"}, detail_cache={})
        self.assertEqual(j[0]["description"], "Short")   # robots.txt says no: keep the feed's own summary


class Links(unittest.TestCase):
    def test_21_non_ascii_link_is_encoded(self):
        self.assertEqual(http._ascii_url("https://ex.com/jobs/ux\u30fbdesigner?q=\u00e9"), "https://ex.com/jobs/ux%E3%83%BBdesigner?q=%C3%A9")


class Clearance(unittest.TestCase):
    def test_22_security_clearance_tag(self):
        from workradar.extract import clearance as c
        self.assertEqual(c("Business Analyst (SC Cleared)", ""), "SC")
        self.assertEqual(c("BA", "Candidates must hold active SC clearance."), "SC")
        self.assertEqual(c("BA", "You must be eligible for DV security clearance."), "DV")
        self.assertEqual(c("BA", "SC or above is required; DV is a bonus."), "SC")
        self.assertEqual(c("BA", "BPSS checks apply."), "BPSS")
        self.assertEqual(c("BA", "Describe the scope of work and discuss options."), "")


if __name__ == "__main__":
    unittest.main()
