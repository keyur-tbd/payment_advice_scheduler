"""
Dorabjee & Co - MahaPayments@mahabank.co.in, 'Payment Advice for Payment Ref No: DG80023'.
No attachment; the mail body says:

    ... initiated a credit of Rs. 222.00 to your account ... on behalf of DORABJEE & CO PVT LTD
    Date : 19/10/2026 | Payment Ref No : DG80023 | Bank Ref No : ... | UTR No/J No : MAHBN8...
    Payment Remark : Dorabjees KoregaonPark Inv No 27MUM 09847 DED RJ DR20795RJ DR20934
                     REJ 534 75 DNote 0 00 DISP 0 00

The remark names the branch, the invoice, and the deductions with the decimal point
dropped ('534 75' = 534.75): REJ (rejections, against the RJ DR... notes), DNote (debit
note), DISP (discrepancy). One invoice -> one line: gross = paid + deductions. A remark
without exactly one invoice is loaded header_only with the remark as narration.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num

_AMT2 = r"(\d+)\s(\d{2})\b"          # '534 75'


def _dropped_point(label: str, remark: str):
    m = re.search(rf"\b{label}\s+{_AMT2}", remark)
    return float(f"{m.group(1)}.{m.group(2)}") if m else 0.0


def parse(mail: Mail) -> list[Advice]:
    body = re.sub(r"\s+", " ", mail.body)
    amount = re.search(r"credit of\s+Rs\.?\s*([\d,]+\.\d{2})", body)
    if not amount:
        raise Skip("no credit amount in the mail")

    def find(p):
        m = re.search(p, body, re.I)
        return m.group(1).strip() if m else None
    remark = find(r"Payment Remark\s*:\s*(.*?)\s*(?:This is a Computer|$)") or ""
    paid = num(amount.group(1))
    branch = re.match(r"Dorabjees\s+(\S+)", remark)
    adv = Advice(customer="dorabjee", payer_name="Dorabjee & Co Pvt Ltd",
                 advice_ref=find(r"Payment Ref No\s*:\s*(\S+)"), utr=find(r"UTR No/J No\s*:\s*(\S+)"),
                 payment_date=date(find(r"\bDate\s*:\s*(\d{2}/\d{2}/\d{4})")), payment_amount=paid,
                 extra={"remark": remark, "bank_ref": find(r"Bank Ref No\s*:\s*(\S+)"),
                        "branch": branch.group(1) if branch else None})
    # 'Inv No 27MUM 09847' / '25 MUM 13637' / '25MUMJ 03640' / '26MUM 110551' (as typed by their AP)
    invoices = [(f"{y}{c}", n) for y, c, n in
                re.findall(r"\b(\d{2})\s?([A-Z]{3,4})\s?-?\s?(\d{4,6})\b", remark.split(" DED ")[0])]
    if len(invoices) != 1:
        adv.header_only = True
        adv.extra["narration"] = remark
        return [adv]
    rej, dnote, disp = (_dropped_point(k, remark) for k in ("REJ", "DNote", "DISP"))
    notes = re.findall(r"\b(?:RJ\s*)?(DR\d+)", remark)
    ded = round(rej + dnote + disp, 2)
    adv.lines.append(Line(
        invoice_no=f"{invoices[0][0]}-{int(invoices[0][1]):05d}" if len(invoices[0][1]) <= 5
        else f"{invoices[0][0]}-{invoices[0][1]}", line_type="INVOICE",
        doc_ref=" ".join(notes) or None, gross_amount=round(paid + ded, 2),
        deduction_amount=ded or None, net_amount=paid,
        extra={k: v for k, v in {"rejection": rej, "debit_note": dnote, "discrepancy": disp}.items() if v}))
    return [adv]
