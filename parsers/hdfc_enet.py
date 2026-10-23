"""
HDFC Bank Enet advices - Enetadvicemailing@hdfcbank.net / @hdfcbank.bank.in.
One password-protected PDF per mail (password = last 6 digits of our account
number, which HDFC prints in most subjects; see pa_common.pdf_passwords).
HDFC carries several payers; the payer is the first line of page 1.

MAGSON RETAIL AND DISTRIBUTION LIMI / MAGSON RETAIL AND DIST LTDAC NASTAB -> magson
    No line table, only: Bank Reference No : FT... (advice_ref), Value Date,
    'vide reference no. HDFCH... for Rs 1000.00' (UTR + amount) and
    'Narration : MIMANSA VAS 1001 1002 1003'. Loaded header_only, narration in extra.

SAP-style advices (RBML, Avenue E-commerce, Trent). Header: Payment Doc No
(advice_ref), Value date, Bank Ref No (UTR); footer row under
'Payment document | Payment Date | Currency | Payment amount' (printed amount).
The table has four cell columns at x ~36 / 156 / 276 / 396 and every row spans
two text lines, so it is read from word positions, not plain text:

RBML SOLUTIONS INDIA LIMITED -> rbml
    DocNo         | YourDocNo (our invoice) | ScrollNo   | ScrolledAmount
    InvoiceAmount | DocDate                 | ScrollDate |
    No signs are printed. The first row for an invoice is the invoice; a repeat
    row for the same invoice is a deduction (GST hold / TDS; the RIL Treasury
    copy of the same payment shows them as '*Inv. not uploaded on GSTN Portal,
    GST amt on hold'). If that does not reach the printed amount, the signs are
    solved as the subset of rows whose negation hits it (flagged signs_inferred).
    The RIL Treasury feed carries the same payments with signs and outranks this copy.

AVENUE ECOMMERCE LTD -> dmart_ecom
    Invoice Number ('26MUM 10001') | Invoice Date | Net Amount | TCS ('0 00' = 0.00)
    TDS ('4 00')                   | Site Code    | Site Name  |

TRENT HYPERMARKET PVT LIMITED -> star_bazaar
    Invoice Number | Document Number   | Invoice Date | Document Type (RE invoice, VC ...)
    Invoice Amount | Amount Deducted.  | Net Amount   | Doc. Header Text
"""

from __future__ import annotations

import re
from itertools import groupby

from pa_common import (Advice, Line, Mail, Skip, date, num, open_pdf_text, pdf_passwords, pdf_words)

PAYERS = (
    ("MAGSON RETAIL", "magson"),
    ("RBML SOLUTIONS", "rbml"),
    ("AVENUE ECOMMERCE", "dmart_ecom"),
    ("TRENT HYPERMARKET", "star_bazaar"),
)
COL_EDGES = (150, 270, 390)          # cell boundaries between the four columns
_AMT = r"[\d,]+\.\d{2}-?"
OUR_INVOICE = re.compile(r"^\d{2}[A-Z]{3}-\d+")


def _ours(ref: str):
    """Our invoice number ('27KOL-01692', '26MUM-12345-Z') or None; payer documents
    (RIL 9100...2026, Avenue charge notes, 'MANUAL PROVISION', 'RECO FNF') go to doc_ref."""
    return ref if ref and OUR_INVOICE.match(ref) else None


def _payer(first_line: str) -> str:
    up = first_line.upper().replace("E-COMMERCE", "ECOMMERCE")     # corporate-netbanking copies hyphenate
    for prefix, customer in PAYERS:
        if up.startswith(prefix):
            return customer
    raise ValueError(f"unknown HDFC Enet payer {first_line!r}")


def _cellnum(cell: str):
    """'4681.00' / '1,00,000.00' / '0 00' (HDFC drops the point on some columns)."""
    cell = cell.strip()
    if re.fullmatch(r"-?\d+ \d{2}-?", cell):
        cell = cell.replace(" ", ".")
    return num(cell)


