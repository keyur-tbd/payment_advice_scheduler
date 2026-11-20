#!/usr/bin/env python3
"""
Download the Instamart vendor portal's two CSV exports with a headless browser.

    python portal_fetch.py --out exports/            # writes Payment_Report.csv + Payment_adjustment_at_Invoice_level.csv
    python portal_fetch.py --out exports/ --headed   # watch it (debugging)

Credentials come from the environment (or --env-file): INSTAMART_PORTAL_EMAIL, INSTAMART_PORTAL_PASSWORD.
The portal (vendors.zohocreatorportal.in) signs in with email + password only, no OTP. Each report is a Zoho
Analytics view embedded in the Creator page; its toolbar's Export -> Export as CSV -> Export ('Current View
(Data)', the default, = the whole table) is clicked exactly as by hand.

If Zoho asks for anything beyond the password (captcha, OTP, a new-location check) the run stops with an error
and a screenshot next to the exports; it never tries to answer those.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PORTAL = "https://vendors.zohocreatorportal.in/"
# (report hash in the portal, file name the loader expects)
REPORTS = [
    ("Payment_Report1", "Payment_Report.csv"),
    ("Payment_Adjustment_Invoice_Level_Report", "Payment_adjustment_at_Invoice_level.csv"),
]
# the Debit Note Report (capping / offer debit notes and our capping RTV notes; load_debit_notes.py, Birbal 272)
DEBIT_NOTES = [("Debit_Note_Report", "Debit_Note_Report.csv")]


class PortalError(RuntimeError):
    pass


def _visible(frame, selector, last=False):
    els = [el for el in frame.query_selector_all(selector) if el.is_visible()]
    return (els[-1] if last else els[0]) if els else None


def _login(page, email: str, password: str) -> None:
    from playwright.sync_api import Error as PlaywrightError
    page.goto(PORTAL, wait_until="domcontentloaded", timeout=90_000)
    # Signed out, the portal shows Zoho's sign-in form in an iframe (/accounts/p/<id>/signin).
    frame = None
    for _ in range(60):
        frame = next((f for f in page.frames if "/signin" in f.url), None)
        if frame and frame.query_selector("#login_id"):
            break
        page.wait_for_timeout(500)
    else:
        raise PortalError("sign-in form never appeared")
    frame.fill("#login_id", email)
    frame.click("#nextbtn")
    frame.wait_for_selector("#password", state="visible", timeout=30_000)
    frame.fill("#password", password)
    frame.click("#nextbtn")

    # Signed in once the sign-in frame is gone and the portal's navigation is there. Zoho may interpose
    # 'Trust this browser?' (answer Not now) or a 'too many sessions' page (Skip); anything that asks for
    # a code or captcha is a stop.
    for _ in range(120):
        page.wait_for_timeout(500)
        try:
            if page.query_selector('a[href="#Payment_Report1"]'):
                return
            f = next((f for f in page.frames if "/signin" in f.url or "/accounts/" in f.url), None)
            if not f:
                continue
            for sel in ("#captcha", "#verifycaptcha", "#otp", "#mfa_otp", "#mfa_totp", "#emailverify", "#backupcode"):
                if _visible(f, sel):
                    raise PortalError(f"Zoho asked for more than the password ({sel.lstrip('#')}); sign in by hand once")
            err = _visible(f, ".fielderror")
            if err and err.inner_text().strip():
                raise PortalError(f"sign-in refused: {err.inner_text().strip()}")
            for label in ("Not now", "Skip"):
                btn = f.get_by_role("button", name=label)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
        except PlaywrightError:
            continue    # the page or sign-in frame navigated away once the password was accepted; look again
    raise PortalError("signed-in portal never appeared")


# The table's own Export button: the one sharing a toolbar with the table's visible Sort button. A view
# can hold a hidden table toolbar too, and the total tile's Export menu has no plain CSV item.
_TABLE_EXPORT = """() => {
  const vis = e => e && e.offsetParent !== null;
  const sort = [...document.querySelectorAll('button[aria-label="Sort"], button[id^="ZDBSortMenu"]')].find(vis);
  for (let n = sort; n; n = n.parentElement) {
    const b = [...n.querySelectorAll('button[aria-label="Export"]')].find(vis);
    if (b) return b;
  }
  return null;
}"""


def _open_report(page, report: str):
    """Click the report's menu link and wait for its table. Returns (analytics frame, Export button)."""
    stale = set(page.frames)                 # the previous report's views linger a moment after the click
    page.click(f'a[href="#{report}"]')
    for _ in range(240):                     # 2 min: the embedded Analytics view is slow on a cold start
        # The Payment Report page embeds two views (the total tile, then the table): search them all.
        for view in [f for f in page.frames if "/open-view/" in f.url and f not in stale]:
            try:
                btn = view.evaluate_handle(_TABLE_EXPORT).as_element()
            except Exception:                # frame navigated mid-evaluate
                btn = None
            if btn:
                return view, btn
        page.wait_for_timeout(500)
    return None, None


