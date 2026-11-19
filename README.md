# Payment advice pipeline

Customer payment advices mailed to **accounts@thebakersdozen.in** -> one Supabase table per
customer (`public.pa_<customer>`), one row per advice line. Parsed locally (pdfplumber /
openpyxl / BeautifulSoup) - **no LlamaIndex, no LLM**. Every advice with lines is loaded and
`pa_mail_log.status` says how much to trust it: `loaded` = the lines add up to the amount the
payer printed (within Rs 1); `loaded_round_off` = they did not, so a `ROUND_OFF` line was added to
bring them to the printed total (review pending, decision 2026-10-26); `loaded_no_total` = the
payer printed no total, lines loaded unchecked with `payment_amount` null. Only `no_lines` /
`error` stay out.

Customer list and senders come from the *Raw Cust Data* tab of "Customer Receipt Settlement
Data FY26-27 with RTV/DN Tracker".

| Feed (sender format) | Mail from | Customer table(s) |
|---|---|---|
| `amazon` | noreply@ / no-reply@amazon.com, mail body | `pa_amazon` |
| `bigbasket` | t1-t4payments@bigbasket.com, .xlsx | `pa_bigbasket` |
| `ril_rr` | RR.Paymentadvice@ril.com, SAP PDF | `pa_reliance` |
| `ril_treasury` | treasury.paymentadvice@zmail.ril.co, PDF | `pa_rbml` |
| `axis` | Straight2axis@ (axis.bank.in), zipped PDF | `pa_dmart`, `pa_reliance` |
| `hdfc_enet` | Enetadvicemailing@ (hdfcbank.bank.in), locked PDF | `pa_magson`, `pa_rbml`, `pa_dmart_ecom`, `pa_star_bazaar` |
| `hdfc_corp` | corporatenetbanking.automailer@hdfcbank.bank.in, locked PDF | `pa_magson` (Udaipur, since Aug 2026), `pa_dmart_ecom` |
| `icici` | ipaycheck@ (icici.bank.in), PDF | `pa_natures_basket`, `pa_spencers` |
| `hyperpure` | hpvendorsupport@zomato.com replies to our requests | `pa_hyperpure` |
| `zepto` | payments@zeptonow.com, SAP PDF (2023: pivot PDF) | `pa_zepto` (Zepto Ltd C00043, Kiranakart Wholesale C00155) |
| `ninjacart` | ninjaping_alerts@ninjacart.com, rolling 2-month GRN snapshot .xlsx | `pa_ninjacart` (63Ideas / Flipkart Quick) |
| `prr_fresh` | prrfreshproducepvtltd@gmail.com, Tally payment voucher PDF | `pa_prr_fresh` |
| `namdhari` | erp@namdharis.in, SAP PDF | `pa_namdhari` |
| `vijetha` | payments@vijetha.in, Tally PDF | `pa_vijetha` |
| `ratnadeep` | automail@ratnadeepretail.com, voucher PDF | `pa_ratnadeep` |
| `dorabjee` | MahaPayments@mahabank.co.in, mail body only | `pa_dorabjee` |
| `instamart` | **instamart@** inbox: Zoho Books 'payment success' from message-service@sender.zoho-books.in, PDF | `pa_instamart` (Moksh C00157, Jupiter Kart C00154, Aariga C00196, Setron C00183; Cloudstore/Cloudkart/PJTJ mapped, none mailed yet) |
| `instamart_vcredits` | **instamart@** inbox: 'Payment Receipts VCredits.xlsx' (query `filename:VCredits`), one mail per Instamart entity x our vendor code | `pa_instamart`, one advice per UTR (blank UTR: `<ENTITY>-<vendor code>-<yyyymmdd>`); payment_amount = sum of the rows we hold (no printed total); priority below `instamart` |
| `instamart_portal` | **not mail**: the Instamart vendor portal's CSV exports (Payment Report + Payment adjustment at Invoice level), fetched daily by `instamart_portal.yml` (`load_portal.py --fetch`) | `pa_instamart`, one advice per UTR with the portal's printed total, all our vendor codes; priority 7, between `instamart` (10) and `instamart_vcredits` (5) |
| `hsbc` | advising.service@advising.hsbc.com, Payment_Advice.pdf (multi-page) | `pa_flipkart` (Flipkart India C00048, since Sep 2023); Zepto / Kiranakart copies skipped - `pa_zepto` has them |
| `jpmorgan` | statements_and_alerts@jpmchase.com, mail body (zip repeats it) | `pa_compass` (C00459), `pa_bottle_lab` (C00503) |
| `benzer` | account@benzerworld.com, Tally payment voucher PDF (PRR Fresh parser) | `pa_benzer` (C00113) |
| `shri_balaji` | message-service@sender.zoho-inventory.in, Zoho Inventory PDF | `pa_shri_balaji` (C00509, since Feb 2026) |
| `blinkit` | **kolwarehouse@** inbox: sap@zomato.com 'Payment Advice - YYYYMMDD', one PDF per UTR (invoices with TDS, CN/DN, variance, brand fund) | `pa_blinkit` (Blink Commerce C00524; since Apr 2026) |
| `zepto_portal` | **not mail**: Payment Advice PDFs from the Zepto brand portal (brands.zepto.co.in, OTP login; same PDF as the mails), saved daily by the accounts team to Drive "Zepto Payment Advices (portal PDFs)"; `load_zepto_portal.py` in `payment_advice.yml` | `pa_zepto`; priority 5, below the mailed `zepto` (10) |
| `kotak_more` | **payments@** inbox: CMSAlerts@kotak.bank.in 'Credit Intimation', PAY_PAID_*.pdf | `pa_more_retail` (More Retail C00049; Feb 2025 - 25 Aug 2026, then HSBC: `more_hsbc`) |
| `more_hsbc` | **payments@** inbox: docs@more.in 'Payment Advice - HSBCN... - MIMANSA INDUSTRIES', payment_advice_MORE*.pdf (Jun 2026: an extension-less .xlsx) + invoice_debitnote_breakup_MORE*.xlsx (Sep 2026 on) | `pa_more_retail` (C00049; since 29 Jun 2026); invoices net of TDS, DMQ shortage notes as GDN lines. docs@more.in's 'Standard Invoice Settlement' workbooks (adjustments only, no cash) are not read |

