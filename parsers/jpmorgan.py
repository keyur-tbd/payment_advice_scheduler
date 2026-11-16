"""
J.P. Morgan remittance advices - statements_and_alerts@jpmchase.com, 'Remittance Advice for payment
transferred'. Corporate clients of JPM pay us this way: Compass India Food Services (since Mar 2026)
and Bottle Lab Technologies. The mail body carries everything (the zipped PDF repeats it), one table
cell per line:

    JPM UTR: CHASN52026101662091550                 UTR = advice_ref
    Senders Ref No: 341368700
    Remitter Information:
    BOTTLE LAB TECHNOLOGIES PRIVATE LIMITED         payer
    Payment Amount: 32175                           printed amount
    Value Date: 16-10-2026
    Transaction Details
    Invoice date / Invoice Reference / Gross Amount / TDS (or WHT/TDS) / Net Amount
    10082026                                        a row starts with its ddmmyyyy date
    27GUR-04201
    30643.5
                                                    TDS cell, blank when nil
    30643.5
     Total ...
Compass prints a TDS figure on both rows of a split invoice but deducts it once, so net_amount is
the Net column as printed.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num

PAYERS = (
    ("COMPASS INDIA", "compass"),
    ("BOTTLE LAB", "bottle_lab"),
)
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse(mail: Mail) -> list[Advice]:
    body = mail.body.replace("\xa0", " ")
    lines = [l.strip() for l in body.splitlines()]

    def find(p):
        m = re.search(p, body, re.I | re.M)
        return m.group(1).strip() if m else None
    utr = find(r"JPM UTR:\s*(\S+)")
    if not utr or "Transaction Details" not in body:
        raise Skip("not a JPM remittance advice")
    i = next(k for k, l in enumerate(lines) if l.startswith("Remitter Information"))
    payer = next(l for l in lines[i + 1:] if l)
    customer = next((c for p, c in PAYERS if payer.upper().startswith(p)), None)
    if customer is None:
        raise ValueError(f"unknown JPM remitter {payer!r}")
    adv = Advice(customer=customer, payer_name=payer, advice_ref=utr, utr=utr,
                 payment_date=date(find(r"^Value Date:\s*(\S+)")),
                 payment_amount=num(find(r"^Payment Amount:\s*([\d,.]+)")),
                 extra={k: v for k, v in {"senders_ref": find(r"Senders Ref No:\s*(\S+)"),
                                          "customer_ref": find(r"Customer Ref No:\s*(\S+)"),
                                          "document_ref": find(r"Document Ref:\s*(\S+)")}.items() if v})

    start = next(k for k, l in enumerate(lines) if l == "Net Amount") + 1
    cells, rows = lines[start:], []
    for c in cells:
        if c.lower().startswith("total"):
            break
        if re.fullmatch(r"\d{8}", c):
            rows.append([c])
        elif rows:
            rows[-1].append(c)
    for r in rows:
        r = [x for x in r if x != ""]                  # a nil TDS cell is blank: 4 cells, else 5
        when, ref, gross, net = r[0], r[1], num(r[2]), num(r[-1])
        tds = num(r[3]) if len(r) == 5 else None
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else ref,
                              invoice_date=date(when), line_type="INVOICE" if ours else "OTHER",
                              gross_amount=gross,
                              tds_amount=round(gross - net, 2) if gross is not None and net is not None
                              and abs(gross - net) >= 0.01 else None,
                              net_amount=net, extra={"tds_printed": tds} if tds else {}))
    return [adv]
