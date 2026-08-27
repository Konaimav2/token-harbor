#!/bin/bash
# Webshare account creator for TokenHarbor
# ALWAYS uses the project venv (.venv) which has playwright pre-installed.
# This avoids the uv/externally-managed PEP 668 block on system python3.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure .venv exists
if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] creating .venv..."
    python3 -m venv .venv
fi

# Activate the venv so playwright + deps resolve
if [ -z "$VIRTUAL_ENV" ] || [ "$VIRTUAL_ENV" != "$SCRIPT_DIR/.venv" ]; then
    echo "[setup] using .venv (playwright pre-installed)"
    source .venv/bin/activate
fi

# Ensure playwright is present in the venv
if ! .venv/bin/python -c "import playwright" 2>/dev/null; then
    echo "[setup] installing playwright into .venv..."
    .venv/bin/pip install playwright --quiet
fi

# Run webshare with the venv python (explicit, not relying on PATH)
.venv/bin/python th-webshare.py "$@"
