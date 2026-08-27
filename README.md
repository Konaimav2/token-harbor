# Token Harbor Farm Suite

Account farming + proxy management toolkit for Token Harbor / 9router / Webshare / xAI.

## Directory Structure

```
token-harbor/
├── th-tui.py              # Main interactive TUI (create/batch/verify/import/proxy/mail/settings)
├── th-webshare.py         # Webshare proxy account farm (audio captcha solver, VNC optional)
├── th-farm.py             # Batch account creator (reuse → reverify → fresh)
├── th-proxy.py            # Proxy utilities (check/live/rotate/smart-pick)
├── th-reverify.py         # Re-verify pending Token Harbor accounts
├── th_lib.py              # Shared library (TH API, webshare, mail.tm)
├── import_tokenharbor.py  # Import API keys to 9router
├── install.sh             # One-shot deps + browser install
├── run-webshare.sh        # Webshare launcher
├── keys.txt → data/       # API keys (symlink, gitignored)
├── config.json → config/  # Config (symlink, gitignored)
├── proxy.txt → proxy/     # Proxy list (symlink, gitignored)
│
├── scripts/               # Additional scripts
│   ├── th-create.py       #   TH signup via system Chromium
│   ├── th-deps.py         #   Lazy dependency manager
│   ├── th-email-audit.py  #   Cloudmail email audit
│   ├── th-email-manager.py #  Email management
│   ├── th-freeplan.py     #   Free plan utilities
│   ├── th-import.py       #   Alternative importer
│   ├── th-l4.py           #   L4 TCP proxy tester
│   ├── th-select.py       #   Token selection
│   └── th-verify.py       #   Account verification
│
├── import/                # Import tools
│   └── tokenharbor.py     #   Legacy auto-signup
│
├── tools/                 # Utilities
│   ├── email-blacklist.py #   Blacklist emails
│   ├── humanize.py        #   Anti-bot humanization
│   ├── quick-check-inbox.py # Cloudmail inbox checker
│   └── enable_free_models.py # Enable free models
│
├── webshare/              # Webshare tools
│   ├── verify-webshare.py #   Batch email activator
│   ├── vnc-signup.py      #   TH signup via VNC
│   └── vnc-signup-fresh.py #  Fresh VNC signup
│
├── data/                  # Data files (gitignored)
│   ├── keys.txt           #   API keys
│   ├── used.txt           #   Used email addresses
│   ├── imported.txt       #   Import history
│   ├── email-blacklist.txt #  Blacklisted emails
│   ├── key-checks.json    #   Key health cache
│   └── webshare-emails.txt # Webshare email pool
│
├── config/                # Config files
│   ├── .env               #   Secrets (gitignored)
│   └── config.json        #   Settings (gitignored)
│
├── proxy/                 # Proxy files (gitignored)
│   └── proxy.txt          #   Proxy list
│
├── logs/                  # Log files (gitignored)
│
└── temp/                  # Temporary/debug files
```

## Quick Start

```bash
# Install dependencies
bash install.sh

# Run main TUI
python3 th-tui.py

# Or run webshare farm
./run-webshare.sh
```

## Main Scripts (Root)

| Script | Description |
|--------|-------------|
| `th-tui.py` | Main interactive TUI — full management |
| `th-webshare.py` | Webshare account farm with audio captcha solver |
| `th-farm.py` | Batch account creator |
| `th-proxy.py` | Proxy management |
| `th-reverify.py` | Re-verify pending accounts |
| `import_tokenharbor.py` | Import keys to 9router |

## Configuration

Live credentials go in `.env` (gitignored). Settings in `config.json` (gitignored).

## Features

- **TUI Interface**: Interactive menu for account management
- **Audio Captcha Solver**: Auto-solves reCAPTCHA via audio (VNC optional)
- **QR to Terminal**: Prints QR codes as ASCII for phone scanning
- **Proxy Management**: Check, rotate, smart-pick proxies
- **9router Import**: Parallel import with dedup protection
- **Mail Server Integration**: CloudMail + MailG support
- **Clear-based Mode**: Terminal-friendly UI (no alt-screen)

## License

See LICENSE file.
