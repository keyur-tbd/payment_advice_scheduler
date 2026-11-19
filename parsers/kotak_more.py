"""
More Retail - Kotak CMS 'Credit Intimation to your account - MIMANSA INDUSTRIES PRIVATE LIMITED - <UTR>',
from CMSAlerts@kotak.bank.in to payments@ (not accounts@), one PAY_PAID_*.pdf per payment.

    Kotak Ref : 26082511S1WL Customer Ref : 109177037 Date : 25 AUG 2026
    ... initiated by our client MORE RETAIL PRIVATE LIMITED ...
    Payment Amount (INR) : 16,716.00 Transaction ID : KKBKN62026082561962851 UTR No. : CMS2372627706512
    SUPP_SITE INVOICE_NO INVOICE_DATE INVOICE_AMT
    KOLKATA 27KOL-01733/26- 2026-07-26 9394
    27_20260727562                                   <- the invoice no wraps onto the next line
More Retail's invoice key is '<our invoice>/<FY>_<their booking no>'; the part before '/' is our invoice.
Lines carry no TDS / GDN split (More deducts those before paying; its 'Standard Invoice Settlement'
workbooks list them, see feeds.py), so INVOICE_AMT is what it paid against the invoice.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

_N = r"-?[\d,]+(?:\.\d+)?"
# site = upper-case words without digits; the invoice no may hold spaces (Jan 2025: "25 BLR")
ROW_RE = re.compile(r"^([A-Z][A-Z&.-]*(?: [A-Z][A-Z&.-]*)*?)\s+(.+?)\s+(\d{4}-\d{2}-\d{2})\s+(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^(\d{2}[A-Z]{3}-\d+)")
SITE_CODE = {"BANGALORE": "BLR", "HYDERABAD": "HYD", "GURGAON": "GUR", "KOLKATA": "KOL",
             "MUMBAI": "MUM", "AHMEDABAD": "AHD"}    # only the first four occur in More's advices so far


def parse_text(text: str, source_file: str) -> Advice:
    if "MORE RETAIL" not in text.upper() or "Payment Amount" not in text:
        raise Skip("not a More Retail Kotak advice")

    def find(p):
        m = re.search(p, text, re.I)
        return m.group(1).strip() if m else None
    utr = find(r"UTR No\.?\s*:\s*(\S+)")
    adv = Advice(customer="more_retail", payer_name="More Retail Pvt Ltd", advice_ref=utr, utr=utr,
                 payment_date=date(find(r"\bDate\s*:\s*(\d{1,2}[ -][A-Z]{3}[ -]\d{4})")),   # 25 AUG 2026 / 22-AUG-2025
                 payment_amount=num(find(r"Payment Amount \(INR\)\s*:\s*(" + _N + r")")), source_file=source_file,
                 extra={"kotak_ref": find(r"Kotak Ref\s*:\s*(\S+)"), "customer_ref": find(r"Customer Ref\s*:\s*(\S+)"),
                        "transaction_id": find(r"Transaction ID\s*:\s*(\S+)")})
    rows = [l.strip() for l in text.splitlines()]
    in_table = False
    for i, s in enumerate(rows):
        if s.startswith("SUPP_SITE"):
            in_table = True; continue
        if s.startswith("Note:") or s.startswith("Page "):
            in_table = False; continue
        if not in_table:
            continue
        m = ROW_RE.match(s)
        if m:
            ref = m.group(2)
            j = i + 1                                        # the invoice no wraps over 1-2 more lines:
            while (j < len(rows) and rows[j] and not ROW_RE.match(rows[j]) and " " not in rows[j]
                   and not rows[j].startswith(("Note:", "Page "))):
                ref += rows[j]; j += 1                       # '27KOL-01733/26-' + '27_20260727562'
            ref = ref.replace(" ", "")
            amt = num(m.group(4))
            inv_date = date(m.group(3))
            invoice_no, alt = our_invoice(ref, m.group(1), inv_date)
            adv.lines.append(Line(
                invoice_no=invoice_no, doc_ref=ref, invoice_date=inv_date,
                line_type="INVOICE" if (amt or 0) >= 0 else "DEDUCTION", gross_amount=amt, net_amount=amt,
                description=f"or {alt}" if alt else None,    # Birbal's 'reference' tier tries it when invoice_no misses
                extra={"site": m.group(1)}))
    return adv


def our_invoice(ref: str, site: str, inv_date) -> tuple:
    """(our invoice no, the other-FY alternative or None) for More's invoice key.
    '27BLR-03992/26-27_...' -> 27BLR-03992; More's typos '27BLY-' / '27BRL-' take the site's code.
    About 1 line in 3 drops our prefix: '05546/26-27_20260727620'. The '26-27' there is More's booking year,
    not ours, so the FY comes from INVOICE_DATE (Apr-Mar) and the code from the site: checked 2026-11-19,
    1,038 of 1,057 such lines found in our ledger; the 19 misses straddle 31 Mar / 1 Apr, hence the alternative."""
    code = SITE_CODE.get(site)
    # ours, as typed: 27BLR-03992/.., 27blr-04088/.., 27BCR02264/.., 25BLR1339025262025012.. (no separator: our
    # numbers always carry 5 digits, so take exactly 5)
    m = re.match(r"^(\d{2})-?([A-Za-z]{3})-*(\d{5})", ref)
    if m:
        letters = m.group(2).upper()
        if code and letters not in SITE_CODE.values():
            letters = code                               # More's typos: 27BLY / 27BRL / 27BCR at site BANGALORE
        return f"{m.group(1)}{letters}-{m.group(3)}", None
    # bare: '05546/26-27_20260727620' or, Jan-Mar 2025, '1341625262025012' (13416 + 2526 + 20250128)
    m = re.match(r"^0*(\d{3,5})/\d{2}-\d{2}", ref)
    if not m:
        b = re.match(r"^(\d{5})(\d{2})(\d{2})20\d{2}", ref)
        m = b if b and int(b.group(3)) == int(b.group(2)) + 1 else None
    if not m or not code or not inv_date:
        return None, None
    fy = inv_date.year + 1 if inv_date.month >= 4 else inv_date.year
    n = int(m.group(1))
    return f"{fy % 100}{code}-{n:05d}", f"{(fy - 1) % 100 if inv_date.month >= 4 else (fy + 1) % 100}{code}-{n:05d}"


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
