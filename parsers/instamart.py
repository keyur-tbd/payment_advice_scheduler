"""
Swiggy Instamart - the distributor entities pay through Zoho Books, which mails
instamart@ (not accounts@) 'Notification of payment success.' from
message-service@sender.zoho-books.in with a 'Payment-<no>.pdf':

    Moksh Enterprises Private Limited            <- payer entity (first line)
    PAYMENTS MADE
    Payment# 344814
    Payment Date 07-10-2026 4,22,417.74          <- printed amount ('Amount Paid'); some books print 05/10/2026
    Reference Number 6FlV0bl7Z5edhZfcXibwJO | 4RV9MXGHKOXP8
    UTR Number HSBCN52026100895333016
    Payment for
    Bill Number Bill Date Bill Amount Payment Amount
    27BLR-08404 28-09-2026 1,72,228.24 1,62,475.57   <- our invoice; part-paid when the amounts differ

advice_ref = the UTR (Zoho payment numbers repeat across the entities' books), else
'<entity>-<payment#>'. 'Notification of payment failure.' mails are skipped. 2023-24 notices
(Setron Retail) carry no PDF and load header_only from the mail body.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

ENTITIES = (                         # first PDF line -> ERP customer
    ("CLOUDSTORE", "C00152"),
    ("CLOUDKART", "C00153"),
    ("JUPITER KART", "C00154"),
    ("PJTJ", "C00156"),
    ("MOKSH", "C00157"),
    ("AARIGA", "C00196"),            # Aariga Strategy Consultants, same Swiggy Zoho books (Jan-Mar 2026)
    ("SETRON", "C00183"),            # Setron Retail, 2023-24: mail body only, no PDF
)
_N = r"[\d,]+(?:\.\d{1,2})?"
ROW_RE = re.compile(r"^(\S+)\s+(\d{2}[-/]\d{2}[-/]\d{4})\s+(" + _N + r")\s+(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if "PAYMENTS MADE" not in text.upper():
        raise Skip("not a Zoho 'payments made' voucher")
    payer = lines[0]
    code = _code(payer)

    def find(p):
        m = re.search(p, text, re.I | re.M)
        return m.group(1).strip() if m else None
    pay_no = find(r"^Payment#\s*(\S+)")
    utr = find(r"^UTR Number\s+(\S+)")
    when = re.search(r"^Payment Date\s+(\d{2}[-/]\d{2}[-/]\d{4})\s+(" + _N + r")", text, re.M)
    adv = Advice(customer="instamart", payer_name=payer,
                 advice_ref=utr or (f"{payer.split()[0].upper()}-{pay_no}" if pay_no else None), utr=utr,
                 payment_date=date(when.group(1)) if when else None,
                 payment_amount=num(when.group(2)) if when else None, source_file=source_file,
                 extra={k: v for k, v in {"customer_code": code, "zoho_payment_no": pay_no,
                                          "reference": find(r"^Reference Number\s+(.+)$"),
                                          "paid_to": find(r"^Paid To\s+(.+)$")}.items() if v})
    for s in lines:
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, billed, paid = m.group(1), num(m.group(3)), num(m.group(4))
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else ref,
                              invoice_date=date(m.group(2)), line_type="INVOICE" if ours else "OTHER",
                              gross_amount=billed,
                              deduction_amount=round(billed - paid, 2) if abs(billed - paid) >= 0.01 else None,
                              net_amount=paid))
    return adv


def _code(payer: str):
    code = next((c for p, c in ENTITIES if payer.upper().startswith(p)), None)
    if code is None:
        raise ValueError(f"unknown Instamart paying entity {payer!r}")
    return code


def parse_body(mail: Mail) -> Advice:
    """2023-24 notices without a PDF: payer, UTR, amount and the bill list, no per-bill
    amounts -> header_only, bills kept in the narration."""
    b = re.sub(r"\s+", " ", mail.body)

    def find(p):
        m = re.search(p, b, re.I)
        return m.group(1).strip() if m else None
    payer = find(r"Payment Initiated By:?\s*(.+?)\s+(?:UTR|Transaction)")
    if not payer:
        raise Skip("no payer in the notice")
    bills = find(r"Bill Number#?:?\s*(.+?)\s+Payment Initiated Date")
    utr = find(r"UTR Number:?\s*([A-Z0-9]{8,})")
    txn = find(r"Transaction Id:?\s*([A-Za-z0-9]{8,})")       # 2023 notices print no UTR
    return Advice(customer="instamart", payer_name=payer, advice_ref=utr or (f"ZOHO-{txn}" if txn else None), utr=utr,
                  payment_date=date(find(r"Payment Initiated Date:?\s*(\d{2}[-/]\d{2}[-/]\d{4})")),
                  payment_amount=num(find(r"Payment Initiated For:?\s*₹?\s*([\d,]+(?:\.\d+)?)")),
                  header_only=True,
                  extra={"customer_code": _code(payer), "narration": f"Bills: {bills}" if bills else None,
                         "bills": [x.strip() for x in (bills or "").split(",") if x.strip()]})


def parse(mail: Mail) -> list[Advice]:
    if "failure" in mail.subject.lower():
        raise Skip("payment failure notice")
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        return [parse_body(mail)]
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
