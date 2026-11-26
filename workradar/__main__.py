"""python -m workradar <command>

  seed            create the test user + the Business Analyst search (test account only)
  discover        assess research/candidates.json (+ agencies seen on job boards), update the registry
  collect         one scheduled pass: due sources -> import -> dedupe -> alerts -> export
  check-now ID    queue a source (or 'all') for the next pass, then run it
  export          rewrite web/data/*.json and SOURCE_REGISTRY.csv
"""
import json
import sys
from pathlib import Path

from . import collector, db as D, discovery, export, notify

ROOT = Path(__file__).resolve().parent.parent
TEST_USER = "test-user"
BA_RULES = {"query": "business analyst", "related": False, "country": "UK", "where": "remote",
            "engagement": ["Day-rate contract"], "posted_days": 1, "ir35": "any"}


def seed(db):
    db.execute("INSERT OR IGNORE INTO users(user_id,name,email,is_test) VALUES(?,?,?,1)", (TEST_USER, "Test account", None))
    db.execute("INSERT OR IGNORE INTO searches(search_id,user_id,name,rules,delivery,baseline_at,created_at) VALUES(?,?,?,?,?,?,?)",
               ("ba-remote-contract", TEST_USER, "Business Analyst · UK · Contract · Remote · 24h", json.dumps(BA_RULES), "immediate",
                D.iso(D.now()), D.iso(D.now())))
    db.execute("INSERT OR IGNORE INTO searches(search_id,user_id,name,rules,delivery,baseline_at,created_at) VALUES(?,?,?,?,?,?,?)",
               ("ba-any-contract", TEST_USER, "Business Analyst · UK · Contract · any location · 7 days",
                json.dumps(dict(BA_RULES, where="any", posted_days=7)), "daily", D.iso(D.now()), D.iso(D.now())))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "collect"
    db = D.connect()
    if cmd == "seed":
        seed(db)
        print("seeded test account")
    elif cmd == "discover":
        cands = json.load(open(ROOT / "research" / "candidates.json"))
        cands += discovery.candidates_from_adverts(db)
        res = discovery.run(db, cands)
        from collections import Counter
        print(json.dumps(Counter(r["state"] for r in res["results"])), "moved:", res["moved"])
        export.write_all(db)
    elif cmd in ("collect", "check-now"):
        only = None
        if cmd == "check-now":
            target = argv[2] if len(argv) > 2 else "all"
            collector.request_check_now(db, target)
            only = None if target == "all" else [target]
        r = collector.run(db, trigger="manual" if cmd == "check-now" else "schedule", only=only,
                          detail_cache_path=str(D.db_path().parent / "reed_details.json"))
        links = collector.check_links(db)
        made = notify.create(db)
        sent = notify.deliver(db)
        db.execute("UPDATE runs SET notifications=? WHERE run_id=?", (made, r["run_id"]))
        export.write_all(db)
        print(json.dumps(dict(r, links=links, notifications=made, delivery=sent)))
    elif cmd == "export":
        print(export.write_all(db))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
