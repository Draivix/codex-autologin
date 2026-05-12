#!/usr/bin/env python3
"""
Status checker for all logged-in codex accounts under ./homes/.

For each ./homes/<account>/auth.json, hits
https://chatgpt.com/backend-api/wham/usage with the access_token and prints
plan, rate-limit windows (5h / 7d), and remaining credits.

Usage:
    python codex_status.py                 # all accounts
    python codex_status.py alice           # one account
    python codex_status.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOMES = ROOT / "homes"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def fmt_window(w: dict | None) -> str:
    if not w:
        return "-"
    used = w.get("used_percent", 0)
    secs = w.get("reset_after_seconds", 0) or 0
    if secs >= 3600:
        eta = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    elif secs >= 60:
        eta = f"{secs // 60}m"
    else:
        eta = f"{secs}s"
    return f"{used:>5.1f}% (reset in {eta})"


def fetch_usage(auth_json: Path) -> dict:
    auth = json.loads(auth_json.read_text())
    tok = auth["tokens"]["access_token"]
    account = auth["tokens"].get("account_id") or ""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {tok}",
            "chatgpt-account-id": account,
            "User-Agent": "codex-cli/0.130.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def collect(account_filter: str | None) -> list[tuple[str, dict | str]]:
    if not HOMES.exists():
        sys.exit(f"no homes dir at {HOMES}")
    rows: list[tuple[str, dict | str]] = []
    for d in sorted(HOMES.iterdir()):
        if not d.is_dir():
            continue
        if account_filter and d.name != account_filter:
            continue
        auth = d / "auth.json"
        if not auth.exists():
            rows.append((d.name, "no auth.json"))
            continue
        try:
            data = fetch_usage(auth)
            rows.append((d.name, data))
        except urllib.error.HTTPError as e:
            rows.append((d.name, f"HTTP {e.code}: {e.reason}"))
        except Exception as e:
            rows.append((d.name, f"error: {e}"))
    return rows


def print_table(rows: list[tuple[str, dict | str]]) -> None:
    print(f"{'account':<14} {'plan':<10} {'email':<28} "
          f"{'5h window':<26} {'7d window':<26} credits")
    print("-" * 120)
    for name, data in rows:
        if isinstance(data, str):
            print(f"{name:<14} {'-':<10} {'-':<28} {data}")
            continue
        plan = data.get("plan_type", "-")
        email = data.get("email", "-")
        rl = data.get("rate_limit", {}) or {}
        primary = fmt_window(rl.get("primary_window"))
        secondary = fmt_window(rl.get("secondary_window"))
        cr = data.get("credits", {}) or {}
        if cr.get("unlimited"):
            credits = "unlimited"
        else:
            bal = cr.get("balance", "0")
            credits = f"${bal}"
        flag = ""
        if rl.get("limit_reached"):
            flag = " LIMIT-REACHED"
        elif rl.get("primary_window", {}).get("used_percent", 0) > 80:
            flag = " (high)"
        print(f"{name:<14} {plan:<10} {email:<28} {primary:<26} {secondary:<26} {credits}{flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("account", nargs="?", help="only show this account")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()
    rows = collect(args.account)
    if args.json:
        out = {name: data for name, data in rows}
        print(json.dumps(out, indent=2, default=str))
    else:
        print_table(rows)


if __name__ == "__main__":
    main()
