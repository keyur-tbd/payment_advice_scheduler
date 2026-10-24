"""
Namdhari Agro Fresh - erp@namdharis.in, 'Payment Advice', one SAP PDF per mail.

Header: Date: 05-August 2026 | Clearing Doc. No.: 5600002308 (advice_ref) | UTR No.: IFT...
Rows (header repeats per page):
    Bill Ref. No. | Accounting Document No | Bill Doc. Date | Bill Amount | Deduction/TDS | Net Amount
    27BLR-02430      5100011642  24-05-2026   2016.01   0.00   2,016.01     our invoice
    TBD.PRN.24.05.26 5100011655  24-05-2026  -4271.27   0.00  -4,271.27     their PRN / debit note
    4320097509       5100012044  26-05-2026   -611.25   0.00    -611.25     their return document
    9366291408 ABDUL 5100016459  16-06-2026   -800.25   0.00    -800.25     refs may contain spaces
Footer: 'Net Amount 100,626.99' (printed amount), then payment mode and 'Total'.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

_N = r"-?[\d,]+(?:\.\d+)?"
ROW_RE = re.compile(r"^(\S.*?)\s+(\d{8,12})\s+(\d{2}-\d{2}-\d{4})\s+(" + _N + r")\s+(" + _N + r")\s+(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    if "Payment Advice" not in text or "NAMDHARI" not in text.upper():
        raise Skip("not a Namdhari payment advice")

    def find(p):
        m = re.search(p, text, re.I)
        return m.group(1).strip() if m else None
    total = re.search(r"^Net Amount\s+(" + _N + r")\s*$", text, re.M)
    adv = Advice(customer="namdhari", payer_name="Namdhari Agro Fresh Pvt Ltd",
                 advice_ref=find(r"Clearing Doc\.?\s*No\.?\s*:\s*(\S+)"), utr=find(r"UTR No\.?\s*:\s*(\S+)"),
                 payment_date=date(find(r"\bDate:\s*(\d{1,2}-[A-Za-z]+ \d{4})")),
                 payment_amount=num(total.group(1)) if total else None, source_file=source_file,
                 extra={"sap_vendor_code": find(r"SAP Vendor Code\s*:\s*(\S+)")})
    for s in (l.strip() for l in text.splitlines()):
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, bill, ded, net = m.group(1), num(m.group(4)), num(m.group(5)), num(m.group(6))
        ours = bool(OUR_INVOICE.match(ref))
        kind = "INVOICE" if ours and (net or 0) >= 0 else ("PRN" if "PRN" in ref.upper() else "DEDUCTION")
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=m.group(2) if ours else f"{ref} / {m.group(2)}",
                              invoice_date=date(m.group(3)), line_type=kind, gross_amount=bill,
                              tds_amount=ded or None, net_amount=net))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
