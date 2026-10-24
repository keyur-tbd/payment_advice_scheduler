"""
63Ideas Infolabs (Ninjacart, Flipkart Quick) - ninjaping_alerts@ninjacart.com,
'... GRN Payment Details for PO raised from last 2 months'. Not one advice per mail: each
mail is a rolling snapshot of every GRN of the last two months, one .xlsx row per GRN.
Three column layouts so far (names differ, meaning does not):

    since Aug 2026  PO_Facility Vendor POID GRNID Vendor_Invoice_id DeliveryDate GRNTotal
                    TotalPenaltyAmount FreightCharge ProcessingCharge CN_DN Net_Payable_Amount
                    TDSAmount Paid_Value UTR_NO Paid_Date Remarks PO_Qty GRN_Qty Short_Qty
    Jul 2026        same without Vendor_Invoice_id and UTR_NO
    to Jun 2026     Vendor POID GRNID Vendor_Invoice_Id podate POTotal GRNTotal Note_Amount
                    Net_Payable_Amount TDSAmount Bank_Payment paid_amount UTR Paid_Date Remarks ...

Every payment settles one GRN, so each paid row is one advice, advice_ref = 'GRN-<GRNID>'
(stable across layouts, so later snapshots of the same payment land as duplicates). The
printed amount is the paid value; the line's net is Net_Payable_Amount - TDSAmount, so the
usual total check holds the row's own arithmetic. The mail body's month table (Paid_Value
per PO month) must also equal the sheet's paid values by month, else the snapshot is rejected.
Unpaid rows (no Paid_Date / UTR) are skipped.
"""

from __future__ import annotations

import io
import re
from collections import defaultdict

from pa_common import Advice, Line, Mail, Skip, date, num

OUR_INVOICE = re.compile(r"\d{2}[A-Z]{3}-\d+")
MONTH = re.compile(r"20\d\d [A-Z][a-z]+")
ALIASES = {
    "utr": ("UTR_NO", "UTR"),
    "invoice": ("Vendor_Invoice_id", "Vendor_Invoice_Id"),
    "paid": ("Paid_Value", "paid_amount"),
    "month_date": ("DeliveryDate", "podate"),
    "cn_dn": ("CN_DN", "Note_Amount"),
}


def _get(r: dict, key: str):
    for name in ALIASES.get(key, (key,)):
        if name in r:
            return r[name]
    return None


def _body_months(body: str) -> dict:
    """{'2026 September': paid} from the month table: header tokens after 'yearmonth', then
    one row per month with as many cells as the header."""
    toks = [t.strip() for t in body.splitlines() if t.strip()]
    if "yearmonth" not in toks:
        return {}
    i = toks.index("yearmonth")
    j = next((k for k in range(i + 1, len(toks)) if MONTH.fullmatch(toks[k])), None)
    if j is None:
        return {}
    head = toks[i:j]
    if "Paid_Value" not in head:
        return {}
    width, at = len(head), head.index("Paid_Value")
    out = {}
    while j + width <= len(toks) and MONTH.fullmatch(toks[j]):
        out[toks[j]] = num(toks[j + at])
        j += width
    return out


def parse(mail: Mail) -> list[Advice]:
    import openpyxl

    books = [a for a in mail.attachments if a.name.lower().endswith(".xlsx")]
    if not books:
        raise Skip("no workbook attached")
    months = _body_months(mail.body)
    advices, by_month = [], defaultdict(float)
    for att in books:
        wb = openpyxl.load_workbook(io.BytesIO(att.data), data_only=True, read_only=True)
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
        head = [str(h or "").strip() for h in rows[0]] if rows else []
        if "GRNID" not in head or not any(n in head for n in ALIASES["paid"]):
            raise ValueError(f"{att.name}: no GRNID / paid value columns: {head}")
        for raw in rows[1:]:
            if not raw or all(c in (None, "") for c in raw):
                continue
            r = dict(zip(head, raw))
            when = date(_get(r, "month_date"))
            paid = num(_get(r, "paid")) or 0.0
            if when:
                by_month[when.strftime("%Y %B")] += paid
            utr = str(_get(r, "utr") or "").strip() or None
            if not utr and not r.get("Paid_Date"):
                continue
            inv_raw = str(_get(r, "invoice") or "").strip()
            m = OUR_INVOICE.search(inv_raw)
            gross, payable = num(r.get("GRNTotal")), num(r.get("Net_Payable_Amount"))
            tds = num(r.get("TDSAmount")) or 0.0
            deductions = sum(num(r.get(k)) or 0.0 for k in ("TotalPenaltyAmount", "FreightCharge", "ProcessingCharge"))
            cn_dn = num(_get(r, "cn_dn")) or 0.0
            adv = Advice(customer="ninjacart", payer_name="63Ideas Infolabs Pvt Ltd (Ninjacart)",
                         advice_ref=f"GRN-{r.get('GRNID')}", utr=utr, payment_date=date(r.get("Paid_Date")),
                         payment_amount=round(paid, 2), source_file=att.name,
                         extra={k: v for k, v in {"facility": r.get("PO_Facility"), "remarks": r.get("Remarks"),
                                                  "bank_payment": num(r.get("Bank_Payment"))}.items() if v})
            adv.lines.append(Line(
                invoice_no=m.group(0) if m else None, invoice_date=date(r.get("DeliveryDate")),
                doc_ref=f"PO {r.get('POID')} / GRN {r.get('GRNID')}", line_type="INVOICE",
                gross_amount=gross, tds_amount=tds or None,
                deduction_amount=round(deductions - cn_dn, 2) or None,
                net_amount=round((payable or 0.0) - tds, 2),
                extra={k: v for k, v in {"vendor_invoice_id": None if m and m.group(0) == inv_raw else inv_raw,
                                         "po_date": date(r.get("podate")), "po_total": num(r.get("POTotal")),
                                         "penalty": num(r.get("TotalPenaltyAmount")), "freight": num(r.get("FreightCharge")),
                                         "processing": num(r.get("ProcessingCharge")), "cn_dn": cn_dn,
                                         "po_qty": r.get("PO_Qty") or r.get("POQuantity"),
                                         "grn_qty": r.get("GRN_Qty") or r.get("GRNQuantity"),
                                         "short_qty": r.get("Short_Qty") or r.get("ShortageQuantity")}.items() if v}))
            advices.append(adv)
    if months:
        off = {k: (v, round(by_month.get(k, 0.0), 2)) for k, v in months.items() if abs((v or 0) - by_month.get(k, 0.0)) > 1}
        if off:
            raise ValueError(f"sheet paid value by month disagrees with the mail body: {off}")
    if not advices:
        raise Skip("no paid GRN in the snapshot")
    return advices
