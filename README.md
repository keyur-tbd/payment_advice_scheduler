# Payment advice pipeline

Customer payment advices mailed to **accounts@thebakersdozen.in** -> one Supabase table per
customer (`public.pa_<customer>`), one row per advice line. Parsed locally (pdfplumber /
openpyxl / BeautifulSoup) - **no LlamaIndex, no LLM**. An advice is loaded only when its lines
add up to the amount the payer printed (within Rs 1); anything else stays out and is listed in
`pa_mail_log` with the reason.

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

The same payment can arrive twice (RBML via HDFC + RIL Treasury; Reliance Retail via RR + Axis).
Inside a table the advice is keyed by `advice_ref`; the higher-priority feed's copy wins
(`feeds.py`), the other is logged `duplicate` / `superseded`.

## Columns (every `pa_*` table)
`advice_ref`, `utr`, `payment_date`, `payment_amount` (printed total) repeat on each line;
`invoice_no`, `invoice_date`, `doc_ref` (GRN / PRN / payer doc), `line_type`
(INVOICE / TDS / GDN / PRN / DEDUCTION / GST_HOLD / PAYMENT ...), `gross_amount`, `tds_amount`,
`deduction_amount`, `net_amount` (signed: `sum(net_amount) = payment_amount` per advice),
plus `mail_id`, `mail_date`, `source_file`, `raw_data`.

## Run
```
python run.py --apply-schema                       # create/update tables (idempotent)
python run.py                                      # all feeds, last 60 days
python run.py --feeds axis --since 2026-04-01
python run.py --feeds hdfc_enet --days 365 --dry-run --cache .cache   # test a parser, no DB
python run.py --retry-failed                       # re-read advices that failed their check
```
Mails already settled in `pa_mail_log` are not fetched again. Exit code 1 = some advice failed
its check this run (see `pa_mail_log where status in ('mismatch','no_total','no_lines','error')`).

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
