#!/usr/bin/env python3
"""
Payment advice pipeline: accounts@ mail -> local parsers -> pa_<customer> tables.

    python run.py                          # every feed, last 60 days, load to Supabase
    python run.py --feeds axis,icici --since 2026-04-01
    python run.py --feeds amazon --days 365 --dry-run --cache .cache --dump out.json
    python run.py --schema                 # print the DDL
    python run.py --apply-schema           # create / update the tables

A mail already settled in pa_mail_log is not fetched again, so a wide window
costs only a Gmail listing. --dry-run parses and checks totals without touching
the database (good for testing a parser against a year of mail).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import traceback
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from feeds import CUSTOMERS, FEEDS  # noqa: E402
from pa_common import Skip, advice_dict, gmail_service, iter_mails, to_jsonable  # noqa: E402

log = logging.getLogger("pa")
DEFAULT_DAYS = int(os.environ.get("PA_DAYS_BACK", "60"))


def run_feed(name: str, svc, conn, after: dt.date, args, refs=None) -> Counter:
    feed = FEEDS[name]
    parse = feed.load_parser()
    query = f"{feed.query} after:{after:%Y/%m/%d}"
    if args.before:
        query += f" before:{args.before:%Y/%m/%d}"
    skip = set()
    if conn is not None and not args.reparse:
        import db
        skip = db.done_mail_ids(conn, name, retry_failed=args.retry_failed)
    counts, per_customer, problems, dump = Counter(), defaultdict(Counter), [], []
    started = dt.datetime.now(dt.timezone.utc)
    n = 0
    for mail in iter_mails(svc, query, cache_dir=args.cache, skip_ids=skip):
        n += 1
        if args.limit and n > args.limit:
            break
        try:
            advices = parse(mail)
        except Skip as exc:
            counts["skipped"] += 1
            if conn is not None and not args.dry_run:
                import db
                db.log_mail(conn, name, mail, "skipped", str(exc))
            continue
        except Exception as exc:                         # noqa: BLE001 - one bad mail must not stop the feed
            counts["error"] += 1
            problems.append((mail.id, mail.subject[:70], "error", f"{type(exc).__name__}: {exc}"))
            log.debug(traceback.format_exc())
            if conn is not None and not args.dry_run:
                import db
                db.log_mail(conn, name, mail, "error", f"{type(exc).__name__}: {exc}")
            continue
        unknown = sorted({a.customer for a in advices} - set(CUSTOMERS))
        if unknown:                                      # a payer with no table yet: surface, don't stop the feed
            counts["error"] += 1
            problems.append((mail.id, mail.subject[:70], "error", f"unknown customer {unknown}"))
            if conn is not None and not args.dry_run:
                import db
                db.log_mail(conn, name, mail, "error", f"parser returned unknown customer {unknown}; add it to feeds.CUSTOMERS")
            continue
        if not advices:
            counts["skipped"] += 1
            if conn is not None and not args.dry_run:
                import db
                db.log_mail(conn, name, mail, "skipped", "parser found no advice")
            continue
        for idx, advice in enumerate(advices):
            status, diff = advice.check()
            if args.dry_run or conn is None:
                final = status if status != "ok" else "loaded"
            else:
                import db
                final = db.load_advice(conn, name, mail, idx, advice, refs)
            counts[final] += 1
            per_customer[advice.customer][final] += 1
            if final in ("mismatch", "no_total", "no_lines"):
                problems.append((mail.id, mail.subject[:70], final,
                                 f"{advice.source_file}: lines {advice.line_sum()} vs printed {advice.payment_amount}"
                                 f" ({len(advice.lines)} lines)"))
            if args.dump:
                dump.append({"feed": name, "mail_id": mail.id, "subject": mail.subject, "status": final,
                             "diff": diff, **advice_dict(advice)})
        if conn is not None and not args.dry_run:
            import db
            db.mark_mail_done(conn, name, mail, len(advices))
    ended = dt.datetime.now(dt.timezone.utc)

    print(f"\n[{name}] {n} new mails  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for cust, c in per_customer.items():
        print(f"    {CUSTOMERS[cust].table:20s} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    for p in problems[: args.show]:
        print("    !", " | ".join(p))
    if len(problems) > args.show:
        print(f"    ! ... {len(problems) - args.show} more")
    if args.dump:
        path = args.dump if len(args.feeds) == 1 else args.dump.replace(".json", f"_{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dump, fh, indent=1, default=to_jsonable)
    if conn is not None and not args.dry_run:
        import db
        db.log_run(conn, name, started, ended, dict(counts),
                   {"after": str(after), "problems": problems[:50]})
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feeds", default=",".join(FEEDS), help="comma list; default all: " + ",".join(FEEDS))
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--since", type=dt.date.fromisoformat, help="overrides --days")
    ap.add_argument("--before", type=dt.date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true", help="parse + check only, no database")
    ap.add_argument("--reparse", action="store_true", help="ignore pa_mail_log and re-read every mail in the window")
    ap.add_argument("--retry-failed", action="store_true", help="re-read mails whose advice failed its check")
    ap.add_argument("--cache", help="directory to cache raw mails in (testing)")
    ap.add_argument("--dump", help="write parsed advices to this JSON file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=15, help="problem lines to print per feed")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--apply-schema", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    args.feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]
    unknown = [f for f in args.feeds if f not in FEEDS]
    if unknown:
        ap.error(f"unknown feed(s): {unknown}")

    if args.schema:
        import db
        print(db.schema_sql())
        return 0
    conn = None
    if not args.dry_run:
        import db
        conn = db.connect(args.env_file)
        if args.apply_schema:
            db.apply_schema(conn)
            print("schema applied")
            if args.feeds == list(FEEDS) and not args.since and "--days" not in sys.argv:
                return 0
    after = args.since or (dt.date.today() - dt.timedelta(days=args.days))
    svc = gmail_service()
    refs = None
    if conn is not None:
        import db
        refs = db.RefIndex(conn)      # one index for the run: ril_rr and axis share pa_reliance
    total = Counter()
    for name in args.feeds:
        total += run_feed(name, svc, conn, after, args, refs)
    failed = sum(total[k] for k in ("mismatch", "no_total", "no_lines", "error"))
    print(f"\nTOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(total.items())))
    return 1 if failed and not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
