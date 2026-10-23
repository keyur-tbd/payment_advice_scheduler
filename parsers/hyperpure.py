"""
Zomato Hyperpure - hpvendorsupport@zomato.com, 'Required Payment Advice_ZOMATO HYPERPURE
PRIVATE LIMITED'. Hyperpure sends no advice of its own: accounts@ raises a ticket
listing the bank credits it cannot knock off, and Zomato support answers on the
ticket ('Re:[## 12345 ##] ...'). One advice per UTR (advice_ref = utr).

Zomato's answer is the FIRST table in the mail that has invoice + amount + UTR
columns (older answers further down the thread are history and are ignored), or,
when the body has none, an attached workbook. Header spellings drift; seen:
    Reference | Amount in local currency | Withholding tax amnt | Document Header Text | UTR No
    PO Number | Vendor Names | GRN number | SupplierBillNumber | TDS | Payout amount | UTR
    Invoice Number | Amount | TDS | GRN Number | UTR No           (+ PO No / Paid Amount variants)
The amount is NET of TDS (a one-line UTR equals its bank credit; Payout amount
equals SAP's vendor-line amount), so net_amount = amount and gross = amount + TDS.

Some replies paste the grid as plain text instead (tab-separated, or one cell per
paragraph; Oct-Nov 2025 ones even drop the header row, and one labels the UTR
column 'Adjusted DN'), so a text fallback reads those; the UTR column is then
recognised by its values. Replies that list invoices without any UTR (PO-layout
tables, a screenshot) cannot be tied to a payment and are skipped.

payment_amount (the independent check) = the bank credit for that UTR from our
own request quoted in the thread: Transaction Date | Particulars
('NEFT/HDFCH01300000001/ZOMATOHYPERPURE...') | Amount(INR), as an HTML table or
as plain text. A UTR not quoted gets no payment_amount and lands as no_total;
fresh 'Required Payment Advice' mails (not replies) carry no quote at all.
Zomato often answers only part of a credit, so those land as mismatch.

Three replies attach Zomato's whole SAP vendor ledger (FBL1N export, 40 columns,
'Document Number ... UTR No ...'). There each UTR is self-balancing: the ZP
payment document is the printed total and the rows it cleared are the lines
(KR invoice, KG credit memo, AB clearing, KA/SA adjustments; amounts are
vendor-side signs, so net_amount = -amount). Rows without a UTR are open items
and are left out. These ledgers repeat earlier UTRs; the loader keeps the first copy.
"""

from __future__ import annotations

import io
import re
from collections import OrderedDict, defaultdict

from pa_common import Advice, Line, Mail, Skip, date, num

PAYER = "Zomato Hyperpure Private Limited"

_COLS = {
    "invoice": ("reference", "invoicenumber", "invicenumber", "supplierbillnumber", "invoiceno", "billnumber"),
    "amount": ("amountinlocalcurrency", "payoutamount", "paidamount", "amount", "netamount"),
    "tds": ("withholdingtaxamnt", "tds", "tdsamount"),
    "grn": ("documentheadertext", "grnnumber", "grn", "grnno"),
    "utr": ("utrno", "utr", "utrnumber"),
    "po": ("ponumber", "pono", "purchaseordernumber"),
    "utr_date": ("utrdate", "paymentdate"),
}
_UTR_IN_TEXT = re.compile(r"(?:NEFT|RTGS|IMPS|FT)[/-]([A-Z0-9]{10,})", re.I)
_UTR_VALUE = re.compile(r"^[A-Z]{4}[A-Z0-9]{10,}$")
_QUOTE = re.compile(r"^\s*(On .{5,120}wrote:|-+\s*Original Message|From:\s)", re.M)
_ROW_START = re.compile(r"^(\d{2}[A-Z]{3}-\d{3,6}|CPC[A-Z]{2}\d{2}-PO-\d+)$")


def _norm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _colmap(header: list) -> dict:
    hn = [_norm(h) for h in header]
    out = {}
    for key, names in _COLS.items():
        for name in names:                     # first spelling in priority order wins
            if name in hn:
                out[key] = hn.index(name)
                break
    return out


