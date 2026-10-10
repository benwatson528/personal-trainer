#!/usr/bin/env python3
"""One-time Google Drive authorisation for the PT backend.

Runs Google's OAuth flow as *you*, so the app can act on your personal Drive
(service accounts can't — they have no storage quota and cannot own files).

Before running this you need an OAuth client:
  1. https://console.cloud.google.com/ → create/reuse a project → search
     "Google Drive API" → Enable.
  2. "APIs & Services" → "OAuth consent screen" → External → fill in the app
     name (`Personal Trainer`) + your email → Save. (App is in "Testing"
     mode; that's fine — you're the only user.)
  3. "Credentials" → "Create credentials" → "OAuth client ID" → type
     **Desktop app** → download the JSON, e.g. to `secrets/gdrive_client_secret.json`.

Then run one of:

  # Same machine with a browser (opens the login page for you):
  PYTHONPATH=src .venv/bin/python scripts/gdrive_auth.py \
      --client-secrets secrets/gdrive_client_secret.json \
      --token secrets/gdrive_token.json

  # Headless / browser elsewhere (two steps, copy the URL into any browser):
  PYTHONPATH=src .venv/bin/python scripts/gdrive_auth.py \
      --client-secrets secrets/gdrive_client_secret.json \
      --print --state /tmp/opencode/gdrive-flow.json
  # ... open the printed URL, consent, then paste the redirected URL back:
  PYTHONPATH=src .venv/bin/python scripts/gdrive_auth.py \
      --client-secrets secrets/gdrive_client_secret.json \
      --finish --state /tmp/opencode/gdrive-flow.json \
      --code '<pasted URL or code>' --token secrets/gdrive_token.json

The script writes a refresh token to --token; the backend then uses it.
Never commit the client secret or the token file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_PORT = 8765


def _extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if pasted.startswith("http"):
        pasted = urlsplit(pasted).query
    code = parse_qs(pasted).get("code", [""])[0]
    if not code:
        sys.exit("No OAuth code found — paste the full address-bar URL after consent.")
    return code


def _write_token(creds, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"\nDone. Token written to {path}.")
    print("config/config.yaml points storage.oauth_token_file at this file; "
          "it is used once storage.backend: gdrive is set.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorise the PT backend for Google Drive")
    parser.add_argument(
        "--client-secrets",
        default=os.environ.get("PT_GOOGLE_CLIENT_SECRETS", "secrets/gdrive_client_secret.json"),
        help="Path to the OAuth client JSON (or PT_GOOGLE_CLIENT_SECRETS).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PT_GOOGLE_OAUTH_TOKEN", "secrets/gdrive_token.json"),
        help="Where to write the token file (default: secrets/gdrive_token.json).",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="Print the authorisation URL and exit (headless two-step flow).",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Save/read PKCE flow state across the two steps (use with --print/--finish).",
    )
    parser.add_argument(
        "--finish",
        action="store_true",
        help="Complete a --print flow: pass the pasted redirect URL/code via --code.",
    )
    parser.add_argument("--code", default=None, help="Pasted redirect URL or code (with --finish).")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Loopback port for the auth redirect."
    )
    args = parser.parse_args()

    if not args.client_secrets:
        sys.exit(
            "Missing --client-secrets. Create an OAuth client first (see the "
            "docstring) and pass its JSON path."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    # ---- two-step flow completion -----------------------------------------
    if args.finish:
        if not args.state or not args.code:
            sys.exit("--finish needs both --state and --code.")
        with open(args.state, encoding="utf-8") as fh:
            state = json.load(fh)
        flow = InstalledAppFlow.from_client_secrets_file(
            args.client_secrets,
            SCOPES,
            redirect_uri=state["redirect_uri"],
            code_verifier=state["code_verifier"],
            autogenerate_code_verifier=False,
        )
        flow.fetch_token(code=_extract_code(args.code))
        _write_token(flow.credentials, args.token)
        return

    flow = InstalledAppFlow.from_client_secrets_file(
        args.client_secrets,
        SCOPES,
        redirect_uri=f"http://localhost:{args.port}",
    )

    # ---- headless: print URL, keep state, exit -----------------------------
    if args.print:
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        if not args.state:
            sys.exit("--print needs --state to keep the flow resumable.")
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "code_verifier": flow.code_verifier,
                    "redirect_uri": flow.redirect_uri,
                },
                fh,
            )
        print("Open this URL in a browser and sign in as the Google account whose")
        print("Drive you want to use:\n")
        print(auth_url)
        print("\nAfter consent the browser will try to open a local URL and fail.")
        print("Copy the full address-bar URL and re-run this script with --finish --code.")
        return

    # ---- same-machine browser flow ----------------------------------------
    creds = flow.run_local_server(
        port=args.port,
        open_browser=True,
        prompt="consent",
        success_message="Authorised. Close this tab and return to the terminal.",
    )
    _write_token(creds, args.token)


if __name__ == "__main__":
    main()