"""
RBML Solutions (Reliance BP Mobility) - treasury.paymentadvice@zmail.ril.co
(earlier TREASURY.PAYMENTADVICE@ril.com), one 'Payment_Advice.PDF' per mail.

Header text:
    Payment Date : 27.08.2026
    We have settled the following items by Payment document no. 4200000002 2026 for INR
    100000.00 Vide E- Payment UTR no. HDFCH01200000003 to Bank account no ...
    ...
    Total 1,00,000.00

The line table cannot be read from plain text: invoice numbers wrap across two
lines ('27KOL-0164' above the row, '5' on the amounts line) and manual-provision
rows scatter their amounts over three lines. So rows are rebuilt from word
x-positions. One row block is
    [invoice prefix]  (Invoice/Reference column, above the row)
    Document No       Scroll No | 'Not'                  <- doc line, x0 ~12
    [invoice suffix]  INR  Invoice Amt | Retention | TDS | TDS amt | Net amount | Purch.Doc
    Posting date      Scroll date | 'Applicable'
                      Invoice date
    *narration        ('*Inv. not uploaded on GSTN Portal, GST amt on hold',
                       '*Reversal of TDS adjusted against manual provision')
Numbers are right-aligned, so columns are matched on the word's right edge.
Net amount is signed (GST holds and TDS-provision reversals negative).

advice_ref = payment document number without the year, the same value the HDFC
Enet copy of this payment prints as 'Payment Doc No' (parsers/hdfc_enet.py), so
the loader keeps one copy.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text, pdf_words

HEAD_RE = re.compile(r"Payment\s+document\s+no\.?\s*(\d+)\s+(\d{4})\s+for\s+INR\s+([\d,]+\.\d{2})"
                     r"(?:\s+Vide\s+E-?\s*Payment\s+UTR\s+no\.?\s*([A-Z0-9]+))?", re.I | re.S)
DATE_RE = re.compile(r"Payment\s+Date\s*:\s*(\d{2}\.\d{2}\.\d{4})", re.I)
TOTAL_RE = re.compile(r"^Total\s+([\d,]+\.\d{2}-?)\s*$", re.I | re.M)
DATE_TXT = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

# column bands (points); amounts by right edge x1, text columns by left edge x0
DOC_X0 = (0, 40)
INV_X0 = (70, 140)
SCROLL_X0 = (138, 208)
AMT_BANDS = {                      # name: (x1 min, x1 max)
    "invoice_amount": (280, 322),
    "retention": (360, 395),
    "tds": (396, 432),
    "tds_deduction": (433, 466),
    "net": (467, 543),
}
PURCH_X0 = 543


def _lines(words):
    """Group words into visual lines: (page, top) clusters within 2pt, sorted left to right."""
    out = []
    for w in sorted(words, key=lambda w: (w["page"], w["top"], w["x0"])):
        if out and out[-1][0]["page"] == w["page"] and abs(out[-1][0]["top"] - w["top"]) <= 2:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _in(v, band):
    return band[0] <= v < band[1]


def _is_doc(line) -> bool:
    w = line[0]
    return _in(w["x0"], DOC_X0) and re.fullmatch(r"\d{6,12}", w["text"]) is not None


def _is_prefix(line) -> bool:
    return (len(line) == 1 and _in(line[0]["x0"], INV_X0) and not DATE_TXT.match(line[0]["text"]))


def _body_lines(words):
    """Table lines only: drop each page's preamble/header and everything from 'Total' on."""
    keep, in_body = [], False
    for line in _lines(words):
        first = line[0]["text"]
        texts = [w["text"] for w in line]
        if "Document" in texts[:1]:
            in_body = False                   # header starts (every page repeats it)
            continue
        if not in_body:
            if first == "Date" and "Amount" in texts:
                in_body = True                # last header line
            continue
        if first == "Total" or first.startswith("If"):
            in_body = False
            continue
        keep.append(line)
    return keep


def _kind(narr: str, net: float) -> str:
    n = narr.upper()
    if "GST AMT ON HOLD" in n or "GST TAX HOLD" in n:
        return "GST_HOLD"
    if "TDS" in n:
        return "TDS"
    return "INVOICE" if net >= 0 else "DEDUCTION"


