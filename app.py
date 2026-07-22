#!/usr/bin/env python3
import http.server
import json
import os
import sqlite3
import hashlib
import base64
import threading
import time
import socket
import select
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import signal
import sys

DB_PATH = Path(__file__).parent / "data.db"
WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASS = os.environ.get("WEB_PASS", "admin8888")
PROXY_USER = os.environ.get("PROXY_USER", "proxy")
PROXY_PASS = os.environ.get("PROXY_PASS", "proxy8888")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "7910"))

db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS global_config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS node_status (
            ip TEXT PRIMARY KEY,
            details TEXT,
            last_seen INTEGER
        );
        CREATE TABLE IF NOT EXISTS proxy_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log TEXT,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS proxy_pool (
            ip TEXT,
            port INTEGER,
            protocol TEXT,
            latency REAL,
            failures INTEGER DEFAULT 0,
            PRIMARY KEY (ip, port)
        );
    """)
    conn.commit()
    conn.close()

def db_query(sql, params=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def db_execute(sql, params=()):
    conn = get_db()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

def save_pool_to_db(pool):
    """Persist proxy pool to database."""
    db_execute("DELETE FROM proxy_pool")
    for p in pool:
        db_execute("INSERT INTO proxy_pool (ip, port, protocol, latency, failures) VALUES (?, ?, ?, ?, ?)",
                   (p["ip"], p["port"], p.get("protocol", "http"), p.get("latency", 0), p.get("failures", 0)))

def load_pool_from_db():
    """Load proxy pool from database on startup."""
    rows = db_query("SELECT ip, port, protocol, latency, failures FROM proxy_pool")
    if rows:
        return [{"ip": r["ip"], "port": r["port"], "protocol": r.get("protocol", "http"),
                 "latency": r.get("latency", 0), "failures": r.get("failures", 0)} for r in rows]
    return []

DEFAULT_CONFIG = {
    "target_url": "https://opencode.ai",
    "proxy_port": "8888",
    "refresh_interval": "300",
    "rotation_mode": "request",
    "rotation_interval": "0",
    "max_workers": "20",
    "proxy_source": "https://www.jiliuip.com/free",
    "web_user": WEB_USER,
    "web_pass": WEB_PASS,
    "proxy_user": PROXY_USER,
    "proxy_pass": PROXY_PASS
}

def get_config():
    rows = db_query("SELECT value FROM global_config WHERE key = 'config'")
    if rows:
        return json.loads(rows[0]["value"])
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    db_execute("INSERT INTO global_config (key, value) VALUES ('config', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (json.dumps(cfg),))

class AuthMixin:
    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            return user == WEB_USER and pwd == WEB_PASS
        except:
            return False

    def send_401(self):
        body = b"Unauthorized Access."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Secure"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

proxy_pool = []
pool_lock = threading.Lock()
refresh_lock = threading.Lock()
current_proxy = None
proxy_failures = 0
rotation_count = 0
start_time = time.time()
running = True

def fetch_proxies(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            text = res.read().decode()

        proxies = []

        # Try JSON format (zdopen.com style)
        try:
            data = json.loads(text)
            if data.get("code") == "10001":
                for p in data.get("data", {}).get("proxy_list", []):
                    protocol = p.get("protocol", "http").lower()
                    if protocol in ("http", "https", "socks4", "socks5"):
                        proxies.append({"ip": p["ip"], "port": int(p["port"]), "protocol": protocol, "failures": 0})
                return proxies
        except:
            pass

        # Try jiliuip.com HTML format (fpsList embedded in JS)
        import re
        match = re.search(r'const\s+fpsList\s*=\s*(\[.*?\]);', text, re.DOTALL)
        if match:
            try:
                fps_list = json.loads(match.group(1))
                for p in fps_list:
                    ip = p.get("ip", "")
                    port = p.get("port", "")
                    if ip and port:
                        proxies.append({"ip": ip, "port": int(port), "protocol": "http", "failures": 0})
                if proxies:
                    return proxies
            except:
                pass

        # Try plain text format (ip:port or protocol://ip:port)
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            protocol = "http"
            raw = line
            if "://" in raw:
                protocol, raw = raw.split("://", 1)
            if ":" in raw:
                parts = raw.split(":")
                ip = parts[0].strip()
                try:
                    port = int(parts[1].strip().split()[0])
                    if ip and port > 0:
                        proxies.append({"ip": ip, "port": port, "protocol": protocol, "failures": 0})
                except:
                    pass

        return proxies
    except Exception as e:
        print(f"[!] Node list fetch failed: {e}", flush=True)
        return []

def check_proxy(proxy, target_url, timeout=5):
    try:
        ph = urllib.request.ProxyHandler({
            "http": f"http://{proxy['ip']}:{proxy['port']}",
            "https": f"http://{proxy['ip']}:{proxy['port']}"
        })
        opener = urllib.request.build_opener(ph)
        req = urllib.request.Request(target_url, headers={"User-Agent": "Mozilla/5.0"})
        t0 = time.time()
        with opener.open(req, timeout=timeout) as r:
            return r.status == 200, time.time() - t0
    except:
        return False, 999

def refresh_pool():
    if not refresh_lock.acquire(blocking=False):
        return  # Another refresh is already in progress
    try:
        global proxy_pool
        cfg = get_config()
        source_urls = cfg.get("proxy_source", DEFAULT_CONFIG["proxy_source"])
        target_url = cfg.get("target_url", DEFAULT_CONFIG["target_url"])
        max_w = int(cfg.get("max_workers", 20))

        # Support multiple sources (comma-separated)
        urls = [u.strip() for u in source_urls.split(",") if u.strip()]

        all_raw = []
        for url in urls:
            print(f"[*] Fetching from: {url}", flush=True)
            raw = fetch_proxies(url)
            print(f"[*] Got {len(raw)} nodes", flush=True)
            all_raw.extend(raw)

        # Deduplicate by ip:port
        seen = set()
        unique = []
        for p in all_raw:
            key = f"{p['ip']}:{p['port']}"
            if key not in seen:
                seen.add(key)
                unique.append(p)

        print(f"[*] Total unique nodes: {len(unique)}, checking...", flush=True)

        valid = []
        with ThreadPoolExecutor(max_workers=max_w) as ex:
            futs = {ex.submit(check_proxy, p, target_url): p for p in unique}
            for f in as_completed(futs):
                ok, lat = f.result()
                if ok:
                    p = futs[f]
                    p["latency"] = lat
                    valid.append(p)

        with pool_lock:
            valid.sort(key=lambda x: x["latency"])
            proxy_pool = valid[:100]

        # Persist pool to database
        save_pool_to_db(proxy_pool)

        print(f"[*] Available: {len(proxy_pool)}/{len(unique)}", flush=True)
        report_log(f"Pool refresh: {len(proxy_pool)}/{len(unique)}")
    finally:
        refresh_lock.release()

def get_next_proxy():
    global current_proxy
    with pool_lock:
        if not proxy_pool:
            return None
        if current_proxy is None:
            current_proxy = proxy_pool[0]
        return current_proxy

def relay(s1, s2):
    socks = [s1, s2]
    while True:
        r, _, err = select.select(socks, [], socks, 60)
        if err:
            break
        for s in r:
            data = s.recv(65536)
            if not data:
                return
            (s2 if s is s1 else s1).sendall(data)

class TunnelServer:
    def __init__(self, host="0.0.0.0", port=8888):
        self.host = host
        self.port = port

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(200)
        print(f"[*] SOCKS5 Tunnel listening: {self.host}:{self.port}", flush=True)
        while running:
            try:
                cli, addr = srv.accept()
                threading.Thread(target=self.handle, args=(cli,), daemon=True).start()
            except:
                break

    def handle(self, cli):
        global rotation_count
        try:
            data = cli.recv(16384)
            if not data:
                return
            # SOCKS5 handshake: version 0x05
            if data[0] == 0x05:
                self.do_socks5(cli, data)
            else:
                # Fallback to HTTP proxy
                first = data.split(b"\r\n")[0].split(b" ")
                if len(first) >= 3 and first[0].upper() == b"CONNECT":
                    self.do_connect(cli, data)
                else:
                    self.do_http(cli, data)
            rotation_count += 1
        except:
            pass
        finally:
            try: cli.close()
            except: pass

    def do_socks5(self, cli, data):
        """SOCKS5 proxy protocol with username/password auth"""
        # Step 1: Auth negotiation
        if data[0] != 0x05:
            return
        
        cfg = get_config()
        auth_user = cfg.get("proxy_user", PROXY_USER)
        auth_pass = cfg.get("proxy_pass", PROXY_PASS)
        
        # Check if client supports username/password auth (0x02)
        methods = data[2:]
        if 0x02 in methods and auth_user:
            # Username/password auth required
            cli.sendall(b"\x05\x02")
            
            # Step 1b: Receive username/password
            auth_data = cli.recv(4096)
            if len(auth_data) < 3 or auth_data[0] != 0x05:
                return
            
            ulen = auth_data[1]
            username = auth_data[2:2+ulen].decode()
            plen = auth_data[2+ulen]
            password = auth_data[3+ulen:3+ulen+plen].decode()
            
            # Verify credentials
            if username != auth_user or password != auth_pass:
                cli.sendall(b"\x05\x01")  # Auth failure
                return
            cli.sendall(b"\x05\x00")  # Auth success
        elif 0x00 in methods:
            # No auth
            cli.sendall(b"\x05\x00")
        else:
            # No acceptable methods
            cli.sendall(b"\x05\xff")
            return
        
        # Step 2: Connection request
        req = cli.recv(4096)
        if len(req) < 4 or req[0] != 0x05:
            return
        
        cmd = req[1]  # 0x01 = connect
        atyp = req[3]  # address type
        
        if atyp == 0x01:  # IPv4
            target_ip = socket.inet_ntoa(req[4:8])
            target_port = int.from_bytes(req[8:10], "big")
        elif atyp == 0x03:  # Domain
            domain_len = req[4]
            target_ip = req[5:5+domain_len].decode()
            target_port = int.from_bytes(req[5+domain_len:5+domain_len+2], "big")
        elif atyp == 0x04:  # IPv6
            target_ip = socket.inet_ntop(socket.AF_INET6, req[4:20])
            target_port = int.from_bytes(req[20:22], "big")
        else:
            cli.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            return

        if cmd != 0x01:  # Only support CONNECT
            cli.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return

        proxy = get_next_proxy()
        if not proxy:
            cli.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            return

        try:
            up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up.settimeout(10)
            up.connect((proxy["ip"], proxy["port"]))
            
            # Forward via upstream HTTP proxy CONNECT
            up.sendall(f"CONNECT {target_ip}:{target_port} HTTP/1.1\r\n\r\n".encode())
            resp = up.recv(4096)
            
            if b"200" in resp:
                # Success reply
                cli.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                relay(cli, up)
            else:
                cli.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            up.close()
        except:
            proxy["failures"] += 1
            try:
                cli.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            except:
                pass

    def do_http(self, cli, data):
        proxy = get_next_proxy()
        if not proxy:
            cli.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        try:
            up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up.settimeout(10)
            up.connect((proxy["ip"], proxy["port"]))
            up.sendall(data)
            resp = b""
            while True:
                try:
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
                    if len(resp) > 1048576:
                        break
                except:
                    break
            cli.sendall(resp)
            up.close()
        except:
            proxy["failures"] += 1
            cli.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

    def do_connect(self, cli, data):
        proxy = get_next_proxy()
        if not proxy:
            cli.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        try:
            target = data.split(b" ")[1].decode()
            up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up.settimeout(15)
            up.connect((proxy["ip"], proxy["port"]))
            up.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            resp = up.recv(4096)
            if b"200" not in resp:
                up.close()
                cli.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            cli.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            relay(cli, up)
        except:
            cli.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

def report_log(msg):
    try:
        db_execute("INSERT INTO proxy_logs (log, created_at) VALUES (?, ?)",
                   (msg, int(time.time() * 1000)))
    except:
        pass

def heartbeat_loop():
    global rotation_count, start_time
    while running:
        time.sleep(10)
        try:
            ip = "unknown"
            try:
                for svc in ["https://httpbin.org/ip", "https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
                    try:
                        req = urllib.request.Request(svc, headers={"User-Agent": "curl/7.68.0"})
                        with urllib.request.urlopen(req, timeout=5) as r:
                            text = r.read().decode().strip()
                            ip = text.replace('"', '').split("Origin")[-1].split(":")[-1].strip().strip("}") if "{" in text else text
                            if ip and "." in ip:
                                break
                    except:
                        continue
            except:
                pass
            with pool_lock:
                pool_size = len(proxy_pool)
            details = {
                "proxy_port": int(get_config().get("proxy_port", 8888)),
                "pool_size": pool_size,
                "rotation_count": rotation_count,
                "uptime": f"{int((time.time() - start_time) / 60)}m",
                "web_user": get_config().get("web_user", WEB_USER),
                "proxy_user": get_config().get("proxy_user", PROXY_USER),
                "proxy_pass": get_config().get("proxy_pass", PROXY_PASS)
            }
            db_execute("INSERT INTO node_status (ip, details, last_seen) VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET details = excluded.details, last_seen = excluded.last_seen",
                       (ip, json.dumps(details), int(time.time() * 1000)))
        except:
            pass

def pool_refresh_loop():
    # Try loading persisted pool on first run
    with pool_lock:
        saved = load_pool_from_db()
        if saved:
            global proxy_pool
            proxy_pool = saved
            print(f"[*] Loaded {len(saved)} proxies from database", flush=True)

    time.sleep(5)
    while running:
        refresh_pool()
        interval = int(get_config().get("refresh_interval", 300))
        time.sleep(interval)

def proxy_health_check_loop():
    """Check current proxy health every 5 seconds, switch on 3 consecutive failures"""
    global current_proxy, proxy_failures, rotation_count
    time.sleep(10)  # Wait for initial pool load
    
    while running:
        time.sleep(5)
        
        with pool_lock:
            if not proxy_pool or current_proxy is None:
                continue
        
        cfg = get_config()
        target_url = cfg.get("target_url", DEFAULT_CONFIG["target_url"])
        
        # Check current proxy
        ok, lat = check_proxy(current_proxy, target_url, timeout=3)
        
        if ok:
            proxy_failures = 0
            current_proxy["latency"] = lat
            print(f"[*] Proxy OK: {current_proxy['ip']}:{current_proxy['port']} ({lat:.1f}s)", flush=True)
        else:
            proxy_failures += 1
            print(f"[*] Proxy FAIL: {current_proxy['ip']}:{current_proxy['port']} ({proxy_failures}/3)", flush=True)
            
            if proxy_failures >= 3:
                # Switch to next proxy
                with pool_lock:
                    if len(proxy_pool) > 1:
                        # Remove failed proxy from pool
                        try:
                            proxy_pool.remove(current_proxy)
                        except:
                            pass
                        # Switch to next
                        current_proxy = proxy_pool[0]
                        proxy_failures = 0
                        rotation_count += 1
                        print(f"[*] Switched to: {current_proxy['ip']}:{current_proxy['port']}", flush=True)
                        report_log(f"Proxy switched: {current_proxy['ip']}:{current_proxy['port']}")
                    else:
                        # No other proxies, keep trying
                        proxy_failures = 0
                        print("[*] No other proxies available, keeping current", flush=True)

def get_auth_from_handler(handler):
    cfg = get_config()
    u = cfg.get("web_user", WEB_USER)
    p = cfg.get("web_pass", WEB_PASS)
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        username, password = decoded.split(":", 1)
        return username == u and password == p
    except:
        return False

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type="text/plain", status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/config":
            if not get_auth_from_handler(self):
                return self.send_text("Unauthorized", status=401)
            return self.send_json(get_config())

        if path == "/api/status":
            if not get_auth_from_handler(self):
                return self.send_text("Unauthorized", status=401)
            cutoff = int(time.time() * 1000) - 180000
            db_execute("DELETE FROM node_status WHERE last_seen < ?", (cutoff,))
            nodes = db_query("SELECT * FROM node_status ORDER BY last_seen DESC")
            logs = db_query("SELECT * FROM proxy_logs ORDER BY created_at DESC LIMIT 50")
            return self.send_json({"nodes": nodes, "logs": logs})

        if path == "/api/proxies":
            if not get_auth_from_handler(self):
                return self.send_text("Unauthorized", status=401)
            cutoff = int(time.time() * 1000) - 120000
            db_execute("DELETE FROM node_status WHERE last_seen < ?", (cutoff,))
            nodes = db_query("SELECT ip, details FROM node_status")
            lines = []
            for n in nodes:
                det = json.loads(n["details"] or "{}")
                if det.get("proxy_port"):
                    lines.append(f"http://{PROXY_USER}:{PROXY_PASS}@{n['ip']}:{det['proxy_port']}")
            return self.send_text("\n".join(lines))

        if path == "/":
            if not get_auth_from_handler(self):
                body = b"Unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Tunnel Controller"')
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self.send_text(DASHBOARD_HTML(), "text/html")

        self.send_text("Not Found", status=404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/config":
            if not get_auth_from_handler(self):
                return self.send_text("Unauthorized", status=401)
            data = json.loads(self.read_body())
            current = get_config()
            cfg = {
                "target_url": data.get("target_url") or current.get("target_url", DEFAULT_CONFIG["target_url"]),
                "proxy_port": str(data.get("proxy_port") or current.get("proxy_port", DEFAULT_CONFIG["proxy_port"])),
                "refresh_interval": str(data.get("refresh_interval") or current.get("refresh_interval", DEFAULT_CONFIG["refresh_interval"])),
                "rotation_mode": data.get("rotation_mode") or current.get("rotation_mode", DEFAULT_CONFIG["rotation_mode"]),
                "rotation_interval": str(data.get("rotation_interval") or current.get("rotation_interval", DEFAULT_CONFIG["rotation_interval"])),
                "max_workers": str(data.get("max_workers") or current.get("max_workers", DEFAULT_CONFIG["max_workers"])),
                "proxy_source": data.get("proxy_source") if data.get("proxy_source") is not None else current.get("proxy_source", DEFAULT_CONFIG["proxy_source"]),
                "web_user": data.get("web_user") or current.get("web_user", DEFAULT_CONFIG["web_user"]),
                "web_pass": data.get("web_pass") or current.get("web_pass", DEFAULT_CONFIG["web_pass"]),
                "proxy_user": data.get("proxy_user") or current.get("proxy_user", DEFAULT_CONFIG["proxy_user"]),
                "proxy_pass": data.get("proxy_pass") or current.get("proxy_pass", DEFAULT_CONFIG["proxy_pass"])
            }
            save_config(cfg)
            return self.send_text("OK")

        if path == "/api/report":
            if not get_auth_from_handler(self):
                return self.send_text("Unauthorized", status=401)
            try:
                data = json.loads(self.read_body())
                db_execute("INSERT INTO node_status (ip, details, last_seen) VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET details = excluded.details, last_seen = excluded.last_seen",
                           (data.get("ip", ""), json.dumps(data.get("details", {})), int(time.time() * 1000)))
                if data.get("log"):
                    db_execute("INSERT INTO proxy_logs (log, created_at) VALUES (?, ?)",
                               (data["log"], int(time.time() * 1000)))
            except:
                pass
            return self.send_text("OK")

        self.send_text("Not Found", status=404)


DASHBOARD_HTML = lambda: f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>隧道控制器</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
body {{ font-family: 'Inter', sans-serif; background: #0f172a; }}
.font-mono {{ font-family: 'JetBrains Mono', monospace; }}
::-webkit-scrollbar {{ width: 8px; }}
::-webkit-scrollbar-track {{ background: rgba(15,23,42,0.5); }}
::-webkit-scrollbar-thumb {{ background: rgba(51,65,85,0.8); border-radius: 4px; }}
.log-entry {{ border-bottom: 1px solid rgba(51,65,85,0.3); }}
</style>
</head>
<body class="text-slate-200 min-h-screen">
<div class="max-w-6xl mx-auto p-6">
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-2xl font-bold text-white">隧道控制器</h1>
      <p class="text-sm text-slate-400 mt-1">多节点隧道调度引擎</p>
    </div>
    <div class="flex items-center gap-3">
      <span id="vps-status" class="px-3 py-1 rounded-full text-xs font-bold bg-slate-800 text-slate-400">离线</span>
      <span id="proxy-count" class="px-3 py-1 rounded-full text-xs font-bold bg-slate-800 text-slate-400">节点池: --</span>
    </div>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
    <div class="lg:col-span-2 bg-slate-900/80 rounded-2xl p-6 border border-slate-800">
      <h2 class="text-lg font-bold text-white mb-4">配置</h2>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div><label class="text-xs text-slate-400 font-medium">目标地址</label>
          <input id="target-url" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value="https://www.cloudflare.com"></div>
        <div><label class="text-xs text-slate-400 font-medium">隧道端口</label>
          <input id="proxy-port" type="number" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value="8888"></div>
        <div><label class="text-xs text-slate-400 font-medium">刷新间隔 (秒)</label>
          <input id="refresh-interval" type="number" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value="300"></div>
        <div><label class="text-xs text-slate-400 font-medium">并发数</label>
          <input id="max-workers" type="number" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value="20"></div>
      </div>
      <div class="mt-4"><label class="text-xs text-slate-400 font-medium">节点源 API 地址</label>
        <input id="proxy-source" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white font-mono" placeholder="请输入代理 API 地址..."></div>
      <div class="mt-4"><label class="text-xs text-slate-400 font-medium">轮换模式</label>
        <div class="flex gap-3 mt-1">
          <label class="flex items-center gap-1 text-sm"><input type="radio" name="rot-mode" value="request" checked> 每次请求</label>
          <label class="flex items-center gap-1 text-sm"><input type="radio" name="rot-mode" value="interval"> 定时轮换</label>
        </div></div>
      <div class="mt-4"><label class="text-xs text-slate-400 font-medium">轮换间隔 (秒) <span class="text-slate-600">- 0=禁用，仅定时模式生效</span></label>
        <input id="rotation-interval" type="number" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value="0"></div>
      <div class="mt-4 grid grid-cols-2 gap-4">
        <div><label class="text-xs text-slate-400 font-medium">面板用户名</label>
          <input id="web-user" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value=""></div>
        <div><label class="text-xs text-slate-400 font-medium">面板密码</label>
          <input id="web-pass" type="password" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value=""></div>
      </div>
      <div class="mt-4 grid grid-cols-2 gap-4">
        <div><label class="text-xs text-slate-400 font-medium">代理认证用户名</label>
          <input id="proxy-user" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value=""></div>
        <div><label class="text-xs text-slate-400 font-medium">代理认证密码</label>
          <input id="proxy-pass" type="password" class="w-full mt-1 px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white" value=""></div>
      </div>
      <div class="flex gap-3 mt-6">
        <button onclick="saveConfig()" class="px-6 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-bold transition-all">保存</button>
        <button onclick="fetchConfig()" class="px-6 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-sm font-bold transition-all">刷新</button>
      </div>
      <div id="save-msg" class="mt-2 text-sm text-green-400 hidden">已保存。端口/密码修改需要重启服务生效。</div>
    </div>
    <div class="bg-slate-900/80 rounded-2xl p-6 border border-slate-800">
      <h2 class="text-lg font-bold text-white mb-4">状态</h2>
      <div id="vps-details" class="text-sm space-y-2 text-slate-400"><p>等待连接...</p></div>
      <div id="proxy-address" class="mt-4 pt-4 border-t border-slate-800 hidden">
        <h3 class="text-sm font-bold text-slate-300 mb-2">代理地址</h3>
        <div class="bg-slate-950 rounded-lg p-3">
          <code id="proxy-url" class="text-xs font-mono text-green-400 break-all"></code>
        </div>
        <button onclick="copyProxy()" class="mt-2 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-bold transition-all">复制</button>
      </div>
    </div>
  </div>
  <div class="mt-6 bg-slate-900/80 rounded-2xl p-6 border border-slate-800">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-bold text-white">日志</h2>
      <button onclick="fetchStatus()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-bold transition-all">刷新</button>
    </div>
    <div id="log-container" class="bg-slate-950 rounded-xl p-4 h-64 overflow-y-auto font-mono text-xs leading-relaxed">
      <div class="text-slate-500">等待日志...</div></div>
  </div>
</div>
<script>
async function fetchConfig() {{
  try {{
    const r = await fetch('/api/config');
    const c = await r.json();
    document.getElementById('target-url').value = c.target_url || '';
    document.getElementById('proxy-port').value = c.proxy_port || 8888;
    document.getElementById('refresh-interval').value = c.refresh_interval || 300;
    document.getElementById('max-workers').value = c.max_workers || 20;
    document.getElementById('proxy-source').value = c.proxy_source || '';
    document.getElementById('rotation-interval').value = c.rotation_interval || 0;
    const mode = c.rotation_mode || 'request';
    document.querySelectorAll('input[name="rot-mode"]').forEach(r => {{ r.checked = (r.value === mode); }});
    document.getElementById('web-user').value = c.web_user || '';
    document.getElementById('web-pass').value = c.web_pass || '';
    document.getElementById('proxy-user').value = c.proxy_user || '';
    document.getElementById('proxy-pass').value = c.proxy_pass || '';
  }} catch(e) {{}}
}}
async function saveConfig() {{
  const mode = document.querySelector('input[name="rot-mode"]:checked')?.value || 'request';
  const p = {{
    target_url: document.getElementById('target-url').value,
    proxy_port: parseInt(document.getElementById('proxy-port').value) || 8888,
    refresh_interval: parseInt(document.getElementById('refresh-interval').value) || 300,
    rotation_mode: mode,
    rotation_interval: parseInt(document.getElementById('rotation-interval').value) || 0,
    max_workers: parseInt(document.getElementById('max-workers').value) || 20,
    proxy_source: document.getElementById('proxy-source').value,
    web_user: document.getElementById('web-user').value,
    web_pass: document.getElementById('web-pass').value,
    proxy_user: document.getElementById('proxy-user').value,
    proxy_pass: document.getElementById('proxy-pass').value
  }};
  await fetch('/api/config', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(p)}});
  document.getElementById('save-msg').classList.remove('hidden');
  setTimeout(() => document.getElementById('save-msg').classList.add('hidden'), 4000);
}}
async function fetchStatus() {{
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();
    const nodes = d.nodes || [];
    const logs = d.logs || [];
    const vpsDiv = document.getElementById('vps-details');
    const st = document.getElementById('vps-status');
    const pc = document.getElementById('proxy-count');
    if (nodes.length > 0) {{
      st.textContent = '在线';
      st.className = 'px-3 py-1 rounded-full text-xs font-bold bg-green-900/50 text-green-400';
      const det = typeof nodes[0].details === 'string' ? JSON.parse(nodes[0].details) : nodes[0].details;
      vpsDiv.innerHTML = `<p>IP: ${{nodes[0].ip}}</p><p>节点池: ${{det.pool_size||0}}</p><p>已轮换: ${{det.rotation_count||0}} 次</p><p>运行时间: ${{det.uptime||'--'}}</p>`;
      pc.textContent = `节点池: ${{det.pool_size||0}}`;
      const proxyDiv = document.getElementById('proxy-address');
      const proxyUrl = document.getElementById('proxy-url');
      const pUser = det.proxy_user || 'proxy';
      const pPass = det.proxy_pass || '***';
      const pPort = det.proxy_port || 8888;
      proxyUrl.textContent = `socks5://${{pUser}}:${{pPass}}@${{nodes[0].ip}}:${{pPort}}`;
      proxyDiv.classList.remove('hidden');
    }} else {{
      st.textContent = '离线';
      st.className = 'px-3 py-1 rounded-full text-xs font-bold bg-red-900/50 text-red-400';
      vpsDiv.innerHTML = '<p class="text-slate-500">暂无数据</p>';
      pc.textContent = '节点池: 0';
      document.getElementById('proxy-address').classList.add('hidden');
    }}
    const lc = document.getElementById('log-container');
    if (logs.length > 0) {{
      lc.innerHTML = logs.map(l => `<div class="log-entry py-1"><span class="text-slate-600">[${{new Date(l.created_at).toLocaleTimeString()}}]</span> <span class="text-slate-300">${{l.log}}</span></div>`).join('');
    }} else {{
      lc.innerHTML = '<div class="text-slate-500">暂无日志</div>';
    }}
  }} catch(e) {{}}
}}
function copyProxy() {{
  const text = document.getElementById('proxy-url').textContent;
  navigator.clipboard.writeText(text).then(() => alert('已复制'));
}}
fetchConfig();
fetchStatus();
setInterval(fetchStatus, 8000);
</script>
</body></html>"""


if __name__ == "__main__":
    init_db()

    print("=" * 50)
    print("  Tunnel Controller - VPS Standalone")
    print(f"  Dashboard: http://0.0.0.0:{LISTEN_PORT}")
    print(f"  Tunnel:    port {int(get_config().get('proxy_port', 8888))}")
    print("=" * 50)

    # Load persisted pool before starting refresh loop
    saved_pool = load_pool_from_db()
    if saved_pool:
        with pool_lock:
            proxy_pool.extend(saved_pool)
        print(f"[*] Restored {len(saved_pool)} proxies from database", flush=True)

    threading.Thread(target=refresh_pool, daemon=True).start()
    threading.Thread(target=pool_refresh_loop, daemon=True).start()
    threading.Thread(target=proxy_health_check_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    cfg = get_config()
    tunnel_port = int(cfg.get("proxy_port", 8888))
    srv = TunnelServer("0.0.0.0", tunnel_port)
    threading.Thread(target=srv.start, daemon=True).start()

    httpd = http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"[*] Dashboard: http://0.0.0.0:{LISTEN_PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        running = False
        print("\n[*] Shutting down...")
