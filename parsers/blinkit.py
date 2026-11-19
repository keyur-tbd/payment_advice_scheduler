"""
Blink Commerce (Blinkit) - sap@zomato.com, 'Payment Advice - YYYYMMDD', one 'Payment Advice.PDF' per UTR.
Mailed to kolwarehouse@ (not accounts@); body: 'Please find attached Payment Advice for UTR No. : HDFCR5...'.

Header: Vendor Code 100066455 (one per our warehouse) | Payment Date Oct 29, 2026 |
        'UTR Details' / 'UTR Amount' / HDFCR52026102978850147 1,238,432.77   (the cash; payment_amount)
Invoice Break-up (header repeats per page):
    Invoice Reference Invoice Date Base Value GST Total Amount TDS Deducted Net Payable
    27GUR-06167 Oct 10, 2026 3,297.33 164.86 3,462.19 3.3 3,458.89
    Total 1,191,339.550 57,537.390 1,248,876.940 1,191.350 1,247,685.590
Credit Note/Debit Note Adjusted (header repeats per page):
    CN/DN Reference Invoice Reference CN/DN Date CN/DN Description CN/DN Amount
    D278289DN2613197 27GUR-06132 Oct 10, 2026 Discrepancy Note 29.97          Blinkit's debit note, deducted
    9771473-DN-PV 27GUR-06137 Oct 10, 2026 Price Variance 724.44
    202 000002710 Jul 27, 2026 Brand Fund 388,594                 refs may hold a space (brand fund)
Before ~Aug 2026 the CN/DN table has no Invoice Reference column:
    CN/DN Reference CN/DN Date CN/DN Description CN/DN Amount
    D32268DN26013663 Jul 5, 2026 Discrepancy Note 7,607.35
Adjustment Details (price variance per invoice, deducted):
    Invoice Reference Invoice Date Amount Adjustment Type
    27KOL-02958 Oct 10, 2026 2,304.54 Variance
'Other Adjustment 2' (a rupee or two of rounding, deducted). Three July advices also print 'Advance Adjusted' /
'Advance Paid' (Blinkit's running advance account; their Final Amount is meaningless) -> kept in extra only.
Footer: 'Final Amount 1,238,433.59' = Net Payable less deductions; the UTR amount sits within a rupee of it.
"""

from __future__ import annotations

import datetime as dt
import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

