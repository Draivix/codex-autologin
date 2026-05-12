#!/usr/bin/env python3
"""
Tiny Flask UI for codex-autologin account statuses.

Serves a single dashboard page on http://127.0.0.1:8765 by default.
Shows plan, 5h / 7d usage windows, credits, and last refresh timestamp
for every account whose auth.json exists under ./homes/.

Auto-refreshes every 60s. Click "Refresh" to refetch immediately.

Usage:
    pip install flask
    python3 status_ui.py            # then open http://127.0.0.1:8765
    python3 status_ui.py --port 9000
    python3 status_ui.py --host 0.0.0.0    # bind on all ifaces

Endpoints:
    GET /            HTML dashboard
    GET /api/status  JSON for every account (machine-readable)
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

try:
    from flask import Flask, jsonify, render_template_string
except ImportError:
    sys.exit("flask not installed. run: pip install flask")

ROOT = Path(__file__).resolve().parent
HOMES = ROOT / "homes"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CACHE_TTL_S = 15  # refetch /wham/usage at most once per 15s per account


_cache: dict[str, tuple[float, dict | str]] = {}


def fetch_usage_cached(account: str, auth_json: Path) -> dict | str:
    now = time.time()
    cached = _cache.get(account)
    if cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    try:
        auth = json.loads(auth_json.read_text())
        tok = auth["tokens"]["access_token"]
        acc_id = auth["tokens"].get("account_id") or ""
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "chatgpt-account-id": acc_id,
                "User-Agent": "codex-cli/0.130.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        _cache[account] = (now, data)
        return data
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.reason}"
        _cache[account] = (now, msg)
        return msg
    except Exception as e:
        msg = f"error: {e}"
        _cache[account] = (now, msg)
        return msg


def collect() -> list[dict]:
    rows: list[dict] = []
    if not HOMES.exists():
        return rows
    for d in sorted(HOMES.iterdir()):
        if not d.is_dir():
            continue
        auth = d / "auth.json"
        if not auth.exists():
            rows.append({"account": d.name, "state": "no auth.json"})
            continue
        data = fetch_usage_cached(d.name, auth)
        if isinstance(data, str):
            rows.append({"account": d.name, "state": data})
            continue
        rl = data.get("rate_limit") or {}
        primary = rl.get("primary_window") or {}
        secondary = rl.get("secondary_window") or {}
        credits = data.get("credits") or {}
        rows.append({
            "account": d.name,
            "state": "ok",
            "email": data.get("email"),
            "plan": data.get("plan_type"),
            "primary_used": primary.get("used_percent"),
            "primary_reset_at": primary.get("reset_at"),
            "primary_window_s": primary.get("limit_window_seconds"),
            "secondary_used": secondary.get("used_percent"),
            "secondary_reset_at": secondary.get("reset_at"),
            "secondary_window_s": secondary.get("limit_window_seconds"),
            "credits_balance": credits.get("balance"),
            "credits_unlimited": credits.get("unlimited"),
            "limit_reached": rl.get("limit_reached"),
            "additional": data.get("additional_rate_limits") or [],
        })
    return rows


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>codex-autologin · status</title>
  <meta http-equiv="refresh" content="60">
  <style>
    :root {
      --bg:#0f1115; --panel:#171a21; --row:#1d2129; --border:#2a2f3a;
      --text:#e6e8ec; --muted:#8a90a0; --good:#3fb950; --warn:#d29922;
      --bad:#f85149; --accent:#6cb6ff;
    }
    *{box-sizing:border-box}
    html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
              font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
              Roboto,Helvetica,Arial,sans-serif;font-size:14px}
    header{display:flex;align-items:baseline;justify-content:space-between;
           padding:18px 28px;border-bottom:1px solid var(--border);
           background:var(--panel)}
    h1{margin:0;font-size:18px;font-weight:600;letter-spacing:.3px}
    h1 .dim{color:var(--muted);font-weight:400}
    .meta{color:var(--muted);font-size:12px}
    .meta button{margin-left:10px;background:transparent;border:1px solid var(--border);
                 color:var(--text);padding:4px 10px;border-radius:4px;cursor:pointer}
    .meta button:hover{border-color:var(--accent);color:var(--accent)}
    main{padding:20px 28px}
    table{width:100%;border-collapse:collapse;background:var(--panel);
          border:1px solid var(--border);border-radius:6px;overflow:hidden}
    th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);
          vertical-align:middle}
    th{background:var(--row);color:var(--muted);font-weight:500;font-size:12px;
       text-transform:uppercase;letter-spacing:.5px}
    tr:last-child td{border-bottom:none}
    .acct{font-weight:600}
    .email{color:var(--muted);font-size:12px}
    .plan{display:inline-block;padding:2px 8px;border-radius:10px;
          background:var(--row);font-size:11px;text-transform:uppercase;
          color:var(--accent);font-weight:600}
    .plan.pro,.plan.business,.plan.enterprise{background:rgba(108,182,255,.12)}
    .bar{display:block;height:14px;width:100%;background:var(--row);
         border-radius:3px;overflow:hidden;margin-top:4px}
    .bar > span{display:block;height:100%}
    .bar > span.lo{background:var(--good)}
    .bar > span.mid{background:var(--warn)}
    .bar > span.hi{background:var(--bad)}
    .pct{font-variant-numeric:tabular-nums;color:var(--text);font-size:13px}
    .reset{color:var(--muted);font-size:12px}
    .state{color:var(--bad);font-family:monospace}
    .ok{color:var(--good);font-weight:600}
    .warn{color:var(--warn);font-weight:600}
    .bad{color:var(--bad);font-weight:600}
    footer{padding:14px 28px;color:var(--muted);font-size:12px;
           border-top:1px solid var(--border)}
    .add-rl{margin-top:6px;font-size:11px;color:var(--muted)}
    .add-rl span{margin-right:10px}
  </style>
</head>
<body>
  <header>
    <h1>codex-autologin <span class="dim">/ status dashboard</span></h1>
    <div class="meta">
      {{ rows|length }} accounts · refreshed {{ now }}
      <button onclick="location.reload()">⟳ refresh</button>
    </div>
  </header>
  <main>
    <table>
      <thead>
        <tr>
          <th style="width:140px">account</th>
          <th style="width:80px">plan</th>
          <th>5h window</th>
          <th>7d window</th>
          <th style="width:90px">credits</th>
          <th style="width:80px">status</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
          {% if r.state != 'ok' %}
            <tr>
              <td class="acct">{{ r.account }}</td>
              <td colspan="5" class="state">{{ r.state }}</td>
            </tr>
          {% else %}
            <tr>
              <td>
                <div class="acct">{{ r.account }}</div>
                <div class="email">{{ r.email }}</div>
              </td>
              <td><span class="plan {{ r.plan }}">{{ r.plan }}</span></td>
              <td>
                <div class="pct">{{ "%.1f"|format(r.primary_used or 0) }}%</div>
                <span class="bar"><span
                  class="{{ 'lo' if (r.primary_used or 0) < 50 else
                            'mid' if (r.primary_used or 0) < 80 else 'hi' }}"
                  style="width: {{ r.primary_used or 0 }}%"></span></span>
                <div class="reset">resets {{ r.primary_reset_human }}</div>
              </td>
              <td>
                <div class="pct">{{ "%.1f"|format(r.secondary_used or 0) }}%</div>
                <span class="bar"><span
                  class="{{ 'lo' if (r.secondary_used or 0) < 50 else
                            'mid' if (r.secondary_used or 0) < 80 else 'hi' }}"
                  style="width: {{ r.secondary_used or 0 }}%"></span></span>
                <div class="reset">resets {{ r.secondary_reset_human }}</div>
              </td>
              <td class="pct">
                {% if r.credits_unlimited %}∞{% else %}${{ r.credits_balance or "0" }}{% endif %}
              </td>
              <td>
                {% if r.limit_reached %}
                  <span class="bad">LIMIT</span>
                {% elif (r.primary_used or 0) > 80 or (r.secondary_used or 0) > 80 %}
                  <span class="warn">high</span>
                {% else %}
                  <span class="ok">ok</span>
                {% endif %}
              </td>
            </tr>
            {% if r.additional %}
            <tr>
              <td></td>
              <td colspan="5" class="add-rl">
                {% for a in r.additional %}
                  <span>
                    <strong>{{ a.limit_name }}</strong>
                    {{ "%.0f"|format(a.rate_limit.primary_window.used_percent) }}% 5h
                    /
                    {{ "%.0f"|format(a.rate_limit.secondary_window.used_percent) }}% 7d
                  </span>
                {% endfor %}
              </td>
            </tr>
            {% endif %}
          {% endif %}
        {% endfor %}
      </tbody>
    </table>
  </main>
  <footer>
    auto-reload every 60s · endpoint <code>/backend-api/wham/usage</code>
    · cache TTL {{ cache_ttl }}s · <a href="/api/status" style="color:var(--accent)">JSON</a>
  </footer>
</body>
</html>
"""


def human_reset(ts: int | None) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    delta = ts - time.time()
    if delta < 0:
        return "now"
    if delta < 3600:
        return f"in {int(delta//60)}m"
    if delta < 86400:
        return f"in {int(delta//3600)}h{int((delta % 3600)//60):02d}m"
    return f"in {int(delta//86400)}d{int((delta % 86400)//3600):02d}h"


def build_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        rows = collect()
        for r in rows:
            if r.get("state") == "ok":
                r["primary_reset_human"] = human_reset(r.get("primary_reset_at"))
                r["secondary_reset_human"] = human_reset(r.get("secondary_reset_at"))
        now = datetime.now().strftime("%H:%M:%S")
        return render_template_string(HTML, rows=rows, now=now, cache_ttl=CACHE_TTL_S)

    @app.route("/api/status")
    def api_status():
        return jsonify(collect())

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    app = build_app()
    print(f"http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
