#!/usr/bin/env python3
"""
Load Dorabjee's purchase return / rejection notes (RTV notes, "Dorabjee's - Purchase Return <branch>" mails to
accounts@, one rej.pdf each) line by line into public.dorabjee_rtv_lines.

    python load_dorabjee_rtv.py --setup --env-file D:/.../.env     # create the table once
    python load_dorabjee_rtv.py --env-file D:/.../.env             # mails of the last 60 days not loaded yet
    python load_dorabjee_rtv.py --since 2026-04-01                 # backfill
    python load_dorabjee_rtv.py --dry-run --since 2026-11-01       # parse + summarise only
    python load_dorabjee_rtv.py --reparse --since 2026-04-01       # re-read mails already loaded too

The PDF is plain text: Rej No (DR30750) + date, the branch, one block per line (SKUCode, description, MRP,
purchase rate, rejected qty, amount; then HSN + SGST/CGST/IGST/CESS; then "Remark : <reason>"), the totals, and
"Deduction from : Inv No: 27MUM-12513 Dt: .. GRR No: DG90341 Dt: ..". Our EXP credit memo for the note quotes the
Rej No as PRN_Document_No (Birbal's PRN tracker, platform Dorabjee).

A note is mailed again when its status moves on (Email Pending -> Email Sent); the latest mail's lines replace the
earlier ones (rej_no is the key). A note whose lines do not add up to its printed totals is still loaded, with
parse_ok = false.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TABLE = "public.dorabjee_rtv_lines"
FEED = "dorabjee_rtv"
QUERY = 'from:dorabjees.com subject:"Purchase Return" has:attachment'

DDL = f"""
create table if not exists {TABLE} (
  rej_no              text not null,      -- Dorabjee's Rej No (DR30750) = our credit memo's PRN_Document_No
  line_no             integer not null,   -- position on the note
  rej_date            date,
  branch              text,               -- Dorabjee store (HADAPSAR, KOREGAONPARK, ...)
  sku_code            text,               -- Dorabjee's SKU code
  description         text,
  mrp                 numeric,
  rate                numeric,            -- PRate: Dorabjee's purchase rate before tax
  quantity            numeric,            -- RejQty
  amount              numeric,            -- before tax
  hsn                 text,
  tax                 numeric,            -- SGST + CGST + IGST + CESS of the line
  total               numeric,            -- amount + tax
  reason              text,               -- Remark (Near Expiry, Damaged, ...)
  doc_quantity        numeric,            -- the note's printed totals
  doc_amount          numeric,
  doc_tax             numeric,
  doc_total           numeric,            -- Final Amount
  parse_ok            boolean not null,   -- lines add up to the printed totals
  status              text,               -- "Stock Updated > Email Sent > Goods Return Pending"
  deduct_invoice_no   text,               -- "Deduction from : Inv No" (the invoice Dorabjee deducts it from)
  deduct_invoice_date date,
  grr_no              text,               -- Dorabjee's goods receipt of that invoice
  grr_date            date,
  mail_id             text not null,
  mail_date           timestamptz,
  source_file         text,
  loaded_at           timestamptz not null default now(),
  primary key (rej_no, line_no)
);
create index if not exists dorabjee_rtv_lines_date on {TABLE} (rej_date);
create index if not exists dorabjee_rtv_lines_mail on {TABLE} (mail_id);
alter table {TABLE} enable row level security;
comment on table {TABLE} is 'Dorabjee purchase return / rejection notes (rej.pdf mailed to accounts@), one row per line (payment_advice_pipeline load_dorabjee_rtv.py, feed dorabjee_rtv).';
"""

COLS = ["rej_no", "line_no", "rej_date", "branch", "sku_code", "description", "mrp", "rate", "quantity", "amount",
        "hsn", "tax", "total", "reason", "doc_quantity", "doc_amount", "doc_tax", "doc_total", "parse_ok", "status",
        "deduct_invoice_no", "deduct_invoice_date", "grr_no", "grr_date", "mail_id", "mail_date", "source_file"]

NUM = r"-?[\d,]*\.?\d+"
RE_TITLE = re.compile(r"PURCHASE RETURN\s*/\s*REJECTION\s*-\s*(.+)", re.I)
RE_REJ = re.compile(r"Rej No\s*:\s*(\S+)\s+Date\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)
RE_LINE = re.compile(rf"^(\d{{3,}})\s+(.+?)\s+({NUM})\s+({NUM})\s+({NUM})\s+({NUM})$")
RE_TAX = re.compile(rf"^HSN:\s*(\S*)\s+SGST@\s*{NUM}=\s*({NUM})\s+CGST@\s*{NUM}=\s*({NUM})\s+IGST@\s*{NUM}=\s*({NUM})"
                    rf"(?:\s+CESS@\s*{NUM}=\s*({NUM}))?", re.I)
RE_REMARK = re.compile(r"^Remark\s*:\s*(.*?)\s*:?\s*$", re.I)
RE_TOTQ = re.compile(rf"Total Qty\s*:\s*({NUM})\s+Amount \(Rs\.\)\s*:\s*({NUM})", re.I)
RE_TOTT = re.compile(rf"SGST\s*:\s*({NUM})\s+CGST\s*:\s*({NUM})\s+IGST\s*:\s*({NUM})\s+CESS\s*:\s*({NUM})", re.I)
RE_FINAL = re.compile(rf"Final Amount \(Rs\.\)\s*:\s*({NUM})", re.I)
RE_STATUS = re.compile(r"^Status\s*:\s*(.+)$", re.I | re.M)
RE_DEDUCT = re.compile(r"Inv No\s*:\s*(\S+)\s+Dt\s*:\s*(\d{1,2}/\d{1,2}/\d{4})(?:\s+GRR No\s*:\s*(\S+)\s+Dt\s*:\s*(\d{1,2}/\d{1,2}/\d{4}))?", re.I)


def _n(v):
    return None if v in (None, "") else float(v.replace(",", ""))


def _d(v):
    return dt.datetime.strptime(v, "%d/%m/%Y").date() if v else None


def parse(text: str) -> dict:
    """One rej.pdf -> {header..., lines: [...]}; raises ValueError when it is not a rejection note."""
    m = RE_REJ.search(text)
    if not m:
        raise ValueError("no 'Rej No : .. Date : ..'")
    head = {"rej_no": m.group(1).strip(), "rej_date": _d(m.group(2))}
    t = RE_TITLE.search(text)
    head["branch"] = t.group(1).strip() if t else None
    lines, cur = [], None
    for raw in text.splitlines():
        s = raw.strip()
        lm = RE_LINE.match(s)
        if lm and not s.lower().startswith(("total qty", "hsn")):
            cur = {"sku_code": lm.group(1), "description": lm.group(2).strip(), "mrp": _n(lm.group(3)),
                   "rate": _n(lm.group(4)), "quantity": _n(lm.group(5)), "amount": _n(lm.group(6)),
                   "hsn": None, "tax": None, "reason": None}
            lines.append(cur)
            continue
        if cur is None:
            continue
        tm = RE_TAX.match(s)
        if tm:
            cur["hsn"] = tm.group(1) or None
            cur["tax"] = round(sum(_n(x) or 0 for x in tm.groups()[1:]), 2)
            continue
        rm = RE_REMARK.match(s)
        if rm:
            cur["reason"] = rm.group(1).strip() or None
            cur = None          # a line's block ends at its remark
    for i, ln in enumerate(lines, 1):
        ln["line_no"] = i
        ln["total"] = round((ln["amount"] or 0) + (ln["tax"] or 0), 2)
    q, tt, f = RE_TOTQ.search(text), RE_TOTT.search(text), RE_FINAL.search(text)
    head["doc_quantity"] = _n(q.group(1)) if q else None
    head["doc_amount"] = _n(q.group(2)) if q else None
    head["doc_tax"] = round(sum(_n(x) or 0 for x in tt.groups()), 2) if tt else None
    head["doc_total"] = _n(f.group(1)) if f else None
    st = RE_STATUS.search(text)
    head["status"] = st.group(1).strip() if st else None
    dm = RE_DEDUCT.search(text)
    head["deduct_invoice_no"] = dm.group(1) if dm else None
    head["deduct_invoice_date"] = _d(dm.group(2)) if dm else None
    head["grr_no"] = dm.group(3) if dm else None
    head["grr_date"] = _d(dm.group(4)) if dm and dm.group(4) else None
    head["parse_ok"] = bool(
        lines and head["doc_amount"] is not None
        and abs(sum(ln["amount"] or 0 for ln in lines) - head["doc_amount"]) < 0.05
        and (head["doc_quantity"] is None or abs(sum(ln["quantity"] or 0 for ln in lines) - head["doc_quantity"]) < 0.005)
        and (head["doc_total"] is None or abs(sum(ln["total"] for ln in lines) - head["doc_total"]) < 0.5))
    head["lines"] = lines
    return head


def rows_of(note: dict, mail, source_file: str) -> list:
    out = []
    for ln in note["lines"]:
        rec = {**note, **ln, "mail_id": mail.id, "mail_date": mail.date, "source_file": source_file}
        out.append(tuple(rec.get(c) for c in COLS))
    return out


def loaded_mail_ids(conn) -> set:
    with conn.cursor() as cur:
        cur.execute(f"select distinct mail_id from {TABLE}")
        return {r[0] for r in cur.fetchall()}


def save(conn, note_rows: dict) -> Counter:
    """note_rows: rej_no -> (mail_date, rows). Each note's lines are replaced unless a later mail is already in."""
    from psycopg2.extras import execute_values
    counts: Counter = Counter()
    with conn, conn.cursor() as cur:
        for rej_no, (mail_date, rows) in note_rows.items():
            cur.execute(f"select max(mail_date) from {TABLE} where rej_no = %s", (rej_no,))
            have = cur.fetchone()[0]
            if have and mail_date and have > mail_date:
                counts["older_mail_skipped"] += 1
                continue
            cur.execute(f"delete from {TABLE} where rej_no = %s", (rej_no,))
            counts["notes_replaced" if cur.rowcount else "notes_new"] += 1
            execute_values(cur, f"insert into {TABLE} ({', '.join(COLS)}) values %s", rows)
            counts["lines"] += len(rows)
    return counts


