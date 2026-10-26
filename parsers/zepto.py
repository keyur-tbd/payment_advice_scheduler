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
The PDF sometimes prints one document line several times with fresh Sr Nos (RTV-1033 x4 in
2000107680, AP-AR adjustments x2/x3 in 1500070067); the printed Amount counts it once, so
identical repeats are dropped (advice.extra['repeated_rows']). Zepto's header Amount also
runs Rs 2-80 below its own rows on most advices - not a parse problem, loaded as ROUND_OFF.
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
    _drop_repeats(adv)
    return adv


def _drop_repeats(adv: Advice) -> None:
    """The same SAP document printed again under a new Sr No is usually one clearing shown
    twice (2000107680: RTV-1033 x4; 1500070067: AP-AR adjustments x2/x3) - but 2000018750
    reconciles only WITH its repeats, so the printed Amount arbitrates: repeats are dropped
    only when the raw rows miss it and the de-duplicated rows land within Zepto's usual
    header gap (Rs 100 or 0.2%)."""
    if adv.payment_amount is None or adv.check()[0] == "ok":
        return
    seen, kept, dropped = set(), [], []
    for l in adv.lines:
        key = (l.doc_ref, l.invoice_no, l.extra.get("ref_doc"), l.gross_amount, l.tds_amount, l.net_amount)
        if key in seen:
            dropped.append(f"{l.doc_ref} {l.net_amount}")
            continue
        seen.add(key)
        kept.append(l)
    if not dropped:
        return
    after = round(sum(l.net_amount or 0.0 for l in kept), 2) - adv.payment_amount
    if abs(after) <= max(100.0, 0.002 * abs(adv.payment_amount)):
        adv.lines = kept
        adv.extra["repeated_rows"] = dropped


# 2023 (Kiranakart Technologies, 'KK-20964.pdf'): a pivot export, one row per invoice, the
# vendor + UTR printed only on the first row of each UTR group, then 'Grand Total':
#     MIMANSA INDUSTRIES HSBCN23261110086 24GUR006648 18-Sep-23Done 408.75
#     24IGUR006509 18-Sep-23Done 600            ('23-08-2023 Done 510.75' in other months)
# Invoice numbers are typed by hand ('241GUR005353', 'Z4IGUR006649' for 24IGUR...).
LEGACY_ROW = re.compile(r"^(?:(?:MIMANSA INDUSTRI\S*(?: PVT LTD| PRIVATE LIMITED)?\s+)?([A-Z]{4}[A-Z0-9]{10,}|\(blank\)|0)\s+)?(\S+)\s+"
                        r"(\d{1,2}-(?:[A-Za-z]{3}|\d{2})-\d{2,4})\s*(Done & Adj agnst TDS|Adj against TDS|Done|TDS|[A-Za-z]+)\s+"
                        r"(\d[\d, ]*(?:\.\d+)?)$")
# The PDF splits figures with spaces ('7 41' = 741, 'Grand Total 3 ,22,358'), so the amount is
# every digit after the status. '(blank)' or '0' in the UTR column + status 'TDS' / 'Adj against
# TDS' = invoices settled against TDS, no cash. 'Done & Adj agnst TDS 8 55 285' = paid 855 and
# 285 in the Adjustment column; that file's 'Grand Total 72,528 285' carries both column totals.
NO_CASH = ("(blank)", "0")


def _paid_and_adj(raw: str, two_columns: bool):
    """'8 55 285' -> (855, 285) when the row/total carries the Adjustment column, else (num, None).
    The adjustment is a plain integer; '3 ,22,358' (a space-split single total) stays one figure."""
    if two_columns and " " in raw.strip():
        paid, adj = raw.strip().rsplit(" ", 1)
        if re.fullmatch(r"\d{1,6}", adj):
            return num(paid.replace(" ", "")), num(adj)
    return num(raw.replace(" ", "")), None


def _legacy_invoice(raw: str) -> str:
    inv = raw.upper()
    return re.sub(r"^(?:241|Z4I)", "24I", inv)


