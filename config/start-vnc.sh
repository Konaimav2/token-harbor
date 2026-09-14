#!/bin/bash
# Start VNC stack for webshare proxy account creation

echo "=== Starting VNC stack ==="

# Portable repo root (replaces hardcoded cd /root/temp/token-harbor)
cd "$(dirname "$0")/.."

# Check if already running
if pgrep -f "Xvfb.*:99" >/dev/null && pgrep -f "x11vnc.*-forever" >/dev/null; then
    echo "VNC already running!"
    echo "  Connect at: http://localhost:6080/vnc.html"
    exit 0
fi

# Start Xvfb
echo "[*] Starting Xvfb :99..."
Xvfb :99 -screen 0 1280x720x24 &
sleep 1

# Get VNC password from email .env or use default
VNC_PASSWORD=$(grep "^VNC_PASSWORD=" "$HOME/projects/gmail-inbox/.env" 2>/dev/null | cut -d'=' -f2)
if [ -z "$VNC_PASSWORD" ]; then
    VNC_PASSWORD="password"
    echo "[!] No VNC_PASSWORD found in gmail-inbox .env, using default 'password'"
fi

# Start x11vnc (port 5900)
echo "[*] Starting x11vnc on port 5900..."
x11vnc -display :99 -forever -shared -rfbauth ~/.vnc/passwd 2>/dev/null || \
    echo "$VNC_PASSWORD" | x11vnc -display :99 -forever -shared -passwd stdin 2>/dev/null &
sleep 1

# Create passwd file if needed
if [ ! -f ~/.vnc/passwd ] || [ ! -s ~/.vnc/passwd ]; then
    echo "$VNC_PASSWORD" > ~/.vnc/passwd
    chmod 600 ~/.vnc/passwd
    vncpasswd_file=~/.vnc/passwd
fi

# Start websockify (port 6080 -> 5900)
echo "[*] Starting websockify on port 6080..."
python3 -m websockify --web=/opt/noVNC --port=6080 localhost:5900 &

sleep 2

echo ""
echo "========================================"
echo "✅ VNC STACK RUNNING!"
echo "========================================"
echo "Connect to:   http://localhost:6080/vnc.html"
echo "Password:     (set, ${#VNC_PASSWORD} chars)"
echo "Display:      :99"
echo "Browser path: /opt/noVNC/"
echo "========================================"
echo ""
echo "Now you can run:"
echo "  cd /root/temp/token-harbor"
echo "  python3 th-webshare.py --count N --vnc"
echo ""
echo "The browser will be visible in your VNC viewer while solving reCAPTCHA."
echo "========================================"
