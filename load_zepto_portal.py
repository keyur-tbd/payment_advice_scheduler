#!/usr/bin/env python3
"""
Load Zepto payment advice PDFs downloaded from the Zepto brand portal into pa_zepto (feed 'zepto_portal').

Zepto mails only some of its advices to accounts@ (about 1 in 3 of its transfers in Oct-Nov 2026); the portal
(brands.zepto.co.in > Payments > Payment Advice, OTP login) lists every one, and each row's download button gives
the SAME 'Payment Advice' PDF the mails carry, so parsers/zepto.py reads it unchanged. The accounts team saves the
day's PDFs (file names like 7b0c16d301706a3b_2000090977.pdf) into a Drive folder; this script loads them.

    python load_zepto_portal.py --dir D:/some/folder --dry-run       # local PDFs, parse + check only
    python load_zepto_portal.py --dir D:/some/folder --env-file D:/Python/Birbal/Uniware/.env
    python load_zepto_portal.py --drive-folder <folder id>           # the shared Drive folder (birbal@ token)

Each PDF is one pseudo-mail 'zportal-<file name>' (Drive: 'zportal-<file id>'), so a file is read once and a
re-run skips it. Priority 5 < the mailed 'zepto' feed (10): an advice that also came by mail logs 'duplicate'
here, and a later mail of an advice first loaded from the portal replaces the portal copy.
Drive: env ZEPTO_PORTAL_DRIVE_TOKEN_JSON (token JSON) or ZEPTO_PORTAL_DRIVE_TOKEN_FILE (path), drive.readonly;
the folder id from --drive-folder or ZEPTO_PORTAL_DRIVE_FOLDER. Read-only: files are never moved or deleted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pa_common import Attachment, Mail, Skip  # noqa: E402
from parsers import zepto  # noqa: E402

FEED = "zepto_portal"


def local_files(folder: str):
    paths = {os.path.normcase(p): p for p in glob.glob(os.path.join(folder, "*.pdf")) + glob.glob(os.path.join(folder, "*.PDF"))}
    for path in sorted(paths.values()):          # Windows globs are case-blind: both patterns match a file
        name = os.path.basename(path)
        stamp = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
        yield f"zportal-{name}", name, stamp, lambda p=path: open(p, "rb").read()


def drive_files(folder_id: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    token = os.environ.get("ZEPTO_PORTAL_DRIVE_TOKEN_JSON")
    path = os.environ.get("ZEPTO_PORTAL_DRIVE_TOKEN_FILE")
    if not token and not path:
        sys.exit("set ZEPTO_PORTAL_DRIVE_TOKEN_JSON or ZEPTO_PORTAL_DRIVE_TOKEN_FILE (birbal@ drive.readonly token)")
    creds = Credentials.from_authorized_user_info(json.loads(token)) if token else Credentials.from_authorized_user_file(path)
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    page = None
    while True:   # PDFs anywhere under the folder (one level of sub-folders, e.g. one per month)
        q = f"'{folder_id}' in parents and trashed = false"
        resp = svc.files().list(q=q, fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                                pageSize=1000, pageToken=page, supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                yield from drive_files_in(svc, f["id"])
            elif f["name"].lower().endswith(".pdf"):
                yield _drive_item(svc, f)
        page = resp.get("nextPageToken")
        if not page:
            break


def drive_files_in(svc, folder_id: str):
    page = None
    while True:
        resp = svc.files().list(q=f"'{folder_id}' in parents and trashed = false",
                                fields="nextPageToken, files(id, name, mimeType, modifiedTime)", pageSize=1000,
                                pageToken=page, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in resp.get("files", []):
            if f["name"].lower().endswith(".pdf"):
                yield _drive_item(svc, f)
        page = resp.get("nextPageToken")
        if not page:
            break


def _drive_item(svc, f):
    from googleapiclient.http import MediaIoBaseDownload

    def fetch(fid=f["id"]):
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=fid, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()
    stamp = dt.datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
    return f"zportal-{f['id']}", f["name"], stamp, fetch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dir", help="folder of downloaded advice PDFs")
    src.add_argument("--drive-folder", default=os.environ.get("ZEPTO_PORTAL_DRIVE_FOLDER"),
                     help="Drive folder id the accounts team saves the PDFs into")
    ap.add_argument("--dry-run", action="store_true", help="parse + check only, no database")
    ap.add_argument("--reparse", action="store_true", help="read files already loaded again")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    args = ap.parse_args()
    if not args.dir and not args.drive_folder:
        ap.error("give --dir or --drive-folder (or ZEPTO_PORTAL_DRIVE_FOLDER)")

    items = local_files(args.dir) if args.dir else drive_files(args.drive_folder)
    conn = refs = None
    done = set()
    if not args.dry_run:
        import db
        conn = db.connect(args.env_file)
        refs = db.RefIndex(conn)
        if not args.reparse:
            done = db.done_mail_ids(conn, FEED)
    started = dt.datetime.now(dt.timezone.utc)
    counts = Counter()
    for mail_id, name, stamp, fetch in items:
        if mail_id in done:
            counts["already_loaded"] += 1
            continue
        mail = Mail(id=mail_id, thread_id="", date=stamp, sender="brands.zepto.co.in",
                    subject=f"Zepto portal payment advice {name}", attachments=[Attachment(name, fetch())])
        try:
            advices = zepto.parse(mail)
        except Skip as exc:
            counts["skipped"] += 1
            print(f"  skip {name}: {exc}")
            if conn is not None:
                db.log_mail(conn, FEED, mail, "skipped", str(exc), source_file=name)
            continue
        for idx, advice in enumerate(advices):
            if args.dry_run:
                status, diff = advice.check()
                counts[status] += 1
                if status != "ok":
                    print(f"  {name}: {advice.advice_ref} {status} diff={diff}")
            else:
                counts[db.load_advice(conn, FEED, mail, idx, advice, refs)] += 1
        if conn is not None:
            db.mark_mail_done(conn, FEED, mail, len(advices))
    if conn is not None:
        db.log_run(conn, FEED, started, dt.datetime.now(dt.timezone.utc), dict(counts),
                   {"source": args.dir or f"drive:{args.drive_folder}"})
    print("TOTAL " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
