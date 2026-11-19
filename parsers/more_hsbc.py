"""
More Retail - HSBC payment advices from docs@more.in to payments@ (and the warehouse inboxes), since 29 Jun 2026:
'Payment Advice - HSBCN52026111708640707 - MIMANSA INDUSTRIES PRIVATE LIMITED', one advice per UTR.
More moved its vendor payments from Kotak CMS (feed kotak_more, last mail 25 Aug 2026) to HSBC.

    Payment Advice
    17 November 2026
    HSBC REF CUSTOMER REF UTR NO
    115850864.0 7513327 HSBCN52026111708640707          <- customer ref = our vendor code at More
    SUPPLIER SITE INVOICE NO. GRN DATE AMOUNT (INR) TYPE
    GURGAON 27GUR-05122/26-27_20260908342 2026-09-08 ₹ 96,666.34 STANDARD
    27GUR-06431/26-27_202610                             <- a long invoice key wraps: head line, row, tail line
    GURGAON 2026-10-18 ₹ 73,222.30 STANDARD
    18342
    ₹ 73,222.30
    TOTAL PAYMENT                                        <- 'NET PAYMENT' (after Total Gross / Deductions) until Aug

Three layouts:
  * Jun-Aug 2026 (PDF only): STANDARD rows at the invoice amount plus CREDIT rows '(₹ 86.49)' for TDS
    ('<invoice key>-TDS-CM620') and DMQ shortage notes ('DMQORD#800187664_...'), summing to NET PAYMENT.
  * 26-29 Jun 2026: the same advice as an .xlsx with no extension (attachment named 'payment_advice_MORE8634605').
  * Sep 2026 on: STANDARD rows at the cash paid (after TDS and DMQ) + 'invoice_debitnote_breakup_MORE*.xlsx':
    per invoice the payment row ('PAYMENT' / 'Bank Payment', UTR, Invoice Amount, Paid & Adj Amt) and the
    adjustments ('TDS' / 'TDS Deducted', 'GDN' / 'DMQ - Quantity Shortage Debit Note', 'Credit Note Applied').
    The breakup also repeats EARLIER payments against the same invoices (other UTRs): only this UTR's rows count.

Lines: one INVOICE per invoice with net_amount = cash against it net of TDS (tds_amount = the TDS), and one GDN
line (negative) per DMQ note deducted in this payment, so the knock-off file applies the invoice net of TDS and
asks for the DMQ credit memo. Sep-on rows whose breakup does not tie (invoice - TDS - DMQ = paid) keep the paid
amount with no split. Invoice keys are '<our invoice>/<More's FY>_<booking no>'; parsers.kotak_more.our_invoice
rebuilds a dropped prefix from the site and the GRN / invoice date.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections import defaultdict

from pa_common import Advice, Line, Mail, Skip, date, num, pdf_text
from parsers.kotak_more import SITE_CODE, our_invoice

_AMT = r"\(?₹\s*[\d,]+(?:\.\d+)?\)?"
_SITE = r"([A-Z][A-Z]+(?: [A-Z][A-Z]+)*)"
ROW_RE = re.compile(r"^" + _SITE + r"\s+(\S.*?)\s+(\d{4}-\d{2}-\d{2})\s+(" + _AMT + r")\s+([A-Z]+)\s*$")
WRAPPED_RE = re.compile(r"^" + _SITE + r"\s+(\d{4}-\d{2}-\d{2})\s+(" + _AMT + r")\s+([A-Z]+)\s*$")
TOTAL_RE = re.compile(r"(?:(?:TOTAL|NET) PAYMENT\s*(" + _AMT + r"))|(?:(" + _AMT + r")\s*\n\s*(?:TOTAL|NET) PAYMENT)")
TDS_RE = re.compile(r"-TDS-CM\d*$", re.I)
PAY_TYPES = {"PAYMENT", "BANK PAYMENT"}


def _typos(ref: str) -> str:
    """More's typing slips our_invoice cannot read: '27HYD/04028' and '27KOL/01097' (slash), '277HYD-04086'
    (doubled digit), 'BLR-05192/26-27_..' (no FY: left bare so the site and date rebuild it)."""
    ref = re.sub(r"^(\d{2})\d?([A-Za-z]{3})[/ ](\d{5})", r"\1\2-\3", ref)
    ref = re.sub(r"^(\d{2})\d([A-Za-z]{3}-\d{5})", r"\1\2", ref)
    return re.sub(r"^[A-Za-z]{3}-(\d{5}/)", r"\1", ref)


def _key(ref: str) -> str:
    return re.sub(r"\s+", "", ref or "").upper()


def _xlsx_rows(data: bytes) -> list:
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets[0]
    return [r for r in ws.iter_rows(values_only=True) if any(v not in (None, "") for v in r)]


def _xlsx_as_text(data: bytes) -> str:
    """The Jun 2026 advices sent as a workbook: one text line per row, cells joined like the PDF prints them."""
    out = []
    for r in _xlsx_rows(data):
        cells = [str(v).strip() for v in r if v not in (None, "")]
        line = " ".join(cells)
        if cells and cells[0] == "Payment Advice" and len(cells) > 1:   # title and date share a row
            out += [cells[0], cells[-1]]
        elif "NET PAYMENT" in line:                   # as the PDF prints it: amount, then the label
            out += [cells[-1], "NET PAYMENT"]
        else:
            out.append(line)
    return "\n".join(out)


def _rows(text: str) -> list:
    """[(site, invoice key, date, amount, type)] from the INVOICE DETAILS table, re-joining wrapped keys."""
    lines = [l.strip() for l in text.splitlines()]
    out = []
    for i, s in enumerate(lines):
        m = ROW_RE.match(s)
        if m and not WRAPPED_RE.match(s):
            out.append((m.group(1), m.group(2), m.group(3), num(m.group(4)), m.group(5)))
            continue
        m = WRAPPED_RE.match(s)
        if m:
            head = lines[i - 1] if i else ""
            tail = lines[i + 1] if i + 1 < len(lines) else ""
            if not head or " " in head and not re.match(r"^\d{2} [A-Z]{3}-", head):
                raise Skip(f"wrapped invoice row without its key: {s!r}")
            ref = head + (tail if re.fullmatch(r"[\w#/-]+", tail or "") and not ROW_RE.match(tail) else "")
            out.append((m.group(1), ref, m.group(2), num(m.group(3)), m.group(4)))
    return out


def _breakup(data: bytes, utr: str) -> dict:
    """{invoice key: {'gross', 'paid', 'tds', 'dmq': [(ref, amount)], 'cn'}} for this UTR's payment rows."""
    rows = _xlsx_rows(data)
    if not rows or str(rows[0][0]).strip() != "Accounting Date":
        return {}
    pay, adj = {}, defaultdict(lambda: {"tds": 0.0, "dmq": [], "cn": 0.0})
    for r in rows[1:]:
        kind = str(r[1] or "").strip().upper()
        inv = _key(str(r[6] or ""))
        if kind in PAY_TYPES:
            if str(r[2] or "").strip().upper() == utr.upper():
                p = pay.setdefault(inv, {"gross": 0.0, "paid": 0.0, "application": r[7]})
                p["gross"] += num(r[9]) or 0.0
                p["paid"] += num(r[10]) or 0.0
        elif "TDS" in kind:
            adj[inv]["tds"] += num(r[10]) or 0.0
        elif kind == "CREDIT NOTE APPLIED":
            adj[inv]["cn"] += num(r[10]) or 0.0
        elif kind:                                    # GDN / DMQ - Quantity Shortage Debit Note
            adj[inv]["dmq"].append((str(r[7] or "").strip(), num(r[10]) or 0.0))
    return {k: {**v, **adj.get(k, {"tds": 0.0, "dmq": [], "cn": 0.0})} for k, v in pay.items()}