The same payment can arrive twice (RBML via HDFC + RIL Treasury; Reliance Retail via RR + Axis).
Inside a table the advice is keyed by `advice_ref`; the higher-priority feed's copy wins
(`feeds.py`), the other is logged `duplicate` / `superseded`.

VCredits rows with no UTR can repeat a payment that is also under its UTR (or under another date key).
After every run that reads instamart@, `db.drop_unkeyed_duplicates` deletes such a row when the same
invoice / doc ref and amount is paid within 30 days under another advice_ref (the UTR copy is kept, else
the earliest), restates the advice's total and notes it in `pa_mail_log.detail` (`duplicate` when
nothing is left).

A VCredits mail covers only the vendor codes copied to instamart@; one bank transfer pays all of an
entity's vendor codes, so many VCredits totals are a part of the ERP receipt and do not match it by amount.

## Instamart vendor portal (daily, headless browser)

The VCredits mails cover only the vendor codes copied to instamart@. The entities' vendor portal
(vendors.zohocreatorportal.in, instamart@ login, email + password, no OTP) lists every payment.
`.github/workflows/instamart_portal.yml` runs daily at 11:30 IST (and on dispatch, optional `since`):

    python load_portal.py --fetch --out <dir>        # portal_fetch.py signs in, exports both CSVs, then loads

`portal_fetch.py` drives Playwright Chromium: sign in, open *Payment Report* and *Payment Adjustment Invoice
Level Report* from the menu, and in each embedded Zoho Analytics view click the table's Export -> Export as CSV
-> Export (default 'Current View (Data)' = the whole table). The Payment Report page embeds two views (a total
tile with its own Export, then the table), so the script uses the Export in the toolbar with the visible Sort.
Credentials: secrets / env `INSTAMART_PORTAL_EMAIL`, `INSTAMART_PORTAL_PASSWORD`. If Zoho asks for a captcha
or code the run fails (screenshot `portal_fetch_failure.png` in `--out`, not uploaded: the repo is public);
sign in by hand once and re-run. With `--fetch` the default `--since` is the 1st of the month three months back.

By hand, still works: export both reports (export icon -> **Export as CSV**, filters 'All'), then

    python load_portal.py Payment_Report.csv Payment_adjustment_at_Invoice_level.csv --dry-run
    python load_portal.py Payment_Report.csv Payment_adjustment_at_Invoice_level.csv --env-file <.env>

Payments from 2024-04-01 on (`--since`), bank payments only (debit adjustments are not cash). Each month is
one pseudo-mail `portal-YYYY-MM` and each advice is rewritten in place, so re-loads are safe. `run.py` never
runs this feed (`Feed.mail=False`).

