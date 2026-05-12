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
GET    /v1/health                                — public liveness probe
GET    /v1/accounts                              — list accounts key can access
GET    /v1/accounts/<name>                       — account metadata (no secrets)
POST   /v1/accounts                              — create account (admin)
PATCH  /v1/accounts/<name>                       — update creds (admin)
DELETE /v1/accounts/<name>                       — remove account + homes/<n>/ (admin)
GET    /v1/accounts/<name>/status                — live /wham/usage data
GET    /v1/accounts/<name>/auth                  — return auth.json (the secret)
DELETE /v1/accounts/<name>/auth                  — local logout: drop auth.json (admin)
POST   /v1/accounts/<name>/reconnect             — re-login in background (admin)
POST   /v1/accounts/<name>/refresh               — alias for /reconnect
GET    /v1/accounts/<name>/reconnect-status      — running/ok/failed + log tail
GET    /v1/keys                                  — list keys (admin)
POST   /v1/keys                                  — mint a new key (admin)
GET    /v1/keys/<key_id>                         — key metadata (admin)
PATCH  /v1/keys/<key_id>                         — update scope/limits/revoke (admin)
DELETE /v1/keys/<key_id>                         — delete a key (admin)
GET    /v1/imap                                  — current IMAP config (admin)
PUT    /v1/imap                                  — update IMAP host/port/ssl (admin)
GET    /v1/audit?limit=N                         — tail audit log (admin)

Usage
-----
    pip install flask waitress
    # 1. mint the first admin key (only CLI can do this when no key exists)
    python3 apikey_admin.py generate --label admin --scope '*'
    # 2. run server (waitress in production)
    python3 auth_api.py --port 8788 --behind-proxy
    # 3. front with nginx + Let's Encrypt (see nginx.example.conf)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
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
ACCOUNTS_FILE = ROOT / "accounts.json"
AUDIT_LOG = ROOT / "audit.log"
MAX_BODY_BYTES = 64 * 1024
DEFAULT_RATE_LIMIT = 30
KEY_PREFIX = "ck_live_"
ACCOUNT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------- Key store ----------------
_keys_lock = threading.RLock()
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


def is_admin(meta: dict) -> bool:
    return "*" in (meta.get("scope") or [])


# ---------------- accounts.json store ----------------
_accounts_lock = threading.RLock()


def load_accounts() -> dict:
    if not ACCOUNTS_FILE.exists():
        return {"imap": {"host": "", "port": 993, "ssl": True}, "accounts": {}}
    with ACCOUNTS_FILE.open() as f:
        return json.load(f)


def save_accounts(data: dict) -> None:
    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    tmp.replace(ACCOUNTS_FILE)


def safe_account_name(name: str) -> bool:
    return bool(ACCOUNT_NAME_RE.match(name)) and ".." not in name and "/" not in name


def _decode_jwt_payload(jwt: str) -> dict | None:
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw)
    except Exception:
        return None


def account_status_meta(name: str) -> dict:
    """Non-secret metadata for an account: file presence/size + JWT email + plan."""
    home = HOMES / name
    auth = home / "auth.json"
    out = {
        "name": name,
        "has_auth": auth.exists(),
        "auth_size": auth.stat().st_size if auth.exists() else 0,
    }
    if auth.exists():
        try:
            d = json.loads(auth.read_text())
            out["last_refresh"] = d.get("last_refresh")
            id_token = d.get("tokens", {}).get("id_token")
            if isinstance(id_token, str):
                claims = _decode_jwt_payload(id_token) or {}
                out["email"] = claims.get("email")
                auth_block = claims.get("https://api.openai.com/auth", {})
                if isinstance(auth_block, dict):
                    out["plan_type"] = auth_block.get("chatgpt_plan_type")
        except Exception:
            pass
    return out


# ---------------- Reconnect (refresh) tracking ----------------
_reconnect_lock = threading.Lock()
_reconnect_state: dict[str, dict] = {}
# {account: {pid, started_at, finished_at|None, exit_code|None, log_path}}


def _wait_proc(account: str, proc: subprocess.Popen, log_path: Path) -> None:
    """Background thread: wait on the autologin subprocess and record outcome."""
    rc = proc.wait()
    with _reconnect_lock:
        st = _reconnect_state.get(account)
        if st is not None:
            st["finished_at"] = datetime.now(timezone.utc).isoformat()
            st["exit_code"] = rc


