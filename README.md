# Token Harbor Farm Suite

Full-stack account farming + proxy management toolkit for **TokenHarbor**, **9router**, **Webshare**, **CloudMail**, and **MailG**.

> 🖤 Lazy-senior-dev tool: stdlib-first, terminal-native, no bloat.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Directory Structure](#directory-structure)
3. [Main TUI (th-tui.py)](#main-tui-th-tuipy)
4. [TUI Menus & Keys](#tui-menus--keys)
5. [CLI Scripts](#cli-scripts)
6. [Configuration Files](#configuration-files)
7. [Features](#features)

---

## Quick Start

```bash
# 1. Install dependencies (one-shot)
bash install.sh

# 2. Configure credentials
cp .env.example .env
cp config.json.example config.json
chmod 600 .env config.json
# Edit both files with your credentials

# 3. Run the main TUI
python3 th-tui.py
```

---

## Directory Structure

```
token-harbor/
├── th-tui.py              # Main interactive TUI (everything in one place)
├── th-webshare.py         # Webshare registration farm (audio captcha solver)
├── th-farm.py             # Batch account creator
├── th-proxy.py            # Proxy management library
├── th-reverify.py         # Re-verify pending accounts
├── th_lib.py              # Shared library (colors, helpers, JWT)
├── import_tokenharbor.py  # Import verified keys → 9router
├── install.sh             # One-shot dependency installer
├── run-webshare.sh        # Webshare launcher
├── .env.example           # Credential template
├── config.json.example    # Config template
│
├── scripts/               # Additional scripts
├── import/                # Import helpers
├── tools/                 # Utilities (enable_free_models, etc.)
├── webshare/              # Webshare tools
├── data/                  # Data files (keys.txt, imported.txt) — gitignored
├── config/                # Config files (grok.py, th-deps.py) — gitignored
├── proxy/                 # Proxy files — gitignored
├── logs/                  # Logs — gitignored
└── temp/                  # Temporary files
```

---

## Main TUI (th-tui.py)

The main interface. Launch with `python3 th-tui.py`.

```
MAIN MENU
1. Create Account     mailg/cloudmail + auto-verify
2. Batch Create       create N + auto-verify
3. View Tokens        list saved API keys
4. Import to 9router  push keys to router
5. Settings           mail server, router, proxy, options
6. Proxy              list/check/scrape/vpngate
E. Exit
```

### Navigation
- **Number keys / Enter** — select option
- **↑↓ / PgUp/PgDn / Home/End** — scroll lists
- **Space** — multi-select (in pickers)
- **Esc / B** — back / cancel
- **Ctrl+C** — abort batch, exit
- **F** — search/filter (in most pickers)

---

## TUI Menus & Keys

### 3. View Tokens
```
F=Filter · K=Check All · C=Check One · V=View Key · D=Delete · B=Back
```
- **F** — filter by account/key status (live, 429, 401, 402, 403, failed, no_key, etc.)
- **K** — check all API keys in parallel
- **C** — check one account's key
- **V** — view full API key (Space to multi-select)
- **D** — delete account(s) from keys.txt (Space to multi-select, exact-email match + auto-backup)

**Key health states:**
| State | Meaning |
|-------|---------|
| LIVE | Key works, free model OK |
| 429 | Rate-limited (free allowance used, resets in period) |
| 402 | Plan-limited (free models can't serve — account-level) |
| 401 | Invalid key (auth rejection) |
| 403 | Forbidden (email not verified yet) |
| FAIL | Genuine network failure (timeout, tunnel, etc.) |
| NOKEY | Account has no API key |

### 5. Settings
```
A. Mail Server   active server (Gmail Inbox / CloudMail / custom)
B. 9router       local/remote config
C. VNC           display toggle
D. Options       email prefix, password, batch, delay, dot-trick
E. Back
```

### 5A. Mail Servers
```
C=Create E=Pick #=Active A=Add D=Del T=Toggle U=Temp F=Search B=Back
```
- **U** — toggle Public Tempmail (free mail.tm emails when no server)
- **C** — switch server to Create-new mode
- **E** — switch server to Pick-existing mode
- **#** — number key sets active server

### 5B. 9router Settings
```
1. Mode           local / remote
2. Base URL       http://localhost:20128 or https://vibecode.omori.my.id
3. Auth mode      jwt_local / password
4. Name tag       import prefix (e.g. "sayang_")
5. Test connection
6. Remote DB      user@host (SSH dedup against remote 9router DB)
```

### 5D. Options
```
1. Email Prefix     (only shown when active server is Create-new mode)
2. Account Password (default random)
3. Batch Count      default 3
4. Batch Delay      rate-limit seconds, default 30
5. Playwright Timeout  default 120s
6. Gmail Dot-Trick  ON/OFF
```

### 6. Proxy
```
1. Status         ON/OFF
2. Mode           List / VPNGate / Combo (local+list)
3. Proxy Order    Top / Random / Least Used
4. No Delete      keep ALL failed proxies
5. Proxies        total count, protocol count
P. Protect        protected proxies
A. Add proxy      http(s)/socks5 with/without auth
F. Search proxies
D. Delete proxy
C. Check live     2-pass; asks before purge (results cached → auto-override)
S. Scrape fresh   set count, pull from lists
L. Add local      proxy-controller :7920/:8118
R. Run proxy-ctrl start/stop bundled
M. Manual proxy   set a locked proxy that overrides auto-check
```

---

## CLI Scripts

### import_tokenharbor.py
Import verified keys from keys.txt to a 9router instance.

```bash
python3 import_tokenharbor.py [options]
```

| Option | Description |
|--------|-------------|
| `--file PATH` | keys file (default `data/keys.txt`) |
| `--router-base URL` | 9router base URL |
| `--router-password PW` | remote 9router password (auth_mode=password) |
| `--provider NODE_ID` | specific provider node to import into |
| `--type TYPE` | `openai` or `anthropic` (default openai) |
| `--prefix PREFIX` | connection name prefix (default `Harbor`) |
| `--start-priority N` | starting priority number |
| `--default-model MODEL` | default model for connections |
| `--force` | bypass local imported.txt cache |
| `--dry-run` | don't actually import, just check |
| `--db PATH` | local 9router sqlite DB for dedup |
| `--remote-db HOST` | SSH user@host to dedup against remote DB |
| `--no-db-check` | skip DB dedup entirely |
| `--allow-unverified` | import keys even if unverified |
| `--skip-verify` | skip key re-check (default True) |
| `--check-keys` | re-check keys before import |
| `--workers N` | parallel workers (default 8) |

**Examples:**
```bash
# Local 9router, default prefix
python3 import_tokenharbor.py

# Remote 9router with password + SSH dedup
python3 import_tokenharbor.py \
    --router-base https://vibecode.omori.my.id \
    --router-password 'your-pass' \
    --remote-db root@162.35.169.101 \
    --prefix sayang_

# Force re-import (still respects DB dedup)
python3 import_tokenharbor.py --force

# Dry run (test connection)
python3 import_tokenharbor.py --dry-run --router-base https://vibecode.omori.my.id
```

### th-webshare.py
Webshare account registration farm with audio captcha solving.

```bash
python3 th-webshare.py [options]
```

| Option | Description |
|--------|-------------|
| `--count N` | number of accounts to register (default 1) |
| `--vnc` | visible browser (manual captcha) |
| `--cleanup-vnc` | kill leftover Xvfb/Chromium on exit (frees :99) |
| `--vnc-auto` | headed browser, fully automatic solving |
| `--proxy URL` | proxy for registration |
| `--captcha-key KEY` | 2captcha API key |
| `--captcha-provider NAME` | captcha provider (default 2captcha) |
| `--email-file PATH` | catch-all emails file |
| `--mails SOURCE` | mail source: `cloud-mail` / `mailg` / server name |
| `--proxy-order ORDER` | `random` / `top` / `least` |
| `--skip-wait-throttle` | skip post-registration throttle |
| `--max-per-proxy N` | max accounts per proxy |

**Examples:**
```bash
# Register 1 account with audio solver
python3 th-webshare.py --count 1

# Full auto, 10 accounts, cloud-mail emails, random proxies
python3 th-webshare.py --count 10 --vnc-auto \
    --mails cloud-mail --proxy proxy-nice-only.txt --proxy-order random

# Headed mode + cleanup
python3 th-webshare.py --vnc --cleanup-vnc
```

### th-farm.py
Batch account creation helper.

```bash
python3 th-farm.py [count] [--workers N] [--delay S]
```

### th-reverify.py
Re-verify pending accounts across multiple passes.

```bash
python3 th-reverify.py
```

### th-proxy.py
Proxy management library (check, rotate, smart-pick) — used by the TUI proxy menu.

```bash
python3 th-proxy.py
```

### tools/enable_free_models.py
Enable free-model consent for accounts (logs into dashboard, clicks consent).

```bash
PYTHONPATH=config:tools python3 tools/enable_free_models.py [--email user@x.com] [--all] [--dry-run] [--file keys.txt]
```

---

## Configuration Files

### .env (credentials — NEVER commit)
See `.env.example` for the template.

| Variable | Description |
|----------|-------------|
| `CM_BASE_URL` | CloudMail worker base URL |
| `CM_ADMIN_EMAIL` | CloudMail admin email |
| `CM_ADMIN_PASSWORD` | CloudMail admin password |
| `TH_MAILG_TOKEN` | MailG API token |
| `MAILG_URL` | MailG API server URL |
| `VNC_BIND` | VNC bind address (default 127.0.0.1) |
| `VNC_PORT` | VNC websockify port (default 6080) |
| `VNC_PASSWORD` | x11vnc auth password |
| `TMPMAIL_PASSWORD` | tempmail password (optional) |

### config.json (settings — NEVER commit)
See `config.json.example` for the template.

| Key | Description |
|-----|-------------|
| `active_mail` | active mail server name |
| `mail_servers` | list of mail server configs (name, type, mode, emails/domains) |
| `import_prefix` | 9router import name prefix |
| `router_mode` | `local` or `remote` |
| `router` | per-mode router config (base_url, auth, password, remote_db) |
| `proxy` | proxy config (enabled, mode, proxy_order, protected, current, etc.) |
| `dot_trick` | dot trick toggle |
| `email_prefix` | prefix for generated emails |
| `account_password` | password for created accounts |
| `batch_count` / `batch_delay` | batch create settings |

### Data files
| File | Description |
|------|-------------|
| `data/keys.txt` | account records: `email\|password\|api_key\|status` |
| `data/used.txt` | used email addresses |
| `data/imported.txt` | imported key hashes (local cache) |
| `data/key-checks.json` | key health cache (fingerprint + state) |
| `proxy/proxy.txt` | proxy list (scheme://user:pass@host:port) |
| `proxy/proxy-usage.json` | proxy usage counts |

---

## Features

- **TUI interface** — everything in one terminal app
- **Audio captcha solver** — solves reCAPTCHA via audio (Webshare/Google)
- **QR to terminal** — prints scannable QR as ASCII (half-block)
- **Cross-origin captcha frames** — Page.createIsolatedWorld (not contentDocument)
- **Proxy management** — check (cached), rotate, smart-pick, protect, manual override
- **9router import** — batch, DB-authoritative dedup, name anti-conflict
- **CloudMail + MailG** — mail server integration, verification email reading
- **Key health cache** — persistent key-checks.json, fingerprint invalidation
- **Clear-based UI** — terminal-native (no alt-screen buffer)
- **Automatic email verification** — reads verification emails, clicks links

---

## License

MIT — use responsibly. No personal accounts harmed.