def _export(page, report: str, dest: str) -> int:
    view, btn = _open_report(page, report)
    if not btn:
        print(f"  {report}: table not loaded after 2 min, reloading once", file=sys.stderr)
        page.goto(PORTAL, wait_until="domcontentloaded")
        page.wait_for_selector(f'a[href="#{report}"]', timeout=90_000)
        view, btn = _open_report(page, report)
    if not btn:
        raise PortalError(f"{report}: the report's table never loaded")
    page.wait_for_timeout(3000)      # let the rows finish loading before exporting
    btn.click()
    view.wait_for_selector("#EmbedExportCSVMenuItem", state="visible", timeout=30_000)
    view.click("#EmbedExportCSVMenuItem")
    ok = view.locator("button.za-primary-dlg-btn", has_text="Export")
    ok.first.wait_for(state="visible", timeout=30_000)
    with page.expect_download(timeout=300_000) as dl:
        ok.first.click()
    dl.value.save_as(dest)
    return os.path.getsize(dest)


def fetch(out_dir: str, email: str, password: str, headed: bool = False, reports=None, optional=None) -> list:
    """Sign in, export the reports into out_dir (default: the two payment reports). Returns their paths in order.
    `optional` reports are exported after those in the same sign-in; one that fails is reported and skipped
    (its path is not returned) so the payment load never depends on it."""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1480, "height": 900},
                                  locale="en-IN", timezone_id="Asia/Kolkata")
        page = ctx.new_page()
        try:
            _login(page, email, password)
            print("signed in")
            for report, name in (reports or REPORTS):
                dest = os.path.join(out_dir, name)
                size = _export(page, report, dest)
                print(f"  {name}: {size:,} bytes")
                paths.append(dest)
            for report, name in (optional or []):
                dest = os.path.join(out_dir, name)
                try:
                    size = _export(page, report, dest)
                    print(f"  {name}: {size:,} bytes")
                except Exception as e:      # noqa: BLE001 - an extra report never sinks the payment load
                    print(f"  {name}: FAILED ({e})", file=sys.stderr)
        except Exception:
            shot = os.path.join(out_dir, "portal_fetch_failure.png")
            try:
                page.screenshot(path=shot, full_page=True)
                print(f"screenshot: {shot}", file=sys.stderr)
            except Exception:
                pass
            raise
        finally:
            browser.close()
    return paths


def credentials(env_file: str = "") -> tuple:
    import db
    db._load_env_file(env_file)
    email = os.environ.get("INSTAMART_PORTAL_EMAIL", "").strip()
    password = os.environ.get("INSTAMART_PORTAL_PASSWORD", "")
    if not email or not password:
        raise PortalError("set INSTAMART_PORTAL_EMAIL and INSTAMART_PORTAL_PASSWORD (env or --env-file)")
    return email, password


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="portal_exports")
    ap.add_argument("--env-file", default=os.environ.get("PA_ENV_FILE", ""))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    fetch(args.out, *credentials(args.env_file), headed=args.headed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
