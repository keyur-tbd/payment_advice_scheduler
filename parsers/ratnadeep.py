"""
Ratnadeep Retail - automail@ratnadeepretail.com, 'Ratnadeep_Mimansa Industries P_Rs. 33432.13 /-Dt. 26-09-26',
one PDF per mail laid out as cheque-style vouchers, one per page:

    Cheque 538166401 / Date 26-09-2026 / Payee ...
    Supplier invoice no | Inv date | Paid amount
    27HYD-03294          21-8-26   2,278.55        our invoice
    PRN|0004460602|A29   24-9-26   -2,031.00       purchase return (U+2212 minus, sometimes '- 391.00')
    TDS|26-27|R63        1-9-26    339.00          TDS line, signed as printed
    (page subtotal in words + figures)
    TOTAL FOR PAYMENT INR 33,432.13                 printed amount, last page

advice_ref = the first voucher ('Cheque') number; later pages carry their own numbers,
kept in extra.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

ROW_RE = re.compile(r"^(\S+)\s+(\d{1,2}-\d{1,2}-\d{2,4})\s+(-?\s*[\d,]+\.\d{2})\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    text = text.replace("−", "-")
    total = re.search(r"TOTAL FOR PAYMENT\s+INR\s+(-?[\d,]+\.\d{2})", text)
    if not total and "Supplier invoice no" not in text:
        raise Skip("not a Ratnadeep payment voucher")
    vouchers = re.findall(r"^Cheque\s+(\d+)", text, re.M)
    when = re.search(r"^Date\s+(\d{2}-\d{2}-\d{4})", text, re.M)
    adv = Advice(customer="ratnadeep", payer_name="Ratnadeep Retail Ltd",
                 advice_ref=vouchers[0] if vouchers else None, utr=None,
                 payment_date=date(when.group(1)) if when else None,
                 payment_amount=num(total.group(1)) if total else None, source_file=source_file,
                 extra={"vouchers": vouchers} if len(vouchers) > 1 else {})
    for s in (l.strip() for l in text.splitlines()):
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, net = m.group(1), num(m.group(3).replace(" ", ""))
        up = ref.upper()
        if OUR_INVOICE.match(ref):
            kind, inv = "INVOICE", re.sub(r"^(\d{2}[A-Z]{3})-0*(\d+)$", lambda x: f"{x.group(1)}-{int(x.group(2)):05d}", ref)
        else:
            kind, inv = ("PRN" if up.startswith("PRN") else "TDS" if up.startswith("TDS") else "DEDUCTION"), None
        adv.lines.append(Line(invoice_no=inv, doc_ref=None if inv else ref, invoice_date=date(m.group(2)),
                              line_type=kind, net_amount=net,
                              extra={"printed_ref": ref} if inv and inv != ref else {}))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
