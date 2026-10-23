#!/usr/bin/env python3
"""
Survey the accounts@ mailbox for payment advice mails, per source, and save a
few samples (attachments + mail body) so parsers can be written against real files.

    python sample_mail.py OUT_DIR [--days 120] [--per-source 6]

Read-only. OUT_DIR gets one sub-folder per source plus survey.json.
"""

import argparse
import base64
import json
import os
import re
from collections import Counter

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.json")

# Indian banks moved mail to *.bank.in during 2026; keep both domains.
SOURCES = {
    "amazon": "from:(noreply@amazon.com OR no-reply@amazon.com) subject:remittance",
    "bigbasket": "from:(t1payments@bigbasket.com OR t2payments@bigbasket.com OR t3payments@bigbasket.com OR t4payments@bigbasket.com)",
    "reliance": "from:(RR.Paymentadvice@ril.com OR treasury.paymentadvice@zmail.ril.co OR TREASURY.PAYMENTADVICE@ril.com)",
    "more_retail": "(from:(Commercial.help@ind.barclays.com OR docs@more.in OR more.in) OR \"more retail\") (payment OR remittance)",
    "hyperpure": "from:hpvendorsupport@zomato.com subject:\"Required Payment Advice\" -subject:ticket",
    "hdfc_enet": "from:(Enetadvicemailing@hdfcbank.net OR enetadvicemailing@hdfcbank.bank.in)",
    "icici_ipaycheck": "from:(ipaycheck@icicibank.com OR ipaycheck@icici.bank.in)",
    "axis_straight2axis": "from:(Straight2axis@axisbank.com OR straight2axis@axis.bank.in)",
}


def gmail():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_ids(svc, query, cap=2000):
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, pageToken=token, maxResults=500).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token or len(ids) >= cap:
            return ids


def walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from walk(p)


def body_text(payload):
    for want in ("text/plain", "text/html"):
        for p in walk(payload):
            if p.get("mimeType") == want and p.get("body", {}).get("data"):
                raw = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
                return raw if want == "text/plain" else re.sub(r"<[^>]+>", " ", raw)
    return ""


def safe(name):
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--per-source", type=int, default=6)
    ap.add_argument("--only", action="append", default=[], metavar="NAME=QUERY",
                    help="sample just this ad-hoc query instead of SOURCES (repeatable)")
    args = ap.parse_args()

    svc = gmail()
    survey = {}
    sources = dict(o.split("=", 1) for o in args.only) if args.only else SOURCES
    for name, query in sources.items():
        q = f"{query} newer_than:{args.days}d"
        ids = list_ids(svc, q)
        subjects, exts = Counter(), Counter()
        folder = os.path.join(args.out_dir, name)
        os.makedirs(folder, exist_ok=True)
        for i, mid in enumerate(ids[:args.per_source]):
            full = True
            msg = svc.users().messages().get(userId="me", id=mid, format="full" if full else "metadata",
                                             metadataHeaders=["Subject", "From", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            subjects[re.sub(r"\d+", "#", headers.get("Subject", ""))[:80]] += 1
            for p in walk(msg["payload"]):
                if p.get("filename"):
                    exts[os.path.splitext(p["filename"])[1].lower()] += 1
            if not full:
                continue
            with open(os.path.join(folder, f"{mid}_mail.txt"), "w", encoding="utf-8") as fh:
                keep = {k: headers.get(k) for k in ("Subject", "From", "Date")}
                fh.write(json.dumps(keep, indent=1) + "\n\n" + body_text(msg["payload"]))
            for p in walk(msg["payload"]):
                att = p.get("body", {}).get("attachmentId")
                if p.get("filename") and att:
                    data = svc.users().messages().attachments().get(userId="me", messageId=mid, id=att).execute()
                    with open(os.path.join(folder, f"{mid}_{safe(p['filename'])}"), "wb") as fh:
                        fh.write(base64.urlsafe_b64decode(data["data"]))
        survey[name] = {"query": q, "mails": len(ids), "subjects": subjects.most_common(8),
                        "attachment_types": dict(exts)}
        print(f"{name:20s} {len(ids):5d} mails  {dict(exts)}")
    with open(os.path.join(args.out_dir, "survey.json"), "w", encoding="utf-8") as fh:
        json.dump(survey, fh, indent=1)


if __name__ == "__main__":
    main()
