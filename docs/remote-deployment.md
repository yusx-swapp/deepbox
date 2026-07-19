# 三机远程部署：Tailscale 私有网络

> 目标拓扑：电脑 A 用浏览器访问；电脑 B 托管 deepbox Server；电脑 C 运行 connector 和真实
> Agent。三台电脑不在同一局域网，但加入同一个 Tailscale Tailnet。
>
> 本方案面向当前 Private Alpha。**使用 Tailscale Serve，不使用 Tailscale Funnel，也不把
> Uvicorn 端口直接暴露到公网。**

---

## 1. 拓扑与信任边界

```text
电脑 A — Viewer
  Browser
     │ HTTPS / WSS
     ▼
Tailscale WireGuard 私有网络
     │
     ▼
电脑 B — Server Host
  Tailscale Serve（TLS 终止）
     │ http://127.0.0.1:8077
     ▼
  deepbox Server
  ├── SQLite 元数据
  └── Session DVR
     ▲
     │ HTTPS / WSS（同一 Tailnet）
     │
电脑 C — Agent Devbox
  deepbox connector
     │ PTY
     ▼
  Claude Code / Codex / Copilot
```

安全边界：

- Agent CLI、API key、登录态、代码目录只在电脑 C。
- Server 只收到终端事件流和非机密配置。
- Tailscale 提供设备间 WireGuard 加密和私有 DNS。
- deepbox 登录和 Devbox token 仍是应用层认证。
- 电脑 B 的 Uvicorn 只监听 `127.0.0.1`，局域网和公网都不能直接访问 8077。

---

## 2. 前置条件

三台电脑均完成：

1. 安装 Tailscale：https://tailscale.com/download/windows
2. 登录同一个 Tailnet。
3. 在 Windows 命令提示符验证：

```bat
tailscale status
```

电脑之间应出现在设备列表中。建议在 Tailscale 管理后台启用 MagicDNS。

> 不要运行 `tailscale funnel`。Funnel 会把服务发布到公开互联网，不适合当前安全阶段。

---

## 3. 电脑 B：部署 Server Host

### 3.1 安装代码和依赖

```bat
cd /d C:\Code
git clone https://github.com/yusx-swapp/deepbox.git
cd /d C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

如果仓库已经存在：

```bat
cd /d C:\Code\deepbox
git pull
.venv\Scripts\python -m pip install -r requirements.txt
```

### 3.2 配置 Tailscale Serve

现代 Tailscale CLI：

```bat
tailscale serve --bg http://127.0.0.1:8077
tailscale serve status
```

命令会显示一个只在 Tailnet 内可访问的 HTTPS URL，例如：

```text
https://server-name.example-tailnet.ts.net
```

不同 Tailscale 版本的 Serve CLI 可能略有区别；如命令被拒绝，先运行：

```bat
tailscale serve --help
```

使用其“HTTPS 代理到 `http://127.0.0.1:8077`”等价形式。

### 3.3 创建 Server 配置

```bat
cd /d C:\Code\deepbox
copy .env.example .env
py -3 -c "import secrets; print(secrets.token_urlsafe(48))"
notepad .env
```

把随机值和 Tailscale HTTPS URL 写入 `.env`：

```dotenv
DEEPBOX_ENV=production
DEEPBOX_SECRET=<刚生成的随机值>
DEEPBOX_DATABASE_URL=sqlite:///C:/deepbox-data/deepbox.db
DEEPBOX_DATA_DIR=C:/deepbox-data
DEEPBOX_PUBLIC_URL=https://server-name.example-tailnet.ts.net
DEEPBOX_ALLOWED_ORIGINS=https://server-name.example-tailnet.ts.net
DEEPBOX_COOKIE_SECURE=true
DEEPBOX_COOKIE_SAMESITE=lax
DEEPBOX_HOST=127.0.0.1
DEEPBOX_PORT=8077
DEEPBOX_PLATFORM=local
DEEPBOX_REGISTRATION_ENABLED=false
```

注意：

- `.env` 已被 gitignore，不能提交。
- URL 不带末尾 `/`。
- `DEEPBOX_SECRET` 修改后，已有浏览器登录 cookie 会失效，这是正常行为。
- SQLite 和 DVR 放在 `C:\deepbox-data`，不要放在 Git 仓库里。

