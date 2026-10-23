#!/usr/bin/env python3
"""
One-time Gmail sign-in for the payment advice mailbox (accounts@thebakersdozen.in).

Opens a browser; sign in as accounts@ and allow read-only Gmail access.
Writes token.json next to this file. Read-only: the pipeline never sends,
labels or deletes mail.

    python payment_advice_pipeline/gmail_auth.py
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(HERE, "credentials.json")
TOKEN_FILE = os.path.join(HERE, "token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
EXPECTED = "accounts@thebakersdozen.in"


def main() -> int:
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
    creds: Credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        login_hint=EXPECTED,
        authorization_prompt_message="Sign in as accounts@ in the browser window that just opened...",
        success_message="Done - you can close this tab and go back to the terminal.",
    )
    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    if email.lower() != EXPECTED:
        print(f"Signed in as {email}, not {EXPECTED}. Token NOT saved - run again and pick accounts@.")
        return 1
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"OK: token saved for {email} ({profile.get('messagesTotal')} messages in mailbox).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
