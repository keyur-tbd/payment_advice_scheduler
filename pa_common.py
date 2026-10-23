"""
Shared pieces for the payment advice pipeline: Gmail fetch, the Mail / Advice /
Line records every parser works with, number + date helpers, and the totals check.

No LLM anywhere: every parser reads the PDF text layer (pdfplumber), the Excel
cells (openpyxl) or the mail body (BeautifulSoup), and an advice only loads when
its lines add up to the amount the payer printed.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import io
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Iterable, Iterator, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get("PA_GMAIL_TOKEN", os.path.join(HERE, "token.json"))

# Lines must add up to the printed payment amount within this many rupees.
TOLERANCE = 1.0

# Gmail drops long sessions now and then (WinError 10054 mid-backfill); the client retries
# connection resets and 5xx with exponential backoff.
GMAIL_RETRIES = 6


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class Attachment:
    name: str
    data: bytes


@dataclass
class Mail:
    id: str
    thread_id: str
    date: Optional[dt.datetime]
    sender: str
    subject: str
    html: str = ""
    text: str = ""
    attachments: List[Attachment] = field(default_factory=list)

    @property
    def body(self) -> str:
        """Plain text of the body; the HTML part flattened when there is no text part."""
        return self.text or html_to_text(self.html)


@dataclass
class Line:
    invoice_no: Optional[str] = None
    invoice_date: Optional[dt.date] = None
    doc_ref: Optional[str] = None          # payer's own doc no: GRN, PRN, SAP doc, scroll no
    line_type: str = "INVOICE"             # INVOICE / TDS / GDN / PRN / DEBIT_NOTE / CREDIT_NOTE / GST_HOLD / GST_RELEASE / DEDUCTION / OTHER
    description: Optional[str] = None
    gross_amount: Optional[float] = None
    tds_amount: Optional[float] = None
    deduction_amount: Optional[float] = None
    net_amount: Optional[float] = None     # signed amount this line adds to the payment
    extra: dict = field(default_factory=dict)


@dataclass
class Advice:
    customer: str                          # key into feeds.CUSTOMERS
    payer_name: Optional[str]
    advice_ref: Optional[str]              # payer's payment document / payment number
    utr: Optional[str]
    payment_date: Optional[dt.date]
    payment_amount: Optional[float]
    lines: List[Line] = field(default_factory=list)
    source_file: str = "mail-body"
    header_only: bool = False              # the advice itself lists no lines (Magson narration)
    extra: dict = field(default_factory=dict)

    def line_sum(self) -> float:
        return round(sum(l.net_amount or 0.0 for l in self.lines), 2)

    def check(self) -> tuple[str, Optional[float]]:
        """('ok' | 'header_only' | 'mismatch' | 'no_total' | 'no_lines', diff)."""
        if self.payment_amount is None:
            return "no_total", None
        if self.header_only:
            return "header_only", 0.0
        if not self.lines:
            return "no_lines", None
        diff = round(self.line_sum() - self.payment_amount, 2)
        # payers round each line, so a long advice drifts a little; never more than Rs 2
        tol = min(2.0, max(TOLERANCE, 0.1 * len(self.lines)))
        return ("ok" if abs(diff) <= tol else "mismatch"), diff


class Skip(Exception):
    """Raised by a parser for a mail that is not a payment advice (ticket, reminder, reply chatter)."""


def to_jsonable(obj):
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    raise TypeError(type(obj))


def advice_dict(a: Advice) -> dict:
    return asdict(a)


# ---------------------------------------------------------------------------
# Number / date / text helpers
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"^\(?-?[\d,]*\.?\d+\)?-?$")


def num(value) -> Optional[float]:
    """'1,05,914.62' / '7,159.86-' / '(1.62)' / '00.84' / 12.5 -> float; blanks and N.A. -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("₹", "").replace("INR", "").replace("Rs.", "").replace(" ", "")
    s = s.rstrip(".") if s.count(".") > 1 else s          # ICICI prints '8003.21.'
    if not s or not _NUM_RE.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = s.strip("()-").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def is_num(value) -> bool:
    return num(value) is not None


_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y",
                 "%d %b %Y", "%b %d, %Y", "%d%m%Y", "%d.%m.%y", "%d/%m/%y", "%d-%m-%y")


def date(value) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T", s)               # Amazon '2026-10-16T00:00:00-08:00'
    if m:
        s = m.group(1)
    s = s.split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2} \d", s) else s
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s.title() if "%b" in fmt or "%B" in fmt else s, fmt).date()
        except ValueError:
            continue
    return None


def html_to_text(html: str) -> str:
    if not html:
        return ""
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").get_text("\n")