### 3.4 启动 Server

```bat
cd /d C:\Code\deepbox
scripts\start-server.cmd
```

等价命令：

```bat
.venv\Scripts\python -m server
```

Server 启动时会在 production 模式下强制检查：

- secret 不是开发默认值。
- allowed origins 非空。
- secure cookie 已开启。
- 端口合法。

### 3.5 验证

在电脑 B 或 Tailnet 内任意设备运行：

```bat
curl https://server-name.example-tailnet.ts.net/api/health
curl https://server-name.example-tailnet.ts.net/api/ready
```

预期：

```json
{"status":"ok","protocol_version":2}
{"status":"ready","protocol_version":2}
```

`health` 表示进程存活；`ready` 还会检查数据库和 recording 目录。

---

## 4. 电脑 A：浏览器登录

电脑 A 只需要 Tailscale 和浏览器，不需要克隆 deepbox。

打开：

```text
https://server-name.example-tailnet.ts.net
```

首次使用：

1. 注册用户。
2. 登录。
3. 创建一台 Devbox（这代表电脑 C）。
4. 复制完整 `hpc_box_...` token；完整 token 只显示一次。
5. 在该 Devbox 下创建 Agent：
   - handle：`claude`
   - runtime：`claude-code`
   - cwd：电脑 C 上真实存在的工作目录

不要在电脑 A 或 B 使用这个 Devbox token 启动 connector；它属于电脑 C。

---

## 5. 电脑 C：运行 Agent Devbox

### 5.1 验证本地 Agent

```bat
where claude
claude --version
claude
```

必须先在电脑 C 本地完成 Claude Code 登录。deepbox 不接触 Claude 凭证。

### 5.2 安装 connector

当前 Private Alpha 直接使用同一仓库：

