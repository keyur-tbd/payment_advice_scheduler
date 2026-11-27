#!/usr/bin/env python3
"""
Load More Retail's GDN copies (Goods Discrepancy Notes: what a More DC rejected at GRN -- damaged stock, short quantity
supplied ...) line by line into public.more_gdn_lines.

    python load_more_gdn.py --setup --env-file D:/.../.env     # create the table once
    python load_more_gdn.py --env-file D:/.../.env             # mails of the last 60 days not loaded yet
    python load_more_gdn.py --since 2026-03-01                 # backfill
    python load_more_gdn.py --dry-run --since 2026-11-01       # parse + summarise only

The mails come from the sender of More's GRN copies (aws-report@moreretail.in, "<GRN no> GDN copy", one
gdn_<GDN no>.pdf) to payments@. The PDF names the PO, OUR invoice, the GDN and GRN numbers and dates, and per line the
More item code, HSN, description, reason ("1 - DAMAGED STOCK", "2 - SHORT QUANTITY SUPPLIED"), GRN cost, MRP,
received / invoiced / rejected quantity and the line amount -- which is the RECEIVED value (received qty x cost), so
the rejected value is computed (rejected qty x cost). Finance books each GDN as an SHGRN credit memo quoting the GDN no.
in External_Document_No, for exactly that rejected value (Birbal's PRN tracker, platform More Retail).

Description and reason wrap onto shared lines, so the table is read from word positions, not plain text. A note whose
lines break the PDF's own arithmetic (amount = received x cost, rejected = invoiced - received) is still loaded, with
parse_ok = false.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TABLE = "public.more_gdn_lines"
FEED = "more_gdn"
MAILBOX = "payments"
QUERY = 'from:aws-report@moreretail.in subject:"GDN copy" has:attachment'

DDL = f"""
create table if not exists {TABLE} (
  gdn_no            text not null,      -- More's Goods Discrepancy Note no. (= our SHGRN memo's External_Document_No)
  line_no           integer not null,   -- Sr. No
  gdn_date          date,
  receipt_date      date,
  grn_no            text,               -- More's GRN of the delivery
  po_no             text,               -- More's PO
  our_invoice_no    text,               -- "Vendor Invoice No"
  site_code         text,               -- More's receiving site (666 = Hoskote, ...)
  site_address      text,
  supplier_code     text,               -- our vendor code at More
  item_code         text,               -- More's article
  hsn               text,
  description       text,
  reason            text,               -- "DAMAGED STOCK", "SHORT QUANTITY SUPPLIED", ...
  reason_code       text,               -- the number in front of the reason
  uom               text,
  cost              numeric,            -- GRN unit cost
  mrp               numeric,
  received_qty      numeric,
  invoiced_qty      numeric,            -- "VEN. INV./PHY. QTY"
  rejected_qty      numeric,
  line_amount       numeric,            -- as printed: received qty x cost
  rejected_value    numeric,            -- rejected qty x cost (what the SHGRN memo credits)
  parse_ok          boolean not null,
  mail_id           text not null,
  mail_date         timestamptz,
  source_file       text,
  loaded_at         timestamptz not null default now(),
  primary key (gdn_no, line_no)
);
create index if not exists more_gdn_lines_date on {TABLE} (gdn_date);
create index if not exists more_gdn_lines_mail on {TABLE} (mail_id);
alter table {TABLE} enable row level security;
comment on table {TABLE} is 'More Retail GDN copies (Goods Discrepancy Notes, rejected at GRN) mailed to payments@, one row per line (payment_advice_pipeline load_more_gdn.py, feed more_gdn).';
"""

COLS = ["gdn_no", "line_no", "gdn_date", "receipt_date", "grn_no", "po_no", "our_invoice_no", "site_code", "site_address",
        "supplier_code", "item_code", "hsn", "description", "reason", "reason_code", "uom", "cost", "mrp", "received_qty",
        "invoiced_qty", "rejected_qty", "line_amount", "rejected_value", "parse_ok", "mail_id", "mail_date", "source_file"]

NUM = re.compile(r"^-?[\d,]*\.?\d+$")
HEAD = {
    "po_no": re.compile(r"Purchase Order No:\s*(\S+)"),
    "our_invoice_no": re.compile(r"Vendor Invoice No:\s*(\S+)"),
    "gdn_no": re.compile(r"GDN No:\s*(\S+)"),
    "grn_no": re.compile(r"GRN No:\s*(\S+)"),
    "receipt_date": re.compile(r"Receipt Date:\s*(\d{2}/\d{2}/\d{4})"),
    "gdn_date": re.compile(r"GDN Date:\s*(\d{2}/\d{2}/\d{4})"),
    "supplier_code": re.compile(r"Supplier:\s*(\S+)"),
    "site_code": re.compile(r"^\s*(\w+)\s+Supplier:", re.M),
}


def _n(v):
    return None if v in (None, "") else float(str(v).replace(",", ""))


def _d(v):
    return dt.datetime.strptime(v, "%d/%m/%Y").date() if v else None


def _rows(words):
    """Words -> visual lines (same `top` within 3 pt), each a list sorted by x."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0] - w["top"]) <= 3:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    return [sorted(ws, key=lambda w: w["x0"]) for _, ws in lines]