def parse_advice(text: str, source_file: str, breakup: bytes | None = None) -> Advice:
    if "MORE RETAIL" not in text.upper() or "UTR NO" not in text:
        raise Skip("not a More Retail HSBC advice")
    m = re.search(r"HSBC REF\s+CUSTOMER REF\s+UTR NO\s*\n\s*(\S+)\s+(\S+)\s+(\S+)", text)
    if not m:
        raise Skip("no HSBC REF / UTR line")
    hsbc_ref, vendor_code, utr = m.group(1).removesuffix(".0"), m.group(2), m.group(3)
    d = re.search(r"^\s*(\d{1,2} [A-Za-z]+ \d{4})\s*$", text, re.M)
    pay_date = dt.datetime.strptime(d.group(1), "%d %B %Y").date() if d else None
    t = TOTAL_RE.findall(text)
    total = num((t[-1][0] or t[-1][1])) if t else None
    adv = Advice(customer="more_retail", payer_name="More Retail Pvt Ltd", advice_ref=utr, utr=utr,
                 payment_date=pay_date, payment_amount=total, source_file=source_file,
                 extra={"hsbc_ref": hsbc_ref, "vendor_code": vendor_code})

    rows = _rows(text)
    if not rows:
        raise Skip("no invoice rows")
    split = _breakup(breakup, utr) if breakup else {}
    invoices = {}                                     # key -> Line, to fold the Jun-Aug TDS credits into
    for site, ref, rdate, amt, typ in rows:
        ref = ref.replace(" ", "")
        rd = date(rdate)
        if typ == "CREDIT" and TDS_RE.search(ref):
            base = _key(TDS_RE.sub("", ref))
            line = invoices.get(base)
            if line is not None:
                line.tds_amount = round((line.tds_amount or 0) - amt, 2)
                line.net_amount = round(line.net_amount + amt, 2)
                continue
            adv.lines.append(Line(invoice_no=our_invoice(TDS_RE.sub("", ref), site, rd)[0], doc_ref=ref,
                                  invoice_date=rd, line_type="TDS", net_amount=amt, tds_amount=-amt,
                                  extra={"site": site}))
            continue
        if typ == "CREDIT":                           # DMQ shortage / other debit note
            adv.lines.append(Line(doc_ref=ref, invoice_date=rd, line_type="GDN" if "DMQ" in ref.upper() else "DEDUCTION",
                                  net_amount=amt, deduction_amount=-amt, extra={"site": site}))
            continue
        invoice_no, alt = our_invoice(_typos(ref), site, rd)
        line = Line(invoice_no=invoice_no, doc_ref=ref, invoice_date=rd, line_type="INVOICE",
                    gross_amount=amt, net_amount=amt, description=f"or {alt}" if alt else None,
                    extra={"site": site, "type": typ})
        b = split.get(_key(ref))
        if b and abs(b["paid"] - amt) <= 0.05:        # Sep on: the PDF prints the cash; the breakup says why
            dmq = round(sum(a for _, a in b["dmq"]), 2)
            line.gross_amount = round(b["gross"], 2)
            line.extra["application_no"] = b.get("application")
            if b["tds"]:
                line.tds_amount = round(b["tds"], 2)
            if b["cn"]:
                line.extra["credit_note_applied"] = round(b["cn"], 2)
            if dmq and abs(b["gross"] - b["tds"] - dmq - b["cn"] - amt) <= 0.05:
                line.net_amount = round(amt + dmq, 2)
                line.deduction_amount = dmq
                adv.lines.append(line)
                for dref, a in b["dmq"]:
                    adv.lines.append(Line(doc_ref=dref, invoice_date=rd, line_type="GDN", net_amount=-a,
                                          deduction_amount=a, description=f"against {invoice_no or ref}",
                                          extra={"site": site}))
                invoices[_key(ref)] = line
                continue
        adv.lines.append(line)
        invoices[_key(ref)] = line
    return adv


def parse(mail: Mail) -> list[Advice]:
    advice = breakup = None
    for a in mail.attachments:
        n = a.name.lower()
        if "breakup" in n:
            breakup = a.data
        elif "payment_advice" in n:
            advice = a
    if advice is None:
        raise Skip("no payment_advice attachment")
    if advice.data[:4] == b"%PDF":
        text = pdf_text(advice.data)
    elif advice.data[:2] == b"PK":                    # Jun 2026: a workbook named without an extension
        text = _xlsx_as_text(advice.data)
    else:
        raise Skip(f"payment advice {advice.name} is neither PDF nor xlsx")
    return [parse_advice(text, advice.name, breakup)]


__all__ = ["parse", "parse_advice", "SITE_CODE"]
