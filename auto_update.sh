#!/bin/bash
# Auto-update tunnel-controller from GitHub via API
cd /opt/tunnel_controller

echo "[*] Fetching latest commit..."
SHA=$(curl -s https://api.github.com/repos/skygnh2/tunnel-controller/commits/main | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'][:7])" 2>/dev/null)

if [ -z "$SHA" ]; then
    echo "[!] Failed to get commit info"
    exit 1
fi

echo "[*] Latest commit: $SHA"

# Check current local version
LOCAL=$(git rev-parse --short HEAD 2>/dev/null || echo "none")
echo "[*] Local version: $LOCAL"

if [ "$SHA" = "$LOCAL" ]; then
    echo "[*] Already up to date"
    exit 0
fi

echo "[*] Downloading app.py..."
curl -sL https://raw.githubusercontent.com/skygnh2/tunnel-controller/main/app.py -o app.py.new

if [ -s app.py.new ]; then
    cp app.py app.py.bak
    mv app.py.new app.py
    echo "[*] Updated! Restarting service..."
    systemctl restart tunnel-ctrl
    echo "[*] Done. Now at $SHA"
else
    echo "[!] Download failed"
    rm -f app.py.new
fi
