"""
Zepto - Payments@zeptonow.com, 'Payment Advice <doc>' with one SAP PDF per mail.
Two payer entities share the template; the first PDF line names it:
    ZEPTO LIMITED (formerly Zepto Pvt Ltd / Kiranakart Technologies)  -> C00043
    KIRANAKART WHOLESALE PRIVATE LIMITED                               -> C00155

Header: Payment Ref No. (bank batch), Payment Date, Payment Doc (advice_ref), Amount (printed).
Rows:
    Sr No. | Type of Document | Doc No | Ref Doc | Amount | Currency | TDS | Payment Amt.
    1820 Invoice Payment 1901033765 27GUR-05861 208,786.1 INR 199.15 208,586.95
Credit memos print their Ref Doc around the row, split in two:
    27GUR-05858
    1813 Credit Memo 1700191891 -2,330.94 INR 2.22 -2,328.72
    _QD                                    -> ref '27GUR-05858_QD' (quantity discrepancy)
    2020 AP-AR Adjustment 100169241 KK10042523 -91,673.1 INR 0 -91,673.1   (set-off of what we owe Zepto)
Payment Amt. is signed and net of TDS, so it is net_amount.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

ENTITIES = (
    ("KIRANAKART WHOLESALE", "C00155"),
    ("ZEPTO", "C00043"),
    ("KIRANAKART TECHNOLOGIES", "C00043"),
)
_N = r"-?[\d,]+(?:\.\d+)?"
ROW_RE = re.compile(r"^(\d+)\s+([A-Za-z][A-Za-z -]*?)\s+(\d{6,12})(?:\s+(\S+))?\s+(" + _N + r")\s+INR\s+("
                    + _N + r")\s+(" + _N + r")\s*$")
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")
KINDS = {"invoice payment": "INVOICE", "credit memo": "CREDIT_NOTE", "debit memo": "DEBIT_NOTE"}


def _field(label: str, text: str):
    m = re.search(rf"^{label}\s*:?\s*(\S+)", text, re.M | re.I)
    return m.group(1) if m else None


def parse_text(text: str, source_file: str) -> Advice:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or "Payment Advice" not in text:
        raise Skip("not a Zepto payment advice")
    head = lines[0].upper()
    code = next((c for p, c in ENTITIES if head.startswith(p)), None)
    if code is None:
        raise ValueError(f"unknown Zepto entity {lines[0]!r}")
    adv = Advice(customer="zepto", payer_name=lines[0].title(),
                 advice_ref=_field(r"Payment Doc", text), utr=_field(r"Payment Ref No\.", text),
                 payment_date=date(_field(r"Payment Date", text)),
                 payment_amount=num(_field(r"Amount", text)), source_file=source_file,
                 extra={"customer_code": code})

    started, pending, last, wrapped = False, None, None, False
    for s in lines:
        if s.startswith("Sr No."):
            started = True
            continue
        if not started:
            continue
        m = ROW_RE.match(s)
        if m:
            kind = KINDS.get(m.group(2).strip().lower())
            if kind is None:             # 'AP-AR Adjustment': Zepto nets what we owe them (claims, fees)
                kind = "DEDUCTION" if (num(m.group(7)) or 0) < 0 else "OTHER"
            ref = m.group(4) or pending
            gross, tds, net = num(m.group(5)), num(m.group(6)), num(m.group(7))
            last = Line(
                invoice_no=ref if ref and OUR_INVOICE.match(ref) and kind == "INVOICE" else None,
                doc_ref=m.group(3), line_type=kind, description=m.group(2).strip(),
                gross_amount=gross, tds_amount=tds, net_amount=net,
                extra={"ref_doc": ref} if ref else {})
            adv.lines.append(last)
            wrapped = m.group(4) is None and pending is not None
            pending = None
            continue
        tail = last is not None and " " not in s and (
            s.startswith("_") or (wrapped and re.fullmatch(r"[A-Z0-9]{1,6}", s)))
        if tail:                     # '_QD' / 'QD' (after '26BLR-04947_') / '00093' (after 'AK052600000')
            ref = (last.extra.get("ref_doc") or "") + s
            last.extra["ref_doc"] = ref
            if "_" in ref:
                last.description = f"{last.description} {ref.split('_')[-1]}"
            if OUR_INVOICE.match(ref):
                last.extra["against_invoice"] = ref.split("_")[0]
            wrapped = False
            continue
        if " " not in s and re.match(r"^[A-Z0-9][\w/-]+$", s):           # ref printed above its row
            pending = s
    return adv


# 2023 (Kiranakart Technologies, 'KK-20964.pdf'): a pivot export, one row per invoice, the
# vendor + UTR printed only on the first row of each UTR group, then 'Grand Total':
#     MIMANSA INDUSTRIES HSBCN23261110086 24GUR006648 18-Sep-23Done 408.75
#     24IGUR006509 18-Sep-23Done 600            ('23-08-2023 Done 510.75' in other months)
# Invoice numbers are typed by hand ('241GUR005353', 'Z4IGUR006649' for 24IGUR...).
LEGACY_ROW = re.compile(r"^(?:MIMANSA INDUSTRI\S*(?: PVT LTD| PRIVATE LIMITED)?\s+([A-Z]{4}[A-Z0-9]{10,}|\(blank\)))?\s*(\S+)\s+"
                        r"(\d{1,2}-(?:[A-Za-z]{3}|\d{2})-\d{2,4})\s*(Done|TDS|[A-Za-z]+)\s+(\d[\d, ]*(?:\.\d+)?)$")
# The PDF splits figures with spaces ('7 41' = 741, 'Grand Total 3 ,22,358'), so the amount is
# every digit after the status. '(blank)' UTR + status TDS = a TDS adjustment file, no cash.


def _legacy_invoice(raw: str) -> str:
    inv = raw.upper()
    return re.sub(r"^(?:241|Z4I)", "24I", inv)


def parse_legacy(text: str, source_file: str) -> list[Advice]:
    grand = re.search(r"^Grand Total\s+(\d[\d, ]*(?:\.\d+)?)\s*$", text, re.M)
    groups: dict = {}
    utr = None
    for s in (l.strip() for l in text.splitlines()):
        m = LEGACY_ROW.match(s)
        if not m:
            continue
        utr = m.group(1) or utr
        if utr is None:
            raise ValueError(f"invoice row before any UTR: {s}")
        tds = m.group(4).upper() == "TDS"
        key = f"TDS-ADJ-{date(m.group(3))}" if utr == "(blank)" else utr
        adv = groups.get(key)
        if adv is None:
            adv = groups[key] = Advice(customer="zepto", payer_name="Kiranakart Technologies Pvt Ltd",
                                       advice_ref=key, utr=None if utr == "(blank)" else utr,
                                       payment_date=date(m.group(3)), payment_amount=None, source_file=source_file,
                                       extra={"customer_code": "C00043", "layout": "2023 pivot"})
        inv = _legacy_invoice(m.group(2))
        ours = bool(re.match(r"^\d{2}I?[A-Z]{3}\d{6}$", inv))
        adv.lines.append(Line(invoice_no=inv if ours else None, doc_ref=None if ours else m.group(2),
                              line_type="TDS" if tds else "INVOICE", net_amount=num(m.group(5).replace(" ", "")),
                              extra={k: v for k, v in {"printed_invoice": m.group(2) if inv != m.group(2) else None,
                                                       "status": m.group(4)}.items() if v}))
    advices = list(groups.values())
    if grand and advices:
        total = num(grand.group(1).replace(" ", ""))
        if len(advices) == 1:
            advices[0].payment_amount = total
        elif abs(sum(a.line_sum() for a in advices) - total) <= 1:
            for a in advices:                      # Grand Total checks the whole file; each UTR gets its own sum
                a.payment_amount = round(a.line_sum(), 2)
                a.extra["total_checked"] = f"file Grand Total {total} over {len(advices)} UTRs"
    return advices


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    out = []
    for a in pdfs:
        if not a.data.lstrip(b"\x00"):
            raise Skip(f"{a.name} is empty (all zero bytes)")
        text = open_pdf_text(a.data, mail.subject)
        head = text[:200].lower()
        out.extend(parse_legacy(text, a.name) if "vendor code" in head or "vendorcode" in head
                   else [parse_text(text, a.name)])
    return out