```bat
cd /d C:\Code
git clone https://github.com/yusx-swapp/deepbox.git
cd /d C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

### 5.3 验证 Server 可达

```bat
curl https://server-name.example-tailnet.ts.net/api/health
```

必须返回 `status=ok`。如果域名无法解析，先检查 `tailscale status` 和 MagicDNS。

### 5.4 运行连接诊断

先设置从 UI 复制的 token，并运行 doctor：

```bat
cd /d C:\Code\deepbox
set DEEPBOX_SERVER_URL=https://server-name.example-tailnet.ts.net
set DEEPBOX_TOKEN=hpc_box_...
.venv\Scripts\python -m connector --doctor
```

它依次检查 URL/TLS、`/api/health`、protocol version 和 token authentication；不会打印 token。
全部显示 `[OK]` 后再启动 connector。

### 5.5 启动 connector

```bat
scripts\start-connector.cmd
```

也可以直接：

```bat
.venv\Scripts\python -u -m connector
```

connector 会：

1. 通过 HTTPS `GET /api/me` 验证 token。
2. 检查 Server protocol version。
3. 探测本机 runtime。
4. 通过 WSS 建立 `/ws/devbox`。
5. 上报存活 Session。
6. 收到用户 New/Resume 后在本机启动或恢复 PTY。

Token 只通过 `Authorization: Bearer` header 发送；Server 不接受 WS query-string token，避免 token
进入 URL、代理日志和浏览器历史。

### 5.6 可选：supervisor / transport 双进程

默认命令仍是兼容的 all-in-one 模式。若要让网络 transport 独立重启而本地 PTY 继续存活，请在两个终端中使用
相同的 `DEEPBOX_SERVER_URL` 和 `DEEPBOX_TOKEN` 环境变量。先启动长期驻留的 session supervisor：

```bat
.venv\Scripts\python -u -m connector --mode supervisor
```

再启动可重启的网络 transport：

```bat
.venv\Scripts\python -u -m connector --mode transport
```

两者通过当前用户专属的 Windows named pipe（POSIX 上为 `0600` Unix socket）通信，并使用当前用户本地密钥做
带 5 秒超时的双向 HMAC 握手；帧是最大 1 MiB 的换行 JSON，不使用 pickle。同一时刻只接受一个 transport。停止或重启
transport 不会关闭 supervisor 持有的 PTY；停止 supervisor 才会关闭这些 PTY。当前 pending 输出仍只在内存中，
因此整台机器或 supervisor 崩溃后的 durable replay 属于后续磁盘 spool Cut，不应误解为本 Cut 已保证。

---

## 6. 端到端验收

在电脑 A：

1. 刷新 deepbox。
2. 确认电脑 C 对应的 Devbox 是绿色 online。
3. 确认 `@claude` 是 online。
4. 打开/新建 Session。
5. 看到电脑 C 上真实 Claude Code TUI。
6. 输入一条测试消息并确认回复。

然后验证平台价值：

1. 关闭电脑 A 浏览器标签。
2. 等待 Claude 在电脑 C 继续运行。
3. 重新打开页面，Resume 同一 live Session。
4. 确认屏幕和上下文仍在。
5. 在电脑 B 重启 Server，但不要停止电脑 C connector。
6. 确认 connector 自动重连、Session 恢复。

---

## 7. Tailscale ACL 建议

默认 Tailnet 内其他成员可能可以访问 Serve URL。deepbox 仍要求登录，但建议增加网络层最小权限：

- 电脑 A 可以访问电脑 B 的 HTTPS 服务。
- 电脑 C 可以访问电脑 B 的 HTTPS 服务。
- 其他设备不能访问电脑 B 的 deepbox 服务。
- 无设备需要直接访问电脑 C 的 Agent 端口；connector 只做出站连接。

具体 ACL/Grants 语法取决于 Tailnet 管理策略，配置前参考当前 Tailscale 文档。不要为了方便使用
Funnel 替代 ACL。

---

## 8. 常见问题

### 浏览器打不开 URL

```bat
tailscale status
tailscale serve status
curl https://<server>/api/health
```

检查电脑 B 上 Server 命令窗口是否仍在运行。

### `/api/ready` 返回 503

检查：

- `DEEPBOX_DATABASE_URL` 目录存在/可创建。
- `DEEPBOX_DATA_DIR` 可写。
- 运行 Server 的 Windows 用户有目录权限。

### 浏览器能登录，但 Terminal WS 被拒绝

检查 `.env`：

```text
DEEPBOX_PUBLIC_URL
DEEPBOX_ALLOWED_ORIGINS
```

它们必须与浏览器地址栏里的 origin 完全一致（协议、主机和端口）。修改 `.env` 后重启 Server。

### Connector 报 protocol mismatch

Server 和电脑 C 的仓库版本不同。两端都执行 `git pull` 并重新安装依赖。

### Connector 401 / 4001

- token 粘贴错误。
- token 已吊销。
- token 属于另一台 Devbox。
- 环境变量带了多余引号/空格。

在 UI 为电脑 C 对应 Devbox 轮换新 token；不要复用其他 Devbox token。

### Devbox online，但 Agent 启动失败

在电脑 C 本地检查：

```bat
where claude
claude --version
```

并确认 Agent 配置里的 `cwd` 在电脑 C 上存在。

### HTTPS 证书问题

只能使用 Tailscale Serve 输出的 HTTPS 域名，不要把 `https://` 加到裸 Tailscale IP 上。确保电脑 B
已在 Tailnet 中启用 HTTPS/Serve。

---

## 9. 停止与回滚

停止 Server：在电脑 B 的 Server 窗口按 `Ctrl+C`。

停止 connector：在电脑 C 的 connector 窗口按 `Ctrl+C`。注意当前架构下停止 connector 会终止它
托管的 PTY Session；Session Supervisor 尚未实现。

关闭 Tailscale Serve：根据当前 CLI 版本运行：

```bat
tailscale serve reset
```

这不会删除 deepbox 数据。

---

## 10. 当前限制

- Server/connector 尚未安装为 Windows Service，需要保持命令窗口运行。
- connector 使用内存 FIFO；Server 短暂重启可恢复，但 connector 自身退出会丢 PTY。
- 当前不是公开互联网部署方案。
- Tailscale 解决网络加密和设备可达性，不代替 deepbox 应用层认证、权限和 recording 隐私。
- Session Control Center、durable seq/ACK spool 和完整 Replay UI 属于后续 Cuts。
