"""
ICICI Bank e-payment advices - ipaycheck@icicibank.com / ipaycheck@icici.bank.in.
Two RPSG companies pay us this way, same template: Nature's Basket
(NBLVEN_<ddmmyy>_<n>.PDF) and Spencer's Retail (SPENCERNEW_*.PDF), one PDF per mail.

Page 1 header:
    NATURES BASKET LIMITED                      <- payer, first line of every page
    Account No. : XXXX  Value Date : 12-Oct-2026
    ... Payment Document No. : 2000000001       <- advice_ref
    ... UTR No. : IN20000000000001
    ... for the value of Rs. 100,000.00 (Rupees ...)   <- printed payment amount
Table, repeated on every page:
    Invoice No. | GRN No. | GRN Date (ddmmyyyy) | DOC Type | Gross Amount | TDS Amount | Discount Amount | Net Amount
Negatives carry a trailing '-', Net a trailing '.' ('1234.56.', '168.00-.').
Net = Gross - TDS - Discount, and the Nets add up to the payment. A negative
Discount is an add-back (Nature's Basket pays more than the GRN value).
Line types: our invoice -> INVOICE; negative-gross 89xxxxxxxx / 44xxxxxxxx /
IN2025... documents -> DEBIT_NOTE (returns, shortages); 'BFS JAN-26/152' ->
DEDUCTION; 'ON A/C' -> ON_ACCOUNT (+ or -). The DOC Type column is blank on
every advice seen. Invoice numbers sometimes print truncated ('08839' for
27MUM-08839): kept as printed and flagged in extra.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

PAYERS = {"NATURES BASKET LIMITED": "natures_basket",
          "SPENCER'S RETAIL LIMITED": "spencers"}     # same RPSG template, SPENCERNEW_*.PDF

OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+$")
_N = r"[\d,]+\.\d{2}-?"
# Invoice column may hold spaces ('BFS JAN-26/152', 'ON A/C'), so it is matched lazily up to the GRN no.
ROW = re.compile(rf"^(.+?)\s+(\d{{6,12}})\s+(\d{{8}})\s+(?:(\S+?)\s+)?({_N})\s+({_N})\s+({_N})\s+({_N})\.?$")


def _field(text: str, label: str) -> str | None:
    m = re.search(label + r"\s*:\s*(\S+)", text)
    return m.group(1).strip() if m else None


def parse_pdf(text: str, source_file: str) -> Advice:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        raise ValueError("empty PDF text")
    payer = lines[0].upper()
    customer = PAYERS.get(payer)
    if customer is None:
        raise ValueError(f"unknown ICICI payer {lines[0]!r}")
    from feeds import CUSTOMERS
    if customer not in CUSTOMERS:
        raise ValueError(f"payer {lines[0]!r} has no customer table yet: add {customer!r} to feeds.CUSTOMERS")
    amount = re.search(r"for the value of Rs\.?\s*([\d,]+\.\d{2})", text)
    adv = Advice(
        customer=customer,
        payer_name=lines[0],
        advice_ref=_field(text, r"Payment Document No\.?"),
        utr=_field(text, r"UTR No\.?"),
        payment_date=date(_field(text, r"Value Date")),
        payment_amount=num(amount.group(1)) if amount else None,
        source_file=source_file,
        extra={k: v for k, v in {
            "bank_reference": _field(text, r"Bank Reference No\.?"),
            "payer_ref": _field(text, r"Payer Ref\.?"),
            "payment_mode": _field(text, r"Payment Mode"),
        }.items() if v},
    )
    unmatched = []
    in_table = False
    for l in lines:
        if l.startswith("Invoice No.") and "Net Amount" in l:
            in_table = True
            continue
        if not in_table:
            continue
        m = ROW.match(l)
        if not m:
            if re.search(_N, l) and not l.startswith("This is a computer"):
                unmatched.append(l)
            continue
        inv, grn, grn_date, doc_type, gross, tds, disc, net = m.groups()
        g, t, d, n = num(gross), num(tds), num(disc), num(net)
        if abs(round(g - t - d - n, 2)) > 0.01:
            raise ValueError(f"row does not add across: {l!r}")
        extra = {"grn_date": grn_date, "grn_no": grn}
        ours = bool(OUR_INVOICE.match(inv))
        if inv.upper().startswith("ON A/C"):
            kind = "ON_ACCOUNT"
        elif inv.upper().startswith("BFS"):
            kind = "DEDUCTION"
        elif g < 0:
            kind = "DEBIT_NOTE"
        else:
            kind = "INVOICE"
        if kind == "INVOICE" and not ours:
            extra["invoice_no_as_printed"] = inv       # truncated in the PDF, e.g. '08839'
        adv.lines.append(Line(
            invoice_no=inv if kind == "INVOICE" else None,
            doc_ref=grn if kind == "INVOICE" else inv,
            line_type=kind,
            description=doc_type,
            gross_amount=g, tds_amount=t, deduction_amount=d, net_amount=n,
            extra=extra,
        ))
    if unmatched:
        adv.extra["unparsed_rows"] = unmatched[:20]
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_pdf(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
