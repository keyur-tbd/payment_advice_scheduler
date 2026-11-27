#!/usr/bin/env python3
"""
Load Reliance's RTV (return to vendor) notes, article by article, from the Reliance SRM portal into
public.ril_rtv_lines.

    python load_ril_rtv.py --setup --env-file D:/.../.env     # create the table once
    python load_ril_rtv.py --env-file D:/.../.env             # last 45 days, every login id
    python load_ril_rtv.py --since 2026-04-01                 # backfill
    python load_ril_rtv.py --dry-run --since 2026-10-01       # fetch + summarise only

The portal's 'RTV Returns' report (srm-rrscm.ril.com/reports/rtv-returns, ARTICLE_DETAILS) lists every goods
return (movement 161) a Reliance store or DC posted against our vendor codes: one row per RTV material document
(MBLNR, the number our EXP credit memo quotes as PRN_Document_No) x line, with the return PO (EBELN), the store
(WERKS), the article, quantity and value, and OUR invoice no. (XBLNR) + sale PO (REF_PO) the goods came on.

Rows are upserted on (rtv_no, rtv_year, line_no); a row the portal stops listing is kept (stale_since says when it
was last seen) and never deleted, so a reversal cannot erase history. Sign-in is ril_portal_fetch.Portal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TABLE = "public.ril_rtv_lines"
FEED = "ril_rtv"
IST = dt.timedelta(hours=5, minutes=30)

DDL = f"""
create table if not exists {TABLE} (
  rtv_no            text not null,          -- MBLNR: RTV material document (= our credit memo's PRN_Document_No)
  rtv_year          text not null,          -- MJAHR
  line_no           text not null,          -- ZEILE
  vendor_code       text,                   -- LIFNR: our vendor code at Reliance (one per GST branch)
  site_code         text,                   -- WERKS: Reliance store / DC returning the goods
  site_name         text,
  return_po         text,                   -- EBELN: Reliance's return PO
  return_po_date    date,                   -- BEDAT
  odn               text,                   -- ODN: outbound delivery / RTV challan no.
  article_code      text,                   -- MATNR: Reliance article
  description       text,
  ean               text,
  hsn               text,
  quantity          numeric,
  uom               text,
  rate              numeric,                -- KBETR
  value             numeric,                -- DMBTR: value before tax
  tax               numeric,                -- TTL_TAX
  total             numeric,                -- TOTAL: value + tax
  our_invoice_no    text,                   -- XBLNR: our invoice the goods came on
  invoice_date      date,                   -- REF_BLDAT
  sale_po           text,                   -- REF_PO: the sale PO of that invoice
  sale_po_date      date,                   -- REF_PODAT
  gr_doc            text,                   -- LFBNR/LFBJA/LFPOS: Reliance's goods receipt of that invoice
  gr_year           text,
  gr_line           text,
  gr_posting_date   date,                   -- REF_BUDAT
  accounting_doc    text,                   -- ZZBELNR / ZZBUDAT
  accounting_date   date,
  invoice_doc       text,                   -- BELNR
  movement_type     text,                   -- BWART (161 = return)
  department        text,                   -- WWGHA
  family            text,                   -- ZFAMILY
  class             text,                   -- ZCLASS
  brand             text,                   -- BRAND_DESCR
  lr_no             text,
  lr_date           date,
  vehicle_no        text,
  portal_login      text,
  raw               jsonb not null,
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz not null default now(),
  primary key (rtv_no, rtv_year, line_no)
);
create index if not exists ril_rtv_lines_invoice on {TABLE} (our_invoice_no);
create index if not exists ril_rtv_lines_date on {TABLE} (return_po_date);
alter table {TABLE} enable row level security;
comment on table {TABLE} is 'Reliance SRM portal RTV Returns report, article details (load_ril_rtv.py, feed ril_rtv).';
"""

COLS = ["rtv_no", "rtv_year", "line_no", "vendor_code", "site_code", "site_name", "return_po", "return_po_date",
        "odn", "article_code", "description", "ean", "hsn", "quantity", "uom", "rate", "value", "tax", "total",
        "our_invoice_no", "invoice_date", "sale_po", "sale_po_date", "gr_doc", "gr_year", "gr_line",
        "gr_posting_date", "accounting_doc", "accounting_date", "invoice_doc", "movement_type", "department",
        "family", "class", "brand", "lr_no", "lr_date", "vehicle_no", "portal_login", "raw"]


def _date(v):
    """'2026-10-04T18:30:00.000+00:00' (IST midnight sent as UTC) -> 2026-10-05; also DD.MM.YYYY."""
    if not v:
        return None
    v = str(v)
    try:
        if "T" in v:
            return (dt.datetime.fromisoformat(v.replace("Z", "+00:00")) + IST).date()
        if len(v) == 10 and v[2] == ".":
            return dt.datetime.strptime(v, "%d.%m.%Y").date()
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        return None


def _s(v):
    v = "" if v is None else str(v).strip()
    return v or None


def row(r: dict, login: str) -> tuple:
    lr = _date(r.get("LRDATE")) or _date(r.get("LRNO"))
    return (
        _s(r.get("MBLNR")), _s(r.get("MJAHR")), _s(r.get("ZEILE")), _s(r.get("LIFNR")), _s(r.get("WERKS")),
        _s(r.get("WER_NAME")), _s(r.get("EBELN")), _date(r.get("BEDAT")), _s(r.get("ODN")), _s(r.get("MATNR")),
        _s(r.get("MAKTX")), _s(r.get("EAN11")), _s(r.get("STEUC")), r.get("MENGE"), _s(r.get("MEINS")),
        r.get("KBETR"), r.get("DMBTR"), r.get("TTL_TAX"), r.get("TOTAL"), _s(r.get("XBLNR")),
        _date(r.get("REF_BLDAT")), _s(r.get("REF_PO")), _date(r.get("REF_PODAT")), _s(r.get("LFBNR")),
        _s(r.get("LFBJA")), _s(r.get("LFPOS")), _date(r.get("REF_BUDAT")), _s(r.get("ZZBELNR")),
        _date(r.get("ZZBUDAT")), _s(r.get("BELNR")), _s(r.get("BWART")), _s(r.get("WWGHA")), _s(r.get("ZFAMILY")),
        _s(r.get("ZCLASS")), _s(r.get("BRAND_DESCR")), _s(r.get("LRNO")), lr, _s(r.get("VEHICLENO")), login,
        json.dumps(r, default=str),
    )


def fetch(env_file: str, since: dt.date, until: dt.date):
    """[(login, row dict)] from every login id; a failing login is reported after the others are read."""
    import ril_portal_fetch as rpf
    out, failed, seen = [], [], set()
    for user, password in rpf.logins(env_file):
        try:
            portal = rpf.Portal(user, password)
            rows = portal.rtv(since, until)
            print(f"{user} ({portal.vendor_name}): {len(rows)} RTV article line(s) {since}..{until}")
            for r in rows:
                key = (r.get("MBLNR"), r.get("MJAHR"), r.get("ZEILE"))
                if key not in seen:          # two logins can see one vendor code
                    seen.add(key)
                    out.append((user, r))
        except Exception as exc:   # noqa: BLE001 - one login failing must not lose the other
            print(f"ERROR {user}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(user)
    return out, failed


def upsert(conn, rows: list) -> Counter:
    from psycopg2.extras import execute_values
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLS[3:])
    counts: Counter = Counter()
    with conn.cursor() as cur:
        for i in range(0, len(rows), 1000):
            res = execute_values(cur, f"""
                insert into {TABLE} ({", ".join(COLS)}) values %s
                on conflict (rtv_no, rtv_year, line_no) do update set {updates}, last_seen_at = now()
                returning (xmax = 0) as inserted""", rows[i:i + 1000], fetch=True)
            for (inserted,) in res:
                counts["inserted" if inserted else "updated"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None, help="first posting date (default 45 days back)")
    ap.add_argument("--until", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--setup", action="store_true", help="create the table (once), then exit")
    ap.add_argument("--dry-run", action="store_true", help="fetch and summarise only, no database")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()

    if args.setup:
        import db
        conn = db.connect(args.env_file)
        with conn.cursor() as cur:
            cur.execute("set lock_timeout = '10s'")
            cur.execute(DDL)
        print(f"{TABLE} ready")
        return 0

    since = args.since or dt.date.today() - dt.timedelta(days=45)
    until = args.until or dt.date.today()
    started = dt.datetime.now(dt.timezone.utc)
    got, failed = fetch(args.env_file, since, until)
    rows = [row(r, user) for user, r in got]
    bad = [r for r in rows if not (r[0] and r[1] and r[2])]
    rows = [r for r in rows if r[0] and r[1] and r[2]]
    docs = {(r[0], r[1]) for r in rows}
    print(f"{len(rows)} line(s), {len(docs)} RTV document(s), value incl. tax "
          f"{sum(float(r[18] or 0) for r in rows):,.2f}; {len(bad)} row(s) without a document key")

    counts: Counter = Counter(lines=len(rows), documents=len(docs), no_key=len(bad))
    if not args.dry_run:
        import db
        conn = db.connect(args.env_file)
        counts.update(upsert(conn, rows))
        ended = dt.datetime.now(dt.timezone.utc)
        secs = (ended - started).total_seconds()
        with conn.cursor() as cur:
            cur.execute(
                """insert into public.workflow_logs (source, workflow, started_at, ended_at, duration_seconds,
                       duration_text, processed, total_items, failed, skipped, status, details)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (FEED, "Reliance SRM portal RTV to Supabase", started, ended, round(secs, 1),
                 f"{int(secs // 60)}m {int(secs % 60)}s", len(rows), len(rows), len(failed), len(bad),
                 "Success" if not failed else "Partial",
                 json.dumps({"counts": counts, "since": str(since), "until": str(until),
                             **({"failed_logins": failed} if failed else {})})))
        print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        import catalog   # describe this feed in Birbal's pipeline catalog (Master board)
        catalog.register(conn, {
            "feed_key": FEED, "grp": "PRN / RTV", "source_kind": "portal",
            "party": "Reliance (Reliance Smart / Signature, Metro C&C, Milk Basket DCs; C00035 / C00356)",
            "sender": "Reliance SRM portal (srm-rrscm.ril.com), report RTV Returns",
            "how_found": "Sign in per login id (ril_portal_fetch.Portal), then POST /srm/rdl/api/v1/report/rtv/fetch "
                         "action ARTICLE_DETAILS, one calendar month per call, last 45 days",
            "document": "JSON rows: RTV document (MBLNR) x article, return PO, store / DC, quantity, value, our invoice + sale PO",
            "parser": "load_ril_rtv.py row()", "repo": catalog.REPO, "script": "load_ril_rtv.py",
            "runs_on": "GitHub Actions: Reliance SRM portal to Supabase (ril_portal.yml), step 'RTV notes (article details)'",
            "schedule": "cron '30 5 * * *' UTC = 11:00 IST", "target_tables": [TABLE], "log_source": FEED})
    if failed:
        print(f"FAILED: login(s) not read: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
