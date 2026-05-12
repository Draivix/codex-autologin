# codex-autologin

> Headless multi-account login for the official OpenAI Codex CLI.
> One toolkit, many ChatGPT accounts, zero clicks, no leaked OTPs.

`codex-autologin` automates the browser part of `codex login` so you stop
re-typing email + password + 6-digit codes every time the access token
expires. It runs a stealth Firefox (Camoufox), reads the OpenAI OTP straight
from your IMAP inbox, and lets the official `codex` binary write its own
`auth.json` — so refresh tokens, plan limits, and `codex login status` keep
working exactly as upstream intends.

Each account lives in its own directory. Switching between accounts is a
single environment variable.

## Why this exists

The OpenAI Codex CLI shipped a clean OAuth + PKCE login flow, but using it
across multiple ChatGPT Pro / Business / Enterprise seats is painful:

- Every `codex login` opens your real desktop browser (via `xdg-open` on
  Linux) and logs *that* browser's session into Codex.
- OTP codes arrive by email and have to be retyped by hand.
- There is no built-in "switch account" command.
- There is no built-in quota / token-window inspector.

This repo solves all four with about 500 lines of Python.

## Features

- **Headless login** for any ChatGPT account that uses email + password +
  email OTP. Works against the public `auth.openai.com` PKCE flow used by
  Codex CLI ≥ 0.130.
- **IMAP-driven OTP retrieval** across `INBOX`, `Junk` and `spam` folders.
  Codes are extracted from the email subject or HTML body. If the OTP
  doesn't arrive within ~70 seconds, the script clicks the OAuth "Resend
  code" button and tries again (3 rounds total).
- **Per-account isolation** via `$CODEX_HOME`. Refresh tokens, history,
  cached state — all separated.
- **Quota dashboard** that calls Codex's own usage endpoint
  (`https://chatgpt.com/backend-api/wham/usage`) and prints plan, primary
  (5h) + secondary (7d) rate-limit windows, and credits balance, per
  account.
- **Czech-friendly**. The login UI on `auth.openai.com` follows the
  browser locale; this toolkit handles both English (`Continue`,
  `Resend code`) and Czech (`Pokračovat`, `znovu`) button labels out of
  the box. Adding more languages is a one-line regex change.
- **Camoufox stealth profile**. Login fingerprints look like a normal
  Czech Firefox user, not a Playwright bot. GeoIP DB is pulled
  automatically.

## What's inside

| File | Purpose |
| ---- | ------- |
| `codex_autologin.py` | The autologin. Spawns `codex login`, drives `auth.openai.com` with Camoufox, fetches the OTP via IMAP, lets codex write `auth.json`. |
| `codex_status.py` | CLI: calls `/backend-api/wham/usage` for every logged-in account; prints plan + 5h/7d rate-limit windows + credits as a table. |
| `status_ui.py` | Flask web dashboard for the same data. Auto-refreshing color-coded bars, served at `http://127.0.0.1:8765`. |
| `auth_api.py` | Flask API that distributes `auth.json` files to authorized clients over HTTPS (front with nginx). Bearer-token auth, per-key account scoping, rate limit, append-only audit log. |
| `apikey_admin.py` | CLI for the API: `generate`, `list`, `revoke`, `rotate`, `delete` keys. Stores only SHA-256 hashes; plaintext shown once at mint. |
| `client_fetch.py` | Client side of the API: downloads `auth.json` from a remote `auth_api` server into a local `$CODEX_HOME`. |
| `nginx.example.conf` | Reference nginx config: TLS termination, security headers, rate-limit zone, request-line-only access log (no Authorization leakage). |
| `codex-auth-api.service` | Hardened systemd unit: `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryDenyWriteExecute`, syscall filter. |
| `codex_as` | Tiny shell wrapper: `./codex_as <account> [codex args]` runs codex with the right `CODEX_HOME` and `BROWSER=true`. |
| `accounts.example.json` | Template — copy to `accounts.json`, chmod 600, fill in real values. |
| `homes/<account>/auth.json` | Per-account state, created by codex itself. **Never** committed. |

## Requirements

- Python 3.10+
- The official `codex` CLI on `$PATH` (`npm i -g @openai/codex` or
  download a release binary; tested against `codex-cli 0.130`).
