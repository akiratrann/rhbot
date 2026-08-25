#!/usr/bin/env python3
"""Authenticate with Schwab and save the tokens. Run this WEEKLY.

Schwab refresh tokens expire after 7 days and cannot be renewed
programmatically, so this is a manual ritual you cannot automate away.

    python schwab_login.py

You will be given a URL to open. You log in at schwab.com — this script never
sees your password. Schwab then redirects your browser to your app's callback
URL, which will fail to load (nothing is listening there); that is expected.
Copy the full URL out of the address bar and paste it back here.

    python schwab_login.py --status    # how long the current token has left
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from rhbot.schwab_client import (
    DEFAULT_TOKEN_PATH,
    SchwabAuthError,
    build_authorize_url,
    exchange_code_for_tokens,
    load_tokens,
)


def _status(token_path: str) -> int:
    tokens = load_tokens(token_path)
    if not tokens:
        print(f"No tokens at {token_path}. Run: python schwab_login.py")
        return 1
    left = tokens.get("refresh_expires_at", 0) - time.time()
    if left <= 0:
        print("Refresh token has EXPIRED. Run: python schwab_login.py")
        return 1
    print(f"Refresh token valid for {left / 3600:.1f} more hours "
          f"({left / 86400:.1f} days).")
    return 0


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Schwab OAuth helper")
    ap.add_argument("--status", action="store_true",
                    help="report time left on the current refresh token")
    ap.add_argument("--token-path", default=DEFAULT_TOKEN_PATH)
    args = ap.parse_args()

    if args.status:
        return _status(args.token_path)

    app_key = os.getenv("SCHWAB_APP_KEY")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    callback = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")

    if not app_key or not app_secret:
        print("SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env.\n"
              "Create them at https://developer.schwab.com (Trader API).")
        return 1

    print("\n1. Open this URL and log in to Schwab:\n")
    print("   " + build_authorize_url(app_key, callback))
    print("\n2. After approving, your browser will be redirected to a page that")
    print("   FAILS TO LOAD. That is expected — nothing is listening there.")
    print("   Copy the entire URL from the address bar.\n")

    redirect_url = input("3. Paste the full redirect URL here: ").strip()
    if not redirect_url:
        print("Nothing pasted, aborting.")
        return 1

    code = (parse_qs(urlparse(redirect_url).query).get("code") or [None])[0]
    if not code:
        print("No `code` parameter found in that URL. Paste the whole thing, "
              "including everything after the '?'.")
        return 1

    try:
        tokens = exchange_code_for_tokens(
            app_key, app_secret, code, callback, args.token_path)
    except SchwabAuthError as e:
        print(f"\nFailed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1

    left = tokens["refresh_expires_at"] - time.time()
    print(f"\nSaved to {args.token_path} (mode 0600).")
    print(f"Valid for {left / 86400:.1f} days — re-run this before then.")
    print("Treat that file like a password; it can trade your account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