def parse(data: bytes) -> dict:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        # a long note runs onto a second page: stack the pages so `top` keeps increasing
        words = [{**w, "top": w["top"] + i * 10000} for i, p in enumerate(pdf.pages) for w in p.extract_words()]
    if "GOODS DISCREPANCY NOTE" not in text.upper():
        raise ValueError("not a Goods Discrepancy Note")
    head = {}
    for k, rx in HEAD.items():
        m = rx.search(text)
        head[k] = m.group(1) if m else None
    if not head["gdn_no"]:
        raise ValueError("no GDN No")
    head["gdn_date"], head["receipt_date"] = _d(head["gdn_date"]), _d(head["receipt_date"])

    rows = _rows(words)
    # More's shipping address: the left column between "More Shipping Address" and "GSTIN:"
    addr, on = [], False
    for ws in rows:
        line = " ".join(w["text"] for w in ws)
        if "More Shipping Address" in line:
            on = True
            continue
        if on and line.startswith("GSTIN"):
            break
        if on:
            left = " ".join(w["text"] for w in ws if w["x0"] < 400 and not w["text"].startswith("Supplier"))
            left = re.sub(r"^\s*\w+\s+Supplier:\s*\S+\s*", "", left) if "Supplier:" in line else left
            if left.strip() and not re.fullmatch(r"\s*\d+\s*", left):
                addr.append(left.strip())
    head["site_address"] = ", ".join(addr) or None

    # column x positions from the header row that starts with "No" / "CODE" / "DESCRIPTION" / "REASON"
    hdr = next((ws for ws in rows if any(w["text"] == "DESCRIPTION" for w in ws) and any(w["text"] == "REASON" for w in ws)), None)
    if hdr is None:
        raise ValueError("no line table header")
    x = {w["text"]: w["x0"] for w in hdr}
    desc_x, reason_x, item_x = x["DESCRIPTION"], x["REASON"], x.get("ITEM", x["REASON"] + 65)
    top_hdr = hdr[0]["top"]

    lines, cur = [], None
    for ws in rows:
        if ws[0]["top"] <= top_hdr:
            continue
        line = " ".join(w["text"] for w in ws)
        if line.startswith("This is a computer generated") or line.startswith("Payment subject"):
            break
        nums = [w["text"] for w in ws if w["x0"] >= x.get("UOM", 430) + 30]
        if ws[0]["x0"] < desc_x - 120 and ws[0]["text"].isdigit() and len(ws) > 3 and len(nums) >= 6 and all(NUM.match(n) for n in nums[-6:]):
            cost, mrp, rec, inv, rej, amt = map(_n, nums[-6:])
            pre = [w for w in ws if w["x0"] < desc_x]
            cur = {"line_no": int(ws[0]["text"]), "item_code": pre[1]["text"] if len(pre) > 1 else None,
                   "hsn": pre[2]["text"] if len(pre) > 2 else None,
                   "desc": [w["text"] for w in ws if desc_x - 2 <= w["x0"] < reason_x - 2],
                   "rsn": [w["text"] for w in ws if reason_x - 2 <= w["x0"] < item_x - 2],
                   "uom": next((w["text"] for w in ws if abs(w["x0"] - x.get("UOM", -99)) < 12), None),
                   "cost": cost, "mrp": mrp, "received_qty": rec, "invoiced_qty": inv, "rejected_qty": rej, "line_amount": amt}
            lines.append(cur)
        elif cur is not None and all(desc_x - 2 <= w["x0"] < item_x - 2 for w in ws) \
                and not any(w["text"] in ("DESCRIPTION", "REASON") for w in ws):
            # a wrapped description / reason; a page break's repeated header or page line has words outside these columns
            cur["desc"] += [w["text"] for w in ws if desc_x - 2 <= w["x0"] < reason_x - 2]
            cur["rsn"] += [w["text"] for w in ws if reason_x - 2 <= w["x0"] < item_x - 2]

    ok = bool(lines)
    for ln in lines:
        ln["description"] = " ".join(ln.pop("desc")) or None
        rsn = " ".join(ln.pop("rsn"))
        m = re.match(r"^\s*(\d+)\s*-\s*(.*)$", rsn)
        ln["reason_code"], ln["reason"] = (m.group(1), m.group(2).strip() or None) if m else (None, rsn or None)
        c, r = ln["cost"] or 0, ln["rejected_qty"] or 0
        ln["rejected_value"] = round(c * r, 2)
        if ln["line_amount"] is not None and abs((ln["received_qty"] or 0) * c - ln["line_amount"]) > 0.05:
            ok = False
        if ln["invoiced_qty"] is not None and abs((ln["invoiced_qty"] or 0) - (ln["received_qty"] or 0) - r) > 0.001:
            ok = False
    head["parse_ok"] = ok
    head["lines"] = lines
    return head


