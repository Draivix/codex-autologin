#!/usr/bin/env python3
"""
CLI for managing auth_api.py API keys.

The key store (api_keys.json) holds only SHA-256 hashes. Plaintext keys
are printed exactly once at generation time; you cannot recover them
later.

Schema of api_keys.json (mode 600):

    {
      "keys": {
        "<key_id>": {
          "label": "human-readable",
          "hash": "sha256 hex of plaintext",
          "scope": ["account1", "account2"]  // or ["*"] for admin
          "created_at": "2026-05-12T07:00:00+00:00",
          "rate_limit": 30,                  // requests per minute, optional
          "ip_allowlist": ["10.0.0.0/8"],    // optional
          "max_uses": null,                  // optional, one-shot if int
          "use_count": 0,
          "last_used_at": null,
          "revoked": false
        }
      }
    }

Usage:
    python3 apikey_admin.py generate --label dev1 --scope alice,carol
    python3 apikey_admin.py generate --label admin --scope '*'
    python3 apikey_admin.py list
    python3 apikey_admin.py revoke <key_id>
    python3 apikey_admin.py rotate <key_id>          # same scope, new value
    python3 apikey_admin.py delete <key_id>           # permanent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS_FILE = ROOT / "api_keys.json"
KEY_PREFIX = "ck_live_"


def load() -> dict:
    if not KEYS_FILE.exists():
        return {"keys": {}}
    with KEYS_FILE.open() as f:
        return json.load(f)


def save(data: dict) -> None:
    tmp = KEYS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    tmp.replace(KEYS_FILE)


def mint() -> tuple[str, str, str]:
    """Returns (key_id, plaintext, hash). Plaintext is the only time you see it."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{raw}"
    h = hashlib.sha256(plaintext.encode()).hexdigest()
    key_id = "key_" + secrets.token_hex(6)
    return key_id, plaintext, h


def parse_scope(s: str) -> list[str]:
    s = s.strip()
    if s == "*":
        return ["*"]
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        sys.exit("scope is empty")
    return parts


def cmd_generate(args) -> None:
    data = load()
    key_id, plaintext, h = mint()
    entry = {
        "label": args.label,
        "hash": h,
        "scope": parse_scope(args.scope),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rate_limit": args.rate_limit,
        "ip_allowlist": [c.strip() for c in args.ip_allowlist.split(",") if c.strip()]
                         if args.ip_allowlist else [],
        "max_uses": args.max_uses,
        "use_count": 0,
        "last_used_at": None,
        "revoked": False,
    }
    data.setdefault("keys", {})[key_id] = entry
    save(data)
    print(f"key_id     {key_id}")
    print(f"label      {args.label}")
    print(f"scope      {entry['scope']}")
    print(f"rate_limit {entry['rate_limit']}/min")
    if entry["ip_allowlist"]:
        print(f"ip_allow   {entry['ip_allowlist']}")
    if entry["max_uses"]:
        print(f"max_uses   {entry['max_uses']}")
    print()
    print("PLAINTEXT KEY (shown only once — copy now):")
    print(f"  {plaintext}")


def cmd_list(args) -> None:
    data = load()
    keys = data.get("keys", {})
    if not keys:
        print("(no keys)")
        return
    print(f"{'key_id':<20} {'label':<14} {'scope':<24} "
          f"{'rate':<6} {'uses':<8} {'last_used':<22} {'revoked'}")
    print("-" * 110)
    for kid, meta in sorted(keys.items()):
        scope = ",".join(meta.get("scope") or [])
        print(f"{kid:<20} {meta.get('label', ''):<14} {scope:<24} "
              f"{str(meta.get('rate_limit', 30)):<6} "
              f"{str(meta.get('use_count', 0)):<8} "
              f"{str(meta.get('last_used_at') or '-'):<22} "
              f"{'yes' if meta.get('revoked') else ''}")


def cmd_revoke(args) -> None:
    data = load()
    keys = data.get("keys", {})
    if args.key_id not in keys:
        sys.exit(f"unknown key_id: {args.key_id}")
    keys[args.key_id]["revoked"] = True
    keys[args.key_id]["revoked_at"] = datetime.now(timezone.utc).isoformat()
    save(data)
    print(f"revoked {args.key_id}")


def cmd_delete(args) -> None:
    data = load()
    keys = data.get("keys", {})
    if args.key_id not in keys:
        sys.exit(f"unknown key_id: {args.key_id}")
    del keys[args.key_id]
    save(data)
    print(f"deleted {args.key_id}")


def cmd_rotate(args) -> None:
    data = load()
    keys = data.get("keys", {})
    if args.key_id not in keys:
        sys.exit(f"unknown key_id: {args.key_id}")
    raw = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{raw}"
    h = hashlib.sha256(plaintext.encode()).hexdigest()
    keys[args.key_id]["hash"] = h
    keys[args.key_id]["rotated_at"] = datetime.now(timezone.utc).isoformat()
    keys[args.key_id]["use_count"] = 0
    save(data)
    print(f"rotated {args.key_id}")
    print("NEW PLAINTEXT KEY (shown only once):")
    print(f"  {plaintext}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="mint a new API key")
    g.add_argument("--label", required=True)
    g.add_argument("--scope", required=True,
                   help='comma-separated account list, or "*" for admin')
    g.add_argument("--rate-limit", type=int, default=30, dest="rate_limit",
                   help="requests/min (default 30)")
    g.add_argument("--ip-allowlist", default="", dest="ip_allowlist",
                   help="comma-separated CIDRs (optional)")
    g.add_argument("--max-uses", type=int, default=None, dest="max_uses",
                   help="one-shot/finite key")
    g.set_defaults(func=cmd_generate)

    l = sub.add_parser("list", help="show all keys")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("revoke", help="mark a key as revoked")
    r.add_argument("key_id")
    r.set_defaults(func=cmd_revoke)

    d = sub.add_parser("delete", help="permanently remove a key entry")
    d.add_argument("key_id")
    d.set_defaults(func=cmd_delete)

    ro = sub.add_parser("rotate", help="generate a new value for an existing key_id")
    ro.add_argument("key_id")
    ro.set_defaults(func=cmd_rotate)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
