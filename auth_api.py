#!/usr/bin/env python3
"""
codex-autologin auth distribution API.

Securely distributes `auth.json` files for codex accounts to authorized
clients over HTTPS, so multiple developers / machines can use one shared
fleet of logged-in accounts without each running their own browser login.

Security model
--------------
- Bind 127.0.0.1 only. Production: TLS termination at nginx (see
  nginx.example.conf). The API server itself NEVER speaks plaintext to
  the internet.
- Bearer-token auth via `Authorization: Bearer <api_key>`.
  - Keys are 32-byte random, base64url-encoded, prefixed `ck_live_`.
  - Server stores only SHA-256 hashes (see apikey_admin.py).
  - Constant-time comparison.
- Per-key scoping: each key has a list of allowed account names. The
  special scope `["*"]` (admin) grants access to all accounts and to the
  refresh endpoint.
- Token-bucket rate limit per key (default 30/min, configurable per-key).
- Optional per-key IP allowlist (CIDR list). Reads client IP from
  `X-Forwarded-For` when `--behind-proxy` is passed.
- Optional one-shot keys: `max_uses` exhausts the key after N successful
  fetches.
- Append-only JSONL audit log of every request: ts, ip, key_id, account,
  action, status. Sensitive endpoint (`/auth`) is logged even on auth
  failure.
- Generic 404 when key cannot access an account (no scope-enumeration).
- HSTS, no-cache, X-Content-Type-Options headers set on every response.
- Request body size capped.

Endpoints
---------
GET  /v1/health                       — public liveness probe (no auth)
GET  /v1/accounts                     — list accounts your key can access
GET  /v1/accounts/<name>/status       — usage info (less sensitive)
GET  /v1/accounts/<name>/auth         — return the auth.json (the secret!)
POST /v1/accounts/<name>/refresh      — trigger re-login (admin scope only)

Usage
-----
    pip install flask
    # 1. mint keys
    python3 apikey_admin.py generate --label dev1 --scope alice,carol
    # 2. run server
    python3 auth_api.py --port 8788 --behind-proxy
    # 3. front with nginx (see nginx.example.conf)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, jsonify, request, Response
except ImportError:
    sys.exit("flask not installed. run: pip install flask")

ROOT = Path(__file__).resolve().parent
HOMES = ROOT / "homes"
KEYS_FILE = ROOT / "api_keys.json"
AUDIT_LOG = ROOT / "audit.log"
MAX_BODY_BYTES = 64 * 1024  # 64 KiB cap on incoming bodies
DEFAULT_RATE_LIMIT = 30      # requests per minute per key
KEY_PREFIX = "ck_live_"


# ---------------- Key store ----------------
_keys_lock = threading.Lock()
_keys_cache: dict | None = None
_keys_mtime: float = 0.0


def _load_keys() -> dict:
    """Reload api_keys.json if it changed on disk. Returns dict[key_hash -> meta]."""
    global _keys_cache, _keys_mtime
    with _keys_lock:
        if not KEYS_FILE.exists():
            _keys_cache = {"keys": {}}
            return _keys_cache
        mtime = KEYS_FILE.stat().st_mtime
        if _keys_cache is None or mtime != _keys_mtime:
            with KEYS_FILE.open() as f:
                _keys_cache = json.load(f)
            _keys_mtime = mtime
        return _keys_cache


def _save_keys(data: dict) -> None:
    global _keys_cache, _keys_mtime
    tmp = KEYS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(KEYS_FILE)
    with _keys_lock:
        _keys_cache = data
        _keys_mtime = KEYS_FILE.stat().st_mtime


def hash_key(plaintext: str) -> str:
    """Stable hex digest of a key. SHA-256 is enough — keys are 32-byte random."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def lookup_key(plaintext: str) -> tuple[str, dict] | None:
    """Constant-time-ish lookup of a key. Returns (key_id, meta) or None."""
    h = hash_key(plaintext)
    data = _load_keys()
    # iterate all keys to keep timing roughly constant on a small key set
    found: tuple[str, dict] | None = None
    for key_id, meta in data.get("keys", {}).items():
        if hmac.compare_digest(h, meta.get("hash", "")):
            found = (key_id, meta)
    return found


# ---------------- Audit log ----------------
_audit_lock = threading.Lock()


def audit(action: str, account: str | None, key_id: str | None,
          status: int, ip: str, extra: dict | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "key_id": key_id,
        "account": account,
        "action": action,
        "status": status,
    }
    if extra:
        entry.update(extra)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with _audit_lock:
        with AUDIT_LOG.open("a") as f:
            f.write(line)


# ---------------- Rate limit ----------------
_rl_lock = threading.Lock()
_rl: dict[str, list[float]] = {}  # key_id -> deque of timestamps within window


def rate_check(key_id: str, per_min: int) -> bool:
    """Return True if request is allowed. Uses a sliding 60s window."""
    now = time.time()
    cutoff = now - 60
    with _rl_lock:
        timestamps = [t for t in _rl.get(key_id, []) if t > cutoff]
        if len(timestamps) >= per_min:
            _rl[key_id] = timestamps
            return False
        timestamps.append(now)
        _rl[key_id] = timestamps
        return True


# ---------------- Helpers ----------------
def client_ip(behind_proxy: bool) -> str:
    if behind_proxy:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def ip_allowed(meta: dict, ip: str) -> bool:
    allow = meta.get("ip_allowlist") or []
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in allow:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def parse_bearer() -> str | None:
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    return h[7:].strip()


def secure_headers(resp: Response) -> Response:
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def deny(action: str, account: str | None, key_id: str | None,
        status: int, ip: str, msg: str) -> Response:
    audit(action, account, key_id, status, ip, {"deny_reason": msg})
    return secure_headers(jsonify(error=msg)), status