def spawn_reconnect(account: str) -> tuple[bool, str]:
    """Spawn the autologin in the background for `account`. Returns (ok, message)."""
    script = ROOT / "codex_autologin.py"
    if not script.exists():
        return False, "autologin script missing"
    with _reconnect_lock:
        cur = _reconnect_state.get(account)
        if cur and cur.get("finished_at") is None:
            return False, "reconnect already in progress"
    log_dir = ROOT / "reconnect_logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{account}-{ts}.log"
    try:
        f = log_path.open("a")
        proc = subprocess.Popen(
            [sys.executable, str(script), account, "--force"],
            cwd=str(ROOT),
            stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"spawn failed: {e}"
    with _reconnect_lock:
        _reconnect_state[account] = {
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "exit_code": None,
            "log_path": str(log_path),
        }
    threading.Thread(target=_wait_proc, args=(account, proc, log_path),
                     daemon=True).start()
    return True, "started"


def reconnect_status(account: str) -> dict:
    with _reconnect_lock:
        st = _reconnect_state.get(account)
        if not st:
            return {"state": "idle"}
        out = dict(st)
        if out.get("finished_at") is None:
            out["state"] = "running"
        else:
            out["state"] = "ok" if out.get("exit_code") == 0 else "failed"
    # tail the log if present
    lp = Path(out.get("log_path", ""))
    if lp.exists():
        try:
            tail = lp.read_text(errors="ignore").splitlines()[-20:]
            out["log_tail"] = tail
        except Exception:
            pass
    return out


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

    # -------- account CRUD (admin scope) --------
    @app.route("/v1/accounts/<account>", methods=["GET"])
    def account_get(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("get_account", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not safe_account_name(account):
            return deny("get_account", account, key_id, 400, ip, "bad name")
        if not scope_allows(meta, account):
            return deny("get_account", account, key_id, 404, ip, "not found")
        with _accounts_lock:
            acc = load_accounts().get("accounts", {}).get(account)
        if not acc:
            # The account dir may exist (homes/) but no creds — still return file status
            home_meta = account_status_meta(account)
            if not home_meta["has_auth"]:
                return deny("get_account", account, key_id, 404, ip, "not found")
            audit("get_account", account, key_id, 200, ip)
            return secure_headers(jsonify(in_config=False, **home_meta))
        # Strip secrets — never return passwords through the API.
        # Use config_email vs jwt_email to keep them distinct in the response.
        meta_info = account_status_meta(account)
        meta_info["jwt_email"] = meta_info.pop("email", None)
        cleaned = {"config_email": acc.get("email"),
                   "has_imap_user_override": "imap_user" in acc,
                   "has_imap_password_override": "imap_password" in acc}
        audit("get_account", account, key_id, 200, ip)
        return secure_headers(jsonify(in_config=True, **cleaned, **meta_info))

    @app.route("/v1/accounts", methods=["POST"])
    def account_create():
        ip = client_ip(behind_proxy)
        res = authenticate("create_account", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("create_account", None, key_id, 403, ip, "admin scope required")
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        email_addr = (body.get("email") or "").strip()
        password = body.get("password") or ""
        imap_user = body.get("imap_user")
        imap_password = body.get("imap_password")
        do_login = bool(body.get("login"))

        if not safe_account_name(name):
            return deny("create_account", name, key_id, 400, ip, "bad name")
        if not EMAIL_RE.match(email_addr):
            return deny("create_account", name, key_id, 400, ip, "bad email")
        if not isinstance(password, str) or len(password) < 4:
            return deny("create_account", name, key_id, 400, ip, "password too short")

        with _accounts_lock:
            data = load_accounts()
            accounts = data.setdefault("accounts", {})
            if name in accounts:
                return deny("create_account", name, key_id, 409, ip, "account exists")
            entry = {"email": email_addr, "password": password}
            if isinstance(imap_user, str) and imap_user:
                entry["imap_user"] = imap_user
            if isinstance(imap_password, str) and imap_password:
                entry["imap_password"] = imap_password
            accounts[name] = entry
            save_accounts(data)
        audit("create_account", name, key_id, 201, ip,
              {"email": email_addr, "login_requested": do_login})
        out = {"name": name, "email": email_addr, "in_config": True, "has_auth": False}
        if do_login:
            ok, msg = spawn_reconnect(name)
            out["login_spawn"] = msg
            if not ok:
                out["login_error"] = True
        return secure_headers(jsonify(out)), 201

    @app.route("/v1/accounts/<account>", methods=["PATCH"])
    def account_update(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("update_account", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("update_account", account, key_id, 403, ip, "admin scope required")
        if not safe_account_name(account):
            return deny("update_account", account, key_id, 400, ip, "bad name")
        body = request.get_json(silent=True) or {}
        with _accounts_lock:
            data = load_accounts()
            acc = data.get("accounts", {}).get(account)
            if not acc:
                return deny("update_account", account, key_id, 404, ip, "not found")
            allowed = ("email", "password", "imap_user", "imap_password")
            changed = []
            for k in allowed:
                if k in body and body[k] is not None:
                    if k == "email" and not EMAIL_RE.match(body[k]):
                        return deny("update_account", account, key_id, 400, ip,
                                    f"bad {k}")
                    if k in ("password", "imap_password") and \
                       (not isinstance(body[k], str) or len(body[k]) < 4):
                        return deny("update_account", account, key_id, 400, ip,
                                    f"bad {k}")
                    acc[k] = body[k]
                    changed.append(k)
            if not changed:
                return deny("update_account", account, key_id, 400, ip,
                            "no updatable fields")
            save_accounts(data)
        audit("update_account", account, key_id, 200, ip, {"fields": changed})
        return secure_headers(jsonify(name=account, updated=changed))

    @app.route("/v1/accounts/<account>", methods=["DELETE"])
    def account_delete(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("delete_account", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("delete_account", account, key_id, 403, ip, "admin scope required")
        if not safe_account_name(account):
            return deny("delete_account", account, key_id, 400, ip, "bad name")
        removed_config = False
        removed_home = False
        with _accounts_lock:
            data = load_accounts()
            if account in data.get("accounts", {}):
                del data["accounts"][account]
                save_accounts(data)
                removed_config = True
        home = HOMES / account
        if home.is_dir():
            shutil.rmtree(home, ignore_errors=True)
            removed_home = True
        if not removed_config and not removed_home:
            return deny("delete_account", account, key_id, 404, ip, "not found")
        audit("delete_account", account, key_id, 200, ip,
              {"removed_config": removed_config, "removed_home": removed_home})
        return secure_headers(jsonify(name=account,
                                      removed_config=removed_config,
                                      removed_home=removed_home))

    @app.route("/v1/accounts/<account>/auth", methods=["DELETE"])
    def account_logout(account: str):
        """Drop the local auth.json (codex sees this account as logged out)."""
        ip = client_ip(behind_proxy)
        res = authenticate("logout", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("logout", account, key_id, 403, ip, "admin scope required")
        if not safe_account_name(account):
            return deny("logout", account, key_id, 400, ip, "bad name")
        auth_path = HOMES / account / "auth.json"
        if not auth_path.exists():
            return deny("logout", account, key_id, 404, ip, "no auth.json")
        try:
            auth_path.unlink()
        except Exception as e:
            return deny("logout", account, key_id, 500, ip, f"unlink: {e}")
        audit("logout", account, key_id, 200, ip)
        return secure_headers(jsonify(name=account, removed=True))

    @app.route("/v1/accounts/<account>/reconnect", methods=["POST"])
    @app.route("/v1/accounts/<account>/refresh", methods=["POST"])
    def account_reconnect(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("reconnect", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("reconnect", account, key_id, 403, ip, "admin scope required")
        if not safe_account_name(account):
            return deny("reconnect", account, key_id, 400, ip, "bad name")
        with _accounts_lock:
            acc = load_accounts().get("accounts", {}).get(account)
        if not acc:
            return deny("reconnect", account, key_id, 404, ip, "no creds in accounts.json")
        ok, msg = spawn_reconnect(account)
        if not ok:
            return deny("reconnect", account, key_id, 409, ip, msg)
        audit("reconnect", account, key_id, 202, ip)
        return secure_headers(jsonify(status="started", message=msg)), 202

    @app.route("/v1/accounts/<account>/reconnect-status", methods=["GET"])
    def account_reconnect_status(account: str):
        ip = client_ip(behind_proxy)
        res = authenticate("reconnect_status", account, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("reconnect_status", account, key_id, 403, ip, "admin scope required")
        if not safe_account_name(account):
            return deny("reconnect_status", account, key_id, 400, ip, "bad name")
        st = reconnect_status(account)
        audit("reconnect_status", account, key_id, 200, ip, {"state": st.get("state")})
        return secure_headers(jsonify(st))

    # -------- API key CRUD (admin only) --------
    @app.route("/v1/keys", methods=["GET"])
    def keys_list():
        ip = client_ip(behind_proxy)
        res = authenticate("keys_list", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("keys_list", None, key_id, 403, ip, "admin scope required")
        data = _load_keys()
        out = []
        for kid, m in (data.get("keys") or {}).items():
            out.append({
                "key_id": kid,
                "label": m.get("label"),
                "scope": m.get("scope"),
                "rate_limit": m.get("rate_limit"),
                "ip_allowlist": m.get("ip_allowlist"),
                "max_uses": m.get("max_uses"),
                "use_count": m.get("use_count", 0),
                "last_used_at": m.get("last_used_at"),
                "created_at": m.get("created_at"),
                "revoked": bool(m.get("revoked")),
            })
        audit("keys_list", None, key_id, 200, ip, {"count": len(out)})
        return secure_headers(jsonify(keys=out))

    @app.route("/v1/keys", methods=["POST"])
    def keys_create():
        ip = client_ip(behind_proxy)
        res = authenticate("keys_create", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("keys_create", None, key_id, 403, ip, "admin scope required")
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip()
        scope = body.get("scope") or []
        rate_limit = int(body.get("rate_limit") or DEFAULT_RATE_LIMIT)
        ip_allowlist = body.get("ip_allowlist") or []
        max_uses = body.get("max_uses")
        if not label or len(label) > 64:
            return deny("keys_create", None, key_id, 400, ip, "bad label")
        if not isinstance(scope, list) or not scope:
            return deny("keys_create", None, key_id, 400, ip, "scope must be non-empty list")
        for s in scope:
            if s != "*" and not safe_account_name(s):
                return deny("keys_create", None, key_id, 400, ip, f"bad scope item: {s}")
        # mint
        raw = secrets.token_urlsafe(32)
        plaintext = f"{KEY_PREFIX}{raw}"
        h = hashlib.sha256(plaintext.encode()).hexdigest()
        new_id = "key_" + secrets.token_hex(6)
        entry = {
            "label": label,
            "hash": h,
            "scope": scope,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rate_limit": rate_limit,
            "ip_allowlist": [c for c in ip_allowlist if isinstance(c, str)],
            "max_uses": max_uses if isinstance(max_uses, int) else None,
            "use_count": 0,
            "last_used_at": None,
            "revoked": False,
        }
        with _keys_lock:
            data = _load_keys()
            data.setdefault("keys", {})[new_id] = entry
            _save_keys(data)
        audit("keys_create", None, key_id, 201, ip,
              {"new_key_id": new_id, "scope": scope})
        return secure_headers(jsonify(key_id=new_id, plaintext=plaintext,
                                      label=label, scope=scope,
                                      warning="plaintext is shown only here; store it now")), 201

    @app.route("/v1/keys/<kid>", methods=["GET"])
    def keys_get(kid: str):
        ip = client_ip(behind_proxy)
        res = authenticate("keys_get", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("keys_get", None, key_id, 403, ip, "admin scope required")
        data = _load_keys()
        m = (data.get("keys") or {}).get(kid)
        if not m:
            return deny("keys_get", None, key_id, 404, ip, "not found")
        out = {k: v for k, v in m.items() if k != "hash"}
        out["key_id"] = kid
        audit("keys_get", None, key_id, 200, ip, {"target": kid})
        return secure_headers(jsonify(out))

    @app.route("/v1/keys/<kid>", methods=["PATCH"])
    def keys_update(kid: str):
        ip = client_ip(behind_proxy)
        res = authenticate("keys_update", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("keys_update", None, key_id, 403, ip, "admin scope required")
        body = request.get_json(silent=True) or {}
        with _keys_lock:
            data = _load_keys()
            m = (data.get("keys") or {}).get(kid)
            if not m:
                return deny("keys_update", None, key_id, 404, ip, "not found")
            changed = []
            if "label" in body and isinstance(body["label"], str):
                m["label"] = body["label"][:64]; changed.append("label")
            if "scope" in body and isinstance(body["scope"], list):
                for s in body["scope"]:
                    if s != "*" and not safe_account_name(s):
                        return deny("keys_update", None, key_id, 400, ip,
                                    f"bad scope item: {s}")
                m["scope"] = body["scope"]; changed.append("scope")
            if "rate_limit" in body and isinstance(body["rate_limit"], int):
                m["rate_limit"] = max(1, body["rate_limit"]); changed.append("rate_limit")
            if "ip_allowlist" in body and isinstance(body["ip_allowlist"], list):
                m["ip_allowlist"] = [c for c in body["ip_allowlist"] if isinstance(c, str)]
                changed.append("ip_allowlist")
            if "max_uses" in body:
                v = body["max_uses"]
                m["max_uses"] = v if isinstance(v, int) else None
                changed.append("max_uses")
            if "revoked" in body and isinstance(body["revoked"], bool):
                m["revoked"] = body["revoked"]; changed.append("revoked")
            if not changed:
                return deny("keys_update", None, key_id, 400, ip,
                            "no updatable fields")
            _save_keys(data)
        audit("keys_update", None, key_id, 200, ip, {"target": kid, "fields": changed})
        return secure_headers(jsonify(key_id=kid, updated=changed))

    @app.route("/v1/keys/<kid>", methods=["DELETE"])
    def keys_delete(kid: str):
        ip = client_ip(behind_proxy)
        res = authenticate("keys_delete", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("keys_delete", None, key_id, 403, ip, "admin scope required")
        if kid == key_id:
            return deny("keys_delete", None, key_id, 400, ip,
                        "refusing to delete the key making this request")
        with _keys_lock:
            data = _load_keys()
            if kid not in (data.get("keys") or {}):
                return deny("keys_delete", None, key_id, 404, ip, "not found")
            del data["keys"][kid]
            _save_keys(data)
        audit("keys_delete", None, key_id, 200, ip, {"target": kid})
        return secure_headers(jsonify(key_id=kid, deleted=True))

    # -------- IMAP config --------
    @app.route("/v1/imap", methods=["GET"])
    def imap_get():
        ip = client_ip(behind_proxy)
        res = authenticate("imap_get", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("imap_get", None, key_id, 403, ip, "admin scope required")
        with _accounts_lock:
            cfg = load_accounts().get("imap", {})
        audit("imap_get", None, key_id, 200, ip)
        return secure_headers(jsonify(cfg))

    @app.route("/v1/imap", methods=["PUT"])
    def imap_put():
        ip = client_ip(behind_proxy)
        res = authenticate("imap_put", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("imap_put", None, key_id, 403, ip, "admin scope required")
        body = request.get_json(silent=True) or {}
        host = body.get("host")
        port = body.get("port", 993)
        ssl_flag = body.get("ssl", True)
        if not isinstance(host, str) or not host:
            return deny("imap_put", None, key_id, 400, ip, "host required")
        if not isinstance(port, int) or port <= 0 or port > 65535:
            return deny("imap_put", None, key_id, 400, ip, "bad port")
        with _accounts_lock:
            data = load_accounts()
            data["imap"] = {"host": host, "port": port, "ssl": bool(ssl_flag)}
            save_accounts(data)
        audit("imap_put", None, key_id, 200, ip, {"host": host, "port": port})
        return secure_headers(jsonify(data["imap"]))

    # -------- Audit log tail --------
    @app.route("/v1/audit", methods=["GET"])
    def audit_tail():
        ip = client_ip(behind_proxy)
        res = authenticate("audit", None, behind_proxy)
        if res[0] is None:
            return res[1]
        key_id, meta = res[0]
        if not is_admin(meta):
            return deny("audit", None, key_id, 403, ip, "admin scope required")
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
        if not AUDIT_LOG.exists():
            return secure_headers(jsonify(entries=[]))
        lines = AUDIT_LOG.read_text(errors="ignore").splitlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        audit("audit", None, key_id, 200, ip, {"returned": len(out)})
        return secure_headers(jsonify(entries=out))

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
