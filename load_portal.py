#!/usr/bin/env python3
"""
Load the Instamart vendor portal's two CSV exports into pa_instamart (feed 'instamart_portal').

    python load_portal.py Payment_Report.csv Payment_adjustment_at_Invoice_level.csv --dry-run
    python load_portal.py Payment_Report.csv Payment_adjustment_at_Invoice_level.csv --env-file D:/.../.env
    python load_portal.py --fetch                  # sign in headless (portal_fetch.py), export both, load

By hand: export both reports from vendors.zohocreatorportal.in (export icon -> CSV, all filters 'All').
--fetch does the same with INSTAMART_PORTAL_EMAIL / INSTAMART_PORTAL_PASSWORD (instamart_portal.yml, daily),
and by default reloads only payments from the start of the month three months back. Re-loading a newer export is safe: each
calendar month of payments is one pseudo-mail ('portal-YYYY-MM'), so a re-export rewrites that month's rows
in place, and the advice_ref (UTR) de-duplicates against the Zoho Books (wins) and VCredits (gives way) copies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pa_common import Mail  # noqa: E402
from parsers.instamart_portal import parse_exports  # noqa: E402

FEED = "instamart_portal"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payments", nargs="?", help="Payment_Report.csv")
    ap.add_argument("invoices", nargs="?", help="Payment_adjustment_at_Invoice_level.csv")
    ap.add_argument("--fetch", action="store_true", help="download both exports first (portal_fetch.py)")
    ap.add_argument("--out", default="portal_exports", help="--fetch: where the exports go")
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None,
                    help="first payment date to load (default 2024-04-01, when the ERP starts; "
                         "with --fetch the 1st of the month three months back)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()
    if args.fetch:
        import portal_fetch
        # the Debit Note Report rides along in the same sign-in; load_debit_notes.py loads it (workflow next step)
        args.payments, args.invoices = portal_fetch.fetch(args.out, *portal_fetch.credentials(args.env_file),
                                                          optional=portal_fetch.DEBIT_NOTES)
        if args.since is None:
            t = dt.date.today()
            m = t.year * 12 + t.month - 1 - 3
            args.since = dt.date(m // 12, m % 12 + 1, 1)
    elif not (args.payments and args.invoices):
        ap.error("give both CSV files, or --fetch")
    if args.since is None:
        args.since = dt.date(2024, 4, 1)

    with open(args.payments, "rb") as fh:
        pay = fh.read()
    with open(args.invoices, "rb") as fh:
        inv = fh.read()
    advices, stats = parse_exports(pay, inv, since=args.since, source_file=os.path.basename(args.payments))
    print("parsed:", "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))

    by_month = defaultdict(list)
    for a in advices:
        by_month[f"{a.payment_date:%Y-%m}"].append(a)
    counts = Counter()
    if args.dry_run:
        for a in advices:
            counts[a.check()[0]] += 1
        print("dry run:", "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return 0

    import db
    conn = db.connect(args.env_file)
    refs = db.RefIndex(conn)
    started = dt.datetime.now(dt.timezone.utc)
    stamp = dt.datetime.fromtimestamp(os.path.getmtime(args.payments), dt.timezone.utc)
    for month, items in sorted(by_month.items()):
        mail = Mail(id=f"portal-{month}", thread_id="", date=stamp, sender="vendors.zohocreatorportal.in",
                    subject=f"Instamart vendor portal export, payments of {month}")
        for idx, advice in enumerate(items):
            counts[db.load_advice(conn, FEED, mail, idx, advice, refs)] += 1
        if dt.date(int(month[:4]), int(month[5:]), 1) >= args.since:
            # The export holds this whole month, so a reference it no longer lists was renamed on the portal:
            # Instamart first types a placeholder ('3zvaOXQni3JCGf26dE4ezF | 4EPLRREIYVDRE', 18 Nov 2026) and
            # later the bank UTR. Without this the placeholder copy stays and the payment counts twice.
            keep = sorted({a.advice_ref for a in items if a.advice_ref})
            with conn.cursor() as cur:
                db._queue(conn, cur.mogrify(
                    f"delete from public.{db.CUSTOMERS['instamart'].table} where feed = %s and mail_id = %s "
                    f"and split_part(advice_ref, '@', 1) <> all(%s)", (FEED, mail.id, keep)))
        db.mark_mail_done(conn, FEED, mail, len(items))
        print(f"  {month}: {len(items)} advice(s)")
    rows, n = db.drop_unkeyed_duplicates(conn)
    print(f"undated copies dropped: {rows} row(s) in {n} advice(s)")
    db.log_run(conn, FEED, started, dt.datetime.now(dt.timezone.utc), dict(counts),
               {"since": str(args.since), "export": os.path.basename(args.payments), **stats})
    print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