def squash(text: str) -> str:
    """Collapse runs of spaces and blank lines; keep one line per visual line."""
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def pdf_passwords(*hints: str) -> List[Optional[str]]:
    """Passwords to try on a bank PDF: none, then the last 6 digits of any long account
    number in the hints (HDFC Enet's rule; the number is in its subject), then the
    comma-separated PA_PDF_PASSWORDS env var. Nothing is kept in code."""
    out: List[Optional[str]] = [None]
    for h in hints:
        for acct in re.findall(r"\b\d{9,18}\b", h or ""):
            out.append(acct[-6:])
    out += [p.strip() for p in os.environ.get("PA_PDF_PASSWORDS", "").split(",") if p.strip()]
    return list(dict.fromkeys(out))


def open_pdf_text(data: bytes, *hints: str) -> str:
    """pdf_text() trying each candidate password in turn."""
    last = None
    for pw in pdf_passwords(*hints):
        try:
            return pdf_text(data, pw)
        except Exception as exc:                 # pdfminer raises a bare PDFPasswordIncorrect wrapper
            last = exc
    raise ValueError(f"PDF could not be opened with any known password ({type(last).__name__})")


def pdf_text(data: bytes, password: Optional[str] = None) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data), password=password) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def pdf_words(data: bytes, password: Optional[str] = None) -> list:
    """Every word with its page and position, for layouts plain text scrambles."""
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data), password=password) as pdf:
        for i, page in enumerate(pdf.pages):
            for w in page.extract_words(keep_blank_chars=False, use_text_flow=False):
                w["page"] = i
                out.append(w)
    return out


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
def gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = os.environ.get("PA_GMAIL_TOKEN_JSON")
    creds = (Credentials.from_authorized_user_info(json.loads(token)) if token
             else Credentials.from_authorized_user_file(TOKEN_FILE))
    if not creds.valid:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(svc, query: str, cap: int = 20000) -> List[str]:
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, pageToken=token, maxResults=500).execute(num_retries=GMAIL_RETRIES)
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token or len(ids) >= cap:
            return ids


def _walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def _expand(name: str, data: bytes) -> Iterator[Attachment]:
    """Axis mails each PDF inside a zip; unpack so parsers only ever see the document."""
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if not member.endswith("/"):
                    yield Attachment(f"{name}/{member}", zf.read(member))
    else:
        yield Attachment(name, data)


def _cache_path(cache_dir: str, mid: str) -> str:
    return os.path.join(cache_dir, f"{mid}.json")


def fetch_mail(svc, mid: str, cache_dir: Optional[str] = None) -> Mail:
    """Full message with decoded bodies and attachments (zips expanded). Cached on disk when asked."""
    if cache_dir and os.path.exists(_cache_path(cache_dir, mid)):
        with open(_cache_path(cache_dir, mid), encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=GMAIL_RETRIES)
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        html = text = ""
        atts = []
        for p in _walk(msg["payload"]):
            body = p.get("body", {})
            if p.get("filename") and body.get("attachmentId"):
                data = svc.users().messages().attachments().get(
                    userId="me", messageId=mid, id=body["attachmentId"]).execute(num_retries=GMAIL_RETRIES)["data"]
                atts.append({"name": p["filename"], "b64": data})
            elif p.get("filename") and body.get("data"):
                atts.append({"name": p["filename"], "b64": body["data"]})
            elif p.get("mimeType") == "text/plain" and body.get("data") and not text:
                text = base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace")
            elif p.get("mimeType") == "text/html" and body.get("data") and not html:
                html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace")
        raw = {"id": mid, "thread_id": msg.get("threadId", ""), "date": headers.get("date", ""),
               "internal_ms": msg.get("internalDate"), "sender": headers.get("from", ""),
               "subject": headers.get("subject", ""), "html": html, "text": text, "attachments": atts}
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            with open(_cache_path(cache_dir, mid), "w", encoding="utf-8") as fh:
                json.dump(raw, fh)

    when = None
    if raw.get("internal_ms"):
        when = dt.datetime.fromtimestamp(int(raw["internal_ms"]) / 1000, tz=dt.timezone.utc)
    elif raw.get("date"):
        try:
            when = email.utils.parsedate_to_datetime(raw["date"])
        except (TypeError, ValueError):
            when = None
    sender = email.utils.parseaddr(raw["sender"])[1].lower() or raw["sender"]
    atts: List[Attachment] = []
    for a in raw["attachments"]:
        atts.extend(_expand(a["name"], base64.urlsafe_b64decode(a["b64"])))
    return Mail(id=raw["id"], thread_id=raw["thread_id"], date=when, sender=sender,
                subject=raw["subject"], html=raw["html"], text=raw["text"], attachments=atts)


def iter_mails(svc, query: str, cache_dir: Optional[str] = None,
               skip_ids: Iterable[str] = ()) -> Iterator[Mail]:
    skip = set(skip_ids)
    for mid in list_message_ids(svc, query):
        if mid not in skip:
            yield fetch_mail(svc, mid, cache_dir)