## Zepto brand portal (daily, by hand + Drive)
Zepto mails only some advices; the portal lists all of them, but its login needs an OTP. Each day the accounts team
opens https://brands.zepto.co.in/vendor/payments/advice, downloads each new row's PDF (the per-row download button,
not the page's top "Download", which is a summary CSV without invoice lines) and saves the PDFs into Drive folder
"Zepto Payment Advices (portal PDFs)" (id `11Ya59bJS0ECTKtU6he0uOpz-75B281Vz`, owner instamart@, accounts@ can edit,
birbal@ reads). Any file name, sub-folders OK; re-saving a file already there does no harm (an advice already
held logs `duplicate`).

    python load_zepto_portal.py --drive-folder 11Ya59bJS0ECTKtU6he0uOpz-75B281Vz   # what the workflow runs
    python load_zepto_portal.py --dir <folder> --dry-run                           # local PDFs, parse only

Each Drive file is one pseudo-mail `zportal-<file id>`, read once. Secret `ZEPTO_PORTAL_DRIVE_TOKEN_JSON` = birbal@
drive.readonly token; the step skips itself while the secret is unset.

## Columns (every `pa_*` table)
`advice_ref`, `utr`, `payment_date`, `payment_amount` (printed total) repeat on each line;
`invoice_no`, `invoice_date`, `doc_ref` (GRN / PRN / payer doc), `line_type`
(INVOICE / TDS / GDN / PRN / DEDUCTION / GST_HOLD / PAYMENT / ROUND_OFF ...), `gross_amount`, `tds_amount`,
`deduction_amount`, `net_amount` (signed: `sum(net_amount) = payment_amount` per advice, ROUND_OFF
included, wherever `payment_amount` is not null),
plus `mail_id`, `mail_date`, `source_file`, `raw_data`.

## Run
```
python run.py --apply-schema                       # create/update tables (idempotent)
python run.py                                      # all feeds, last 60 days
python run.py --feeds axis --since 2026-04-01
python run.py --feeds hdfc_enet --days 365 --dry-run --cache .cache   # test a parser, no DB
python run.py --retry-failed                       # re-read failed / rounded-off / untotalled advices (after a parser fix)
```
Mails already settled in `pa_mail_log` are not fetched again. Exit code 1 = some advice could not
be loaded this run (`pa_mail_log where status in ('no_lines','error')`).

What is loaded but still needs a decision:
```sql
select feed, status, count(*) advices, round(sum(abs(diff))) rupees_rounded_off
from pa_mail_log where status in ('loaded_round_off','loaded_no_total') group by 1,2 order by 1,2;
select * from pa_zepto where line_type = 'ROUND_OFF' order by abs(net_amount) desc;   -- any pa_* table
```

## Secrets / local setup
* `token.json` - Gmail read-only token for accounts@ (`python gmail_auth.py`; signs in once).
  On GitHub: secret `PA_GMAIL_TOKEN_JSON` = contents of token.json.
* `SUPABASE_DB_URL` (+ `SUPABASE_DB_SSLROOTCERT` locally) - via env or `--env-file`.
* `PA_PDF_PASSWORDS` - comma list of passwords for locked bank PDFs (HDFC Enet's rule is the
  last 6 digits of our receiving account; the subject usually carries it, this is the fallback).

## Adding a customer
1. Sample mails: `python sample_mail.py OUT --only 'name=from:x@y.com'`.
2. Add a `Customer` (table) and a `Feed` (query + parser) in `feeds.py`.
3. Write `parsers/<feed>.py` (`parse(mail) -> list[Advice]`; `raise Skip` for non-advices).
4. `--dry-run --days 365` until every advice is `loaded`, then `--apply-schema` and run.

The `instamart` feed reads a second mailbox: secret `PA_GMAIL_TOKEN_JSON_INSTAMART` (locally `PA_GMAIL_TOKEN_FILE_INSTAMART=<path to token>`). Without it that feed is skipped and the rest run.

Since 2026-11-19 two more mailboxes: `blinkit` reads kolwarehouse@ (secret `PA_GMAIL_TOKEN_JSON_KOLWAREHOUSE`), `kotak_more` reads payments@ (secret `PA_GMAIL_TOKEN_JSON_PAYMENTS`). Sign in once with `python gmail_auth.py kolwarehouse` / `python gmail_auth.py payments` (writes token_<mailbox>.json, git-ignored); locally set `PA_GMAIL_TOKEN_FILE_<MAILBOX>` to that file.
More Retail's own 'Standard Invoice Settlement' workbooks (docs@more.in to payments@) are NOT payments: they list the GDN / TDS More books against each invoice when it books it, weeks before paying; none equals a receipt (checked 2026-11-19).