- An IMAP mailbox you control. The script logs in over IMAP and reads OTPs
  from it — so the script must be able to authenticate to it directly, no
  webmail.
- Linux/macOS. The `BROWSER=true` trick (see "Footguns" below) is
  Linux-specific; the rest is portable.

## Install (dev / local workstation)

```bash
git clone https://github.com/Draivix/codex-autologin.git
cd codex-autologin

pip install "camoufox[geoip]" flask waitress
python3 -m camoufox fetch           # downloads patched Firefox + GeoIP

cp accounts.example.json accounts.json
chmod 600 accounts.json
$EDITOR accounts.json               # fill in real credentials
```

Then jump to [Usage](#usage). For a real server deployment see
[Production deployment](#production-deployment) below.

## `accounts.json` schema

```jsonc
{
  "imap": {
    "host": "mail.example.com",     // IMAP server (TLS on 993)
    "port": 993,
    "ssl": true
  },
  "accounts": {
    "alice": {                       // <- account key used on the CLI
      "email": "alice@example.com",  // OpenAI / ChatGPT login email
      "password": "openai-password", // OpenAI / ChatGPT password

      // OPTIONAL — only set these when the IMAP mailbox uses different
      // credentials than the OpenAI account. If omitted, the IMAP login
      // reuses `email` / `password`.
      "imap_user":     "alice@example.com",
      "imap_password": "different-imap-password"
    },
    "bob": {
      "email": "bob@example.com",
      "password": "openai-password"
    }
  }
}
```

The file is loaded as-is, so any extra keys (e.g. `"_comment_"`) are
silently ignored.

> **Never commit `accounts.json`.** It is excluded by `.gitignore` but you
> are still responsible for not pasting it into an issue, prompt, or PR.

## Usage

### Log into one account

```bash
python3 codex_autologin.py alice                 # headless
python3 codex_autologin.py alice --headed        # watch Camoufox
python3 codex_autologin.py alice --headed --debug # + per-step screenshots
```

What happens, in order:

1. Spawns `BROWSER=true CODEX_HOME=./homes/alice codex login` and grabs the
   OAuth URL it prints to stderr.
2. Launches Camoufox, navigates to that URL on `auth.openai.com`.
3. Fills email → Enter, password → Enter.
4. On the `email-verification` page, polls IMAP across `INBOX`, `Junk`,
   `spam` for mail from `noreply@tm.openai.com` or `otp@tm1.openai.com`.
   Extracts the 6-digit code from the subject or HTML body, types it in.
5. If no OTP arrives in ~70 s, clicks the on-page "Resend code" /
   "Pokračovat" / `znovu` button and polls again (up to 3 rounds).
6. On the codex consent page (`/sign-in-with-chatgpt/codex/consent`)
   clicks **Continue** / **Pokračovat**.
7. The browser is redirected to `http://localhost:1455/auth/callback?code=…`.
   The codex login server exchanges that code for tokens and writes
   `auth.json` into `$CODEX_HOME` (i.e. `./homes/alice/auth.json`).
8. We confirm `auth.json` was written.

Typical end-to-end time: 20–30 s when the OTP lands in <10 s, up to ~90 s
when it has to be resent.

### Log into all accounts in one go

```bash
python3 codex_autologin.py --all                # bootstrap everything
python3 codex_autologin.py --all --cooldown 60  # gentler between accounts
python3 codex_autologin.py --all --force        # ignore existing auth.json
```

`--all` iterates every entry in `accounts.json` sequentially. Accounts that
already pass `codex login status` are skipped unless you pass `--force`.
Between accounts the script sleeps `--cooldown` seconds (default 30) so
OpenAI does not flag seven consecutive logins from one IP as suspicious.

A summary is printed at the end:

```
========== SUMMARY ==========
  alice          ok
  bob            skipped
  carol          ok
  dan            failed
4/4 accounts ready
```

Exit status is 0 only if every account is `ok` or `skipped`.

Login flows run sequentially because codex's local OAuth callback server
binds a fixed port (1455). After bootstrap, *using* the accounts in
parallel is fine — see "How multi-account works".

### Use a specific account

```bash
./codex_as alice                       # interactive codex as alice
./codex_as alice exec "fix the bug"    # one-shot prompt
./codex_as alice login status          # confirm logged in
./codex_as alice logout                # drop tokens for that account
```

The wrapper is literally one line:
`env BROWSER=true CODEX_HOME=./homes/<account> codex "$@"`.

### Check quota usage

```bash
$ python3 codex_status.py
account  plan       email                 5h window               7d window               credits
-------- ---------- --------------------- ----------------------- ----------------------- -------
alice    pro        alice@example.com     12.3% (reset in 4h21m)  46.0% (reset in 5d12h)  $0
bob      pro        bob@example.com        0.0% (reset in 5h00m)  22.0% (reset in 6h18m)  $0
```

For machine-readable output:

```bash
python3 codex_status.py --json | jq '.alice.rate_limit'
python3 codex_status.py --json | jq '.alice.additional_rate_limits'
```

### Web dashboard

For a permanently-open browser tab with auto-refresh:

```bash
pip install flask
python3 status_ui.py                 # http://127.0.0.1:8765
python3 status_ui.py --port 9876     # pick a port
python3 status_ui.py --host 0.0.0.0  # bind on all interfaces (lan-visible)
```

The page renders one row per account with color-coded usage bars (green
< 50 %, amber < 80 %, red ≥ 80 %), a badge if the account hit its limit,
and the per-model `additional_rate_limits` (e.g. `GPT-5.3-Codex-Spark`)
under each account. It refreshes itself every 60 seconds and caches the
upstream call for 15 s so OpenAI is not hammered.

`GET /api/status` returns the same data as JSON for programmatic use.

## Distributing `auth.json` to remote clients (the API)

Once your accounts are logged in, you usually don't want every dev machine
re-running the browser flow. `auth_api.py` is a small Flask service that
hands out the existing `homes/<account>/auth.json` files to authorized
clients over HTTPS.

### Threat model & design

- The server binds **127.0.0.1 only**. TLS terminates at nginx (see
  `nginx.example.conf`). The Python service never speaks plaintext to the
  internet.
- Bearer-token authentication via `Authorization: Bearer ck_live_<32 bytes>`.
  Tokens are 32-byte random; the server stores only SHA-256 hashes.
- **Per-key account scoping**: every key has a `scope` list of account
  names. Scope `["*"]` is admin (all accounts + can trigger re-logins).
- Token-bucket **rate limit** per key (default 30 req/min, tunable).
- Optional **IP allowlist** (CIDR list) per key.
- Optional **`max_uses`** for one-shot or finite-use keys.
- **Append-only JSONL audit log** (`audit.log`) of every request: timestamp,
  IP, key_id, account, action, status. Failures are logged too.
- Scope misses return **404** (not 403) so a key cannot enumerate
  account names it does not own.
- `Cache-Control: no-store`, HSTS, `X-Content-Type-Options`, `Referrer-Policy`
  set on every response.

### Endpoints

| Method   | Path                                       | Auth   | Description |
| -------- | ------------------------------------------ | ------ | ----------- |
| `GET`    | `/v1/health`                               | none   | liveness probe |
| `GET`    | `/v1/accounts`                             | bearer | list accounts the key can access |
| `GET`    | `/v1/accounts/<name>`                      | bearer | account metadata (no secrets) — file presence, size, plan, JWT email, in-config flag |
| `POST`   | `/v1/accounts`                             | admin  | create an account in `accounts.json` (body: `name`, `email`, `password`, optional `imap_user` / `imap_password`, optional `login:true` to immediately start the browser flow) |
| `PATCH`  | `/v1/accounts/<name>`                      | admin  | update fields on an existing account (any of `email`, `password`, `imap_user`, `imap_password`) |
| `DELETE` | `/v1/accounts/<name>`                      | admin  | remove from `accounts.json` AND `rm -rf homes/<name>` |
| `GET`    | `/v1/accounts/<name>/status`               | bearer | live `/wham/usage` for that account |
| `GET`    | `/v1/accounts/<name>/auth`                 | bearer | **the auth.json** (the actual secret) |
| `DELETE` | `/v1/accounts/<name>/auth`                 | admin  | local "logout" — delete the auth.json file only |
| `POST`   | `/v1/accounts/<name>/reconnect`            | admin  | spawn `codex_autologin.py --force` in the background |
| `POST`   | `/v1/accounts/<name>/refresh`              | admin  | alias for `/reconnect` (kept for compatibility) |
| `GET`    | `/v1/accounts/<name>/reconnect-status`     | admin  | poll `idle` / `running` / `ok` / `failed` plus a 20-line tail of the autologin log |
| `GET`    | `/v1/keys`                                 | admin  | list every API key (label, scope, rate limit, use count, revoked flag — never the plaintext) |
| `POST`   | `/v1/keys`                                 | admin  | mint a new key. **Plaintext returned exactly once.** Body: `label`, `scope` (list, `["*"]` for admin), optional `rate_limit`, `ip_allowlist`, `max_uses` |
| `GET`    | `/v1/keys/<key_id>`                        | admin  | metadata for one key (still no plaintext) |
| `PATCH`  | `/v1/keys/<key_id>`                        | admin  | update `label` / `scope` / `rate_limit` / `ip_allowlist` / `max_uses` / `revoked` |
| `DELETE` | `/v1/keys/<key_id>`                        | admin  | delete a key permanently (the API refuses to delete the key currently being used to authenticate the call) |
| `GET`    | `/v1/imap`                                 | admin  | read the IMAP config currently in use |
| `PUT`    | `/v1/imap`                                 | admin  | overwrite the IMAP config (`host`, `port`, `ssl`) |
| `GET`    | `/v1/audit?limit=N`                        | admin  | tail the audit log (default 100, max 1000 entries) as JSON |

"admin" means the bearer key's `scope` is `["*"]`. Every admin call is
recorded in `audit.log` with the calling `key_id`.

### Setup on the central server

```bash
pip install flask waitress

# 1. mint keys
python3 apikey_admin.py generate --label admin --scope '*'
python3 apikey_admin.py generate --label dev1 --scope alice,carol \
        --rate-limit 10 --ip-allowlist 10.0.0.0/8

# both commands print the plaintext key ONCE — copy it to the client now.

# 2. start the API (waitress in production)
python3 auth_api.py --port 8788 --behind-proxy

# 3. front with nginx + Let's Encrypt
sudo cp nginx.example.conf /etc/nginx/sites-available/codex-auth-api
sudo ln -s /etc/nginx/sites-available/codex-auth-api /etc/nginx/sites-enabled/
sudo certbot --nginx -d api.example.com
sudo nginx -t && sudo systemctl reload nginx

# 4. (optional) run under systemd
sudo cp codex-auth-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-auth-api
```

### Using a key from a client machine

```bash
export CODEX_API_BASE=https://api.example.com
export CODEX_API_KEY=ck_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# one account
python3 client_fetch.py alice --dest ~/.codex-alice/auth.json
BROWSER=true CODEX_HOME=~/.codex-alice codex

# every account this key can see
python3 client_fetch.py --all --root ~/codex-fleet
BROWSER=true CODEX_HOME=~/codex-fleet/alice codex
```

### Key management

```bash
python3 apikey_admin.py list                  # show all keys + use counts
python3 apikey_admin.py revoke <key_id>       # mark revoked (still in store)
python3 apikey_admin.py rotate <key_id>       # new value, same scope
python3 apikey_admin.py delete <key_id>       # remove permanently
```

A revoked or deleted key returns `401` on the next request and is logged.

### Hardening to consider

- Run the systemd unit as a dedicated `codex` user with only read access to
  `homes/` and write access to `audit.log` + `api_keys.json` (see
  `codex-auth-api.service`).
- Add `fail2ban` rules on repeated `401` lines in `/var/log/nginx/codex-api.log`.
- Require **mTLS** (`ssl_client_certificate` + `ssl_verify_client on` in
  nginx) for the highest-sensitivity tier — pin client certs per laptop.
- Hold `audit.log` on a write-once mount (e.g. a remote syslog sink).
- Rotate keys periodically (`apikey_admin.py rotate`) — the rotation also
  resets `use_count` so finite-use semantics still hold.
- Consider an outbound firewall rule that only permits the host to reach
  `chatgpt.com` and `auth.openai.com`; the `/status` endpoint needs that,
  the `/auth` endpoint does not.

The endpoint queried is `GET https://chatgpt.com/backend-api/wham/usage`,
authenticated with the account's `access_token` and a `chatgpt-account-id`
header. The response includes `plan_type`, `rate_limit.primary_window`
(5 h rolling), `rate_limit.secondary_window` (7 d rolling),
`additional_rate_limits` (per model, e.g. the `GPT-5.3-Codex-Spark` /
`codex_bengalfox` feature) and `credits.balance`.

## Production deployment

End-to-end guide for putting `codex-autologin` on a Linux server with a real
TLS hostname (here: `codexapi.draivix.com`). The server fetches OTPs from an
IMAP mailbox you control, holds all `auth.json` files, and serves them to
authenticated clients over HTTPS.

### 0. Server prerequisites

- Ubuntu 22.04 / 24.04 or Debian 12 (other distros work; commands assume apt).
- A hostname pointing to the server's IPv4 (and ideally IPv6) — see [DNS
  setup](#dns-setup) below.
- Open ports `80/tcp` (Let's Encrypt http-01 challenge) and `443/tcp` (TLS).
- Server must reach the public internet — Camoufox talks to
  `auth.openai.com`, OTP polling talks to your IMAP host, and the `/status`
  endpoint talks to `chatgpt.com/backend-api`.

```bash
sudo apt update
sudo apt install -y python3 python3-pip git nginx curl jq \
                    libgtk-3-0 libxt6 libdbus-glib-1-2 \
                    fonts-liberation libasound2
# Camoufox is patched Firefox; the libs above let its headless mode launch
# even on a server with no Xorg.
```

### 1. Service user + repo

```bash
sudo useradd --system --create-home --shell /bin/bash codex
sudo -u codex git clone https://github.com/Draivix/codex-autologin.git /srv/codex-autologin
sudo chown -R codex:codex /srv/codex-autologin
```

### 2. Python deps + Camoufox browser

```bash
sudo -u codex pip install --user "camoufox[geoip]" flask waitress
sudo -u codex python3 -m camoufox fetch    # ~120 MB download
```

### 3. Install the official codex CLI

```bash
# Node-based release (recommended — same channel OpenAI publishes):
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs
sudo npm install -g @openai/codex
codex --version            # should print codex-cli 0.130+ as `codex`
```

(Standalone Rust binaries also work; just make sure `codex` is on
`$PATH` for the `codex` user.)

### 4. Configure accounts

```bash
sudo -u codex cp /srv/codex-autologin/accounts.example.json \
                 /srv/codex-autologin/accounts.json
sudo chmod 600 /srv/codex-autologin/accounts.json
sudo -u codex $EDITOR /srv/codex-autologin/accounts.json
```

Fill in your real IMAP host and one entry per ChatGPT account. See the
[`accounts.json` schema](#accountsjson-schema) section above for the field
reference.

### 5. Mint the first admin API key

```bash
cd /srv/codex-autologin
sudo -u codex python3 apikey_admin.py generate --label admin --scope '*'
```

**The plaintext key is printed exactly once.** Copy it now into your
password manager — it cannot be recovered, only rotated.

### 6. Bootstrap every account (browser logins)

This is the slow part — each account takes 20–90 s and they run sequentially.

```bash
sudo -u codex python3 /srv/codex-autologin/codex_autologin.py --all
```

Verify with the CLI status dump:

```bash
sudo -u codex python3 /srv/codex-autologin/codex_status.py
```

You should see one row per account with its plan + 5 h / 7 d windows.
`homes/<account>/auth.json` is now a 4–5 KB file with the long-lived
refresh token. Codex refreshes the short-lived access token automatically
in the background.

### 7. DNS setup

Point your hostname at the server. Example for `codexapi.draivix.com` →
`225.davidstrejc.cz` (`94.130.15.46` + `2a01:4f8:10b:1353::2`):

```text
codexapi.draivix.com.   3600   IN  A     94.130.15.46
codexapi.draivix.com.   3600   IN  AAAA  2a01:4f8:10b:1353::2
```

Wait until both records resolve:

```bash
dig +short codexapi.draivix.com A
dig +short codexapi.draivix.com AAAA
```

### 8. nginx + Let's Encrypt

```bash
sudo cp /srv/codex-autologin/nginx.example.conf \
        /etc/nginx/sites-available/codex-auth-api
sudo $EDITOR /etc/nginx/sites-available/codex-auth-api
#   ^ replace `api.example.com` with `codexapi.draivix.com`
sudo ln -s /etc/nginx/sites-available/codex-auth-api \
           /etc/nginx/sites-enabled/codex-auth-api
sudo nginx -t

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d codexapi.draivix.com \
             --redirect --no-eff-email --agree-tos -m you@example.com
# certbot rewrites the server { listen 443 } block with real cert paths
# and sets up auto-renewal under systemd (certbot.timer).

sudo systemctl reload nginx
```

### 9. systemd unit for the API

```bash
sudo cp /srv/codex-autologin/codex-auth-api.service \
        /etc/systemd/system/codex-auth-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now codex-auth-api
sudo systemctl status codex-auth-api --no-pager
```

The unit:
- runs as the unprivileged `codex` user;
- binds only on `127.0.0.1:8788` (nginx is the only ingress);
- uses `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryDenyWriteExecute`,
  a syscall allow-list, and `CapabilityBoundingSet=` (no caps).

### 10. Smoke test from your laptop

```bash
export CODEX_API_BASE=https://codexapi.draivix.com
export CODEX_API_KEY=ck_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

curl -s "$CODEX_API_BASE/v1/health"
# {"status":"ok"}

curl -s -H "Authorization: Bearer $CODEX_API_KEY" \
        "$CODEX_API_BASE/v1/accounts" | jq
# {"accounts": ["alice", "bob", ...]}

python3 client_fetch.py alice --dest ~/.codex-alice/auth.json
BROWSER=true CODEX_HOME=~/.codex-alice codex login status
# Logged in using ChatGPT
```

### 11. Day-2 operations

```bash
# rotate / mint per-developer keys
sudo -u codex python3 apikey_admin.py generate \
     --label "dev1 - alice@example.com" --scope alice,carol \
     --rate-limit 20 --ip-allowlist 10.0.0.0/8

# trigger a re-login via the API (admin key required)
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     https://codexapi.draivix.com/v1/accounts/alice/reconnect

# poll the live progress
curl -H "Authorization: Bearer $ADMIN_KEY" \
     https://codexapi.draivix.com/v1/accounts/alice/reconnect-status | jq

# inspect audit log
sudo tail -f /srv/codex-autologin/audit.log | jq -c

# view systemd / nginx logs
sudo journalctl -u codex-auth-api -f
sudo tail -f /var/log/nginx/codex-api.log
```

### Where things live on disk

```text
/srv/codex-autologin/                   # repo (codex user)
    accounts.json                       # mode 600, IMAP + per-account creds
    api_keys.json                       # mode 600, SHA-256 hashes only
    audit.log                           # append-only JSONL
    homes/<account>/auth.json           # codex's auth files (mode 600)
    reconnect_logs/<account>-<ts>.log   # autologin output per reconnect

/etc/nginx/sites-enabled/codex-auth-api # TLS, security headers, rate limit
/etc/letsencrypt/live/codexapi.draivix.com/{fullchain,privkey}.pem
/etc/systemd/system/codex-auth-api.service
```

### Hardening checklist

- [ ] Run the systemd unit as a dedicated unprivileged user (`codex`).
- [ ] `chmod 600` on `accounts.json`, `api_keys.json`; owned by `codex`.
- [ ] Per-developer API keys with narrow `scope`, not one shared master key.
- [ ] Restrict the admin key to a single trusted IP via `ip_allowlist`.
- [ ] `fail2ban` on repeated 401 lines in `/var/log/nginx/codex-api.log`.
- [ ] Optional: require **mTLS** for the highest-sensitivity tier
      (`ssl_client_certificate` + `ssl_verify_client on` in nginx).
- [ ] Ship `audit.log` to a write-once sink (syslog → loki/papertrail).
- [ ] Rotate every key on a schedule (`apikey_admin.py rotate <key_id>`).
- [ ] Outbound egress firewall: only allow `auth.openai.com`,
      `chatgpt.com`, and your IMAP host from the `codex` user.

## How multi-account works under the hood

The codex CLI reads and writes its credentials to `$CODEX_HOME/auth.json`,
defaulting to `~/.codex`. By giving each account its own directory and
exporting `CODEX_HOME=./homes/<account>` per invocation, codex runs against
isolated state — no global config edits, no symlink swapping, no shell
profile hacks.

You can run any number of accounts in parallel at *runtime* (after they're
all logged in):

```bash
./codex_as alice exec "task A" &
./codex_as bob   exec "task B" &
./codex_as carol exec "task C" &
wait
```

The **login flow itself** must be serialized because the codex login server
listens on a fixed port (1455). Once each account has a valid `auth.json`,
the refresh_token keeps the access_token alive without further interaction.

## Known footguns

- **Never run `codex login` without `BROWSER=true`.** The Rust binary
  calls `webbrowser::open()`, which on Linux invokes `xdg-open`, which
  opens your real desktop Firefox/Chrome and tries to log *that* session
  into Codex. All the wrappers in this repo set `BROWSER=true`. If you
  invoke codex directly, set it yourself.
- **OTP routinely lands in spam.** OpenAI's mail goes to the `spam` (or
  `Junk`) folder on fresh logins from new IPs / fingerprints. The IMAP
  poller looks in all three folders by default. If your provider uses an
  exotic folder name (e.g. `Bulk`, `[Gmail]/Spam`), patch `_list_folders`
  in `codex_autologin.py`.
- **Locale matters.** Camoufox is launched with
  `locale=["cs-CZ", "en-US"]`. The login UI will be rendered in Czech.
  Button-name regexes already cover both Czech and English; adjust if you
  use a different default locale.
- **Single login at a time.** Two `codex_autologin.py` runs in parallel
  will collide on TCP port 1455. Run them back-to-back.
- **`accounts.json` is mode 600**, like an SSH key. It contains every
  password you list in it.
- **OpenAI may break this any week.** This is a brittle UI scraper of a
  third-party login page. Pin a copy of the OAuth login HTML for the
  version you tested against if you want to debug regressions later.

## Architecture diagram

```
+----------------------+        +-------------------------+        +-----------------------+
|  codex_autologin.py  |  spawn |  codex login (rust)     |  HTTP  |   localhost:1455      |
|  (this repo)         +-------->  prints OAuth URL       <--------+   /auth/callback      |
|                      |  read  |  starts local server    |  302   |   (codex listens)     |
+----------+-----------+        +-------------------------+        +-----------+-----------+
           | drive                                                              ^
           v                                                                    | code=…
+----------+-----------+        +-------------------------+                     |
|  Camoufox (Firefox)  | HTTPS  |  auth.openai.com OAuth  +---------------------+
|  email/pwd/OTP       +-------->                          |
+----------+-----------+        +-------------------------+
           ^ OTP code
           |
+----------+-----------+
|  IMAPS poller        |
|  INBOX / Junk / spam |
+----------------------+
                                            tokens persisted:
                                        $CODEX_HOME/auth.json
```

## Roadmap

- [ ] Replace IMAP polling with IMAP IDLE for sub-second OTP latency.
- [ ] `--all` flag to log in every account from `accounts.json` in one go.
- [ ] Dismiss "upgrade to passkey" upsell pages automatically.
- [ ] Pre-warm a single Camoufox instance and reuse across accounts.
- [ ] Cron-friendly daemon mode for the status checker (Prometheus exporter
      / Slack alert when an account approaches 80 % of its 7 d window).
- [ ] Native Windows support (mostly: replace `BROWSER=true` with a Windows
      equivalent that no-ops `cmd /c start`).

## Contributing

Issues and PRs welcome. If something stops working because OpenAI shipped a
new login screen, the most useful first step is `--debug` mode (dumps
`debug_*.png` + `debug_*.html` for every step) and a copy of the
problematic page's HTML.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This tool automates **your own** legitimate ChatGPT / Codex login on
accounts you own and have authorization to use. It does not bypass any
authentication, captcha, or rate limit. It does not scrape ChatGPT content.
It does the same thing a human would do at a browser, faster, headlessly,
and without typing the OTP by hand.

Use of this tool is subject to the
[OpenAI Terms of Service](https://openai.com/policies/terms-of-use). If
your accounts are part of a workspace plan, ask the workspace owner first.
You are responsible for how you use it.
