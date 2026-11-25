#!/usr/bin/env python3
"""
Load Reliance Retail payment advices from the Reliance SRM portal into pa_reliance (feed 'ril_portal').

    python load_ril_portal.py --dry-run                       # sign in, download, parse + check only
    python load_ril_portal.py --env-file D:/.../.env          # last 45 days, every login id
    python load_ril_portal.py --since 2026-04-01              # backfill
    python load_ril_portal.py --dir D:/some/folder            # PDFs already on disk (no sign-in)

The portal's Statement of Accounts lists every payment document of our vendor codes, and each one's 'Payment
Advice' PDF is the file RR.Paymentadvice@ril.com mails (no such mail reached us after 30 Oct 2026), so
parsers/ril_rr.py reads it unchanged. ril_portal_fetch.py signs in with each RELIANCE_SRM_USER_<n> /
RELIANCE_SRM_PASSWORD_<n> pair and downloads only the advices not loaded yet.

Each PDF is one pseudo-mail 'rilportal-<company><document><fiscal year>.PDF', so an advice is downloaded once and
a re-run skips it. Priority 15: below the mailed 'ril_rr' copy (20, the same PDF), above the Axis bank copy (10).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pa_common import Attachment, Mail, Skip  # noqa: E402
from parsers import ril_rr  # noqa: E402

FEED = "ril_portal"
PREFIX = "rilportal-"


def local_files(folder: str):
    paths = {os.path.normcase(p): p for p in glob.glob(os.path.join(folder, "*.pdf")) + glob.glob(os.path.join(folder, "*.PDF"))}
    for path in sorted(paths.values()):
        with open(path, "rb") as fh:
            yield os.path.basename(path), dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc), fh.read(), {}


def portal_files(env_file: str, since: dt.date, until, done: set, counts: Counter):
    import ril_portal_fetch as rpf
    now = dt.datetime.now(dt.timezone.utc)
    for user, p, pdf in rpf.fetch(rpf.logins(env_file), since, until, skip=lambda name: PREFIX + name in done):
        name = rpf.file_name(p)
        if PREFIX + name in done:
            counts["already_loaded"] += 1
        elif pdf is None:            # listed, but the portal has no PDF yet: not marked done, so the next run asks again
            counts["no_pdf"] += 1
            print(f"  no PDF yet: {p['doc_no']}/{p['fiscal_year']} of {p['date']} ({p['amount']:,.2f})")
        else:
            yield name, now, pdf, {"vendor_code": (p["vendor"] or "").lstrip("0"), "portal_login": user}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="folder of advice PDFs already downloaded (skips the sign-in)")
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None,
                    help="first payment date to look for (default 45 days back)")
    ap.add_argument("--until", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true", help="parse + check only, no database")
    ap.add_argument("--reparse", action="store_true", help="read advices already loaded again")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()
    since = args.since or dt.date.today() - dt.timedelta(days=45)

    conn = refs = None
    done: set = set()
    if not args.dry_run:
        import db
        conn = db.connect(args.env_file)
        refs = db.RefIndex(conn)
        if not args.reparse:
            done = db.done_mail_ids(conn, FEED)
    started = dt.datetime.now(dt.timezone.utc)
    counts: Counter = Counter()
    failure = None
    items = local_files(args.dir) if args.dir else portal_files(args.env_file, since, args.until, done, counts)
    try:
        for name, stamp, data, extra in items:
            mail = Mail(id=PREFIX + name, thread_id="", date=stamp, sender="srm-rrscm.ril.com",
                        subject=f"Reliance SRM portal payment advice {name}", attachments=[Attachment(name, data)])
            if mail.id in done:
                counts["already_loaded"] += 1
                continue
            try:
                advices = ril_rr.parse(mail)
            except Skip as exc:
                counts["skipped"] += 1
                print(f"  skip {name}: {exc}")
                if conn is not None:
                    db.log_mail(conn, FEED, mail, "skipped", str(exc), source_file=name)
                continue
            for idx, advice in enumerate(advices):
                advice.extra.update(extra)
                if args.dry_run:
                    status, diff = advice.check()
                    counts[status] += 1
                    if status != "ok":
                        print(f"  {name}: {advice.advice_ref} {status} diff={diff}")
                else:
                    counts[db.load_advice(conn, FEED, mail, idx, advice, refs)] += 1
            if conn is not None:
                db.mark_mail_done(conn, FEED, mail, len(advices))
    except Exception as exc:   # noqa: BLE001 - a login that failed: what the others gave is loaded, the run still fails
        failure = exc
    if conn is not None:
        db.log_run(conn, FEED, started, dt.datetime.now(dt.timezone.utc), dict(counts),
                   {"source": args.dir or "portal", "since": str(since), **({"error": str(failure)} if failure else {})})
    print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