def parse_legacy(text: str, source_file: str) -> list[Advice]:
    grand = re.search(r"^Grand Total\s+(\d[\d, ]*(?:\.\d+)?)\s*$", text, re.M)
    two_columns = "& Adj" in text              # an Adjustment column is populated somewhere in the file
    groups: dict = {}
    utr = None

    def group(key: str, when, cash: bool) -> Advice:
        adv = groups.get(key)
        if adv is None:
            adv = groups[key] = Advice(customer="zepto", payer_name="Kiranakart Technologies Pvt Ltd",
                                       advice_ref=key, utr=key if cash else None, payment_date=when,
                                       payment_amount=None, source_file=source_file,
                                       extra={"customer_code": "C00043", "layout": "2023 pivot"})
        return adv

    for s in (l.strip() for l in text.splitlines()):
        m = LEGACY_ROW.match(s)
        if not m:
            continue
        utr = m.group(1) or utr
        if utr is None:
            raise ValueError(f"invoice row before any UTR: {s}")
        status = m.group(4)
        when = date(m.group(3))
        no_cash = utr in NO_CASH                 # '(blank)' / '0' in the UTR column: settled against TDS
        tds_line = no_cash or ("TDS" in status.upper() and "&" not in status)
        paid, adj = _paid_and_adj(m.group(5), two_columns and "&" in status)
        inv = _legacy_invoice(m.group(2))
        ours = bool(re.match(r"^\d{2}I?[A-Z]{3}\d{6}$", inv))
        line_extra = {k: v for k, v in {"printed_invoice": m.group(2) if inv != m.group(2) else None,
                                        "status": status}.items() if v}
        adv = group(f"TDS-ADJ-{when}" if no_cash else utr, when, not no_cash)
        adv.lines.append(Line(invoice_no=inv if ours else None, doc_ref=None if ours else m.group(2),
                              line_type="TDS" if tds_line else "INVOICE", net_amount=paid, extra=line_extra))
        if adj:                                  # the Adjustment column: settled against TDS, not paid
            tds_adv = group(f"TDS-ADJ-{when}", when, False)
            tds_adv.lines.append(Line(invoice_no=inv if ours else None, doc_ref=None if ours else m.group(2),
                                      line_type="TDS", net_amount=adj,
                                      extra={**line_extra, "column": "Adjustment"}))
    advices = list(groups.values())
    for a in advices:                              # whole-rupee rows: allow Rs 0.5 of rounding per row
        a.extra["tolerance"] = max(2.0, 0.5 * len(a.lines))
    if grand and advices:
        total, adj_total = _paid_and_adj(grand.group(1), two_columns)
        if len(advices) == 1 and not adj_total:
            advices[0].payment_amount = total
        else:
            # Grand Total is the Paid column over every group (cash UTRs and TDS-settled rows alike);
            # the Adjustment column is totalled separately. Each group gets its own sum once the
            # file reconciles, otherwise the residual lands on the single cash group.
            paid_sum = round(sum(l.net_amount or 0 for a in advices for l in a.lines
                                 if l.extra.get("column") != "Adjustment"), 2)
            rows = sum(len(a.lines) for a in advices)
            cash = [a for a in advices if a.utr]
            if abs(paid_sum - total) <= max(2.0, 0.5 * rows):
                for a in advices:
                    a.payment_amount = round(a.line_sum(), 2)
                    a.extra["total_checked"] = f"file Grand Total {total} over {len(advices)} groups"
            elif len(cash) == 1:
                others = round(sum(l.net_amount or 0 for a in advices if a is not cash[0] for l in a.lines
                                   if l.extra.get("column") != "Adjustment"), 2)
                cash[0].payment_amount = round(total - others, 2)
                cash[0].extra["total_checked"] = f"file Grand Total {total} less {others} settled against TDS"
                for a in advices:
                    if a is not cash[0]:
                        a.payment_amount = round(a.line_sum(), 2)
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
