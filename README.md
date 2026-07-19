# deepbox

一个 **agent 交换机 / 管理面**：把你本地 devbox 里的 agent CLI（Claude Code、
Copilot CLI、Codex CLI…）连接到 server，然后登录平台，就能像在本地终端里一样和它们交互。

> 我们是平台，不是 AI 产品。Server 永不跑模型、永不持有 key。智能与凭证都留在你的 devbox 上。

详见 [`docs/design.md`](docs/design.md)。

## 组件
- `server/` — FastAPI + WebSocket + SQLite。身份 / 频道 / 消息 / presence / 帧中继 /
  **会话持久化（LiveSession + pyte 屏幕 + asciicast DVR 录制）**。
- `connector/` — 用户自启进程，桥接本地 CLI 的 PTY 会话 ↔ server。
- `web/` — 极简 web（登录 + 管理 + xterm.js 终端，**自动重连 + 屏幕还原**）。

## 文档
- [`docs/product-design.md`](docs/product-design.md) — **产品定位、用户、对象模型、核心流程与设计原则**
- [`docs/planning.md`](docs/planning.md) — **从当前 P0 到 MVP、Internal Beta 和团队产品的实施计划**
- [`docs/remote-deployment.md`](docs/remote-deployment.md) — **三台 Windows 电脑通过 Tailscale 远程连接**
- [`docs/azure-deployment.md`](docs/azure-deployment.md) — **Azure App Service (Linux) 部署 server**
- [`docs/design.md`](docs/design.md) — 整体技术架构
- [`docs/implementation.md`](docs/implementation.md) — 当前代码实现说明
- [`docs/onboarding.md`](docs/onboarding.md) — **首个 owner 引导、角色、邀请与成员生命周期（P1 Cut 1）**
- [`docs/operations.md`](docs/operations.md) — **运维手册：结构化日志、连接可见性、就绪检查、备份/恢复、容量告警、版本与冒烟检查（P1 Cut 3）**
- [`docs/persistence.md`](docs/persistence.md) — 会话持久化设计（平台的立身之本）

## 快速开始（P0，mock agent 端到端）
```bat
cd C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
:: connector 机器另装（含 Windows-only pywinpty）：
::   .venv\Scripts\python -m pip install -r requirements-connector.txt
:: 1) 启动本地开发 server（默认 127.0.0.1:8077）
.venv\Scripts\python -m server
:: 2) 浏览器打开 http://localhost:8077，注册/登录，创建 Devbox（复制 token），
::    在该 Devbox 下建一个 runtime=mock 的 agent
:: 3) 启动 connector（新终端）
set DEEPBOX_SERVER_URL=http://localhost:8077
set DEEPBOX_TOKEN=hpc_box_...
.venv\Scripts\python -m connector
:: 4) 回到 web，打开那个 agent 的终端，开始交互
```
