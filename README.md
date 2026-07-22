# Tunnel Controller

Multi-node tunnel scheduling engine with web dashboard.

## Features

- Web-based configuration dashboard
- Free node source integration (zdopen.com API)
- SQLite-based local storage (no external DB)
- Per-request node switching
- Real-time pool monitoring
- systemd service management

## Quick Start

```bash
# One-line deploy on fresh VPS (Debian/Ubuntu)
curl -sL https://raw.githubusercontent.com/YOUR_USERNAME/tunnel-controller/main/deploy.sh | bash

# Or manual
git clone https://github.com/YOUR_USERNAME/tunnel-controller.git /opt/tunnel_controller
cd /opt/tunnel_controller
chmod +x deploy.sh && ./deploy.sh
```

Dashboard: `http://YOUR_VPS_IP:7910`
Default credentials: `admin / admin8888`

## Configuration

| Env Variable | Default | Description |
|---|---|---|
| `LISTEN_PORT` | 7910 | Dashboard web port |
| `WEB_USER` | admin | Dashboard login |
| `WEB_PASS` | admin8888 | Dashboard password |
| `PROXY_USER` | proxy | Tunnel auth user |
| `PROXY_PASS` | proxy8888 | Tunnel auth password |

## Update

```bash
cd /opt/tunnel_controller && git pull && systemctl restart tunnel-ctrl
```
