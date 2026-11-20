#!/usr/bin/env python3
"""
Load the Instamart vendor portal's Debit Note Report export into public.instamart_portal_debit_notes (Birbal 272).

    python load_debit_notes.py Debit_Note_Report.csv --env-file D:/.../.env
    python load_debit_notes.py --fetch                 # sign in and export it first (portal_fetch.py)
    python load_debit_notes.py Debit_Note_Report.csv --dry-run

The export is the whole report (every note since 2021, ~17.6k rows), so the table is replaced whole in one
transaction. A file with far fewer rows than the table holds (an export cut short) is refused. After the load
app.refresh_platform_dn() re-matches the notes to the ERP (Receivables > Capping DNs & RTVs).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TABLE = "public.instamart_portal_debit_notes"
FILE_NAME = "Debit_Note_Report.csv"
MIN_SHARE = 0.9          # refuse a file with fewer rows than this share of the table

# CSV header -> (column, kind)
COLUMNS = [
    ("Organization Name", "org_name", "text"),
    ("Vendor Name", "vendor_name", "text"),
    ("Vendor Code", "vendor_code", "text"),
    ("GSTIN", "gstin", "text"),
    ("Status", "status", "text"),
    ("GRN No.", "grn_no", "text"),
    ("GRN Date", "grn_date", "date"),
    ("Classification", "classification", "text"),
    ("Date", "dn_date", "date"),
    ("Document no.", "document_no", "text"),
    ("Reversal Document No.", "reversal_document_no", "text"),
    ("Reversal Date", "reversal_date", "date"),
    ("Amount", "amount", "num"),
    ("Reversal", "reversal", "num"),
    ("Net Amount", "net_amount", "num"),
    ("Adjusted with invoice recorded", "adjusted_with_invoice", "num"),
    ("Balance Amount", "balance_amount", "num"),
    ("Invoice Breakup", "invoice_breakup", "text"),
    ("Bill Numbers", "bill_numbers", "text"),
    ("Original Details", "original_details", "text"),
    ("Vendor Credit ID", "vendor_credit_id", "text"),
    ("Modified Time", "modified_time", "ts"),
    ("Added Time", "added_time", "ts"),
]
REQUIRED = {"Organization Name", "Document no.", "Date", "Net Amount", "Invoice Breakup", "Classification"}


def _date(s: str):
    s = (s or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _ts(s: str):
    s = (s or "").strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(s: str):
    s = re.sub(r"[^0-9.\-]", "", (s or "").replace("INR", ""))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


CONVERT = {"text": lambda s: (s or "").strip() or None, "date": _date, "ts": _ts, "num": _num}


def parse(data: bytes) -> list:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"not the Debit Note Report: missing column(s) {sorted(missing)}")
    rows = []
    for i, r in enumerate(reader, start=1):
        rec = {"row_no": i}
        for head, col, kind in COLUMNS:
            rec[col] = CONVERT[kind](r.get(head, ""))
        rec["raw"] = json.dumps(r, ensure_ascii=False)
        rows.append(rec)
    return rows


def load(conn, rows: list, source_file: str, force: bool = False) -> int:
    """Replace the table with rows (no commit)."""
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    cur.execute(f"select count(*) from {TABLE}")
    have = cur.fetchone()[0]
    if have and len(rows) < MIN_SHARE * have and not force:
        raise SystemExit(f"refused: the export has {len(rows):,} rows, the table {have:,} (an export cut short?); --force to load anyway")
    cols = ["row_no"] + [c for _, c, _ in COLUMNS] + ["raw", "source_file"]
    cur.execute(f"delete from {TABLE}")
    execute_values(cur, f"insert into {TABLE} ({', '.join(cols)}) values %s",
                   [tuple(r[c] for c in cols[:-1]) + (source_file,) for r in rows], page_size=1000)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help=FILE_NAME)
    ap.add_argument("--fetch", action="store_true", help="sign in and export the report first")
    ap.add_argument("--out", default="portal_exports")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="load even if the export is much shorter than the table")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()
    if args.fetch:
        import portal_fetch
        args.csv = portal_fetch.fetch(args.out, *portal_fetch.credentials(args.env_file), reports=portal_fetch.DEBIT_NOTES)[0]
    if not args.csv:
        ap.error(f"give {FILE_NAME}, or --fetch")
    with open(args.csv, "rb") as fh:
        rows = parse(fh.read())
    in_scope = sum(1 for r in rows if (r["document_no"] or "").upper().startswith("BAKERS")
                   or (r["classification"] or "") != "Purchase Return")
    print(f"parsed: {len(rows):,} rows ({in_scope:,} capping / offer / RTV candidates)")
    if args.dry_run:
        return 0

    import db
    conn = db.connect(args.env_file)
    conn.autocommit = False                  # db.connect hands out autocommit: the replace must be one transaction
    try:
        n = load(conn, rows, os.path.basename(args.csv), force=args.force)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    print(f"loaded: {n:,} rows into {TABLE}")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("select app.refresh_platform_dn()")
    msg = cur.fetchone()[0]
    print("refresh:", msg)
    return 1 if str(msg).startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