_N = r"-?[\d,]+(?:\.\d+)?"
_D = r"([A-Z][a-z]{2} \d{1,2}, \d{4})"
INV_RE = re.compile(r"^(\S+)\s+" + _D + r"\s+(" + _N + r")\s+(" + _N + r")\s+(" + _N + r")\s+(" + _N + r")\s+(" + _N + r")\s*$")
CN_RE = re.compile(r"^(.+?)\s+(\S+)\s+" + _D + r"\s+(?:(.*?)\s+)?(" + _N + r")\s*$")   # description may be blank
CN_OLD_RE = re.compile(r"^(.+?)\s+" + _D + r"\s+(?:(.*?)\s+)?(" + _N + r")\s*$")   # 27MUM-09569R Sep 26, 2026 237,760.61
ADJ_RE = re.compile(r"^(\S+)\s+" + _D + r"\s+(" + _N + r")\s*(.*)$")
UTR_RE = re.compile(r"^([A-Z0-9]{10,30})\s+(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    if "Payment advice from Blinkit" not in text and "Blinkit Finance" not in text:
        raise Skip("not a Blinkit payment advice")

    def find(p):
        m = re.search(p, text, re.I | re.M)
        return m.group(1).strip() if m else None

    lines = [l.strip() for l in text.splitlines()]
    utrs, section = [], None
    adv = Advice(customer="blinkit", payer_name="Blink Commerce Pvt Ltd", advice_ref=None, utr=None,
                 payment_date=date(find(r"^Payment Date\s+(.+)$")), payment_amount=None, source_file=source_file,
                 extra={"vendor_code": find(r"^Vendor Code\s+(\S+)"),
                        "final_amount": num(find(r"^Final Amount\s+(" + _N + r")"))})
    for s in lines:
        if s.startswith("UTR Details"):
            section = "utr"; continue
        if s.startswith("Invoice Break-up"):
            section = "inv"; continue
        if s.startswith("Credit Note/Debit Note"):
            section = "cn"; continue
        if s.startswith("CN/DN Reference CN/DN Date"):
            section = "cn_old"; continue
        if s.startswith("CN/DN Reference Invoice"):
            section = "cn"; continue
        if s.startswith("Adjustment Details"):
            section = "adj"; continue
        if s.startswith("Final Amount") or s.startswith("In case of any"):
            section = None; continue
        m = re.match(r"^Advance (Adjusted|Paid)\s+(" + _N + r")$", s)
        if m:
            adv.extra["advance_" + m.group(1).lower()] = num(m.group(2)); continue
        m = re.match(r"^Other Adjustment\s+(" + _N + r")$", s)
        if m:
            amt = num(m.group(1))
            adv.lines.append(Line(line_type="DEDUCTION", description="Other Adjustment",
                                  deduction_amount=amt, net_amount=-amt if amt is not None else None))
            continue
        if section == "utr":
            m = UTR_RE.match(s)
            if m:
                utrs.append((m.group(1), num(m.group(2))))
        elif section == "inv":
            m = INV_RE.match(s)
            if m and not s.startswith("Total"):
                ref = m.group(1)
                ours = bool(OUR_INVOICE.match(ref))
                adv.lines.append(Line(
                    invoice_no=ref if ours else None, doc_ref=None if ours else ref,
                    invoice_date=date(m.group(2)), line_type="INVOICE",
                    gross_amount=num(m.group(5)), tds_amount=num(m.group(6)) or None, net_amount=num(m.group(7)),
                    extra={"base_value": num(m.group(3)), "gst": num(m.group(4))}))
        elif section == "cn":
            m = CN_RE.match(s)
            if m and not s.startswith("Total") and not s.startswith("CN/DN Reference"):
                amt = num(m.group(5))
                inv = m.group(2)
                desc = m.group(4) or ""
                adv.lines.append(Line(
                    invoice_no=inv if OUR_INVOICE.match(inv) else None, doc_ref=m.group(1),
                    invoice_date=date(m.group(3)),
                    line_type="CREDIT_NOTE" if "credit" in desc.lower() else "DEBIT_NOTE",
                    description=desc or None, deduction_amount=amt, net_amount=-amt if amt is not None else None))
        elif section == "cn_old":
            m = CN_OLD_RE.match(s)
            if m and not s.startswith("Total"):
                amt, desc = num(m.group(4)), m.group(3) or ""
                adv.lines.append(Line(
                    doc_ref=m.group(1), invoice_date=date(m.group(2)),
                    line_type="CREDIT_NOTE" if "credit" in desc.lower() else "DEBIT_NOTE",
                    description=desc or None, deduction_amount=amt, net_amount=-amt if amt is not None else None))
        elif section == "adj":
            m = ADJ_RE.match(s)
            if m and not s.startswith("Total") and not s.startswith("Invoice Reference"):
                amt, inv = num(m.group(3)), m.group(1)
                adv.lines.append(Line(
                    invoice_no=inv if OUR_INVOICE.match(inv) else None, doc_ref=None if OUR_INVOICE.match(inv) else inv,
                    invoice_date=date(m.group(2)), line_type="DEDUCTION", description=m.group(4) or "Adjustment",
                    deduction_amount=amt, net_amount=-amt if amt is not None else None))
    if not utrs:
        raise Skip("no UTR line found")
    adv.utr = utrs[0][0] if len(utrs) == 1 else ", ".join(u for u, _ in utrs)
    adv.advice_ref = utrs[0][0]
    adv.payment_amount = round(sum(a or 0 for _, a in utrs), 2)
    if len(utrs) > 1:
        adv.extra["utrs"] = [{"utr": u, "amount": a} for u, a in utrs]
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    out = [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
    m = re.search(r"Payment Advice - (\d{8})", mail.subject or "")
    for adv in out:
        if adv.payment_date is None and m:     # a few PDFs print 'Payment Date' with no value
            adv.payment_date = dt.date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]))
            adv.extra["payment_date_from"] = "mail subject"
    return out
