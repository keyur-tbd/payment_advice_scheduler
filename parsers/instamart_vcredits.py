"""
Swiggy Instamart distributor entities - '<ENTITY>_Payment_Details_<vendor code>_and_ MIMANSA ..._<dd-mm-yyyy>'
mails to instamart@ with 'Payment Receipts VCredits.xlsx'. Senders changed over time: sw@aorborc.com
(2024-Dec 2025), notifications@mailer.zohocreatormail.com (Oct 2024), rajeev.kumar@swiggy.in (Dec 2025),
noreply-trade@scootsy.com (Jan 2026 on); the feed query is the attachment name, so a new sender needs no change.

One sheet, one row per ledger entry, grouped per invoice of ours:

    Vendor Code | Vendor Name | Vendor GSTIN | Transaction Type | Txn Reference Number | UTR Number | Date | Amount | Entity | Remarks
    1N60007318  | MIMANSA ... | 19AAICM...   | Invoice          | 27KOL-03182          |            | 27-10 | 49334.36 | PJTJ |
                                               Payment          | SCBLN5202611130...   | SCBLN52... | 12-11 | -49285.14 | PJTJ |   <- this invoice's
                                               TDS              | 27KOL-03182          |            | 27-10 | -49.22 | PJTJ |         share of the transfer
                                               PR               | GGN-DN911781         |            |       | -134.9 |      | Purchase Return
                                               Adjustment       | ...

A Payment row is one invoice's share of one bank transfer; a transfer (UTR) spans many invoices and
sometimes more than one mail. So one Advice per UTR (advice_ref = UTR, the same ref the Zoho Books
'payment success' advices in pa_instamart use, so the two feeds de-duplicate), one line per Payment
row, tied to the invoice group it sits in. Before mid-2024 and on and off since, the UTR is blank: those
payments group by entity + vendor code + payment date (advice_ref '<ENTITY>-<vendor code>-<yyyymmdd>').

No printed total: payment_amount = the sum of the transfer's Payment rows (marked in extra). A later mail
often carries more (or other) rows of the same transfer; the feed's merge_repeats rule (db.load_advice)
adds the rows earlier mails lacked and restates the transfer's total across all of them.
"""

from __future__ import annotations

import datetime as dt
import io
import struct
import zipfile
import zlib
from collections import OrderedDict

from pa_common import Advice, Line, Mail, Skip, date, num, our_invoice_no

ENTITIES = {                       # Entity column -> ERP customer (feeds.CUSTOMERS['instamart'])
    "CLOUDSTORE": "C00152",
    "CLOUDKART": "C00153",
    "JUPITER KART": "C00154",
    "PJTJ": "C00156",
    "MOKSH": "C00157",
    "AARIGA": "C00196",
}
HEADER = ["Vendor Code", "Vendor Name", "Vendor GSTIN", "Transaction Type", "Txn Reference Number",
          "UTR Number", "Date", "Amount", "Entity", "Remarks"]


def _rezip(data: bytes) -> bytes:
    """Some files are streamed zips whose central directory Python rejects ('Bad magic number for
    central directory'): rebuild the archive from its local file headers."""
    out, pos = io.BytesIO(), 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        while data[pos:pos + 4] == b"PK\x03\x04":
            flags, method = struct.unpack("<HH", data[pos + 6:pos + 10])
            csize, = struct.unpack("<I", data[pos + 18:pos + 22])
            nlen, xlen = struct.unpack("<HH", data[pos + 26:pos + 30])
            name = data[pos + 30:pos + 30 + nlen].decode("utf-8", "replace")
            start = pos + 30 + nlen + xlen
            if method == 8:
                d = zlib.decompressobj(-15)
                body = d.decompress(data[start:]) if (flags & 8 or not csize) else d.decompress(data[start:start + csize])
                used = len(data) - start - len(d.unused_data) if (flags & 8 or not csize) else csize
            else:
                body, used = data[start:start + csize], csize
            z.writestr(name, body)
            pos = start + used
            if flags & 8:                                  # data descriptor (12-24 bytes; ZIP64 here)
                nxt = data.find(b"PK\x03\x04", pos, pos + 32)
                if nxt < 0:
                    break
                pos = nxt
    return out.getvalue()


