"""
HDFC corporate net banking - corporatenetbanking.automailer@hdfcbank.bank.in, one locked PDF per mail.

Seen only for MAGSON RETAIL AND DIST LTD A/C UDAIPUR (Sep 2026 on). The PDF opens with the
FIRST 6 digits of our receiving account (not the last 6 like HDFC Enet), so the password
comes from PA_PDF_PASSWORDS. Key/value layout, no line table:
    Transaction Date : 11/09/2026     Remitter Name : MAGSON ...
    Transaction Remarks : DOMNEFT01 - UTIB0000003 - C... - MIMANSA UDA 1001 - ...
    Reference No : 1900000001         UTR / RRN No : HDFCH01200000004     Amount : 1000.00
'Payment Details 1..3' are printed but empty, so the advice is header-only and the
remarks (which may name our invoice tails, 'UDA 1001') go to extra['narration'].
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text, squash

PAYERS = {"MAGSON": "magson"}


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"{label}\s*:\s*(.*)", text)
    return m.group(1).strip() if m else None


_AVENUE_ROW = re.compile(r"^(\d{2}[A-Z]{3})[ -](\d+)\s+(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})\s+(\d+)\s(\d{2})$")
_AVENUE_TDS = re.compile(r"^(\d+)\s(\d{2})\s+(\S+)\s+(.*)$")


def _avenue(text: str, source_file: str) -> Advice:
    """Avenue E-commerce's SAP advice as this sender renders it (columns sit elsewhere than in the
    HDFC Enet copy, so rows are read from text): 'INV DATE NET TCS' over 'TDS SITE NAME', where
    TCS/TDS print '315 00' for 315.00."""
    first = text.splitlines()[0].strip()
    if "AVENUE" not in first.upper():
        raise ValueError(f"unknown HDFC corporate payer {first!r}")
    lines = text.splitlines()
    total = re.search(r"Payment Amount\s*\n\s*(\S+)\s+(\S+)\s+INR\s+([\d,]+\.\d{2})", text, re.I)
    adv = Advice(
        customer="dmart_ecom", payer_name=first,
        advice_ref=_field(text, "Payment Doc No").split()[0] if _field(text, "Payment Doc No") else None,
        utr=(re.search(r"UTR/RRN no\s+(\S+)", text) or [None, None])[1],
        payment_date=date(total.group(2)) if total else None,
        payment_amount=num(total.group(3)) if total else None,
        source_file=source_file)
    for i, l in enumerate(lines):
        m = _AVENUE_ROW.match(l)
        if not m:
            continue
        tds = _AVENUE_TDS.match(lines[i + 1]) if i + 1 < len(lines) else None
        net = num(m.group(4))
        adv.lines.append(Line(
            invoice_no=f"{m.group(1)}-{m.group(2)}", invoice_date=date(m.group(3)), line_type="INVOICE",
            tds_amount=float(f"{tds.group(1)}.{tds.group(2)}") if tds else None, net_amount=net,
            extra={"tcs": float(f"{m.group(5)}.{m.group(6)}"),
                   **({"site_code": tds.group(3), "site_name": tds.group(4)} if tds else {})}))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    out = []
    for att in pdfs:
        text = squash(open_pdf_text(att.data, mail.subject))
        if "PAYMENT ADVICE" not in text.upper():
            raise Skip(f"{att.name} is not a payment advice")
        if "Remitter Name" not in text:            # a payer's own SAP advice (Avenue E-commerce, Aug 2026)
            out.append(_avenue(text, att.name))
            continue
        remitter = _field(text, "Remitter Name")
        customer = next((c for k, c in PAYERS.items() if k in remitter.upper()), None)
        if customer is None:
            raise ValueError(f"unknown HDFC corporate payer {remitter!r}")
        m = re.search(r"Transaction Remarks\s*:\s*(.*?)\s*Reference No", text, re.S)
        remarks = " ".join(m.group(1).split()) if m else None
        refs = re.findall(r"MIMANSA\s+([A-Z]+)\s+((?:\d{3,5}\s*)+)", remarks or "")
        out.append(Advice(
            customer=customer, payer_name=remitter,
            advice_ref=_field(text, "Reference No"), utr=_field(text, "UTR / RRN No"),
            payment_date=date(_field(text, "Transaction Date")),
            payment_amount=num(_field(text, "Amount")),
            source_file=att.name, header_only=True,
            extra={"narration": remarks,
                   "narration_refs": [n for _, nums in refs for n in nums.split()],
                   "narration_store": refs[0][0] if refs else None,
                   "remitter_account": _field(text, "Remitter's Account No")}))
    return out