def _search(pattern: str, text: str):
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
def _magson(text: str, first: str, source_file: str) -> Advice:
    amount = _search(r"for\s+Rs\.?\s*(" + _AMT + r")", text)
    narration = _search(r"Narration\s*:\s*([^\n]*)", text)
    return Advice(
        customer="magson", payer_name=first,
        advice_ref=_search(r"Bank Reference No\s*:\s*(\S+)", text),
        utr=_search(r"vide\s+ref(?:erence)?\.?\s*no\.?\s*(\S+)", text),
        payment_date=date(_search(r"Value Date\s*:\s*(\S+)", text)),
        payment_amount=num(amount), source_file=source_file, header_only=True,
        extra={"narration": narration,
               "narration_refs": re.findall(r"\b\d{3,}\b", narration or "")},
    )


def _table_rows(words: list) -> list[list[str]]:
    """Text lines inside each page's line table, as four cells each."""
    out = []
    for _, page_words in groupby(sorted(words, key=lambda w: (w["page"], w["top"], w["x0"])),
                                 key=lambda w: w["page"]):
        lines = []
        for _, ws in groupby(page_words, key=lambda w: round(w["top"])):
            lines.append(sorted(ws, key=lambda w: w["x0"]))
        rules = [i for i, ws in enumerate(lines) if ws[0]["text"].startswith("_____")]
        head = next((i for i, ws in enumerate(lines)
                     if ws[0]["text"] in ("Invoice", "DocNo")), None)
        if head is None:
            continue
        after = [i for i in rules if i > head]
        if len(after) < 2:
            continue
        for ws in lines[after[0] + 1: after[1]]:
            cells = ["", "", "", ""]
            for w in ws:
                c = sum(w["x0"] >= e for e in COL_EDGES)
                cells[c] = (cells[c] + " " + w["text"]).strip()
            out.append(cells)
    return out


def _pairs(rows: list[list[str]]) -> list[tuple[list[str], list[str]]]:
    """Two text lines per table row; the second always opens with an amount. A line
    that is neither (a wrapped site name) is folded into the previous row."""
    pairs, first = [], None
    for cells in rows:
        if first is None:
            first = cells
            continue
        if _cellnum(cells[0]) is None:
            if pairs:                                   # continuation of the last row
                pairs[-1][1][3] = (pairs[-1][1][3] + " " + " ".join(c for c in first if c)).strip()
            first = cells
            continue
        pairs.append((first, cells))
        first = None
    if first is not None:
        raise ValueError(f"table row without its second line: {first}")
    return pairs


_DMY = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _avenue_pairs(rows: list[list[str]]) -> list[tuple[list[str], list[str]]]:
    """Avenue rows open on the line whose second cell is the invoice date. The second line
    (TDS | site code | site name) is optional: deduction rows leave TDS blank and one-off
    rows ('GST DEFT 2023 24') have no second line at all (2023-24 advices)."""
    pairs = []
    for cells in rows:
        if _DMY.match(cells[1]):
            pairs.append((cells, ["", "", "", ""]))
        elif pairs and not any(pairs[-1][1]):
            pairs[-1] = (pairs[-1][0], cells)
        elif pairs:                                     # wrapped site name
            pairs[-1][1][2] = (pairs[-1][1][2] + " " + " ".join(c for c in cells if c)).strip()
        else:
            raise ValueError(f"table line before any invoice row: {cells}")
    return pairs


def _signs_by_subset(amounts: list[float], target: float) -> list[int] | None:
    """Indexes to negate so that sum == target, by subset sum on paise."""
    total = round(sum(amounts) * 100)
    need = total - round(target * 100)
    if need < 0 or need % 2:
        return None
    need //= 2
    reach = {0: []}
    for i, a in enumerate(amounts):
        p = round(a * 100)
        for s, idx in list(reach.items()):
            if s + p <= need and s + p not in reach:
                reach[s + p] = idx + [i]
        if need in reach:
            return reach[need]
    return reach.get(need)


