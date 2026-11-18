"""
Instamart vendor portal (vendors.zohocreatorportal.in, the entities' Zoho Creator app) - two CSV exports,
downloaded by hand while logged in as instamart@ (the portal needs a login, so this is not a mail feed):

    Payment_Report.csv                       one row per payment
        Organization Name | Vendor Name | Vendor Code | GSTIN | PAN | Trade Vendor Type | Payment type |
        Payment Date (dd/mm/yyyy) | Payment Number | Payment reference no. | Amount | Reversal | Net Amount
    Payment_adjustment_at_Invoice_level.csv  one row per invoice x payment
        Organization Name | ... | Payment Reference No | Invoice Number | Adjusted with invoice | Payment Number |
        Payment Date | Added Time | Payment ID

Unlike the VCredits mails (only the vendor codes copied to instamart@), the portal lists every payment for
all our vendor codes with its printed total. Checked 2026-11-18: 2,366 of 2,390 bank payments since Apr 2024
match an ERP receipt by amount.

One Advice per bank transfer: Payment reference no. = the UTR, the advice_ref the Zoho Books and VCredits
advices use, so the copies de-duplicate by feed priority. A transfer can pay several vendor codes (one
payment row each): their rows are summed. Lines are the invoice rows of the transfer's payment numbers.
'Debit adjustment' rows (debit notes set against invoices, no cash) are not advices and are left out.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import OrderedDict, defaultdict
from typing import Dict, List, Tuple

from pa_common import Advice, Line, our_invoice_no

# Organization Name (upper case, prefix) -> ERP customer (feeds.CUSTOMERS['instamart']). The 2021-23 entities
# (Scootsy, Max Reach, B & H, Getmax, ...) pre-date the ERP (Apr 2024) and stay unmapped.
ENTITIES = [
    ("CLOUDSTORE", "C00152"),
    ("CLOUDKART", "C00153"),
    ("JUPITER KART", "C00154"),
    ("PJTJ", "C00156"),
    ("MOKSH", "C00157"),
    ("AARIGA", "C00196"),
    ("SETRON", "C00183"),
]
PAY_COLS = {"Organization Name", "Vendor Code", "Payment type", "Payment Date", "Payment Number",
            "Payment reference no.", "Net Amount"}
INV_COLS = {"Organization Name", "Invoice Number", "Adjusted with invoice", "Payment Number"}


def _num(s) -> float:
    s = (s or "").replace("INR", "").replace(",", "").strip()
    return float(s) if s else 0.0


def _date(s) -> dt.date:
    return dt.datetime.strptime(s.strip(), "%d/%m/%Y").date()


def _rows(data: bytes, need: set, what: str) -> List[dict]:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    if not rows or not need <= set(rows[0]):
        raise ValueError(f"{what}: expected columns {sorted(need)}, got {sorted(rows[0]) if rows else 'no rows'}")
    return rows


def customer_code(org: str):
    u = org.upper()
    return next((c for k, c in ENTITIES if u.startswith(k)), None)


def parse_exports(payments: bytes, invoices: bytes, since: dt.date = None,
                  source_file: str = "Payment_Report.csv") -> Tuple[List[Advice], dict]:
    """Returns (advices, stats)."""
    P = _rows(payments, PAY_COLS, "Payment Report")
    I = _rows(invoices, INV_COLS, "Payment adjustment at Invoice level")
    inv: Dict[tuple, list] = defaultdict(list)
    for r in I:
        inv[(r["Organization Name"].strip(), r["Payment Number"].strip())].append(r)
    stats = defaultdict(int)
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for r in P:
        if r["Payment type"].strip() != "Bank payment":
            stats["debit_adjustments_skipped"] += 1
            continue
        when = _date(r["Payment Date"])
        if since and when < since:
            stats["before_since"] += 1
            continue
        org, ref = r["Organization Name"].strip(), r["Payment reference no."].strip()
        if not ref:
            ref = f"PORTAL-{org.split()[0].upper()}-{r['Payment Number'].strip()}"
        groups.setdefault((org, ref), []).append(r)
    out = []
    for (org, ref), rows in groups.items():
        lines, vendor_codes, numbers = [], [], []
        for r in rows:
            vendor_codes.append(r["Vendor Code"].strip())
            numbers.append(r["Payment Number"].strip())
            got = inv.get((org, r["Payment Number"].strip()), [])
            if not got:
                stats["payments_without_invoice_rows"] += 1
            for x in got:
                no = x["Invoice Number"].strip()
                ours = our_invoice_no(no)
                lines.append(Line(invoice_no=ours, doc_ref=None if ours else (no or None),
                                  line_type="INVOICE" if ours else "OTHER",
                                  net_amount=round(_num(x["Adjusted with invoice"]), 2),
                                  extra={"vendor_code": r["Vendor Code"].strip(),
                                         "payment_number": r["Payment Number"].strip()}))
        reversal = sum(_num(r.get("Reversal")) for r in rows)
        out.append(Advice(
            customer="instamart", payer_name=org, advice_ref=ref, utr=ref if not ref.startswith("PORTAL-") else None,
            payment_date=min(_date(r["Payment Date"]) for r in rows),
            payment_amount=round(sum(_num(r["Net Amount"]) for r in rows), 2),
            lines=lines, source_file=source_file,
            extra={"customer_code": customer_code(org), "vendor_codes": sorted(set(vendor_codes)),
                   "payment_numbers": numbers, **({"reversal": round(reversal, 2)} if reversal else {})}))
        stats["advices"] += 1
        stats["multi_payment_transfers"] += len(rows) > 1
    return out, dict(stats)