def rows_of(note: dict, mail, source_file: str) -> list:
    return [tuple({**note, **ln, "mail_id": mail.id, "mail_date": mail.date, "source_file": source_file}.get(c) for c in COLS)
            for ln in note["lines"]]


def main() -> int:
    import pa_common as pc
    import db
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None, help="first mail date (default 60 days back)")
    ap.add_argument("--setup", action="store_true", help="create the table (once), then exit")
    ap.add_argument("--dry-run", action="store_true", help="parse and summarise only, no database")
    ap.add_argument("--reparse", action="store_true", help="re-read mails already loaded")
    ap.add_argument("--cache-dir", default=None, help="keep fetched mails here (local backfills)")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()

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
    skip = set()
    if conn and not args.reparse:
        with conn.cursor() as cur:
            cur.execute(f"select distinct mail_id from {TABLE}")
            skip = {r[0] for r in cur.fetchall()}
    svc = pc.gmail_service(MAILBOX)

    notes, failures, counts = {}, [], Counter()
    for mail in pc.iter_mails(svc, f"{QUERY} after:{since:%Y/%m/%d}", cache_dir=args.cache_dir, skip_ids=skip, oldest_first=True):
        counts["mails"] += 1
        for a in [a for a in mail.attachments if a.name.lower().endswith(".pdf")]:
            try:
                note = parse(a.data)
            except Exception as exc:   # noqa: BLE001 - one bad PDF must not stop the rest
                failures.append(f"{mail.id} {a.name}: {type(exc).__name__}: {exc}")
                continue
            counts["notes_read"] += 1
            counts["parse_ok" if note["parse_ok"] else "parse_mismatch"] += 1
            if not note["parse_ok"]:
                print(f"  mismatch GDN {note['gdn_no']} ({mail.id})", file=sys.stderr)
            prev = notes.get(note["gdn_no"])
            if prev is None or (mail.date and prev[0] and mail.date >= prev[0]):
                notes[note["gdn_no"]] = (mail.date, rows_of(note, mail, a.name))

    lines = sum(len(r) for _, r in notes.values())
    value = sum(float(r[COLS.index("rejected_value")] or 0) for _, rs in notes.values() for r in rs)
    print(f"{counts['mails']} new mail(s) since {since}: {len(notes)} GDN(s), {lines} line(s), rejected value "
          f"{value:,.2f}; {counts['parse_mismatch']} breaking their own arithmetic; {len(failures)} unreadable")
    for f in failures:
        print("  unreadable " + f, file=sys.stderr)

    if conn:
        from psycopg2.extras import execute_values
        with conn, conn.cursor() as cur:
            for gdn_no, (_, rows) in notes.items():
                cur.execute(f"delete from {TABLE} where gdn_no = %s", (gdn_no,))
                counts["gdn_replaced" if cur.rowcount else "gdn_new"] += 1
                execute_values(cur, f"insert into {TABLE} ({', '.join(COLS)}) values %s", rows)
        ended = dt.datetime.now(dt.timezone.utc)
        secs = (ended - started).total_seconds()
        with conn, conn.cursor() as cur:
            cur.execute(
                """insert into public.workflow_logs (source, workflow, started_at, ended_at, duration_seconds,
                       duration_text, processed, total_items, failed, skipped, status, details)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (FEED, "More Retail GDN copies (mail) to Supabase", started, ended, round(secs, 1),
                 f"{int(secs // 60)}m {int(secs % 60)}s", counts["notes_read"], counts["mails"], len(failures),
                 counts["parse_mismatch"], "Success" if not failures else "Partial",
                 json.dumps({"counts": counts, "since": str(since), **({"unreadable": failures[:50]} if failures else {})})))
        print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
