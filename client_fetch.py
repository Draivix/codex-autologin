#!/usr/bin/env python3
"""
Client-side fetcher: download auth.json files from a remote auth_api
into a local CODEX_HOME directory tree.

Typical use on a developer workstation that does NOT run autologin
locally:

    export CODEX_API_BASE=https://api.example.com
    export CODEX_API_KEY=ck_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python3 client_fetch.py alice --dest ~/.codex-alice/auth.json
    BROWSER=true CODEX_HOME=~/.codex-alice codex

Or fetch every account this key can see, into per-account dirs:

    python3 client_fetch.py --all --root ~/codex-fleet
    ~/codex-fleet/alice/auth.json
    ~/codex-fleet/carol/auth.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def get_env_or_exit(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"set ${name} (or pass --{name.lower().replace('_', '-')})")
    return v


def http(base: str, path: str, key: str, method: str = "GET") -> bytes:
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "codex-autologin-client/1.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        sys.exit(f"HTTP {e.code} from {path}: {body[:200]}")
    except Exception as e:
        sys.exit(f"network error: {e}")


def list_accounts(base: str, key: str) -> list[str]:
    body = http(base, "/v1/accounts", key)
    return json.loads(body).get("accounts", [])


def fetch_account(base: str, key: str, account: str, dest: Path) -> int:
    body = http(base, f"/v1/accounts/{account}/auth", key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.chmod(tmp, 0o600)
    tmp.replace(dest)
    return len(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("account", nargs="?",
                    help="account name (omit when using --all)")
    ap.add_argument("--all", action="store_true",
                    help="fetch every account this key can access")
    ap.add_argument("--api-base", default=os.environ.get("CODEX_API_BASE"),
                    help="API base URL (or set CODEX_API_BASE)")
    ap.add_argument("--api-key", default=os.environ.get("CODEX_API_KEY"),
                    help="bearer key (or set CODEX_API_KEY)")
    ap.add_argument("--dest", type=Path,
                    help="write auth.json to this path (single-account mode)")
    ap.add_argument("--root", type=Path, default=Path("./codex-fleet"),
                    help="root dir for --all mode; writes <root>/<account>/auth.json")
    args = ap.parse_args()

    if not args.api_base:
        sys.exit("missing --api-base (or CODEX_API_BASE)")
    if not args.api_key:
        sys.exit("missing --api-key (or CODEX_API_KEY)")

    if args.all:
        accounts = list_accounts(args.api_base, args.api_key)
        if not accounts:
            sys.exit("no accounts accessible to this key")
        print(f"fetching {len(accounts)} accounts into {args.root}")
        for a in accounts:
            dest = args.root / a / "auth.json"
            n = fetch_account(args.api_base, args.api_key, a, dest)
            print(f"  {a:<14} -> {dest}  ({n} bytes)")
        return

    if not args.account:
        sys.exit("account required (or pass --all)")
    if not args.dest:
        sys.exit("--dest required when fetching a single account")
    n = fetch_account(args.api_base, args.api_key, args.account, args.dest)
    print(f"{args.account} -> {args.dest}  ({n} bytes)")


if __name__ == "__main__":
    main()
