# Token Harbor Farm Suite

Account farming + proxy management toolkit for Token Harbor / 9router / Webshare / xAI.

## Components

| Tool | What it does |
|------|-------------|
| `th-tui.py` | **Main interactive TUI** — create/batch accounts, verify, API keys, import, proxy mgmt, mail servers, options |
| `th-farm.py` | Batch account creator (reuse unused → reverify → fresh, proxy + domain rotation) |
| `th-webshare.py` | Webshare proxy account farm (sticky proxies, block detect, rejected-email logging) |
| `import_tokenharbor.py` | Import API keys to 9router (parallel, skip-verify default) |
| `th-email-audit.py` | Fetch all cloudmail catch-all emails + match against keys.txt |
| `th-email-manager.py` | Manage emails / manual VNC registration |
| `th-proxy.py` | Proxy utilities (check/live/rotate/smart-pick) |
| `th-reverify.py` | Re-verify pending Token Harbor accounts |
| `th-l4.py` | L4 TCP proxy connectivity tester (fast, no HTTP) |
| `humanize.py` | Human-like browser interaction (anti-bot detection) |
| `email-blacklist.py` | Blacklist personal/used emails |
| `tokenharbor.py` | Legacy auto-signup tokenharbor.ai (mail.tm tempmail, resmi alur) |
| `grok.py` | xAI (Grok) console signup via API |
| `enable_free_models.py` | Enable free models (consent) for all TH accounts, verify via API |
| `verify_tokenharbor_emails.py` | Verify emails of 403-rejected accounts, re-test key in 9router |
| `th-signup-system.py` | TH signup via system Chromium (headless, cloudmail verify) |
| `vnc-signup.py` / `vnc-signup-fresh.py` | TH signup via Playwright, FULLY VISIBLE in VNC |
| `quick-check-inbox.py` | Check cloudmail inbox for verification email (headless chromium) |
| `th-deps.py` | Lazy dependency manager (installs only what a feature needs) |
| `install.sh` | One-shot deps + browser install |
| `run-webshare.sh` | Webshare launcher (auto-activates `.venv`) |
| `start-vnc.sh` | Start VNC stack (Xvfb + x11vnc + websockify/noVNC) |
| `playwright-config.js` | Playwright/Chromium config helpers |

## Setup

```bash
bash install.sh                        # deps + playwright + browsers
# or create venv + install deps
python3 -m venv .venv
.venv/bin/pip install playwright requests PySocks curl_cffi
```

Live credentials go in `.env` (0600, never committed). See `.env.example`.

## TH-TUI (main tool)

```bash
python3 th-tui.py
```

### Main menu
| Key | Action |
|-----|--------|
| `1` | Create account (1, auto-verify) |
| `2` | Batch create (N) |
| `3` | View tokens |
| `4` | Import to 9router |
| `5` | Settings (mail server, router, VNC, options) |
| `6` | Proxy manager |
| `E` | Exit |

### Mail server (Settings → A)
Add / delete / toggle create-vs-pick / set active (`#`). New server becomes active automatically. Types: mailg (Gmail inbox API), cloudmail (self-hosted worker), smtp (generic).

### Options (Settings → D)
- **Email prefix** — `Reika` → `Reika1`, `Reika2`... auto-numbered, no conflicts
- **Account password** — fixed or random
- **Batch count / delay** — rate-limit pacing
- **Gmail dot-trick** — plus-alias for mailg + cloudmail

### Proxy menu (6)
- `1-4` toggles (status/mode/tempmail/no-delete), `5` cached live/dead/protected stats
- `C` check live — **realtime per-proxy logs** `✓ alive (IP, ms)` / `✗ dead`, keeps live + protected only
- `P` protect proxies (persists, auto-prunes stale)
- `A`/`D` add/delete, `S` scrape, `L` local proxy-controller, `R` run it

## Webshare farm

```bash
python3 th-webshare.py --count 30 --vnc --proxy proxy.txt --proxy-order random
# or
./run-webshare.sh --count 30 --vnc --proxy proxy.txt
```

| Flag | Meaning |
|------|---------|
| `--count N` | number of accounts |
| `--vnc` | visible browser (solve captcha in VNC) |
| `--proxy <file>` | proxy rotation from file (sticky until throttle) |
| `--proxy-order random\|top\|least` | proxy selection order |
| `--skip-wait-throttle` | don't wait on throttle, just swap |
| `--email-file <file>` | email pool (default `webshare-emails.txt`) |
| `--captcha-key` | paid captcha solver |

Behavior:
- **Sticky proxy** — reuse one proxy until throttled, then auto-swap
- **Block detect** — Google "automated queries" page → auto-rotate proxy
- **Rejected emails** — `✗ REJECTED <email> — <reason>` logged + saved to `ws_used.txt` (never reused)
- **Verification** — reads inbox, opens verify link in same browser (non-blocking)
- **Humanized input** — curved mouse, jitter, variable typing (anti-detection)

## Import to 9router

```bash
python3 import_tokenharbor.py                    # skip key re-check, parallel (8 workers)
python3 import_tokenharbor.py --check-keys       # force re-check all keys via API
python3 import_tokenharbor.py --workers 16       # parallel workers
python3 import_tokenharbor.py --prefix Harbor    # connection name prefix
python3 import_tokenharbor.py --allow-unverified # import even if unverified
python3 import_tokenharbor.py --dry-run          # preview only
```

Needs JWT secret at `~/.9router/jwt-secret` (or `JWT_SECRET` env) + router base `https://vibecode.omori.my.id`.

## Email flow (farm)

