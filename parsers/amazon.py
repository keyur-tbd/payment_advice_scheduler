"""
Amazon Retail India - remittance advice in the mail body (HTML), no attachment.

Two senders, same content:
    noreply@amazon.com   'EFT Remittance Advice'
        lines: Invoice Number | Invoice Date (2026-09-24) | Invoice Description |
               Discount Taken | Amount Paid ('-27.54') | Amount Remaining
    no-reply@amazon.com  'Remittance Advice - MIMANSA ...(KGJ4P) Payment# N'
        lines: Invoice Number: | Invoice Date: (21-NOV-2025) | Invoice description |
               Invoice amount | TDS Amount | Discount Taken | Amount Paid ('(1.62)') | Amount Remaining
Both open with a key/value table: Payment made to, Our Supplier (site) No.,
Supplier site name, Payment number, Payment date, Payment currency, Payment amount.

The KGJ4P layout leaves <TD> tags unclosed (and emits one cell too many), so
BeautifulSoup nests the cells; rows are split on the raw <TD> tags instead and
read from the right: Amount Paid is always the second-to-last cell.

Line types (Amount Paid is already signed):
    27BLR-00001-TDS-CM-ABCD          TDS credit memo -> TDS on 27BLR-00001
    'Shortage Claim for Invoice - X' shortage debit -> DEDUCTION on X
    'Price Claim ...'                price debit    -> DEDUCTION
    27BLR-00093-FDN-10-2026          financial debit note -> DEBIT_NOTE on 27BLR-00093
    27BLR-00093SCR-FCN-10-2026       financial credit note -> CREDIT_NOTE on 27BLR-00093SCR
    anything else negative           -> DEBIT_NOTE (Amazon-raised credit memo: co-op, SPA, volume incentive)
Invoice numbers are kept as Amazon prints them, including its 'SCR' resubmission suffix.
The same payment number arrived from both senders in Dec 2025 (27 payments); the
loader keeps the first copy.
    positive                         -> INVOICE
advice_ref = Payment number.
"""

from __future__ import annotations

import html as htmllib
import re

from pa_common import Advice, Line, Mail, Skip, date, num

_TR = re.compile(r"<tr[^>]*>(.*?)(?=<tr[^>]*>|</table>)", re.I | re.S)
_TD = re.compile(r"<t[dh][^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _clean(cell: str) -> str:
    s = re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", cell))).strip()
    m = re.fullmatch(r'\[\s*"(.*)"\s*\]', s)      # since Sep 2026 some EFT mails print '["216538.13"]'
    return m.group(1).strip() if m else s


def _cells(tr: str) -> list[str]:
    return [_clean(p) for p in _TD.split(tr)[1:]]


def _rows(html: str) -> list[list[str]]:
    return [_cells(m.group(1)) for m in _TR.finditer(html)]


_REF_RE = re.compile(r"(?:Shortage|Price|Quantity|Damage)\w*\s+Claim\s+for\s+Invoice\s*-\s*([\w/-]+?)(?:##|\s|$)", re.I)


def _line(cells: list[str]) -> Line:
    inv, inv_date, desc = cells[0], cells[1], cells[2]
    paid = num(cells[-2])
    gross = tds = None
    if len(cells) >= 8:                       # KGJ4P layout carries Invoice amount + TDS Amount
        gross, tds = num(cells[3]), num(cells[4])
    desc_clean = desc.replace("##YES", "").replace("##NO", "").strip() or None
    m = re.match(r"^(.*?)-TDS-CM-\w+$", inv, re.I)
    note = re.match(r"^(.*?)-(FDN|FCN)-[\d-]+$", inv, re.I)
    claim = _REF_RE.search(desc)
    if m:
        kind, invoice_no, doc_ref = "TDS", m.group(1), inv
    elif note:                                # financial debit / credit note against an invoice
        kind = "DEBIT_NOTE" if note.group(2).upper() == "FDN" else "CREDIT_NOTE"
        invoice_no, doc_ref = note.group(1), inv
    elif claim:
        kind, invoice_no, doc_ref = "DEDUCTION", claim.group(1), inv
    elif paid is not None and paid < 0:
        kind, invoice_no, doc_ref = "DEBIT_NOTE", None, inv
    else:
        kind, invoice_no, doc_ref = "INVOICE", inv, None
    return Line(invoice_no=invoice_no, invoice_date=date(inv_date), doc_ref=doc_ref, line_type=kind,
                description=desc_clean, gross_amount=gross, tds_amount=tds, net_amount=paid,
                extra={"amazon_invoice_number": inv, "amount_remaining": num(cells[-1])})


def parse(mail: Mail) -> list[Advice]:
    if not mail.html:
        raise Skip("no HTML body")
    rows = _rows(mail.html)
    head = {}
    for r in rows:
        if len(r) == 2 and r[0].endswith(":"):
            head[r[0].rstrip(":").strip().lower()] = r[1]
    if "payment number" not in head:
        raise Skip("not a remittance advice")

    lines: list[Line] = []
    in_table = False
    for r in rows:
        if r and r[0].lower().rstrip(":") == "invoice number":
            in_table = True
            continue
        if not in_table or len(r) < 5 or not r[0]:
            continue
        lines.append(_line(r))

    return [Advice(
        customer="amazon",
        payer_name="Amazon Retail India Private Limited",
        advice_ref=head["payment number"],
        utr=None,
        payment_date=date(head.get("payment date")),
        payment_amount=num(head.get("payment amount")),
        lines=lines,
        extra={"supplier_no": head.get("our supplier site no.") or head.get("our supplier no."),
               "supplier_site_name": head.get("supplier site name"),
               "payment_made_to": head.get("payment made to"),
               "currency": head.get("payment currency")},
    )]