def authenticate(action: str, account: str | None,
                 behind_proxy: bool) -> tuple[str, dict] | tuple[None, Response]:
    """Returns (key_id, meta) on success, or (None, response) on failure."""
    ip = client_ip(behind_proxy)
    token = parse_bearer()
    if not token:
        return None, deny(action, account, None, 401, ip, "missing bearer token")
    pair = lookup_key(token)
    if not pair:
        return None, deny(action, account, None, 401, ip, "invalid api key")
    key_id, meta = pair
    if meta.get("revoked"):
        return None, deny(action, account, key_id, 401, ip, "key revoked")
    if not ip_allowed(meta, ip):
        return None, deny(action, account, key_id, 403, ip, "ip not allowed")
    per_min = int(meta.get("rate_limit", DEFAULT_RATE_LIMIT))
    if not rate_check(key_id, per_min):
        return None, deny(action, account, key_id, 429, ip, "rate limit exceeded")
    max_uses = meta.get("max_uses")
    if max_uses is not None and meta.get("use_count", 0) >= int(max_uses):
        return None, deny(action, account, key_id, 403, ip, "key exhausted")
    return (key_id, meta), None


def scope_allows(meta: dict, account: str) -> bool:
    scope = meta.get("scope") or []
    return "*" in scope or account in scope


def bump_usage(key_id: str) -> None:
    data = _load_keys()
    keys = data.setdefault("keys", {})
    if key_id not in keys:
        return
    keys[key_id]["use_count"] = int(keys[key_id].get("use_count", 0)) + 1
    keys[key_id]["last_used_at"] = datetime.now(timezone.utc).isoformat()
    _save_keys(data)


def list_accessible(meta: dict) -> list[str]:
    available = sorted(d.name for d in HOMES.iterdir()
                       if d.is_dir() and (d / "auth.json").exists())
    if "*" in (meta.get("scope") or []):
        return available
    scope = set(meta.get("scope") or [])
    return [a for a in available if a in scope]


# ---------------- App ----------------
def build_app(behind_proxy: bool) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    @app.before_request
    def _trace_id():
        request.environ["trace_id"] = uuid.uuid4().hex[:12]

    @app.route("/v1/health")
    def health():
        return secure_headers(jsonify(status="ok"))

    @app.route("/v1/accounts")
    def list_accounts():
        ip = client_ip(behind_proxy)
        res = authenticate("list_accounts", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        names = list_accessible(meta)
        audit("list_accounts", None, key_id, 200, ip, {"count": len(names)})
        return secure_headers(jsonify(accounts=names))

    @app.route("/v1/accounts/<account>/status")
    def account_status(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("status", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not scope_allows(meta, account):
            return deny("status", account, key_id, 404, ip, "not found")
        auth_path = HOMES / account / "auth.json"
        if not auth_path.exists():
            return deny("status", account, key_id, 404, ip, "no auth.json")
        # Lazy import to avoid loading status helpers when unused
        from codex_status import fetch_usage  # type: ignore
        try:
            data = fetch_usage(auth_path)
        except Exception as e:
            return deny("status", account, key_id, 502, ip, f"upstream: {e}")
        audit("status", account, key_id, 200, ip)
        return secure_headers(jsonify(data))

    @app.route("/v1/accounts/<account>/auth")
    def account_auth(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("auth", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        # Treat scope miss as 404 to avoid leaking account existence
        if not scope_allows(meta, account):
            return deny("auth", account, key_id, 404, ip, "not found")
        auth_path = HOMES / account / "auth.json"
        if not auth_path.exists():
            return deny("auth", account, key_id, 404, ip, "no auth.json")
        body = auth_path.read_bytes()
        bump_usage(key_id)
        audit("auth", account, key_id, 200, ip, {"bytes": len(body)})
        # Return as JSON content-type; do not log body
        return secure_headers(Response(body, mimetype="application/json"))

    @app.route("/v1/accounts/<account>/refresh", methods=["POST"])
    def account_refresh(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("refresh", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if "*" not in (meta.get("scope") or []):
            return deny("refresh", account, key_id, 403, ip, "admin scope required")
        # spawn re-login in background, do not block the request
        script = ROOT / "codex_autologin.py"
        if not script.exists():
            return deny("refresh", account, key_id, 500, ip, "autologin script missing")
        try:
            subprocess.Popen(
                [sys.executable, str(script), account, "--force"],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            return deny("refresh", account, key_id, 500, ip, f"spawn: {e}")
        audit("refresh", account, key_id, 202, ip)
        return secure_headers(jsonify(status="started"))

    @app.errorhandler(404)
    def _404(_):
        return secure_headers(jsonify(error="not found")), 404

    @app.errorhandler(405)
    def _405(_):
        return secure_headers(jsonify(error="method not allowed")), 405

    @app.errorhandler(413)
    def _413(_):
        return secure_headers(jsonify(error="payload too large")), 413

    @app.errorhandler(500)
    def _500(e):
        ip = client_ip(behind_proxy)
        audit("server_error", None, None, 500, ip, {"err": repr(e)})
        return secure_headers(jsonify(error="server error")), 500

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; KEEP 127.0.0.1 in production (TLS terminates at nginx)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--behind-proxy", action="store_true",
                    help="trust X-Forwarded-For (required behind nginx)")
    args = ap.parse_args()

    # production WSGI: prefer waitress if available, else use Flask's server
    app = build_app(behind_proxy=args.behind_proxy)
    print(f"auth_api listening on http://{args.host}:{args.port}  "
          f"(behind_proxy={args.behind_proxy})")
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8,
              ident="codex-auth-api")
    except ImportError:
        # dev fallback
        print("[warn] waitress not installed; using Flask dev server (not for prod)")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
