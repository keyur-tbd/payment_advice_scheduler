"""
PRR Fresh Produce - prrfreshproducepvtltd@gmail.com, 'Payment Voucher No : 9853, Date : 28-9-2026 ...',
a TallyPrime voucher PDF (sometimes attached under a '.xlsx' name; the bytes are PDF).

    No. : 9853 Dated : 28-Sep-26                       advice_ref = voucher no
    Account :
    MIMANSA INDUSTRIES PRIVATE 1,29,460.78              printed amount (party line)
    Agst Ref 27GUR-05196 41,487.97 Dr                  bill settled (Dr to our ledger = paid)
    Agst Ref DN/123 1,000.00 Cr                        their debit note (Cr = deducted)
    On Account 560.516 Dr                              advance, no bill
    On Account of : NEFT/IDFB6271M3493224/...          UTR
Only 'Payment Voucher' mails; their 'Purchase Invoice Voucher' mails are bookings, not payments.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

_N = r"[\d,]+\.\d{2,3}"             # Tally printed three decimals until May 2026 ('94,455.660')
ROW_RE = re.compile(r"^(Agst Ref|New Ref|Advance|On Account)\s+(?:(.*?)\s+)?(" + _N + r")\s+(Dr|Cr)\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    if "Payment Voucher" not in text:
        raise Skip("not a payment voucher")
    no = re.search(r"No\.\s*:\s*(\S+)\s+Dated\s*:\s*(\S+)", text)
    party = re.search(r"^MIMANSA INDUSTRIES.*?(" + _N + r")\s*$", text, re.M | re.I)
    utr = re.search(r"On Account of\s*:\s*(?:NEFT|RTGS|IMPS|UPI)?/?([A-Z0-9]{10,})", text, re.S)
    adv = Advice(customer="prr_fresh", payer_name="PRR Fresh Produce Pvt Ltd",
                 advice_ref=no.group(1) if no else None, utr=utr.group(1) if utr else None,
                 payment_date=date(no.group(2)) if no else None,
                 payment_amount=num(party.group(1)) if party else None, source_file=source_file)
    for s in (l.strip() for l in text.splitlines()):
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, amt = (m.group(2) or "").strip(), num(m.group(3))         # 'On Account 560.516 Dr' has no ref
        net = amt if m.group(4) == "Dr" else -amt
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else (ref or m.group(1)),
                              line_type="INVOICE" if ours and net >= 0 else "DEDUCTION",
                              description=m.group(1), net_amount=net))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.data[:5] == b"%PDF-"]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_text(open_pdf_text(a.data, mail.subject), a.name) for a in pdfs]