def main() -> int:
    import pa_common as pc
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None, help="first mail date (default 60 days back)")
    ap.add_argument("--setup", action="store_true", help="create the table (once), then exit")
    ap.add_argument("--dry-run", action="store_true", help="parse and summarise only, no database")
    ap.add_argument("--reparse", action="store_true", help="re-read mails already loaded")
    ap.add_argument("--cache-dir", default=None, help="keep fetched mails here (local backfills)")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()

    import db
    if args.setup:
        conn = db.connect(args.env_file)
        with conn, conn.cursor() as cur:
            cur.execute("set lock_timeout = '10s'")
            cur.execute(DDL)
        print(f"{TABLE} ready")
        return 0

    since = args.since or dt.date.today() - dt.timedelta(days=60)
    started = dt.datetime.now(dt.timezone.utc)
    conn = None if args.dry_run else db.connect(args.env_file)
    skip = loaded_mail_ids(conn) if conn and not args.reparse else set()
    svc = pc.gmail_service("accounts")
    query = f"{QUERY} after:{since:%Y/%m/%d}"

    notes: dict = {}
    counts: Counter = Counter()
    failures = []
    for mail in pc.iter_mails(svc, query, cache_dir=args.cache_dir, skip_ids=skip, oldest_first=True):
        counts["mails"] += 1
        pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
        if not pdfs:
            counts["mails_without_pdf"] += 1
            continue
        for a in pdfs:
            try:
                note = parse(pc.pdf_text(a.data))
            except Exception as exc:   # noqa: BLE001 - one bad PDF must not stop the rest
                failures.append(f"{mail.id} {a.name}: {type(exc).__name__}: {exc}")
                continue
            counts["notes_read"] += 1
            counts["parse_ok" if note["parse_ok"] else "parse_mismatch"] += 1
            if not note["parse_ok"]:
                print(f"  mismatch {note['rej_no']} ({mail.id}): lines {sum(l['amount'] or 0 for l in note['lines']):.2f}"
                      f" vs printed {note['doc_amount']}", file=sys.stderr)
            prev = notes.get(note["rej_no"])
            if prev is None or (mail.date and prev[0] and mail.date >= prev[0]):
                notes[note["rej_no"]] = (mail.date, rows_of(note, mail, a.name))

    lines = sum(len(r) for _, r in notes.values())
    value = sum(float(r[COLS.index("total")] or 0) for _, rs in notes.values() for r in rs)
    print(f"{counts['mails']} new mail(s) since {since}: {len(notes)} note(s), {lines} line(s), "
          f"value incl. tax {value:,.2f}; {counts['parse_mismatch']} not adding up; {len(failures)} unreadable")
    for f in failures:
        print("  unreadable " + f, file=sys.stderr)

    if conn:
        counts.update(save(conn, notes))
        ended = dt.datetime.now(dt.timezone.utc)
        secs = (ended - started).total_seconds()
        with conn, conn.cursor() as cur:
            cur.execute(
                """insert into public.workflow_logs (source, workflow, started_at, ended_at, duration_seconds,
                       duration_text, processed, total_items, failed, skipped, status, details)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (FEED, "Dorabjee RTV notes (mail) to Supabase", started, ended, round(secs, 1),
                 f"{int(secs // 60)}m {int(secs % 60)}s", counts["notes_read"], counts["mails"], len(failures),
                 counts["parse_mismatch"], "Success" if not failures else "Partial",
                 json.dumps({"counts": counts, "since": str(since), **({"unreadable": failures[:50]} if failures else {})})))
        print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