def _rows(data: bytes) -> list[list]:
    import openpyxl
    if data[:4] == bytes.fromhex("d0cf11e0"):          # early-2024 Moksh files are old .xls
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        rows = []
        for i in range(sh.nrows):
            row = []
            for c in sh.row(i):
                v = c.value
                if c.ctype == xlrd.XL_CELL_DATE:
                    v = xlrd.xldate_as_datetime(v, book.datemode)
                row.append(v if v != "" else None)
            rows.append(row)
        return rows
    # not read_only: some files carry a wrong <dimension> (A1:A1) that read-only mode trusts
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except zipfile.BadZipFile:
        wb = openpyxl.load_workbook(io.BytesIO(_rezip(data)), data_only=True)
    return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]


def _date(v):
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # an Excel serial left unformatted
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    return date(v)


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def parse_rows(rows: list[list], source_file: str) -> list[Advice]:
    head = [_s(x) for x in rows[0]]
    if head[:len(HEADER)] != HEADER:
        raise ValueError(f"unexpected VCredits header {head}")
    advices: "OrderedDict[str, Advice]" = OrderedDict()
    group = None                                   # the invoice group the current rows belong to
    groups = []
    for r in rows[1:]:
        r = (list(r) + [None] * 10)[:10]
        vcode, typ, ref, utr, when, amount, entity = (_s(r[0]), _s(r[3]), _s(r[4]), _s(r[5]), _date(r[6]),
                                                      num(r[7]), _s(r[8]))
        if not typ or amount is None:
            continue
        if typ == "Invoice":
            group = {"invoice": our_invoice_no(ref) or ref, "date": when, "amount": amount,
                     "tds": 0.0, "pr": 0.0, "adj": 0.0, "paid": 0.0, "lines": []}
            groups.append(group)
            continue
        if typ != "Payment":
            if group is not None:
                group[{"TDS": "tds", "PR": "pr"}.get(typ, "adj")] += -amount
            continue
        key = utr or (f"{entity.upper()}-{vcode}-{when:%Y%m%d}" if when else None)
        if key is None:
            raise ValueError(f"payment row with neither UTR nor date: {r}")
        adv = advices.get(key)
        if adv is None:
            adv = advices[key] = Advice(
                customer="instamart", payer_name=entity, advice_ref=key, utr=utr or None, payment_date=when,
                payment_amount=0.0, source_file=source_file,
                extra={"customer_code": ENTITIES.get(entity.upper()), "vendor_code": vcode,
                       "payment_amount": "sum of the transfer's Payment rows (no printed total)"})
        inv = group["invoice"] if group else None
        ours = bool(inv and our_invoice_no(inv))
        line = Line(invoice_no=inv if ours else None, invoice_date=group["date"] if group else None,
                    doc_ref=None if ours else (inv or ref or None), line_type="INVOICE" if ours else "OTHER",
                    description="Payment" if not group else None,
                    gross_amount=group["amount"] if group else None, net_amount=-amount)
        adv.lines.append(line)
        adv.payment_amount = round(adv.payment_amount - amount, 2)
        if group is not None:
            group["paid"] += -amount
            group["lines"].append(line)
    # TDS / purchase returns are per invoice, not per payment: put them on the payment line only when the
    # invoice settles in one payment inside this file (otherwise an instalment would carry them twice).
    for g in groups:
        if len(g["lines"]) == 1 and abs(g["amount"] - g["tds"] - g["pr"] - g["adj"] - g["paid"]) < 1:
            ln = g["lines"][0]
            ln.tds_amount = round(g["tds"], 2) or None
            ln.deduction_amount = round(g["pr"] + g["adj"], 2) or None
        for ln in g["lines"]:
            ln.extra = {k: round(g[k], 2) for k in ("tds", "pr", "adj") if g[k]}
    return list(advices.values())


def parse(mail: Mail) -> list[Advice]:
    books = [a for a in mail.attachments if "vcredits" in a.name.lower() and a.name.lower().endswith((".xlsx", ".xls"))]
    if not books:
        raise Skip("no VCredits workbook")
    out = []
    for a in books:
        out += parse_rows(_rows(a.data), a.name)
    if not out:
        raise Skip("workbook has no Payment rows")
    return out
