"""
BigBasket (Innovative Retail Concepts) - t1..t4payments@bigbasket.com, one .xlsx per mail.

Sheet layout (pivot export): title rows, then
    Payee Site | UTR No | UTR Date | Invoice Number | Sum of Invoice Amount
Payee site / UTR / date are filled only on the first row of each UTR group, and
each group closes with a '<UTR> Total' row. One UTR = one advice; the Total row
is the printed amount the lines must add up to.

Invoice Number encodes the line type:
    27GUR-02931            invoice
    27GUR-02931_GDN        goods-discrepancy note against that invoice
    TDS_27GUR-02931[_GDN]  TDS on the invoice / on the GDN
    PRN-100001-BBHYD002    purchase return note
    TDS_PRN-...            TDS reversal on a PRN
    1290000000001_ADDN     other deduction (marketing, visibility...)
"""

from __future__ import annotations

import io
import re

from pa_common import Advice, Line, Mail, Skip, date, num

ROUND = 2


def _line(ref: str, amount: float) -> Line:
    ref = ref.strip()
    body = ref[4:] if ref.upper().startswith("TDS_") else ref
    is_tds = body is not ref
    m = re.match(r"^(.*?)_([A-Z]+)$", body)
    suffix = m.group(2) if m else ""
    base = m.group(1) if m else body
    if is_tds:
        kind = "TDS"
    elif suffix == "GDN":
        kind = "GDN"
    elif base.upper().startswith("PRN"):
        kind = "PRN"
    elif suffix:
        kind = "DEDUCTION"
    else:
        kind = "INVOICE"
    is_prn = base.upper().startswith("PRN")
    return Line(
        invoice_no=None if is_prn or kind == "DEDUCTION" else base,
        doc_ref=ref if (is_prn or suffix or is_tds) else None,
        line_type=kind,
        description=suffix or None,
        net_amount=round(amount, ROUND),
        extra={"bb_reference": ref},
    )


def parse(mail: Mail) -> list[Advice]:
    import openpyxl

    books = [a for a in mail.attachments if a.name.lower().endswith((".xlsx", ".xlsm"))]
    if not books:
        raise Skip("no workbook attached")
    advices: list[Advice] = []
    for att in books:
        wb = openpyxl.load_workbook(io.BytesIO(att.data), data_only=True, read_only=True)
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            hdr_i = next((i for i, r in enumerate(rows[:10])
                          if r and any(str(c or "").strip().lower() in ("utr no", "utr number") for c in r)), None)
            if hdr_i is None:
                continue
            hdr = [str(c or "").strip().lower() for c in rows[hdr_i]]

            def col(*names):
                return next(i for i, h in enumerate(hdr) if h in names)
            # Sep 2025 layout: Party Ledger Name | UTR NUMBER | UTR DATE ('17/09/2025' text) | Inv Ref Number | Sum of Amount
            c_site = col("payee site", "party ledger name")
            c_utr, c_date = col("utr no", "utr number"), col("utr date")
            c_inv = col("invoice number", "inv ref number")
            c_amt = next(i for i, h in enumerate(hdr) if "amount" in h)

            cur: Advice | None = None
            site = None
            for r in rows[hdr_i + 1:]:
                if not r or all(c in (None, "") for c in r):
                    continue
                utr_cell = str(r[c_utr] or "").strip()
                if utr_cell.lower().endswith("total"):
                    if utr_cell.lower().startswith("grand"):
                        continue
                    if cur is None or utr_cell[:-len("total")].strip() != cur.utr:
                        raise ValueError(f"total row {utr_cell!r} does not close UTR {cur and cur.utr!r}")
                    cur.payment_amount = round(num(r[c_amt]), ROUND)
                    advices.append(cur)
                    cur = None
                    continue
                if r[c_site]:
                    site = str(r[c_site]).strip()
                if utr_cell:
                    if cur is not None:     # a 13 Jan 2026 workbook has no Total rows: nothing to check
                        advices.append(cur)  # against, so it lands as no_total and is not loaded
                    cur = Advice(customer="bigbasket", payer_name="Innovative Retail Concepts Pvt Ltd",
                                 advice_ref=utr_cell, utr=utr_cell, payment_date=date(r[c_date]),
                                 payment_amount=None, source_file=att.name,
                                 extra={"payee_site": site})
                if cur is None:
                    raise ValueError(f"line before any UTR: {r}")
                amount = num(r[c_amt])
                if r[c_inv] in (None, "") or amount is None:
                    continue
                cur.lines.append(_line(str(r[c_inv]), amount))
            if cur is not None:                       # sheet ended without a Total row
                advices.append(cur)
    if not advices:
        raise Skip("workbook has no UTR groups")
    return advices
