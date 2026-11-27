"""
Self-registration in Birbal's pipeline catalog (the Master board, /master; Birbal migration 403).

Every run tells app.register_pipeline() what this pipeline knows about itself -- the inbox it reads, the Gmail query,
the parser, the script, the tables it writes, its workflow_logs source -- so a feed added to feeds.py (or a new
loader that calls register()) shows up on the Master board on its first run, with no hand-written catalog change.
The catalog keeps whatever people wrote (notes, downstream, party text); only machine-known facts follow the code.

Never fatal: a catalog that cannot be reached must not stop a load.
"""

from __future__ import annotations

import json
import sys

REPO = "keyur-tbd/payment_advice_scheduler"
ADVICE_RUNS_ON = "GitHub Actions: Payment advice to Supabase (payment_advice.yml)"
ADVICE_SCHEDULE = "cron '30 4,9,14 * * *' UTC = 10:00, 15:00, 20:00 IST"
# feeds loaded by their own script (Feed.mail = False), not by run.py
OWN_SCRIPT = {"instamart_portal": "load_portal.py (portal_fetch.py signs in)", "ril_portal": "load_ril_portal.py",
              "zepto_portal": "load_zepto_portal.py"}
OWN_RUNS_ON = {"instamart_portal": "GitHub Actions: Instamart portal (instamart_portal.yml)",
               "ril_portal": "GitHub Actions: Reliance SRM portal to Supabase (ril_portal.yml)",
               "zepto_portal": "GitHub Actions: Payment advice to Supabase, step 'Zepto portal PDFs (Drive)'"}


def register(conn, entry: dict) -> None:
    """Upsert one catalog entry; prints and carries on if the catalog is unavailable."""
    try:
        with conn.cursor() as cur:
            cur.execute("select app.register_pipeline(%s::jsonb)", (json.dumps(entry),))
        conn.commit()
    except Exception as exc:   # noqa: BLE001 - the load matters more than its catalog entry
        try:
            conn.rollback()
        except Exception:      # noqa: BLE001
            pass
        print(f"catalog: could not register {entry.get('feed_key')}: {type(exc).__name__}: {exc}", file=sys.stderr)


def advice_entry(name: str) -> dict:
    """What feeds.py knows about one payment-advice feed."""
    from feeds import CUSTOMERS, FEEDS
    feed = FEEDS[name]
    cust = CUSTOMERS.get(name)
    tables = ([f"public.{cust.table}"] if cust else []) + ["public.pa_mail_log"]
    entry = {
        "feed_key": f"pa_{name}",
        "grp": "Payment advice",
        "mailbox": f"{feed.mailbox}@thebakersdozen.in",
        "parser": f"parsers/{feed.parser}.py",
        "repo": REPO,
        "script": "run.py" if feed.mail else OWN_SCRIPT.get(name),
        "runs_on": ADVICE_RUNS_ON if feed.mail else OWN_RUNS_ON.get(name),
        "schedule": ADVICE_SCHEDULE if feed.mail else None,
        "target_tables": tables,
        "log_source": f"pa_{name}",
    }
    if feed.mail:
        entry.update(source_kind="mail", how_found=f"Gmail ({feed.mailbox}@): {feed.query} + the run's date window")
    if cust:
        entry["party"] = f"{cust.label} ({', '.join(cust.codes)})" if cust.codes else cust.label
    return entry


def register_advice_feed(conn, name: str) -> None:
    from feeds import FEEDS
    if name in FEEDS:
        register(conn, advice_entry(name))
