# Token Harbor Farm Suite

Account farming + proxy management toolkit for Token Harbor / 9router / Webshare / xAI.

## Quick Start

```bash
# 1. Install dependencies
bash install.sh

# 2. Copy and configure credentials
cp .env.example .env
chmod 600 .env
# Edit .env with your credentials

# 3. Run the main TUI
python3 th-tui.py
```

## Directory Structure

```
token-harbor/
├── th-tui.py              # Main interactive TUI
├── th-webshare.py         # Webshare account farm
├── th-farm.py             # Batch account creator
├── th-proxy.py            # Proxy management
├── th-reverify.py         # Re-verify pending accounts
├── th_lib.py              # Shared library
├── import_tokenharbor.py  # Import to 9router
├── install.sh             # One-shot deps
├── run-webshare.sh        # Webshare launcher
├── .env.example           # Credential template
│
├── scripts/               # Additional scripts
├── import/                # Import tools
├── tools/                 # Utilities
├── webshare/              # Webshare tools
├── data/                  # Data files (gitignored)
├── config/                # Config files (gitignored)
├── proxy/                 # Proxy files (gitignored)
├── logs/                  # Logs (gitignored)
└── temp/                  # Temporary files
```

## Main Scripts

| Script | Description |
|--------|-------------|
| `th-tui.py` | Main interactive TUI — full management |
| `th-webshare.py` | Webshare farm with audio captcha solver |
| `th-farm.py` | Batch account creator |
| `th-proxy.py` | Proxy management |
| `th-reverify.py` | Re-verify pending accounts |
| `import_tokenharbor.py` | Import keys to 9router |

## Configuration

### Environment Variables (.env)

```bash
# Required
CM_BASE_URL=https://your-cloudmail-worker.com
CM_ADMIN_EMAIL=admin@yourdomain.com
CM_ADMIN_PASSWORD=your-password

# Optional
TH_MAILG_TOKEN=your-token
MAILG_URL=http://127.0.0.1:8790
VNC_PASSWORD=your-vnc-password
```

### 9router Settings (in TUI)

```
Settings → B. 9router
1. Mode: local/remote
2. Base URL: http://localhost:20128 or https://vibecode.omori.my.id
3. Auth mode: jwt_local/password
4. Name tag: Custom import prefix
5. Test connection
6. Remote DB: user@host for SSH dedup
```

## Features

- **TUI Interface**: Interactive menu for account management
- **Audio Captcha Solver**: Auto-solves reCAPTCHA via audio (VNC optional)
- **QR to Terminal**: Prints QR codes as ASCII for phone scanning
- **Proxy Management**: Check, rotate, smart-pick proxies
- **9router Import**: Parallel import with dedup protection
- **Mail Server Integration**: CloudMail + MailG support
- **Clear-based Mode**: Terminal-friendly UI (no alt-screen)

## Commands

```bash
# Create account
python3 th-tui.py  # Select "1. Create Account"

# Batch create
python3 th-tui.py  # Select "2. Batch Create"

# Import to 9router
python3 th-tui.py  # Select "4. Import to 9router"

# Webshare farm
./run-webshare.sh

# Manual import
python3 import_tokenharbor.py --router-base https://vibecode.omori.my.id \
    --router-password your-password --force
```

## License

See LICENSE file.
