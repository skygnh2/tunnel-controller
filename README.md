# Tunnel Controller

多节点隧道调度引擎，自带 Web 管理面板。

## 功能

- Web 可视化配置面板（免登录即可调整配置）
- 免费节点源 API 接入（支持自定义 API 地址）
- SQLite 本地存储，无需外部数据库
- 每次请求自动轮换节点
- 实时节点池监控与日志
- systemd 服务管理，开机自启
- 账户密码可在面板内修改

## 快速部署

### 一键部署（推荐）

在全新 VPS（Debian / Ubuntu）上执行：

```bash
curl -sL https://raw.githubusercontent.com/skygnh2/tunnel-controller/main/deploy.sh | bash
```

### 手动部署

```bash
git clone https://github.com/skygnh2/tunnel-controller.git /opt/tunnel_controller
cd /opt/tunnel_controller
chmod +x deploy.sh && ./deploy.sh
```

部署完成后访问：`http://你的VPS_IP:7910`

默认账号密码：`admin / admin8888`

## 面板配置说明

打开面板后，可配置以下选项：

| 配置项 | 说明 |
|--------|------|
| **API Source URL** | 节点源 API 地址（站大爷等免费代理 API） |
| **Target URL** | 连通性检测目标地址 |
| **Tunnel Port** | 本地隧道监听端口（默认 8888） |
| **Concurrency** | 并发检测线程数（默认 20） |
| **Refresh Interval** | 节点池刷新间隔（秒），默认 300 |
| **Rotation Mode** | 轮换模式：Per-request（每次请求换 IP）/ Timed（定时换 IP） |
| **Rotation Interval** | 定时轮换间隔（秒），仅 Timed 模式生效 |
| **Dashboard User** | 面板登录用户名 |
| **Dashboard Password** | 面板登录密码 |
| **Proxy Auth User** | 隧道代理认证用户名 |
| **Proxy Auth Password** | 隧道代理认证密码 |

> ⚠️ 修改端口或密码后需要重启服务：`systemctl restart tunnel-ctrl`

## 环境变量（可选）

部署时可通过环境变量覆盖默认值：

```bash
LISTEN_PORT=7910 WEB_USER=admin WEB_PASS=mypassword ./app.py
```

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `LISTEN_PORT` | 7910 | 面板监听端口 |
| `WEB_USER` | admin | 面板用户名 |
| `WEB_PASS` | admin8888 | 面板密码 |
| `PROXY_USER` | proxy | 隧道认证用户名 |
| `PROXY_PASS` | proxy8888 | 隧道认证密码 |

## 代理使用方式

隧道启动后，可通过以下方式使用：

```bash
# HTTP 代理
curl -x http://proxy:proxy8888@你的VPS_IP:8888 https://example.com

# 浏览器代理设置
代理地址：你的VPS_IP
端口：8888
认证：proxy / proxy8888
```

## 更新

```bash
cd /opt/tunnel_controller && git pull && systemctl restart tunnel-ctrl
```

## 文件结构

```
app.py          — 主程序（Web 面板 + 代理引擎 + SQLite 存储）
deploy.sh       — 一键部署脚本
data.db         — 运行时自动生成的数据库（已 gitignore）
```

## 系统要求

- Python 3.6+
- Linux VPS（推荐 Debian / Ubuntu）
- Root 权限
