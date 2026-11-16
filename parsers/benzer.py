"""
Benzer Department Store - account@benzerworld.com, 'Payment Voucher' (also replies on 'PAYMENT ADVICE FOR
BENZER ...' threads), a TallyPrime voucher PDF 'Payment_<no>_<fy>.pdf' / 'MIMANSA Payment_<no>_<fy>.pdf':

    No. : 6233/26-27 Dated : 9-Nov-26                   advice_ref = voucher no
    MIMANSA INDUSTRIES PRIVATE 4,228.00                 printed amount
    New Ref '27MUM-10129 1,990.00 Dr                    our invoice (Tally prints a quote before it)
    Agst Ref '26MUM-12466 777.00 Dr
    On Account of : BEING RTV DED RS.397/- ...          narration (RTV deductions already netted per bill)
Same layout as PRR Fresh, so the PRR parser reads it. Thread mails without a voucher PDF are skipped.
"""

from __future__ import annotations

from pa_common import Advice, Mail, Skip, open_pdf_text
from parsers.prr_fresh import parse_text


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.data[:5] == b"%PDF-"]
    if not pdfs:
        raise Skip("no voucher PDF attached")
    out = []
    for a in pdfs:
        text = open_pdf_text(a.data, mail.subject)
        if "Payment Voucher" not in text or "BENZER" not in text.upper():
            continue                                     # ledgers / other PDFs in the thread
        out.append(parse_text(text, a.name, customer="benzer", payer_name="Benzer Department Store Pvt Ltd"))
    if not out:
        raise Skip("no Benzer payment voucher among the PDFs")
    return out
