"""
Axis Bank 'Straight2axis' advices - Straight2axis@axisbank.com, later straight2axis@axis.bank.in.
Each mail carries a zip holding one PDF (pa_common unzips it: 'CB...zip/CB....pdf').
Axis sends these for two payers, told apart by 'Company Name' in the PDF:

    AVENUE SUPERMARTS LIMITED  -> 'dmart'
    RELIANCE RETAIL LIMITED    -> 'reliance'
    anything else              -> ValueError (surfaces as an error; add a route here)

Header (both payers):
    Beneficiary Code : 0000100001        Payment Ini. Date : 23-02-2026
    Beneficiary Name : MIMANSA ...       Amount : 12,345.67
    ...                                  Company Name : AVENUE SUPERMARTS LIMITED
    ...                                  Payment Ref. No. : 20010000012025ASPL
    UTR Number : CB0100000001            Bank Ref. No. : CB0100000001

DMart table (pages repeat the header row; site names wrap onto a second line):
    Sr.No. Invoice Number Invoice Date TCS Collected Deduction: TDS Net Amount Site Site Name
    1 26MUM-10001 09-02-2026 00.00 00.84 839.16 4138 Example DMart
    16 41500001 06-11-2025 00.00 00.00 -270.00 4158 Example Site      <- DMart debit note
    DMart
Net Amount is signed and the rows add up to Amount.

Reliance table: Axis prints only the FIRST document of the payment (its SAP doc,
e.g. 0010000001) with zero deductions, so the PDF can never add up. The same
payment arrives complete from RR.Paymentadvice@ril.com (parsers/ril_rr.py) and
Axis 'Payment Ref. No.' equals that advice's SAP doc number (all 19 Axis copies
to Oct 2026 matched). So Reliance copies load as header-only advices under the
same advice_ref; the ril_rr feed has the higher priority and replaces them.
"""

from __future__ import annotations

import re

from pa_common import Advice, Line, Mail, Skip, date, num, open_pdf_text

AMT = r"-?[\d,]+\.\d{2}-?"
FIELD = {
    "date": re.compile(r"Payment Ini\. Date\s*:\s*(\d{2}-\d{2}-\d{4})"),
    "amount": re.compile(r"\bAmount\s*:\s*(" + AMT + ")"),
    "company": re.compile(r"Company Name\s*:\s*(.+?)\s*$", re.M),
    "ref": re.compile(r"Payment Ref\. No\.\s*:\s*(\S+)"),
    "utr": re.compile(r"UTR Number\s*:\s*(\S+)"),
    "code": re.compile(r"Beneficiary Code\s*:\s*(\S+)"),
}
DMART_ROW = re.compile(r"^(\d{1,4})\s+(.+?)\s+(\d{2}-\d{2}-\d{4})\s+(" + AMT + r")\s+(" + AMT + r")\s+(" + AMT
                       + r")\s+(\d{3,6})\b\s*(.*)$")
RIL_ROW = re.compile(r"^(\d{1,4})\s+(\S+)\s+(\d{2}-\d{2}-\d{4})\s+(" + AMT + r")\s+(" + AMT + r")\s+(" + AMT
                     + r")\s+(" + AMT + r")\s*$")
INVOICE_NO = re.compile(r"^\d{2}[A-Z]{3}[- ]?\d+$")
ROUTES = {"AVENUE SUPERMARTS": "dmart", "RELIANCE RETAIL": "reliance"}


def _field(name: str, text: str):
    m = FIELD[name].search(text)
    return m.group(1).strip() if m else None


def _table_lines(text: str):
    """Lines between each 'Sr.No.' header and the footer that follows it."""
    inside = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("Sr.No."):
            inside = True
            continue
        if s.startswith("* The time taken") or s.startswith("Enrichment"):
            inside = False
            continue
        if inside and s:
            yield s


CONT_REF = re.compile(r"^(\d*)[\(\[](\d+)[\)\]]$")


def _invoice(ref: str):
    """'26MUM-10001' / '26MUM/16458' / '26/MUM-13669' -> '26MUM-10001'; None if not one of ours."""
    m = re.fullmatch(r"(\d{2})\s*/?\s*([A-Z]{3})\s*[-/ ]?\s*(\d{3,6})", ref)
    return f"{m.group(1)}{m.group(2)}-{m.group(3)}" if m else None