1. **Use unused emails** first (inbox ready, just create account)
2. **Reverify pending** (has account, needs verification)
3. **Create new** catch-all inboxes only to fill gaps

Blacklist prevents reuse of personal/used emails (`email-blacklist.txt`, seeded from keys.txt + cloudmail).

## Standalone tools

### th-farm.py — batch farm
```bash
python3 th-farm.py --count 50 --workers 6
```
Reuses unused/pending emails first, generates fresh catch-all only to fill gaps. Proxy + domain rotation built in. Writes to `keys.txt`.

### th-email-audit.py — cloudmail inventory
```bash
python3 th-email-audit.py              # full list: all cloudmail emails + status
python3 th-email-audit.py --save       # export unused emails to webshare-emails.txt
python3 th-email-audit.py --unmatched  # only free (unused) slots
```
Fetches all cloudmail catch-all addresses via `/api/user/list`, matches against keys.txt → marks `[ok]` used / `[FREE]` available.

### th-email-manager.py — email management
```bash
python3 th-email-manager.py
```
Menu: view TH-registered emails, free slots, check specific email, manual VNC registration.

### th-reverify.py — re-verify pending accounts
```bash
python3 th-reverify.py
```
Logs into pending accounts, resends verification, polls inbox, clicks link, re-tests key with `:free` model.

### th-proxy.py — proxy utilities
```bash
python3 th-proxy.py                    # list proxies (creds hidden)
python3 th-proxy.py --check            # check all, keep live only
python3 th-proxy.py --check --no-delete  # keep dead too
```
Functions reused by TUI + webshare: `check_proxy`, `check_proxies`, `keep_live_only`, `smart_pick_proxy`, `proxy_to_playwright`.

### th-l4.py — L4 TCP live check
```bash
python3 th-l4.py                       # read proxy.txt, TCP-connect test each
```
Much faster than HTTP checks (no HTTP overhead). Reports live proxies.

### email-blacklist.py — used/personal email blacklist
```bash
python3 email-blacklist.py --seed      # reseed from keys.txt + cloudmail
python3 email-blacklist.py --list      # show blacklist
```
Farm never picks blacklisted emails. Manual entries in `email-blacklist.txt`.

### humanize.py — anti-bot browser helpers
```python
from humanize import human_type, human_click, human_mouse
human_type(page, locator, "email@x.com")   # curved mouse + variable typing + typos
```
Skewed timing (not flat random), Fitts-law mouse, occasional pauses — used by webshare + TH signup.

### tokenharbor.py — legacy auto-signup
```bash
./tokenharbor --count 5 --label prod --fast --turbo --threads 3 --proxy
```
Official flow: create tempmail inbox → signup → API key. Output `tokenharbor_keys.txt`.

### grok.py — xAI signup
```bash
./grok --count 5 --threads 2
```
Create xAI (Grok) accounts via console.x.ai. Output `accounts.txt`.

### enable_free_models.py — free model consent
```bash
python3 enable_free_models.py --count 3
```
Enable free models (consent) for TH accounts, verify via API.

### verify_tokenharbor_emails.py — verify 403 accounts
```bash
python3 verify_tokenharbor_emails.py          # all unverified
python3 verify_tokenharbor_emails.py --all    # verify everything
```
For accounts rejected 403 ("verify your email"), login mail.tm → get link → open → re-test key in 9router.

### vnc-signup.py / vnc-signup-fresh.py — VNC visible signup
```bash
python3 vnc-signup.py                 # visible in VNC
```
Full-visible Playwright signup — watch/solve in VNC viewer.

### quick-check-inbox.py — inbox check
```bash
python3 quick-check-inbox.py <email>
```
Check cloudmail inbox for verification email (headless chromium).

### th-signup-system.py — headless signup
```bash
python3 th-signup-system.py
```
TH signup via system Chromium, headless (no VNC), cloudmail verification.

## Scripts & config

| File | Purpose |
|------|---------|
| `install.sh` | Install all deps + browsers |
| `run-webshare.sh` | Webshare launcher (auto-uses `.venv`) |
| `start-vnc.sh` | Start VNC stack (Xvfb + x11vnc + websockify/noVNC) |
| `th-deps.py` | Lazy dependency installer (installs on-demand) |
| `playwright-config.js` | Playwright config helpers |
| `config.json` | TUI config (mail servers, proxy, options) |
| `.env` | Live credentials (0600, never commit) |
| `AGENTS.md` | Agent guidance for this repo |

## VNC setup

```bash
bash start-vnc.sh
# VNC viewer: http://<host>:6080/vnc.html  (password from .env VNC_PASSWORD)
```
Starts Xvfb :99 → x11vnc :5900 → websockify :6080 (noVNC). Only `/opt/noVNC/` served.

## Data files

| File | Content |
|------|---------|
| `keys.txt` | created accounts `email\|pass\|apikey\|status` |
| `proxy.txt` | proxy pool `proto://user:pass:host:port` |
| `ws_accounts.txt` | webshare accounts `email:pass:token` |
| `ws_used.txt` | webshare emails rejected/used (deduped) |
| `webshare-emails.txt` | pool of unused catch-all emails for webshare |
| `email-blacklist.txt` | blacklisted (personal/used) emails |
| `imported.txt` | 9router-imported key hashes |
| `proxy-usage.json` | per-proxy usage counters |
| `config.json` | TUI config |
| `tokenharbor_keys.txt` | legacy tokenharbor.py output |

## Security

- `.env`, keys, proxies, accounts — never commit
- JWT secret kept in `~/.9router/jwt-secret` (0600)
- Rejected/used emails tracked to avoid waste
- Logs redact proxy passwords / API keys
