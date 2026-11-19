#!/usr/bin/env python3
"""
One-time Gmail sign-in for a payment advice mailbox.

Opens a browser; sign in as <mailbox>@thebakersdozen.in and allow read-only Gmail access.
accounts@ (the default) writes token.json; any other mailbox writes token_<mailbox>.json, which
run.py reads via PA_GMAIL_TOKEN_FILE_<MAILBOX> locally and PA_GMAIL_TOKEN_JSON_<MAILBOX> in Actions.
Read-only: the pipeline never sends, labels or deletes mail.

    python payment_advice_pipeline/gmail_auth.py               # accounts@
    python payment_advice_pipeline/gmail_auth.py kolwarehouse  # kolwarehouse@
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(HERE, "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> int:
    box = (sys.argv[1] if len(sys.argv) > 1 else "accounts").split("@")[0].lower()
    EXPECTED = f"{box}@thebakersdozen.in"
    TOKEN_FILE = os.path.join(HERE, "token.json" if box == "accounts" else f"token_{box}.json")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
    creds: Credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        login_hint=EXPECTED,
        authorization_prompt_message=f"Sign in as {EXPECTED} in the browser window that just opened...",
        success_message="Done - you can close this tab and go back to the terminal.",
    )
    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    if email.lower() != EXPECTED:
        print(f"Signed in as {email}, not {EXPECTED}. Token NOT saved - run again and pick {EXPECTED}.")
        return 1
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"OK: token saved to {os.path.basename(TOKEN_FILE)} for {email} ({profile.get('messagesTotal')} messages in mailbox).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
