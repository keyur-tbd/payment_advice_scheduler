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
    # (first payment date ISO, code): the ERP moved this payer to a new customer card; an advice paid on or
    # after the date carries that code (the latest one that applies), earlier advices codes[0]
    code_from: Tuple[Tuple[str, str], ...] = ()

    def code_on(self, paid) -> str:
        code = self.codes[0]
        for since, c in sorted(self.code_from):
            if paid is not None and str(paid) >= since:
                code = c
        return code


CUSTOMERS: Dict[str, Customer] = {
    "amazon":         Customer("pa_amazon",         ("C00002",), "Amazon Retail India"),
    "bigbasket":      Customer("pa_bigbasket",      ("C00005",), "Innovative Retail Concepts (BigBasket)"),
    "reliance":       Customer("pa_reliance",       ("C00035",), "Reliance Retail"),
    "rbml":           Customer("pa_rbml",           ("C00402",), "RBML Solutions (Reliance BP Mobility)"),
    # No longer a customer (owner, 2026-10-23); advices stop 27 Feb 2026, table kept for history.
    "dmart":          Customer("pa_dmart",          ("C00415",), "Avenue Supermarts (DMart)"),
    # ERP card C00547 (party 'DMart Ready') from 20 Aug 2026; C00123 (party 'DMart') before. Tagged by payment date
    # so the Birbal refresh links each advice to the receipt on the right card.
    "dmart_ecom":     Customer("pa_dmart_ecom",     ("C00123", "C00547"), "Avenue E-commerce (DMart Ready)",
                               code_from=(("2026-08-20", "C00547"),)),
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
    "instamart":      Customer("pa_instamart",      ("C00152", "C00153", "C00154", "C00156", "C00157", "C00196", "C00183"),
                               "Swiggy Instamart payers (Cloudstore, Cloudkart, Jupiter Kart, PJTJ, Moksh, Aariga, Setron)"),
    # added 2026-11-16 after an inbox sweep found advice senders no feed read
    "flipkart":       Customer("pa_flipkart",       ("C00048",), "Flipkart India (HSBC advices)"),
    "shri_balaji":    Customer("pa_shri_balaji",    ("C00509",), "Shri Balaji Fruits (Zoho Inventory)"),
    "benzer":         Customer("pa_benzer",         ("C00113",), "Benzer Department Store (Tally voucher)"),
    "compass":        Customer("pa_compass",        ("C00459",), "Compass India Food Services (J.P. Morgan advices)"),
    "bottle_lab":     Customer("pa_bottle_lab",     ("C00503",), "Bottle Lab Technologies (J.P. Morgan advices)"),
    # added 2026-11-19: advices that never reach accounts@ (user: Blinkit -> kolwarehouse@, More Retail -> payments@)
    "blinkit":        Customer("pa_blinkit",        ("C00524",), "Blink Commerce (Blinkit; sap@zomato.com to kolwarehouse@)"),
    "more_retail":    Customer("pa_more_retail",    ("C00049",), "More Retail (Kotak CMS, then HSBC from docs@more.in; both to payments@)"),
}


@dataclass(frozen=True)
class Feed:
    query: str                      # Gmail query; the runner adds the date window
    parser: str                     # module in parsers/ exposing parse(mail) -> list[Advice]
    priority: int = 10              # higher wins when two feeds carry the same advice_ref
    mailbox: str = "accounts"       # which @thebakersdozen.in inbox (see pa_common.gmail_service)
    merge_repeats: bool = False     # same advice_ref again from this feed: add the lines earlier mails lacked
                                    # (statements that repeat a transfer as it fills in; see db.load_advice)
    mail: bool = True               # False = loaded from files by its own script, never by run.py
    oldest_first: bool = False      # read a run's mails oldest first: a later mail can void what an earlier one
                                    # loaded (Vijetha ledgers: a bounced cheque shows only in later ledgers)

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
        # Since Mar 2026 DMart's advices come from noreply@axis.bank.in to another mailbox (not accounts@); accounts@ only gets
        # the ones the team forwards ('Fwd: Payment Advice::AVENUE SUPERMARTS ...'), so the subject is matched too.
        "(from:(Straight2axis@axisbank.com OR straight2axis@axis.bank.in OR noreply@axis.bank.in) OR "
        "subject:\"Payment Advice::AVENUE SUPERMARTS\") has:attachment", "axis"),
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
        # subject 'Payment Advice' to Sep 2026, then 'Mimansa Industries Pvt Ltd (Ledger Account)' (Tally ledger)
        "from:payments@vijetha.in has:attachment", "vijetha", oldest_first=True),
    "ratnadeep": Feed(
        "from:automail@ratnadeepretail.com has:attachment", "ratnadeep"),
    "dorabjee": Feed(   # body-only bank advice, no attachment
        "from:(MahaPayments@mahabank.co.in OR mahapayments@mahabank.bank.in) subject:\"Payment Advice\"", "dorabjee"),
    "instamart": Feed(   # Zoho Books payment vouchers, mailed to instamart@ (not accounts@)
        "from:message-service@sender.zoho-books.in subject:\"payment success\"", "instamart", mailbox="instamart"),
    "instamart_vcredits": Feed(   # Instamart entities' 'Payment Receipts VCredits.xlsx' (2024 on); below the
        "filename:VCredits", "instamart_vcredits",   # Zoho Books advices, which carry a printed total
        priority=5, mailbox="instamart", merge_repeats=True),
    "instamart_portal": Feed(   # NOT mail: the Instamart vendor portal's CSV exports, loaded by load_portal.py.
        "", "instamart_portal",  # Every payment with its printed total, all our vendor codes; above VCredits,
        priority=7, mailbox="instamart", mail=False),   # below the Zoho Books advices (they carry TDS / gross)
    "hsbc": Feed(        # Flipkart India; Zepto / Kiranakart copies skipped (pa_zepto has them)
        "from:advising.service@advising.hsbc.com subject:\"Payment Advice\" has:attachment", "hsbc"),
    "jpmorgan": Feed(    # Compass India, Bottle Lab; advice in the body
        "from:statements_and_alerts@jpmchase.com subject:\"Remittance Advice\"", "jpmorgan"),
    "benzer": Feed(
        "from:account@benzerworld.com has:attachment", "benzer"),
    "shri_balaji": Feed(  # Zoho Inventory (the Instamart payers use Zoho Books, to instamart@)
        "from:message-service@sender.zoho-inventory.in subject:\"payment has been made\"", "shri_balaji"),
    "blinkit": Feed(     # Blink Commerce, mailed to kolwarehouse@ only
        "from:sap@zomato.com subject:\"Payment Advice\" has:attachment", "blinkit", mailbox="kolwarehouse"),
    "zepto_portal": Feed(   # NOT mail: Zepto brand-portal advice PDFs (same layout as the mails), loaded by
        "", "zepto", priority=5, mail=False),   # load_zepto_portal.py; below the mailed copy
    "kotak_more": Feed(  # More Retail via Kotak CMS, mailed to payments@ only; none reach it after 25 Aug 2026
        "from:(CMSAlerts@kotak.bank.in OR cmsalerts@kotak.com) subject:\"Credit Intimation\" has:attachment",
        "kotak_more", mailbox="payments"),
    "more_hsbc": Feed(   # More Retail via HSBC since 29 Jun 2026 (Kotak stopped 25 Aug): docs@more.in to payments@,
        "from:docs@more.in subject:\"Payment Advice\" has:attachment",   # PDF + invoice / debit-note breakup xlsx
        "more_hsbc", mailbox="payments"),
}

MAIL_FEEDS = [k for k, f in FEEDS.items() if f.mail]
