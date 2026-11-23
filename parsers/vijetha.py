"""
Vijetha Supermarkets - payments@vijetha.in, 'Payment Advice', a Tally bill-wise PDF.

    Date : 15-Sep-26
    Bill Ref. Bill Date Amount
    Agst Ref / 27HYD-00792 4-May-26 790.00
    New Ref / DN-123 5-May-26 1,200.00 Dr        (Dr = their debit note, deducted)
    Less : VJ01/12650 13-May-26 (-)807.00         their return / debit note, deducted
    Nett Amount (rupee) 1,12,841.00             printed amount
    Payment Details: 'Cheque No.: 101521 Hdfc Bank ...' / 'Rtgs ...' / 'Neft ...'
Tally has no payment number, so advice_ref = the instrument number when printed, else
'<date>/<amount>'.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text, our_invoice_no

_N = r"[\d,]+\.\d{2}"
ROW_RE = re.compile(r"^(Agst Ref|New Ref|Advance|On Account)\s*/?\s*(.*?)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})?\s*("
                    + _N + r")\s*(Dr|Cr)?\s*$")
LESS_RE = re.compile(r"^Less\s*:\s*(.*?)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})?\s*\(-\)\s*(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def parse_text(text: str, source_file: str) -> Advice:
    if "Payment Advice" not in text:
        raise Skip("not a payment advice")
    total = re.search(r"^Nett Amount\s*(?:\(cid:\d+\)|₹)?\s*(" + _N + r")\s*$", text, re.M)   # rupee glyph -> '(cid:299)'
    instr = re.search(r"(?:Cheque No\.?|Chq\.? No\.?|UTR No\.?|Ref No\.?)\s*:\s*(\S+)", text, re.I)
    when = re.search(r"Date\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", text)
    amount = num(total.group(1)) if total else None
    pay_date = date(when.group(1)) if when else None
    adv = Advice(customer="vijetha", payer_name="Vijetha Supermarkets Pvt Ltd",
                 advice_ref=instr.group(1) if instr else (f"{pay_date}/{amount}" if pay_date and amount else None),
                 utr=instr.group(1) if instr else None, payment_date=pay_date, payment_amount=amount,
                 source_file=source_file)
    for s in (l.strip() for l in text.splitlines()):
        less = LESS_RE.match(s)
        if less:
            adv.lines.append(Line(doc_ref=less.group(1), invoice_date=date(less.group(2)), line_type="DEDUCTION",
                                  description="Less", net_amount=-num(less.group(3))))
            continue
        m = ROW_RE.match(s)
        if not m:
            continue
        ref, amt, side = m.group(2).strip(), num(m.group(4)), m.group(5)
        net = -amt if side == "Dr" else amt
        ours = bool(OUR_INVOICE.match(ref))
        adv.lines.append(Line(invoice_no=ref if ours else None, doc_ref=None if ours else (ref or m.group(1)),
                              invoice_date=date(m.group(3)), line_type="INVOICE" if ours and net >= 0 else "DEDUCTION",
                              description=m.group(1), net_amount=net))
    return adv


# ---- Since Oct 2026: 'Mimansa Industries Pvt Ltd (Ledger Account)' - Vijetha's Tally ledger of our account for the
# year so far, as PDF and/or xlsx (Payment_NNNNN.pdf, Payment_NNNNN.xlsx, Tally.pdf, LedBills.xlsx). Each mail repeats
# every earlier voucher. Payment vouchers are the payments; the bill-wise lines under one are what it settled:
#
#     19-Oct-26 Cr Kotak Mahindra Bank - 760011013411 Payment 24664 1,50,802.00
#     Cheque 037801 19-10-2026 1,50,802.00 Cr                  (only some exports print the instrument)
#     Agst Ref 27HYD-02991 30 Days 1,305.00 Dr                 Dr = our invoice paid
#     Agst Ref VJ29/7500 30 Days 1,064.00 Cr                   Cr = their debit note / return, deducted
#
# A bounced cheque is a later Receipt voucher for the same amount ('I/W CHQ RTN:37802'): the latest earlier payment of
# that amount is dropped (27 Oct, 2 Nov and 6 Nov 2026 bounced; 19 Oct and 5 Nov are the two ERP receipts).
# advice_ref = 'PV<voucher no.>', the same in every export of the ledger; the cheque number goes to utr. Some exports
# list vouchers only, without bill-wise lines: those are skipped so a later export with lines is not held off as a
# duplicate of a header-only copy.

_LN = r"([\d,]+\.\d{2})"
VCH_RE = re.compile(r"^(?:(\d{1,2}-[A-Za-z]{3}-\d{2,4})\s+)?(Dr|Cr)\s+(.+?)\s+(\S+)\s+(\S+)\s+" + _LN + r"$")
INSTR_RE = re.compile(r"^(Cheque|NEFT|RTGS|Neft|Rtgs|e-Fund Transfer|Inter Bank Transfer)\s+(\S+)\s+"
                      r"(\d{1,2}-\d{1,2}-\d{4})\s+" + _LN + r"(?:\s+(?:Dr|Cr))?$")
ALLOC_RE = re.compile(r"^(Agst Ref|New Ref|Advance|On Account)\s+(.*?)\s+(?:\d+\s+Days\s+)?" + _LN + r"\s+(Dr|Cr)$")


def _xlsx_lines(data: bytes) -> str:
    """The xlsx export as the same lines the PDF gives (first-column date as 1-Apr-26, amounts with decimals)."""
    import io
    import openpyxl
    out = []
    for ws in openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = []
            for i, v in enumerate(row):
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if hasattr(v, "year"):
                    cells.append(f"{v.day}-{v:%b-%y}" if i == 0 else f"{v.day}-{v.month}-{v.year}")
                elif isinstance(v, (int, float)):
                    cells.append(f"{v:,.2f}")
                else:
                    cells.append(str(v).strip())
            if cells:
                out.append(" ".join(cells))
    return "\n".join(out)


def parse_ledger(text: str, source_file: str) -> list[Advice]:
    vouchers, cur, day, any_alloc = [], None, None, False
    for s in (l.strip() for l in text.splitlines()):
        v = VCH_RE.match(s)
        if v and v.group(4) in ("Payment", "Receipt", "Purchase", "Debitnote", "Journal", "Contra", "Sales",
                                "Credit Note", "Debit Note"):
            day = date(v.group(1)) if v.group(1) else day
            cur = {"type": v.group(4), "no": v.group(5), "date": day, "amount": num(v.group(6)),
                   "bank": v.group(3), "instr": None, "lines": []}
            vouchers.append(cur)
            continue
        if cur is None:
            continue
        i = INSTR_RE.match(s)
        if i and cur["type"] == "Payment":
            cur["instr"] = (i.group(1), i.group(2))
            continue
        a = ALLOC_RE.match(s)
        if a:
            any_alloc = True
            cur["lines"].append((a.group(1), a.group(2).strip(), num(a.group(3)), a.group(4)))
    if not any_alloc:
        raise Skip("Vijetha ledger without bill-wise lines (voucher list only)")

    payments = [v for v in vouchers if v["type"] == "Payment"]
    for r in (v for v in vouchers if v["type"] == "Receipt"):      # bounced cheques
        hit = [p for p in payments if not p.get("bounced") and p["amount"] == r["amount"]
               and p["date"] and r["date"] and p["date"] <= r["date"] and (r["date"] - p["date"]).days <= 15]
        if hit:
            hit[-1]["bounced"] = f"receipt voucher {r['no']} on {r['date']}"

    out = []
    for p in payments:
        if p.get("bounced"):      # voids a copy an earlier ledger loaded before the bounce was booked
            out.append(Advice(customer="vijetha", payer_name="Vijetha Supermarkets Pvt Ltd", advice_ref=f"PV{p['no']}", utr=None,
                              payment_date=p["date"], payment_amount=p["amount"], source_file=source_file,
                              extra={"void": f"bounced: {p['bounced']}", "voucher_no": p["no"]}))
            continue
        adv = Advice(customer="vijetha", payer_name="Vijetha Supermarkets Pvt Ltd", advice_ref=f"PV{p['no']}",
                     utr=p["instr"][1] if p["instr"] else None, payment_date=p["date"], payment_amount=p["amount"],
                     source_file=source_file,
                     extra={"voucher_no": p["no"], "bank": p["bank"], "format": "tally ledger",
                            "instrument": p["instr"][0] if p["instr"] else None})
        for kind, ref, amt, side in p["lines"]:
            ours = our_invoice_no(ref)
            net = amt if side == "Dr" else -amt
            adv.lines.append(Line(invoice_no=ours, doc_ref=None if ours else ref,
                                  line_type="INVOICE" if ours and net >= 0 else ("DEDUCTION" if net < 0 else "OTHER"),
                                  description=kind, net_amount=net))
        if not adv.lines:
            adv.header_only = True
        out.append(adv)
        if adv.utr:
            # The old 'Payment Advice' mails (to Sep 2026) keyed this same payment by its cheque number; the ledger
            # copy replaces that one so the payment is not counted twice.
            out.append(Advice(customer="vijetha", payer_name=adv.payer_name, advice_ref=adv.utr, utr=adv.utr,
                              payment_date=adv.payment_date, payment_amount=adv.payment_amount,
                              source_file=source_file,
                              extra={"void": f"replaced by ledger voucher {adv.advice_ref}", "voucher_no": p["no"]}))
    return out


def parse(mail: Mail) -> list[Advice]:
    out, docs = [], 0
    for a in mail.attachments:
        name = a.name.lower()
        if name.endswith(".pdf") or a.data[:5] == b"%PDF-":
            text = open_pdf_text(a.data, mail.subject)
        elif name.endswith(".xlsx"):
            text = _xlsx_lines(a.data)
        else:
            continue
        docs += 1
        if "Ledger Account" in text:
            out += parse_ledger(text, a.name)
        else:
            out.append(parse_text(text, a.name))
    if not docs:
        raise Skip("no PDF / xlsx attached")
    return out
