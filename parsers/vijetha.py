"""
Vijetha Supermarkets - payments@vijetha.in, 'Payment Advice', a Tally bill-wise PDF.

    Date : 15-Sep-26
    Bill Ref. Bill Date Amount
    Agst Ref / 27HYD-00792 4-May-26 790.00
    New Ref / DN-123 5-May-26 1,200.00 Dr        (Dr = their debit note, deducted)
    Less : VJ01/12650 13-May-26 (-)807.00         their return / debit note, deducted
    Nett Amount (rupee) 1,12,841.00             printed amount
    Payment Details: 'Cheque No.: 101521 Hdfc Bank ...' / 'Rtgs ...' / 'Neft ...'
Tally has no payment number, so advice_ref = the instrument number when printed, else
'<date>/<amount>'.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

_N = r"[\d,]+\.\d{2}"
ROW_RE = re.compile(r"^(Agst Ref|New Ref|Advance|On Account)\s*/?\s*(.*?)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})?\s*("
                    + _N + r")\s*(Dr|Cr)?\s*$")
LESS_RE = re.compile(r"^Less\s*:\s*(.*?)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})?\s*\(-\)\s*(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    if "Payment Advice" not in text:
        raise Skip("not a payment advice")
    total = re.search(r"^Nett Amount\s*(?:\(cid:\d+\)|₹)?\s*(" + _N + r")\s*$", text, re.M)   # rupee glyph -> '(cid:299)'
    instr = re.search(r"(?:Cheque No\.?|Chq\.? No\.?|UTR No\.?|Ref No\.?)\s*:\s*(\S+)", text, re.I)
    when = re.search(r"Date\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", text)
    amount = num(total.group(1)) if total else None
    pay_date = date(when.group(1)) if when else None
    adv = Advice(customer="vijetha", payer_name="Vijetha Supermarkets Pvt Ltd",
                 advice_ref=instr.group(1) if instr else (f"{pay_date}/{amount}" if pay_date and amount else None),
                 utr=instr.group(1) if instr else None, payment_date=pay_date, payment_amount=amount,
                 source_file=source_file)
    for s in (l.strip() for l in text.splitlines()):
        less = LESS_RE.match(s)
        if less:
            adv.lines.append(Line(doc_ref=less.group(1), invoice_date=date(less.group(2)), line_type="DEDUCTION",
                                  description="Less", net_amount=-num(less.group(3))))
            continue
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, amt, side = m.group(2).strip(), num(m.group(4)), m.group(5)
        net = -amt if side == "Dr" else amt
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else (ref or m.group(1)),
                              invoice_date=date(m.group(3)), line_type="INVOICE" if ours and net >= 0 else "DEDUCTION",
                              description=m.group(1), net_amount=net))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf") or a.data[:5] == b"%PDF-"]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