def _html_tables(html: str) -> list[list[list[str]]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for t in soup.find_all("table"):
        if t.find("table"):
            continue
        rows = [[re.sub(r"\s+", " ", c.get_text(" ", strip=True).replace("\xa0", " ")).strip()
                 for c in r.find_all(["td", "th"])] for r in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if rows:
            out.append(rows)
    return out


def _bank_credits(tables) -> dict:
    """UTR -> (credited amount, credit date), from our own quoted request tables."""
    out = {}
    for rows in tables:
        hn = [_norm(h) for h in rows[0]]
        if "particulars" not in hn:
            continue
        ip = hn.index("particulars")
        ia = next((i for i, h in enumerate(hn) if h.startswith("amount")), None)
        idt = next((i for i, h in enumerate(hn) if "date" in h), None)
        if ia is None:
            continue
        for r in rows[1:]:
            if len(r) <= max(ip, ia):
                continue
            m = _UTR_IN_TEXT.search(r[ip].replace(" ", ""))
            amt = num(r[ia])
            if m and amt is not None:
                out.setdefault(m.group(1).upper(), (amt, date(r[idt]) if idt is not None and idt < len(r) else None))
    return out


def _text_credits(text: str) -> dict:
    """Same, when our quoted request is plain text: one paragraph per cell
    ('05-02-2026' / 'NEFT/HDFCH00700000002/ZOMATO ...' / '10,000.00')."""
    out = {}
    blocks = [re.sub(r"\s+", " ", b.replace("\xa0", " ")).strip(" >")
              for b in re.split(r"\r?\n[\s>]*\r?\n", text or "")]
    blocks = [b for b in blocks if b]
    for i, b in enumerate(blocks[:-1]):
        m = _UTR_IN_TEXT.search(b.replace(" ", ""))
        amt = num(blocks[i + 1])
        if m and amt is not None:
            out.setdefault(m.group(1).upper(), (amt, date(blocks[i - 1]) if i else None))
    return out


def _lines_table(rows) -> tuple[dict, list] | None:
    """(column map, data rows) when rows[0] is a header with invoice + amount + UTR columns.
    A mislabelled UTR column ('Adjusted DN', Nov 2025) is found by its values instead."""
    cm = _colmap(rows[0])
    if {"invoice", "amount"} <= cm.keys() and "utr" not in cm:
        data = [r for r in rows[1:] if r and len(r) == len(rows[0])]
        for i in range(len(rows[0])):
            vals = [re.sub(r"\s+", "", str(r[i] or "")).upper() for r in data]
            if data and all(_UTR_VALUE.match(v) for v in vals):
                cm["utr"] = i
                break
    if {"invoice", "amount", "utr"} <= cm.keys():
        return cm, rows[1:]
    return None


def _text_tables(text: str) -> list[list[list[str]]]:
    """Tables pasted as plain text: tab-separated lines, or one cell per paragraph
    (Outlook tables flattened to text). Only the reply itself, never the quoted thread."""
    m = _QUOTE.search(text)
    text = (text[:m.start()] if m else text).replace("\xa0", " ").replace("\r", "")
    out = []
    tabbed = [l.split("\t") for l in text.split("\n") if "\t" in l]
    if len(tabbed) > 1:
        out.append([[c.strip() for c in r] for r in tabbed])
    blocks = [re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n\s*\n", text)]
    blocks = [b for b in blocks if b]
    start = next((i for i, b in enumerate(blocks) if _ROW_START.match(b)), None)
    if start:
        # header cells sit between the 'Please find the below details.' line and the first row
        hdr_start = max((i + 1 for i, b in enumerate(blocks[:start])
                         if re.search(r"please find|details|^hi\b", b, re.I)), default=0)
        header = blocks[hdr_start:start]
        if not header:
            # Oct-Nov 2025 replies paste the grid without its header row; the
            # cells are always Invoice | Amount | TDS | GRN | UTR (checked: the UTR
            # column must still look like a UTR, see _lines_table)
            nxt = next((j for j in range(start + 1, len(blocks)) if _ROW_START.match(blocks[j])), None)
            if nxt == start + 5:
                header = ["Invoice Number", "Amount", "TDS", "GRN Number", "UTR No"]
        n = len(header)
        rows = [header]
        i = start
        while n and i + n <= len(blocks) and _ROW_START.match(blocks[i]):
            rows.append(blocks[i:i + n])
            i += n
        if len(rows) > 1:
            out.append(rows)
    return out


def _from_table(cm: dict, body: list, credits: dict, source: str) -> list[Advice]:
    groups: "OrderedDict[str, list[Line]]" = OrderedDict()
    utr_dates: dict = {}
    for r in body:
        r = list(r)
        if len(r) <= max(cm.values()):
            r += [None] * (max(cm.values()) + 1 - len(r))
        inv = str(r[cm["invoice"]] or "").strip()
        amt = num(r[cm["amount"]])
        utr = re.sub(r"\s+", "", str(r[cm["utr"]] or "")).upper()
        if not inv or amt is None:
            continue
        if not utr:
            raise ValueError(f"line {inv} has no UTR")
        tds = num(r[cm["tds"]]) if "tds" in cm else None
        if "utr_date" in cm and r[cm["utr_date"]]:
            utr_dates.setdefault(utr, date(r[cm["utr_date"]]))
        groups.setdefault(utr, []).append(Line(
            invoice_no=inv,
            doc_ref=str(r[cm["grn"]]).strip() if "grn" in cm and r[cm["grn"]] else None,
            line_type="INVOICE" if amt >= 0 else "DEBIT_NOTE",
            gross_amount=round(amt + (tds or 0), 2),
            tds_amount=tds,
            net_amount=round(amt, 2),
            extra={"po": str(r[cm["po"]]).strip()} if "po" in cm and r[cm["po"]] else {},
        ))
    return [Advice(customer="hyperpure", payer_name=PAYER, advice_ref=utr, utr=utr,
                   payment_date=credits.get(utr, (None, None))[1] or utr_dates.get(utr),
                   payment_amount=credits.get(utr, (None, None))[0], lines=lines, source_file=source)
            for utr, lines in groups.items()]


_SAP_TYPES = {"KR": "INVOICE", "KG": "DEBIT_NOTE", "AB": "OTHER", "KA": "OTHER", "SA": "OTHER", "KZ": "OTHER"}


def _from_sap_ledger(header: list, body: list, source: str) -> list[Advice]:
    h = [str(x or "").strip() for x in header]
    ix = {k: h.index(k) for k in ("Document Number", "Document Type", "Reference", "Amount in local currency",
                                   "Withholding tax amnt", "UTR No", "Document Header Text", "Payment date",
                                   "Posting Date")}
    pay: dict = defaultdict(float)
    pay_date: dict = {}
    lines: dict = defaultdict(list)
    for r in body:
        utr = re.sub(r"\s+", "", str(r[ix["UTR No"]] or "")).upper()
        amt = num(r[ix["Amount in local currency"]])
        if not utr or amt is None:
            continue
        dtype = str(r[ix["Document Type"]] or "").strip()
        if dtype == "ZP":
            pay[utr] += amt
            pay_date.setdefault(utr, date(r[ix["Posting Date"]]))
            continue
        tds = num(r[ix["Withholding tax amnt"]])
        lines[utr].append(Line(
            invoice_no=str(r[ix["Reference"]] or "").strip() or None,
            doc_ref=str(r[ix["Document Header Text"]] or "").strip() or None,
            line_type=_SAP_TYPES.get(dtype, "OTHER"),
            description=f"SAP {dtype} {r[ix['Document Number']]}",
            gross_amount=round(-amt - (tds or 0), 2) if tds is not None else None,
            tds_amount=-tds if tds is not None else None,
            net_amount=round(-amt, 2),
            extra={"sap_doc": str(r[ix["Document Number"]]), "sap_type": dtype},
        ))
    return [Advice(customer="hyperpure", payer_name=PAYER, advice_ref=utr, utr=utr,
                   payment_date=pay_date.get(utr), payment_amount=round(pay[utr], 2) if utr in pay else None,
                   lines=lines[utr], source_file=source, extra={"layout": "sap_vendor_ledger"})
            for utr in lines]


def parse(mail: Mail) -> list[Advice]:
    if "required payment" not in mail.subject.lower():
        raise Skip("not a payment advice thread")
    tables = _html_tables(mail.html)
    credits = {**_text_credits(mail.text or mail.body), **_bank_credits(tables)}
    for rows in tables:
        found = _lines_table(rows)
        if found:
            return _from_table(found[0], found[1], credits, "mail-body")

    import openpyxl
    for att in mail.attachments:
        if not att.name.lower().endswith((".xlsx", ".xlsm")):
            continue
        wb = openpyxl.load_workbook(io.BytesIO(att.data), data_only=True, read_only=True)
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True) if r and any(c not in (None, "") for c in r)]
            if not rows:
                continue
            hdr_i = next((i for i in range(min(10, len(rows))) if _lines_table(rows[i:])), None)
            if hdr_i is None:
                continue
            if "Document Type" in [str(x or "").strip() for x in rows[hdr_i]]:
                return _from_sap_ledger(rows[hdr_i], rows[hdr_i + 1:], att.name)
            cm, body = _lines_table(rows[hdr_i:])
            return _from_table(cm, body, credits, att.name)

    for rows in _text_tables(mail.text or mail.body):
        found = _lines_table(rows)
        if found:
            return _from_table(found[0], found[1], credits, "mail-body")
    quote = _QUOTE.search(mail.body)
    reply = mail.body[:quote.start()] if quote else mail.body
    has_xlsx = any(a.name.lower().endswith((".xlsx", ".xlsm")) for a in mail.attachments)
    if has_xlsx or re.search(r"\b\d{2}[A-Z]{3}-\d{4,5}\b", reply):
        raise Skip("reply lists invoices but gives no UTR, so it cannot be tied to a payment")
    raise Skip("no payment table in the reply (chatter / acknowledgement)")
