"""
Reliance Retail - RR.Paymentadvice@ril.com, one SAP 'Payment Advice' PDF per mail
(named like 900840000000012026.PDF = vendor 90084 + doc 4200000001 + year 2026).

Text layout:
    We have settled the following items by Doc.number 4200000001/2026 for
    INR 100,000.00
    ____
    Doc. No.  Inv./Ref. Doc.No.  Inv./Ref Doc. Amt.  Payment Amount
    Doc. Date Inv./Ref. Doc.Date
    Narration.
    ____
    11600001   27KOL-00001  101500.00  101,400.00     <- goods receipt: gross, net paid
    07.09.2026 02.09.2026
    *Short payment of Rs. 100.00 ...                   <- free-text narration (optional)
    (TDS Amount 101.40- )
    4370000001 27KOL-00001  1,400.00-                  <- GST adjustment: one amount only
    07.09.2026 02.09.2026
    GST TAX HOLD                                        (GST TAX PAID = hold released, positive)
    ____
    Total INR 100,000.00

The Payment Amount column is already net of TDS and short-payments and is
signed (trailing minus), so it is net_amount. advice_ref = SAP doc number
without the '/2026' year, which is also the 'Payment Ref. No.' on the Axis
copy of the same payment (parsers/axis.py).
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

AMT = r"[\d,]+\.\d{2}-?"
HEAD_RE = re.compile(r"settled the following items by Doc\.?\s*number\s+(\d+)\s*/\s*(\d{4})\s+for\s+INR\s+(" + AMT + ")",
                     re.I | re.S)
TOTAL_RE = re.compile(r"^Total\s+INR\s+(" + AMT + r")\s*$", re.I | re.M)
LINE_RE = re.compile(r"^(\d{6,12})\s+(\S+)\s+(" + AMT + r")(?:\s+(" + AMT + r"))?\s*$")
DATES_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}\.\d{2}\.\d{4}))?\s*$")
TDS_RE = re.compile(r"\(TDS Amount\s+(" + AMT + r")\s*\)", re.I)
DATE_RE = re.compile(r"Date\s*:\s*(\d{2}\.\d{2}\.\d{4})")
SKIP_RE = re.compile(r"^(_{5,}|Doc\. No\.|Doc\. Date|Narration\.|Payment Advice|Page\s*:|Total INR)", re.I)


def _kind(narration: str, net: float) -> str:
    n = narration.upper()
    if "GST TAX HOLD" in n:
        return "GST_HOLD"
    if "GST TAX PAID" in n or "GST TAX RELEASE" in n:
        return "GST_RELEASE"
    if "DEBIT NOTE" in n:
        return "DEBIT_NOTE"
    return "INVOICE" if net >= 0 else "DEDUCTION"


def parse_text(text: str, source_file: str) -> Advice:
    head = HEAD_RE.search(text)
    if not head:
        raise Skip("no 'settled the following items' header")
    total = TOTAL_RE.search(text)
    advice = Advice(customer="reliance", payer_name="Reliance Retail Limited",
                    advice_ref=head.group(1), utr=None,
                    payment_date=date(DATE_RE.search(text).group(1)) if DATE_RE.search(text) else None,
                    payment_amount=num(total.group(1)) if total else num(head.group(3)),
                    source_file=source_file, extra={"fiscal_year": head.group(2)})
    if total and num(total.group(1)) != num(head.group(3)):
        advice.extra["header_amount"] = num(head.group(3))

    body = text[head.end():total.start() if total else len(text)]
    cur: Line | None = None
    notes: list[str] = []

    def close():
        if cur is None:
            return
        narr = " ".join(notes).strip()
        cur.description = narr or None
        cur.line_type = _kind(narr, cur.net_amount or 0.0)
        tds = TDS_RE.search(narr)
        if tds:
            cur.tds_amount = abs(num(tds.group(1)))
        if cur.gross_amount is not None and cur.net_amount is not None:
            ded = round(cur.gross_amount - cur.net_amount - (cur.tds_amount or 0.0), 2)
            cur.deduction_amount = ded if abs(ded) >= 0.01 else None
        advice.lines.append(cur)

    for raw in body.splitlines():
        s = raw.strip()
        if not s or SKIP_RE.match(s):
            continue
        m = LINE_RE.match(s)
        if m:
            close()
            notes = []
            one, two = num(m.group(3)), num(m.group(4)) if m.group(4) else None
            cur = Line(doc_ref=m.group(1), invoice_no=m.group(2),
                       gross_amount=one if two is not None else None,
                       net_amount=two if two is not None else one)
            continue
        if cur is None:
            continue
        d = DATES_RE.match(s)
        if d and not notes and "doc_date" not in cur.extra:
            cur.extra["doc_date"] = str(date(d.group(1)))
            cur.invoice_date = date(d.group(2)) if d.group(2) else None
            continue
        notes.append(s)
    close()
    return advice


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    advices, seen = [], set()
    for att in pdfs:
        a = parse_text(open_pdf_text(att.data, mail.subject), att.name)
        key = (a.advice_ref, a.payment_amount)
        if key in seen:                 # same advice attached twice under two names
            continue
        seen.add(key)
        advices.append(a)
    return advices
