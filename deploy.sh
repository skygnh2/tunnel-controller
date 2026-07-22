#!/bin/bash
set -e

INSTALL_DIR="/opt/tunnel_controller"
REPO_URL="https://github.com/skygnh2/tunnel-controller.git"

echo "========================================"
echo "  Tunnel Controller - Deploy"
echo "========================================"

echo "[1/3] Installing dependencies..."
apt-get update -qq && apt-get install -y -qq python3 git > /dev/null 2>&1

echo "[2/3] Pulling source from GitHub..."
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR
if [ -d ".git" ]; then
    git pull
else
    rm -f app.py
    git clone $REPO_URL .
fi

echo "[3/3] Creating systemd service..."
cat > /lib/systemd/system/tunnel-ctrl.service << 'EOF'
[Unit]
Description=Tunnel Controller
After=network.target

[Service]
Type=simple
Environment="LISTEN_PORT=7910"
WorkingDirectory=/opt/tunnel_controller
ExecStart=/usr/bin/python3 -u app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tunnel-ctrl.service
systemctl restart tunnel-ctrl.service

echo ""
echo "Done!"
echo "Dashboard: http://YOUR_VPS_IP:7910"
echo "Logs:      journalctl -u tunnel-ctrl -f"
echo "Update:    cd /opt/tunnel_controller && git pull && systemctl restart tunnel-ctrl"
