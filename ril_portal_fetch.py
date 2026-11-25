#!/usr/bin/env python3
"""
Reliance SRM portal: the Statement of Accounts and its payment advice PDFs, over plain HTTPS (no browser).

    python ril_portal_fetch.py --out advices/ --env-file D:/.../.env            # last 45 days, every login
    python ril_portal_fetch.py --out advices/ --since 2026-04-01 --list-only    # statement rows only

Sign-in is the SAP form on retsrm.ril.com (user id + password, no OTP). The dashboard's 'Statement of Accounts'
link posts the event SoaOpenSrcClick, which redirects to srm-rrscm.ril.com/login?sap-params=<one-time text>; the
report app trades that text for a bearer token (/srm/uaa/api/v1/authenticate) and reads everything from JSON APIs:

    statement-off-acc/fetch              vendor line items of a company code between two dates (SAP field names)
    statement-off-acc/download/document  the 'Payment Advice' PDF of a payment document (rows with dt = 'PA')

The PDF is the one RR.Paymentadvice@ril.com mails (parsers/ril_rr.py reads it), so the portal fills in the advices
that never reach a mailbox. We hold several login ids (each sees its own vendor codes): RELIANCE_SRM_USER_1 /
RELIANCE_SRM_PASSWORD_1, RELIANCE_SRM_USER_2 / ..._2, and so on.

A refused password is never tried again in the same run: SAP locks a user after a few wrong attempts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import sys
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SAP = "https://retsrm.ril.com"
SRM = "https://srm-rrscm.ril.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
COMPANIES = ("9008",)        # Reliance Retail Limited: the only company code with postings on our vendor codes
WINDOW_DAYS = 60             # the report screen's own range; longer spans are read in windows of this size


class PortalError(RuntimeError):
    pass


def logins(env_file: str = "") -> List[Tuple[str, str]]:
    """[(user id, password)] from RELIANCE_SRM_USER_<n> / RELIANCE_SRM_PASSWORD_<n>, n = 1, 2, ..."""
    import db
    db._load_env_file(env_file)
    out, n = [], 1
    while os.environ.get(f"RELIANCE_SRM_USER_{n}", "").strip():
        user, password = os.environ[f"RELIANCE_SRM_USER_{n}"].strip(), os.environ.get(f"RELIANCE_SRM_PASSWORD_{n}", "")
        if not password:
            raise PortalError(f"RELIANCE_SRM_PASSWORD_{n} is empty")
        out.append((user, password))
        n += 1
    if not out:
        raise PortalError("set RELIANCE_SRM_USER_1 / RELIANCE_SRM_PASSWORD_1 (env or --env-file)")
    return out


def _hidden(form: str) -> Dict[str, str]:
    out = {}
    for tag in re.findall(r"<input[^>]*>", form, re.I):
        if re.search(r'type\s*=\s*"hidden"', tag, re.I):
            name = re.search(r'name\s*=\s*"([^"]+)"', tag, re.I)
            value = re.search(r'value\s*=\s*"([^"]*)"', tag, re.I)
            if name:
                out[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return out


class Portal:
    """One signed-in login id."""

    def __init__(self, user: str, password: str, timeout: int = 300):
        import requests
        self.user, self.timeout = user, timeout
        self.http = requests.Session()
        self.http.headers["User-Agent"] = UA
        self.vendor_name = ""
        self._sign_in(password)

    def _sign_in(self, password: str) -> None:
        r = self.http.get(SAP, timeout=90)
        form = re.search(r'<form name="loginForm"[^>]*action="([^"]+)"(.*?)</form>', r.text, re.S | re.I)
        if not form:
            raise PortalError("the SAP sign-in form is not where it was")
        data = _hidden(form.group(2))
        data.update({"sap-user": self.user, "sap-password": password, "sap-system-login": "onLogin"})
        r = self.http.post(urljoin(r.url, html.unescape(form.group(1))), data=data, timeout=90)
        if 'name="sap-password"' in r.text or "SoaOpenSrcClick" not in r.text:
            if re.search(r"captcha|one.?time|otp", r.text, re.I) and 'name="sap-password"' not in r.text:
                raise PortalError(f"{self.user}: the portal asked for more than the password; sign in by hand once")
            raise PortalError(f"{self.user}: sign-in refused (wrong or expired password?); not retried")
        # 'Statement of Accounts' on the dashboard: myform with the event, answered by a redirect to the report app
        r = self.http.post(r.url, data={"OnInputProcessing": "SoaOpenSrcClick", "parameter": "", "pagelink": "",
                                        "AbacFlag": "X", "AcceptAbacFlag": "", "ActionFlag": "", "FromNavBar": "",
                                        "Action": ""}, timeout=90, allow_redirects=False)
        auth_text = parse_qs(urlparse(r.headers.get("Location", "")).query).get("sap-params", [""])[0]
        if not auth_text:
            raise PortalError(f"{self.user}: the dashboard did not hand over to the report app (HTTP {r.status_code})")
        r = self.http.post(SRM + "/srm/uaa/api/v1/authenticate", json={"authText": unquote(auth_text)}, timeout=90)
        j = r.json() if r.status_code == 200 else {}
        if not j.get("token"):
            raise PortalError(f"{self.user}: the report app gave no token (HTTP {r.status_code}, {j.get('message')})")
        self.vendor_name = j.get("vendorName") or ""
        self.http.headers.update({"Authorization": f"Bearer {j['token']}", "Content-Type": "application/json"})

    def statement(self, company: str, start: dt.date, end: dt.date) -> List[dict]:
        """Statement of Accounts line items with a document date in [start, end], read in WINDOW_DAYS windows."""
        rows, lo = [], start
        while lo <= end:
            hi = min(lo + dt.timedelta(days=WINDOW_DAYS - 1), end)
            r = self.http.post(SRM + "/srm/rdl/api/v1/report/statement-off-acc/fetch", timeout=self.timeout,
                               json={"companyCode": company, "fromDate": f"{lo:%Y%m%d}", "toDate": f"{hi:%Y%m%d}"})
            if r.status_code != 200:
                raise PortalError(f"{self.user}: statement {lo}..{hi} HTTP {r.status_code}: {r.text[:200]}")
            param = (r.json() or {}).get("param") or {}
            errors = [m.get("MESSAGE", "") for m in param.get("T_RETURN") or [] if m.get("TYPE") == "E"]
            errors = [m for m in errors if m.strip() != "No data found for selection criteria"]
            if errors:
                raise PortalError(f"{self.user}: statement {lo}..{hi}: {'; '.join(sorted(set(errors)))[:300]}")
            rows += param.get("T_ITEMS") or []
            lo = hi + dt.timedelta(days=1)
        return rows

    def advice_pdf(self, company: str, fiscal_year: str, doc_no: str) -> Optional[bytes]:
        """The payment advice of one payment document; None when the portal has no PDF for it."""
        r = self.http.post(SRM + "/srm/rdl/api/v1/report/statement-off-acc/download/document", timeout=self.timeout,
                           headers={"Accept": "application/pdf,application/json"},
                           json={"companyCode": company, "year": fiscal_year, "statementOfAccountDocNo": doc_no,
                                 "documentType": "PA"})
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            raise PortalError(f"{self.user}: advice {doc_no}/{fiscal_year} HTTP {r.status_code}: {r.text[:200]}")
        return r.content


def payments(rows: List[dict]) -> List[dict]:
    """One entry per payment document (dt = 'PA') of a statement: doc no, fiscal year, date, vendor, net paid."""
    out: Dict[tuple, dict] = {}
    for r in rows:
        if r.get("dt") != "PA":
            continue
        key = (r.get("COMP_CODE"), r.get("FISC_YEAR"), r.get("DOC_NO"))
        p = out.setdefault(key, {"company": key[0], "fiscal_year": key[1], "doc_no": key[2], "vendor": r.get("VENDOR"),
                                 "date": dt.datetime.strptime(r["DOC_DATE"], "%d.%m.%Y").date(),
                                 "request_id": r.get("documentRequestId"), "amount": 0.0})
        p["amount"] = round(p["amount"] + float(r.get("amount") or 0), 2)
    return sorted(out.values(), key=lambda p: (p["date"], p["doc_no"]))


def file_name(p: dict) -> str:
    # the mailed advices are named the same way: company + document + fiscal year
    return f"{p['request_id'] or p['company'] + p['doc_no'] + p['fiscal_year']}.PDF"


def fetch(creds: List[Tuple[str, str]], since: dt.date, until: Optional[dt.date] = None,
          skip=lambda name: False) -> Iterator[Tuple[str, dict, Optional[bytes]]]:
    """Yield (login id, payment, PDF bytes) for every payment document dated since..until under every login.
    `skip(file name)` says the advice is already held: it is yielded with bytes None and not downloaded.
    A login that fails raises after the others have been read."""
    until = until or dt.date.today()
    failed, seen = [], set()
    for user, password in creds:
        try:
            portal = Portal(user, password)
            for company in COMPANIES:
                found = payments(portal.statement(company, since, until))
                print(f"{user} ({portal.vendor_name}) company {company}: {len(found)} payment document(s) "
                      f"{since}..{until}")
                for p in found:
                    name = file_name(p)
                    if name in seen:            # two logins can see one vendor code
                        continue
                    seen.add(name)
                    yield user, p, (None if skip(name) else portal.advice_pdf(company, p["fiscal_year"], p["doc_no"]))
        except PortalError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            failed.append(user)
        except Exception as exc:   # noqa: BLE001 - network trouble on one login must not lose the other
            print(f"ERROR {user}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(user)
    if failed:
        raise PortalError(f"login(s) not read: {', '.join(failed)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="ril_portal_advices")
    ap.add_argument("--since", type=dt.date.fromisoformat, default=dt.date.today() - dt.timedelta(days=45))
    ap.add_argument("--until", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--list-only", action="store_true", help="print the payment documents, download nothing")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    have = lambda name: args.list_only or os.path.exists(os.path.join(args.out, name))
    n = 0
    for user, p, pdf in fetch(logins(args.env_file), args.since, args.until, skip=have):
        name = file_name(p)
        if args.list_only:
            print(f"  {p['date']} {p['doc_no']}/{p['fiscal_year']} vendor {p['vendor']} {p['amount']:>14,.2f}")
        elif pdf:
            with open(os.path.join(args.out, name), "wb") as fh:
                fh.write(pdf)
            n += 1
    print(f"{n} advice PDF(s) saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
