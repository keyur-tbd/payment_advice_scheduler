"""
Shri Balaji Fruits - Zoho Inventory 'Payment has been made for your invoice(s)' from
message-service@sender.zoho-inventory.in (accounts@, since Feb 2026) with 'Payment-<no>.pdf':

    Shri Balaji Fruits Private Limited          payer (first line)
    PAYMENTS MADE
    Payment# 2378                               advice_ref = SBF-<payment#>
    Payment Date 05/11/2026 2,69,792.42         printed amount
    Reference Number RTGS/ICICR42026110500604929/UTIB0000003    UTR = second '/' field
    /81847468/P                                 (wraps)
    Bill Number Bill Date Bill Amount Payment Amount
    27GUR-05474 20/09/2026 9,618.30 8,735.29    part-paid when the amounts differ
    27-GUR-06311 15/10/2026 ...                 retyped invoice nos normalised (pa_common.our_invoice_no)
Same Zoho layout as the Instamart payers' Zoho Books vouchers (parsers/instamart.py).
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text, our_invoice_no
from parsers.instamart import OUR_INVOICE, ROW_RE, _N


def parse_text(text: str, source_file: str) -> Advice:
    if "PAYMENTS MADE" not in text.upper():
        raise Skip("not a Zoho 'payments made' voucher")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if "BALAJI" not in lines[0].upper():
        raise ValueError(f"unknown Zoho Inventory payer {lines[0]!r}")

    def find(p):
        m = re.search(p, text, re.I | re.M)
        return m.group(1).strip() if m else None
    pay_no = find(r"^Payment#\s*(\S+)")
    reference = find(r"^Reference Number\s+(.+)$")
    utr = None
    if reference:
        parts = [p for p in reference.split("/") if p]
        utr = next((p for p in parts if re.fullmatch(r"[A-Z]{4}[A-Z0-9]{8,}", p)), None)
    when = re.search(r"^Payment Date\s+(\d{2}[-/]\d{2}[-/]\d{4})\s+(" + _N + r")", text, re.M)
    adv = Advice(customer="shri_balaji", payer_name=lines[0],
                 advice_ref=f"SBF-{pay_no}" if pay_no else utr, utr=utr,
                 payment_date=date(when.group(1)) if when else None,
                 payment_amount=num(when.group(2)) if when else None, source_file=source_file,
                 extra={k: v for k, v in {"zoho_payment_no": pay_no, "reference": reference,
                                          "payment_mode": find(r"^Payment Mode\s+(.+)$")}.items() if v})
    for s in lines:
        m = ROW_RE.match(s)
        if not m:
            continue
        ref = our_invoice_no(m.group(1)) or m.group(1)      # '27-GUR-06311', '27gur-01946', '27GUR02831'
        billed, paid = num(m.group(3)), num(m.group(4))
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else ref,
                              invoice_date=date(m.group(2)), line_type="INVOICE" if ours else "OTHER",
                              gross_amount=billed,
                              deduction_amount=round(billed - paid, 2) if abs(billed - paid) >= 0.01 else None,
                              net_amount=paid))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.data[:5] == b"%PDF-"]
    if not pdfs:
        raise Skip("no payment PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