def _dmart_lines(text: str) -> list[Line]:
    rows: list[dict] = []
    for s in _table_lines(text):
        m = DMART_ROW.match(s)
        if m:
            _, ref, d, tcs, tds, net, site, name = m.groups()
            rows.append({"ref": ref, "date": d, "tcs": num(tcs), "tds": num(tds), "net": num(net),
                         "site": site, "name": name.strip(), "ded_doc": None})
            continue
        if not rows:
            raise ValueError(f"unreadable first table line: {s[:80]!r}")
        # Second line of a row: the invoice column may continue ('12181(41542000)' = rest of
        # '26MUM-' + DMart's deduction doc, '(41422367)', '[41371858]'), the site name wraps.
        last, tokens = rows[-1], s.split()
        c = CONT_REF.match(tokens[0])
        if c:
            last["ref"] += c.group(1)
            last["ded_doc"] = c.group(2)
            tokens = tokens[1:]
        elif last["ref"].endswith("-") and tokens[0].isdigit():
            last["ref"] += tokens[0]
            tokens = tokens[1:]
        elif "(" in last["ref"] and ")" not in last["ref"] and re.fullmatch(r"\d+\)", tokens[0]):
            last["ref"] += tokens[0]              # '26MUM-13818 (4169' + '7888)'
            tokens = tokens[1:]
        if tokens:
            last["name"] = f"{last['name']} {' '.join(tokens)}".strip()

    lines = []
    for r in rows:
        inline = re.fullmatch(r"(.+?)\s*\((\d+)\)", r["ref"])
        if inline:
            r["ref"], r["ded_doc"] = inline.group(1), inline.group(2)
        inv = _invoice(r["ref"])
        kind = ("INVOICE" if r["net"] >= 0 else "DEDUCTION") if inv else ("DEDUCTION" if r["net"] < 0 else "OTHER")
        lines.append(Line(
            invoice_no=inv,
            invoice_date=date(r["date"]),
            doc_ref=r["ded_doc"] or (None if inv else r["ref"]),
            line_type=kind,
            tds_amount=r["tds"] or None,
            net_amount=r["net"],
            extra={k: v for k, v in {"site_code": r["site"], "site_name": r["name"],
                                      "tcs_collected": r["tcs"] or None,
                                      "printed_ref": r["ref"] if inv and inv != r["ref"] else None}.items() if v},
        ))
    return lines


def _reliance_first_row(text: str) -> dict:
    for s in _table_lines(text):
        m = RIL_ROW.match(s)
        if m:
            return {"axis_first_doc": m.group(2).lstrip("0"), "axis_first_doc_date": str(date(m.group(3))),
                    "axis_first_doc_amount": num(m.group(4))}
    return {}


def parse_pdf(data: bytes, hint: str, source_file: str) -> Advice:
    text = open_pdf_text(data, hint)
    company = _field("company", text)
    if not company or not _field("ref", text):
        raise Skip("not an Axis payment advice")
    customer = next((c for key, c in ROUTES.items() if key in company.upper()), None)
    if customer is None:
        raise ValueError(f"Axis advice from unrouted payer {company!r}")
    advice = Advice(customer=customer, payer_name=company.title(), advice_ref=_field("ref", text),
                    utr=_field("utr", text), payment_date=date(_field("date", text)),
                    payment_amount=num(_field("amount", text)), source_file=source_file,
                    extra={"beneficiary_code": _field("code", text)})
    if customer == "dmart":
        advice.lines = _dmart_lines(text)
    else:
        advice.header_only = True
        advice.extra.update(_reliance_first_row(text))
        advice.extra["narration"] = "Axis copy lists only the first document; full advice via RR.Paymentadvice"
    return advice


def parse(mail: Mail) -> list[Advice]:
    pdfs = [a for a in mail.attachments if a.name.lower().endswith(".pdf")]
    if not pdfs:
        raise Skip("no PDF in the attachment / zip")
    return [parse_pdf(a.data, mail.subject, a.name) for a in pdfs]
