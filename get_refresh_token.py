"""Skrip sekali jalan: dapatkan OAuth refresh token untuk upload YouTube.

Jalankan di komputer kamu sendiri (butuh browser):

    python get_refresh_token.py --client-secret client_secret.json
    # atau
    python get_refresh_token.py --client-id XXX.apps.googleusercontent.com --client-secret-value YYY

Login dengan akun Google pemilik channel, pilih channel yang benar, izinkan akses,
lalu salin refresh token yang tercetak ke GitHub Secret YT_REFRESH_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-secret", help="path file JSON OAuth client (tipe Desktop app)")
    p.add_argument("--client-id", default=os.environ.get("YT_CLIENT_ID"))
    p.add_argument("--client-secret-value", default=os.environ.get("YT_CLIENT_SECRET"))
    args = p.parse_args()

    if args.client_secret:
        with open(args.client_secret, encoding="utf-8") as f:
            config = json.load(f)
    elif args.client_id and args.client_secret_value:
        config = {"installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret_value,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}
    else:
        p.error("butuh --client-secret FILE, atau --client-id dan --client-secret-value")
        return 2

    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    # prompt=consent memastikan Google selalu mengeluarkan refresh token baru
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  authorization_prompt_message="Buka URL ini di browser:\n{url}\n")
    if not creds.refresh_token:
        print("Tidak ada refresh token. Cabut akses aplikasi di https://myaccount.google.com/permissions lalu ulangi.",
              file=sys.stderr)
        return 1
    print("\n=== REFRESH TOKEN (simpan sebagai GitHub Secret YT_REFRESH_TOKEN) ===\n")
    print(creds.refresh_token)
    print("\nJangan bagikan token ini ke siapa pun.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
