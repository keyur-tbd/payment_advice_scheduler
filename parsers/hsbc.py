"""
HSBC Global Payments advices - advising.service@advising.hsbc.com, 'Payment Advice - Advice Ref:[...]',
one 'Payment_Advice.pdf' per payment. Flipkart India pays us this way (since Sep 2023); Zepto and
Kiranakart copies arrive here too, but pa_zepto already loads those from payments@zeptonow.com.

    Advice reference no: A2wAFKpe5swU-IN                    advice_ref (2023 wraps the '-IN' onto the next line)
    ... Remittance amount: INR37,451.74                      printed amount
    ... Value date: 09 Nov 2026                              payment date
    ... Remitter's name: HSBC-0**********1-FKI               masked payer ('FLI****T IN**A PRI***E L*D' until 2024)
    Other reference: HSBCN31306650986650986                  UTR
    Document Number Amount Date Net Amount Narration/Inv No
    1441004 INR 37451.74 06/10/2026 37451.74 20261006-2400191-27A
    HD-02861                                                 narration wraps: <date>-<vendor>-<our invoice / RDN-...>

Two layouts of the Net Amount column:
  * 2023-24: per line (Amount = invoice value, Net = what this payment settles of it);
  * 2025 on: the payment total repeated on every row, and Amount is the signed line (RDN = Flipkart's
    return debit note, negative).
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text, our_invoice_no

_N = r"-?[\d,]*\.?\d+"
ROW_RE = re.compile(r"^(\S+)\s+INR\s+(" + _N + r")\s+(\d{2}/\d{2}/\d{4})\s+(" + _N + r")(?:\s+(.*))?$")
WRAP_RE = re.compile(r"^[A-Za-z0-9\-][A-Za-z0-9\-/_.]*$")    # the wrap can start with '-' ('-FKIIN41908629')
NARR_RE = re.compile(r"^(\d{8})-([^-]*)-(.+)$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3,6}-?\d{3,}$")
PAYERS = (                                        # masked remitter name -> customer key
    (re.compile(r"FKI$|^FLI\*+T IN\*+A"), "flipkart"),
)
ZEPTO = re.compile(r"^(ZE\*+O|KIRA\*+T)")


def parse_text(text: str, source_file: str) -> Advice:
    def find(p, flags=re.M):
        m = re.search(p, text, flags)
        return m.group(1).strip() if m else None

    remitter = find(r"Remitter's name:\s*(.+)$")
    if remitter is None:
        raise Skip("not an HSBC payment advice")
    if ZEPTO.match(remitter):
        raise Skip(f"Zepto/Kiranakart copy ({remitter}); pa_zepto loads it from payments@zeptonow.com")
    customer = next((c for p, c in PAYERS if p.search(remitter)), None)
    if customer is None:
        raise ValueError(f"unknown HSBC remitter {remitter!r}")

    ref = re.search(r"Advice reference no:\s*(\S+)(?:\s*\n(IN)\b)?", text)
    advice_ref = (ref.group(1) + (ref.group(2) or "")) if ref else None
    adv = Advice(customer=customer, payer_name=remitter, advice_ref=advice_ref,
                 utr=find(r"Other reference:\s*(\S+)"),
                 payment_date=date(find(r"Value date:\s*(\d{2} \w{3} \d{4})")),
                 payment_amount=num(find(r"Remittance amount:\s*INR\s*(" + _N + ")")),
                 source_file=source_file,
                 extra={k: v for k, v in {"customer_reference": find(r"Customer reference:\s*(\S+)"),
                                          "instruction_reference": find(r"Instruction reference:\s*(\S+)")}.items() if v})

    rows, cur, in_table = [], None, False
    for s in (l.strip() for l in text.splitlines()):   # every page repeats the 'Document Number' header
        if not s:
            continue
        if s.startswith("Document Number"):
            in_table, cur = True, None
            continue
        if not in_table:
            continue
        m = ROW_RE.match(s)
        if m:
            cur = [m.group(1), num(m.group(2)), date(m.group(3)), num(m.group(4)), m.group(5) or ""]
            rows.append(cur)
        elif cur is not None and WRAP_RE.match(s):
            cur[4] += s
        else:
            cur = None
            if s.startswith("Important notes"):
                in_table = False
    total = adv.payment_amount
    repeated = len(rows) > 1 and total is not None and all(abs((r[3] or 0) - total) < 0.01 for r in rows)
    # Some advices print a row twice (same document, amount, date, narration). Drop the repeats only
    # when that is what makes the advice add up to the printed amount.
    uniq = list({tuple(map(str, r)): r for r in rows}.values())
    if total is not None and len(uniq) < len(rows):
        def tot(rs):
            return sum((r[1] if repeated else r[3]) or 0 for r in rs)
        if abs(tot(rows) - total) > 1 and abs(tot(uniq) - total) <= 1:
            adv.extra["repeated_rows_dropped"] = len(rows) - len(uniq)
            rows = uniq
    for doc, amount, when, net_col, narr in rows:
        nm = NARR_RE.match(narr)
        inv = nm.group(3) if nm else narr or None
        net = amount if repeated else net_col
        if inv and not OUR_INVOICE.match(inv):      # retyped / unsplit narration: '...126BLR05948'
            inv = our_invoice_no(inv) or our_invoice_no(narr, tail=True) or inv
        ours = bool(inv and OUR_INVOICE.match(inv))
        # Flipkart debit notes: RDN (return), SDN / PDN (shortage / price), FKIDN...; '_Reverse' ones are positive
        kind = "INVOICE" if ours else ("DEBIT_NOTE" if inv and re.match(r"^(RDN|SDN|PDN|FKIDN)", inv) else "OTHER")
        adv.lines.append(Line(invoice_no=inv if ours else None, invoice_date=when, doc_ref=doc,
                              line_type=kind, description=narr or None,
                              gross_amount=amount,
                              deduction_amount=None if repeated or abs((amount or 0) - (net or 0)) < 0.01
                              else round(amount - net, 2),
                              net_amount=net,
                              extra={"payer_ref": inv} if inv and not ours else {}))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.data[:5] == b"%PDF-"]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
