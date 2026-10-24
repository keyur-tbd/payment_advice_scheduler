"""
Which mails feed which customer table.

A FEED is one sender format: a Gmail query plus the parser for that layout.
Banks carry several payers (HDFC Enet: Magson, RBML, Avenue E-commerce, Trent),
so a parser returns advices tagged with a CUSTOMER key and the loader routes
each advice to that customer's own table.

The same payment can arrive twice (RBML: HDFC Enet copy + RIL Treasury copy;
Reliance Retail: RR SAP advice + Axis copy). Within a table an advice is keyed
by advice_ref; the copy from the feed with the higher `priority` wins.

Source: 'Raw Cust Data' tab of "Customer Receipt Settlement Data FY26-27"; mailbox accounts@thebakersdozen.in.
Indian banks moved their mail to *.bank.in during 2026, so bank queries carry both domains.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Dict, Tuple


@dataclass(frozen=True)
class Customer:
    table: str
    codes: Tuple[str, ...]          # ERP customer codes from Raw Cust Data
    label: str


CUSTOMERS: Dict[str, Customer] = {
    "amazon":         Customer("pa_amazon",         ("C00002",), "Amazon Retail India"),
    "bigbasket":      Customer("pa_bigbasket",      ("C00005",), "Innovative Retail Concepts (BigBasket)"),
    "reliance":       Customer("pa_reliance",       ("C00035",), "Reliance Retail"),
    "rbml":           Customer("pa_rbml",           ("C00402",), "RBML Solutions (Reliance BP Mobility)"),
    # No longer a customer (owner, 2026-10-23); advices stop 27 Feb 2026, table kept for history.
    "dmart":          Customer("pa_dmart",          ("C00415",), "Avenue Supermarts (DMart)"),
    "dmart_ecom":     Customer("pa_dmart_ecom",     ("C00123",), "Avenue E-commerce (DMart Ready)"),
    "star_bazaar":    Customer("pa_star_bazaar",    ("C00149", "C00046"), "Trent Hypermarket (Star Bazaar)"),
    "natures_basket": Customer("pa_natures_basket", ("C00026",), "Nature's Basket"),
    "spencers":       Customer("pa_spencers",       ("C00041", "C00468"), "Spencer's Retail (RPSG; same ICICI template)"),
    "magson":         Customer("pa_magson",         ("C00019", "C00020", "C00085"), "Magson Retail and Distribution"),
    "hyperpure":      Customer("pa_hyperpure",      ("C00063",), "Zomato Hyperpure"),
    "zepto":          Customer("pa_zepto",          ("C00043", "C00155"), "Zepto Ltd / Kiranakart Wholesale"),
    "ninjacart":      Customer("pa_ninjacart",      ("C00408",), "63Ideas Infolabs (Ninjacart, Flipkart Quick)"),
    "prr_fresh":      Customer("pa_prr_fresh",      ("C00409",), "PRR Fresh Produce"),
    "namdhari":       Customer("pa_namdhari",       ("C00025",), "Namdhari Agro Fresh"),
    "vijetha":        Customer("pa_vijetha",        ("C00075",), "Vijetha Supermarkets"),
    "ratnadeep":      Customer("pa_ratnadeep",      ("C00033",), "Ratnadeep Retail"),
    "dorabjee":       Customer("pa_dorabjee",       ("C00011",), "Dorabjee & Co (Bank of Maharashtra advices)"),
}


@dataclass(frozen=True)
class Feed:
    query: str                      # Gmail query; the runner adds the date window
    parser: str                     # module in parsers/ exposing parse(mail) -> list[Advice]
    priority: int = 10              # higher wins when two feeds carry the same advice_ref

    def load_parser(self) -> Callable:
        return importlib.import_module(f"parsers.{self.parser}").parse


FEEDS: Dict[str, Feed] = {
    "amazon": Feed(
        "from:(noreply@amazon.com OR no-reply@amazon.com) subject:\"Remittance Advice\"", "amazon"),
    "bigbasket": Feed(
        "from:(t1payments@bigbasket.com OR t2payments@bigbasket.com OR t3payments@bigbasket.com "
        "OR t4payments@bigbasket.com) has:attachment", "bigbasket"),
    "ril_rr": Feed(
        "from:(RR.Paymentadvice@ril.com) has:attachment", "ril_rr", priority=20),
    "ril_treasury": Feed(
        "from:(treasury.paymentadvice@zmail.ril.co OR TREASURY.PAYMENTADVICE@ril.com) has:attachment",
        "ril_treasury", priority=20),
    "axis": Feed(
        "from:(Straight2axis@axisbank.com OR straight2axis@axis.bank.in) has:attachment", "axis"),
    "hdfc_enet": Feed(
        "from:(Enetadvicemailing@hdfcbank.net OR enetadvicemailing@hdfcbank.bank.in) has:attachment",
        "hdfc_enet"),
    "hdfc_corp": Feed(   # Magson Udaipur since Sep 2026; PDF password = FIRST 6 digits of our account
        "from:corporatenetbanking.automailer@hdfcbank.bank.in has:attachment", "hdfc_corp"),
    "icici": Feed(
        "from:(ipaycheck@icicibank.com OR ipaycheck@icici.bank.in) has:attachment", "icici"),
    "hyperpure": Feed(
        "from:hpvendorsupport@zomato.com subject:\"Required Payment\"", "hyperpure"),
    "zepto": Feed(
        "from:(payments@zeptonow.com) subject:\"Payment Advice\" has:attachment", "zepto"),
    "ninjacart": Feed(
        "from:ninjaping_alerts@ninjacart.com subject:\"GRN Payment Details\" has:attachment", "ninjacart"),
    "prr_fresh": Feed(
        "from:prrfreshproducepvtltd@gmail.com subject:\"Payment Voucher\" has:attachment", "prr_fresh"),
    "namdhari": Feed(
        "from:erp@namdharis.in subject:\"Payment Advice\" has:attachment", "namdhari"),
    "vijetha": Feed(
        "from:payments@vijetha.in subject:\"Payment Advice\" has:attachment", "vijetha"),
    "ratnadeep": Feed(
        "from:automail@ratnadeepretail.com has:attachment", "ratnadeep"),
    "dorabjee": Feed(   # body-only bank advice, no attachment
        "from:(MahaPayments@mahabank.co.in OR mahapayments@mahabank.bank.in) subject:\"Payment Advice\"", "dorabjee"),
}