def _sap(text: str, words: list, first: str, customer: str, source_file: str) -> Advice:
    footer = re.search(r"Payment document\s+Payment Date\s+Currency\s+Payment amount\s*\n\s*(\S+)\s+(\S+)\s+\S+\s+("
                       + _AMT + ")", text, re.I)
    adv = Advice(
        customer=customer, payer_name=first,
        advice_ref=_search(r"Payment Doc No\s*:\s*(\S+)", text) or (footer.group(1) if footer else None),
        utr=_search(r"UTR/RRN no\s+(\S+)", text) or _search(r"Bank Ref No\s*:\s*(\S+)", text),
        payment_date=date(_search(r"Value date\s*:\s*(\S+)", text)) or date(footer.group(2) if footer else None),
        payment_amount=num(footer.group(3)) if footer else num(_search(r"for\s+Rs\.?\s*(" + _AMT + ")", text)),
        source_file=source_file,
        extra={"beneficiary_code": _search(r"Beneficiary Code\s*:\s*(\S+)", text)},
    )
    rows = _table_rows(words)
    pairs = _avenue_pairs(rows) if customer == "dmart_ecom" else _pairs(rows)

    if customer == "rbml":
        for a, b in pairs:
            amt = _cellnum(b[0])
            adv.lines.append(Line(
                invoice_no=_ours(a[1]), invoice_date=date(b[1]),
                doc_ref=None if _ours(a[1]) else (a[1] or a[0] or a[2] or None),
                line_type="INVOICE", gross_amount=amt, net_amount=amt,
                extra={k: v for k, v in {"scroll_no": a[2], "scrolled_amount": a[3], "scroll_date": b[2]}.items() if v}))
        # Signs per invoice: one row -> paid as printed (an invoice, or a held GST amount
        # released later). Several rows -> the largest is the invoice, equal-amount pairs
        # are a hold and its release (net zero; which is which is not printed), and any
        # other row is a GST hold / TDS deducted.
        by_inv = {}
        for l in adv.lines:
            by_inv.setdefault(l.invoice_no or l.doc_ref, []).append(l)
        for rows in by_inv.values():
            if len(rows) == 1:
                continue
            rest = sorted(rows, key=lambda l: -l.gross_amount)[1:]
            while rest:
                l = rest.pop(0)
                twin = next((r for r in rest if abs(r.gross_amount - l.gross_amount) < 0.005), None)
                l.net_amount, l.line_type = -l.gross_amount, "GST_HOLD"
                if twin is not None:
                    rest.remove(twin)
                    twin.line_type = "GST_RELEASE"
                    l.extra["sign_ambiguous"] = twin.extra["sign_ambiguous"] = True
        status, _ = adv.check()
        if status == "mismatch" and adv.payment_amount is not None:
            amounts = [l.gross_amount for l in adv.lines]
            neg = _signs_by_subset(amounts, adv.payment_amount)
            if neg is not None:
                for i, l in enumerate(adv.lines):
                    l.net_amount = -l.gross_amount if i in neg else l.gross_amount
                    l.line_type = "DEDUCTION" if i in neg else "INVOICE"
                adv.extra["signs_inferred"] = "subset of rows negated to reach the printed amount"

    elif customer == "dmart_ecom":
        for a, b in pairs:
            inv = re.sub(r"^(\d{2}[A-Z]{3})\s+(\d+)$", r"\1-\2", a[0])
            net, tcs, tds = _cellnum(a[2]), _cellnum(a[3]), _cellnum(b[0])
            adv.lines.append(Line(
                invoice_no=_ours(inv), doc_ref=None if _ours(inv) else inv, invoice_date=date(a[1]),
                line_type="INVOICE" if (net or 0) >= 0 else "DEDUCTION",
                description=b[2] or None, tds_amount=tds, net_amount=net,
                extra={k: v for k, v in {"site_code": b[1], "tcs": tcs}.items() if v}))

    elif customer == "star_bazaar":
        for a, b in pairs:
            gross, deducted, net = _cellnum(b[0]), _cellnum(b[1]), _cellnum(b[2])
            doc_type = a[3] or None
            adv.lines.append(Line(
                invoice_no=_ours(a[0]), invoice_date=date(a[2]),
                doc_ref=a[1] if _ours(a[0]) else (" ".join(x for x in (a[0], a[1]) if x) or None),
                line_type="INVOICE" if doc_type == "RE" else "OTHER",
                description=" ".join(x for x in (doc_type, b[3]) if x) or None,
                gross_amount=gross, deduction_amount=deducted, net_amount=net))
    return adv


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF attached")
    out = []
    for att in pdfs:
        text = open_pdf_text(att.data, mail.subject)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            raise ValueError(f"{att.name}: no text layer")
        customer = _payer(lines[0])
        if customer == "magson":
            out.append(_magson(text, lines[0], att.name))
            continue
        words = None
        for pw in pdf_passwords(mail.subject):
            try:
                words = pdf_words(att.data, pw)
                break
            except Exception:                              # wrong password: try the next one
                continue
        out.append(_sap(text, words or [], lines[0], customer, att.name))
    return out