def _block_to_line(block) -> Line:
    doc_line = next(l for l in block if _is_doc(l))
    inv_parts, inv_dates, notes, amounts = [], [], [], {}
    posting = scroll_date = scroll_no = None
    for line in block:
        if line[0]["text"].startswith("*") and line[0]["x0"] < 40:
            notes.append(" ".join(w["text"] for w in line).lstrip("*"))
            continue
        for w in line:
            t = w["text"]
            if line is doc_line and w is line[0]:
                continue
            if _in(w["x0"], DOC_X0) and DATE_TXT.match(t):
                posting = t
            elif _in(w["x0"], INV_X0):
                (inv_dates if DATE_TXT.match(t) else inv_parts).append(t)
            elif _in(w["x0"], SCROLL_X0):
                if DATE_TXT.match(t):
                    scroll_date = t
                elif re.fullmatch(r"\d{6,}", t):
                    scroll_no = t
            elif w["x0"] >= PURCH_X0:
                if t != "N.A.":
                    amounts["purch_doc"] = t
            else:
                for name, band in AMT_BANDS.items():
                    if _in(w["x1"], band) and num(t) is not None:
                        amounts.setdefault(name, []).append(num(t))
    # '9100000001' + '2026' = SAP document + fiscal year (TDS-provision reversals);
    # '27KOL-0164' + '5' = an invoice number wrapped by the column width.
    # 'MANUAL' + 'PROVISION' = a TDS manual-provision row, no invoice behind it.
    reference = None
    if len(inv_parts) == 2 and inv_parts[0].isdigit() and re.fullmatch(r"20\d\d", inv_parts[1]):
        invoice = f"{inv_parts[0]}/{inv_parts[1]}"
    elif inv_parts and all(p.isalpha() for p in inv_parts):
        invoice, reference = None, " ".join(inv_parts)
    else:
        invoice = "".join(inv_parts) or None
    nets = amounts.get("net", [])
    if len(nets) != 1:
        raise ValueError(f"row {doc_line[0]['text']}: {len(nets)} net amounts {nets}")
    net = nets[0]
    narr = " ".join(notes)
    tds = [v for v in amounts.get("tds", []) if v]
    ded = [v for v in amounts.get("tds_deduction", []) if v]
    return Line(
        invoice_no=invoice,
        invoice_date=date(inv_dates[0]) if inv_dates else None,
        doc_ref=doc_line[0]["text"],
        line_type=_kind(narr, net),
        description=narr or None,
        gross_amount=(amounts.get("invoice_amount") or [None])[0] or None,
        tds_amount=abs(tds[0]) if tds else None,
        deduction_amount=ded[0] if ded else None,
        net_amount=net,
        extra={k: v for k, v in {"reference": reference,
                                  "posting_date": str(date(posting)) if posting else None,
                                  "scroll_no": scroll_no,
                                  "scroll_date": str(date(scroll_date)) if scroll_date else None,
                                  "purch_doc": amounts.get("purch_doc")}.items() if v},
    )


def parse_pdf(data: bytes, hint: str, source_file: str) -> Advice:
    text = open_pdf_text(data, hint)
    head = HEAD_RE.search(text)
    if not head:
        raise Skip("no 'Payment document no.' header")
    total = TOTAL_RE.search(text)
    d = DATE_RE.search(text)
    advice = Advice(customer="rbml", payer_name="RBML Solutions India Ltd",
                    advice_ref=head.group(1), utr=head.group(4),
                    payment_date=date(d.group(1)) if d else None,
                    payment_amount=num(total.group(1)) if total else num(head.group(3)),
                    source_file=source_file, extra={"fiscal_year": head.group(2)})
    if total and num(total.group(1)) != num(head.group(3)):
        advice.extra["header_amount"] = num(head.group(3))

    blocks, pending = [], None
    lines = _body_lines(pdf_words(data))
    for i, line in enumerate(lines):
        if _is_doc(line):
            blocks.append(([pending] if pending else []) + [line])
            pending = None
        elif _is_prefix(line) and i + 1 < len(lines) and _is_doc(lines[i + 1]):
            pending = line
        elif blocks:
            blocks[-1].append(line)
    advice.lines = [_block_to_line(b) for b in blocks]
    _repair_nets(advice)
    return advice


def _repair_nets(advice: Advice) -> None:
    """Some RBML PDFs print a wrong Net amount on invoice rows (10 Feb 2026, doc 4200000003:
    invoice 14,850.00, TDS -14.14, net printed 59,400.00 = 4x the invoice) while the
    printed Total is right. If the advice does not add up as printed but does once
    every such row's net is recomputed as invoice - TDS + deduction, use the recomputed
    nets and keep the printed value in extra['printed_net']."""
    if advice.check()[0] != "mismatch":
        return
    fixes = {}
    for i, l in enumerate(advice.lines):
        if l.gross_amount is None:
            continue
        calc = round(l.gross_amount - (l.tds_amount or 0.0) + (l.deduction_amount or 0.0), 2)
        if abs(calc - (l.net_amount or 0.0)) > 0.01:
            fixes[i] = calc
    if not fixes:
        return
    total = round(sum(fixes.get(i, l.net_amount or 0.0) for i, l in enumerate(advice.lines)), 2)
    if advice.payment_amount is None or abs(total - advice.payment_amount) > 1.0:
        return
    for i, calc in fixes.items():
        line = advice.lines[i]
        line.extra["printed_net"] = line.net_amount
        line.net_amount = calc
    advice.extra["net_recomputed_rows"] = len(fixes)


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    return [parse_pdf(a.data, mail.subject, a.name) for a in pdfs]
