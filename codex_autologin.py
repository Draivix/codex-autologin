#!/usr/bin/env python3
"""
Codex (OpenAI CLI) autologin via Camoufox + IMAP OTP.

Usage:
    python codex_autologin.py <account-key> [--headed] [--debug]
    python codex_autologin.py --all [--cooldown 30] [--force]

Examples:
    python codex_autologin.py alice                 # log in one account
    python codex_autologin.py alice --headed        # watch Camoufox
    python codex_autologin.py --all                 # bootstrap every account
    python codex_autologin.py --all --force         # re-login everyone

What it does:
    1. Reads accounts.json for credentials + IMAP config.
    2. Spawns `codex login` (with BROWSER=true so it never hijacks the real
       desktop browser via xdg-open).
    3. Parses the auth URL from codex stderr.
    4. Launches Camoufox (stealth Firefox), navigates to the auth URL.
    5. Fills email -> continue, password -> continue.
    6. If OpenAI prompts for a 6-digit OTP, polls IMAP across INBOX, Junk
       and spam folders for the latest mail from noreply@tm.openai.com or
       otp@tm1.openai.com and submits the code.
    7. Browser is redirected to http://localhost:1455/auth/callback?code=...
       (and then to /success). Codex's local server captures the code,
       exchanges for tokens, writes $CODEX_HOME/auth.json, exits 0.
    8. We confirm `auth.json` exists.

Multi-account: each account gets its own dir under ./homes/<key>/, set as
$CODEX_HOME for that account. Run `CODEX_HOME=./homes/alice codex` to use
that account, independent from any other.

Requires an IMAP mailbox to receive the OTP. If the IMAP mailbox and the
OpenAI account share credentials, set just `email` + `password`. If they
differ, set `imap_user` + `imap_password` per account. See
accounts.example.json.
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

from camoufox.sync_api import Camoufox

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.json"
HOMES_DIR = ROOT / "homes"
AUTH_URL_RE = re.compile(r"https://auth\.openai\.com/oauth/authorize\?[^\s]+")
OTP_RE = re.compile(r"\b(\d{6})\b")
OPENAI_OTP_SENDERS = (
    "noreply@tm.openai.com",
    "otp@tm1.openai.com",
    "noreply@openai.com",
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_accounts() -> dict:
    with ACCOUNTS_FILE.open() as f:
        return json.load(f)


def pick_account(key: str, accounts: dict) -> tuple[str, str, str, str, dict]:
    """Return (openai_email, openai_password, imap_user, imap_password, imap_cfg)."""
    if key not in accounts["accounts"]:
        sys.exit(f"unknown account '{key}'. available: {list(accounts['accounts'])}")
    a = accounts["accounts"][key]
    email_addr = a["email"]
    password = a["password"]
    imap_user = a.get("imap_user", email_addr)
    imap_password = a.get("imap_password", password)
    return email_addr, password, imap_user, imap_password, accounts["imap"]


def spawn_codex_login(codex_home: Path) -> tuple[subprocess.Popen, str]:
    codex_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["BROWSER"] = "true"  # CRITICAL: stops webbrowser::open hijacking real browser
    env["CODEX_HOME"] = str(codex_home)
    log(f"spawning: CODEX_HOME={codex_home} codex login")
    proc = subprocess.Popen(
        ["codex", "login"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # Drain stderr until we see the auth URL.
    auth_url = None
    deadline = time.time() + 15
    buf = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        buf.append(line)
        m = AUTH_URL_RE.search(line)
        if m:
            auth_url = m.group(0)
            break
    if not auth_url:
        proc.kill()
        sys.exit("did not capture auth URL from codex stderr:\n" + "".join(buf))
    log(f"captured auth URL: {auth_url[:80]}...")
    return proc, auth_url


def _list_folders(M: imaplib.IMAP4_SSL) -> list[str]:
    """Return list of folder names worth scanning for OTP mail."""
    typ, data = M.list()
    if typ != "OK":
        return ["INBOX"]
    out = []
    # LIST line: (\flags...) "delim" name   — name may be bare or quoted
    line_re = re.compile(r'^\(.*?\)\s+(?:"[^"]*"|NIL)\s+(?:"([^"]+)"|(\S+))\s*$')
    for raw in data:
        line = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
        m = line_re.match(line)
        if not m:
            continue
        name = m.group(1) or m.group(2)
        if not name:
            continue
        low = name.lower()
        if low == "inbox" or "junk" in low or "spam" in low:
            out.append(name)
    if "INBOX" not in out:
        out.insert(0, "INBOX")
    return out


def _extract_otp_from_msg(msg: Message) -> str | None:
    # Try subject first (sometimes the code is in subject), then plain, then html.
    candidates = []
    subj = msg.get("Subject") or ""
    try:
        # decode RFC2047 (UTF-8 base64 subjects etc.)
        decoded = "".join(
            part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
            for part, enc in email.header.decode_header(subj)
        )
    except Exception:
        decoded = subj
    candidates.append(decoded)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    candidates.append(
                        part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
                    )
                except Exception:
                    pass
    else:
        try:
            candidates.append(
                msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", errors="ignore"
                )
            )
        except Exception:
            pass
    for text in candidates:
        m = OTP_RE.search(text)
        if m:
            return m.group(1)
    return None


def imap_get_otp(imap_cfg: dict, mailbox: str, password: str,
                  since_epoch: float, timeout_s: int = 90) -> str:
    """Poll IMAP across INBOX/Junk/spam until an OpenAI OTP email arrives."""
    log(f"IMAP poll for OTP, timeout {timeout_s}s ...")
    host = imap_cfg["host"]
    port = imap_cfg.get("port", 993)
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port)
            M.login(mailbox, password)
            folders = _list_folders(M)
            for folder in folders:
                try:
                    M.select(folder, readonly=False)
                except Exception:
                    continue
                # All messages (don't trust UNSEEN flag — webmail / prior automation may have read it)
                typ, data = M.search(None, "ALL")
                if typ != "OK":
                    continue
                ids = data[0].split()
                # newest first
                for mid in reversed(ids[-30:]):  # last 30 in folder is plenty
                    _, msg_data = M.fetch(mid, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue
                    msg: Message = email.message_from_bytes(msg_data[0][1])
                    sender = (msg.get("From") or "").lower()
                    if not any(s in sender for s in OPENAI_OTP_SENDERS):
                        continue
                    date_hdr = msg.get("Date")
                    try:
                        msg_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                    except Exception:
                        msg_ts = 0
                    if msg_ts + 5 < since_epoch:
                        continue  # predates our login attempt
                    code = _extract_otp_from_msg(msg)
                    if code:
                        log(f"OTP found in {folder}: {code} (from {sender})")
                        M.logout()
                        return code
            M.logout()
        except Exception as e:
            log(f"IMAP error: {e}; retrying")
        time.sleep(3)

    raise TimeoutError(f"no OpenAI OTP within {timeout_s}s for {mailbox}")


def _snap(page, debug: bool, label: str) -> None:
    if not debug:
        return
    try:
        path = ROOT / f"debug_{label}.png"
        page.screenshot(path=str(path), full_page=True)
        html_path = ROOT / f"debug_{label}.html"
        html_path.write_text(page.content())
        log(f"snapshot saved: {path.name}, url={page.url}")
    except Exception as e:
        log(f"snapshot failed ({label}): {e}")


def _find_otp_input(page):
    """Return (selector_string, count) or (None, 0)."""
    for sel in (
        "input[autocomplete=one-time-code]",
        "input[name=code]",
        "input[name='otp[]']",
        "input[inputmode=numeric]",
    ):
        cnt = page.locator(sel).count()
        if cnt > 0:
            return sel, cnt
    return None, 0


def drive_auth(auth_url: str, email_addr: str, password: str,
               imap_cfg: dict, mailbox: str, mailbox_pw: str,
               headed: bool, debug: bool) -> None:
    """Run Camoufox, walk through OpenAI login screens."""
    started_at = time.time()
    log(f"launching Camoufox headed={headed}")
    with Camoufox(
        headless=not headed,
        humanize=True,
        locale=["cs-CZ", "en-US"],
        geoip=True,
        os=("linux",),
    ) as browser:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        # only log warn/error from page, not the firehose
        page.on("pageerror", lambda e: log(f"[pageerror] {e}"))

        try:
            log("navigating to auth.openai.com")
            page.goto(auth_url, wait_until="domcontentloaded")
            _snap(page, debug, "01_landing")

            # ---- STEP 1: email ----
            log("step 1: filling email")
            email_sel = "input[name=email], input#email-input, input[type=email]"
            page.wait_for_selector(email_sel, timeout=25_000)
            time.sleep(0.7)
            page.fill(email_sel, email_addr, timeout=10_000)
            _snap(page, debug, "02_email_typed")
            page.press(email_sel, "Enter")
            log("submitted email -> waiting for password page or OTP page")

            # wait for any of: password input, OTP input, or callback redirect
            try:
                page.wait_for_function(
                    """() => {
                        if (location.href.includes('localhost:1455')) return true;
                        if (document.querySelector('input[type=password]')) return true;
                        if (document.querySelector('input[autocomplete=one-time-code]')) return true;
                        if (document.querySelector('input[name=code]')) return true;
                        return false;
                    }""",
                    timeout=25_000,
                )
            except Exception as e:
                log(f"timeout waiting for next-page selector after email: {e}")
            time.sleep(0.5)
            _snap(page, debug, "03_after_email_submit")

            # ---- STEP 2: password OR direct OTP ----
            current_url = page.url
            log(f"after email submit, url={current_url}")

            pwd_sel = "input[name=password], input#password, input[type=password]"
            if page.locator(pwd_sel).count() > 0:
                log("step 2: filling password")
                page.fill(pwd_sel, password, timeout=10_000)
                # verify it actually entered
                val = page.eval_on_selector(pwd_sel, "el => el.value")
                log(f"password field value length: {len(val) if val else 0}")
                _snap(page, debug, "04_password_typed")
                page.press(pwd_sel, "Enter")
                log("submitted password -> waiting for OTP/redirect")
                try:
                    page.wait_for_function(
                        """() => {
                            if (location.href.includes('localhost:1455')) return true;
                            if (document.querySelector('input[autocomplete=one-time-code]')) return true;
                            if (document.querySelector('input[name=code]')) return true;
                            if (location.href.includes('email-verification')) return true;
                            if (location.href.includes('mfa')) return true;
                            return false;
                        }""",
                        timeout=20_000,
                    )
                except Exception as e:
                    log(f"timeout waiting after password: {e}")
                time.sleep(1)
            else:
                log("no password field on this step")

            _snap(page, debug, "05_after_password_submit")
            current_url = page.url
            log(f"after password submit, url={current_url}")

            # ---- STEP 3: OTP (if shown) ----
            otp_sel, otp_count = _find_otp_input(page)
            if otp_sel:
                log(f"OTP screen detected: {otp_count} input(s) matching '{otp_sel}'")
                # Poll IMAP. If first 70s pass with nothing, try clicking "Resend code".
                code = None
                attempts = 0
                last_err: Exception | None = None
                while code is None and attempts < 3:
                    attempts += 1
                    try:
                        code = imap_get_otp(
                            imap_cfg, mailbox, mailbox_pw, started_at,
                            timeout_s=70 if attempts == 1 else 60,
                        )
                    except TimeoutError as e:
                        last_err = e
                        log(f"OTP not received yet ({e}); trying 'Resend code' button")
                        resent = False
                        for name_re in (r"resend", r"poslat", r"znovu"):
                            try:
                                btn = page.get_by_role(
                                    "button", name=re.compile(name_re, re.I)
                                ).first
                                if btn.is_visible(timeout=1500):
                                    btn.click(force=True)
                                    log(f"clicked resend button matching '{name_re}'")
                                    resent = True
                                    started_at = time.time()
                                    break
                            except Exception:
                                continue
                        if not resent:
                            # try link role too
                            try:
                                link = page.get_by_role(
                                    "link", name=re.compile(r"resend|poslat|znovu", re.I)
                                ).first
                                if link.is_visible(timeout=1500):
                                    link.click(force=True)
                                    log("clicked resend link")
                                    resent = True
                                    started_at = time.time()
                            except Exception:
                                pass
                        if not resent:
                            log("no resend button found, giving up")
                            raise
                if code is None:
                    raise TimeoutError(
                        f"OTP never arrived after {attempts} attempts: {last_err}"
                    )
                inputs = page.locator(otp_sel)
                if otp_count >= 6:
                    for i, ch in enumerate(code):
                        inputs.nth(i).fill(ch)
                else:
                    inputs.first.fill(code)
                _snap(page, debug, "06_otp_typed")
                try:
                    page.press(otp_sel, "Enter")
                except Exception:
                    pass
                time.sleep(2)
                _snap(page, debug, "07_after_otp")
            else:
                log("no OTP screen visible")

            # ---- STEP 4: device verify / consent interstitials ----
            # OpenAI sometimes shows "Authorize" / "Continue" / "Pokračovat" (CZ) before redirecting.
            # Keep clicking primary buttons until URL becomes localhost callback or deadline.
            deadline = time.time() + 45
            interstitial_names = (
                r"^pokra",           # Pokračovat (CZ)
                r"^authorize",
                r"^continue",
                r"^allow",
                r"^accept",
                r"^povolit",         # CZ "allow"
                r"^souhlas",         # CZ "agree"
            )
            cancel_names = (r"^cancel$", r"^zrušit$", r"^zrusit$")
            while time.time() < deadline:
                if "localhost:1455" in page.url:
                    break
                clicked = False
                for name_re in interstitial_names:
                    try:
                        btn = page.get_by_role(
                            "button", name=re.compile(name_re, re.I)
                        ).first
                        if btn.is_visible(timeout=1500):
                            # make sure we're not about to click Cancel/Zrušit
                            txt = (btn.inner_text() or "").strip().lower()
                            if any(re.search(c, txt) for c in cancel_names):
                                continue
                            log(f"clicking interstitial button '{txt}' (match {name_re})")
                            btn.click()
                            clicked = True
                            _snap(page, debug, f"08_interstitial_{int(time.time())}")
                            time.sleep(2)
                            break
                    except Exception:
                        continue
                if not clicked:
                    time.sleep(1.5)

            # ---- STEP 5: wait for callback redirect ----
            # codex's local server first sees /auth/callback?code=... then redirects
            # the browser to /success?id_token=... — either URL on localhost:1455
            # means the token exchange completed.
            log("waiting for redirect to localhost:1455 (codex callback)")
            try:
                page.wait_for_url(
                    re.compile(r"https?://localhost:1455/.*"),
                    timeout=30_000,
                )
                log(f"localhost callback hit (url={page.url[:80]}...)")
                _snap(page, debug, "09_callback_hit")
            except Exception:
                _snap(page, debug, "99_redirect_failed")
                log(f"redirect did not happen; final URL: {page.url[:120]}")
                raise

            time.sleep(3)  # codex finalizes
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def is_logged_in(codex_home: Path) -> bool:
    """Quick check: auth.json exists AND `codex login status` returns 0."""
    auth_json = codex_home / "auth.json"
    if not auth_json.exists():
        return False
    env = os.environ.copy()
    env["BROWSER"] = "true"
    env["CODEX_HOME"] = str(codex_home)
    try:
        r = subprocess.run(
            ["codex", "login", "status"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def login_one(account_key: str, accounts: dict, headed: bool, debug: bool) -> bool:
    """Run a full login flow for one account. Returns True on success."""
    email_addr, password, imap_user, imap_password, imap_cfg = pick_account(
        account_key, accounts
    )
    HOMES_DIR.mkdir(exist_ok=True)
    codex_home = HOMES_DIR / account_key
    proc, auth_url = spawn_codex_login(codex_home)
    try:
        drive_auth(
            auth_url=auth_url,
            email_addr=email_addr,
            password=password,
            imap_cfg=imap_cfg,
            mailbox=imap_user,
            mailbox_pw=imap_password,
            headed=headed,
            debug=debug,
        )
        log("waiting for codex login subprocess to exit")
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            log("codex did not exit in 20s, killing")
            proc.kill()
            rc = -1
        log(f"codex login exit code: {rc}")
        auth_json = codex_home / "auth.json"
        if auth_json.exists():
            log(f"SUCCESS: {auth_json} written ({auth_json.stat().st_size} bytes)")
            return True
        log("FAILED: auth.json missing")
        return False
    except Exception as e:
        # Camoufox may raise on a navigation we no longer care about
        # (e.g. codex already wrote auth.json before the browser finished
        # rendering /success). Re-check the file before reporting failure.
        log(f"camoufox/drive_auth raised: {e}")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        auth_json = codex_home / "auth.json"
        if auth_json.exists():
            log(f"SUCCESS (post-exception): {auth_json} written "
                f"({auth_json.stat().st_size} bytes)")
            return True
        log("FAILED: auth.json missing after exception")
        return False
    finally:
        if proc.poll() is None:
            proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("account", nargs="?",
                    help="account key from accounts.json (e.g. alice). "
                         "Required unless --all is given.")
    ap.add_argument("--all", action="store_true",
                    help="log in every account from accounts.json sequentially")
    ap.add_argument("--skip-logged-in", action="store_true", default=True,
                    help="(default) skip accounts where `codex login status` already passes")
    ap.add_argument("--force", action="store_true",
                    help="re-login even if already logged in (cancels --skip-logged-in)")
    ap.add_argument("--cooldown", type=int, default=30,
                    help="seconds to sleep between accounts in --all mode (default: 30)")
    ap.add_argument("--headed", action="store_true",
                    help="run Camoufox visible (for debugging)")
    ap.add_argument("--debug", action="store_true",
                    help="dump screenshots/HTML on every step")
    args = ap.parse_args()

    accounts = load_accounts()

    if args.all:
        keys = list(accounts["accounts"].keys())
        log(f"--all mode: {len(keys)} accounts -> {keys}")
        results: dict[str, str] = {}
        for i, key in enumerate(keys):
            log(f"\n========== [{i+1}/{len(keys)}] {key} ==========")
            codex_home = HOMES_DIR / key
            if not args.force and args.skip_logged_in and is_logged_in(codex_home):
                log(f"skip: {key} already logged in")
                results[key] = "skipped"
                continue
            try:
                ok = login_one(key, accounts, headed=args.headed, debug=args.debug)
                results[key] = "ok" if ok else "failed"
            except Exception as e:
                log(f"{key} crashed: {e}")
                results[key] = f"crashed:{e}"
            # cooldown so OpenAI doesn't flag 7 logins in 2 minutes from one IP
            if i < len(keys) - 1 and args.cooldown > 0:
                log(f"cooldown {args.cooldown}s before next account")
                time.sleep(args.cooldown)

        # summary
        log("\n========== SUMMARY ==========")
        for k, v in results.items():
            log(f"  {k:<14} {v}")
        n_ok = sum(1 for v in results.values() if v in ("ok", "skipped"))
        log(f"\n{n_ok}/{len(keys)} accounts ready")
        sys.exit(0 if n_ok == len(keys) else 1)

    if not args.account:
        ap.error("account is required (or use --all)")
    if not args.force and args.skip_logged_in:
        codex_home = HOMES_DIR / args.account
        if is_logged_in(codex_home):
            log(f"{args.account} already logged in (use --force to re-login)")
            sys.exit(0)

    ok = login_one(args.account, accounts, headed=args.headed, debug=args.debug)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
