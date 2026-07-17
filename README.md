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
- [`docs/design.md`](docs/design.md) — 整体架构
- [`docs/implementation.md`](docs/implementation.md) — 实现说明
- [`docs/persistence.md`](docs/persistence.md) — **会话持久化设计（平台的立身之本）**

## 快速开始（P0，mock agent 端到端）
```bat
cd C:\Code\deepbox
python -m pip install -r requirements.txt
:: 1) 启动 server
python -m uvicorn server.app.main:app --reload --port 8000
:: 2) 浏览器打开 http://localhost:8000  注册/登录，创建 Devbox（复制 token），
::    在该 Devbox 下建一个 runtime=mock 的 agent
:: 3) 启动 connector（新终端）
set DEEPBOX_SERVER_URL=http://localhost:8000
set DEEPBOX_TOKEN=hpc_box_...
python -m connector
:: 4) 回到 web，打开那个 agent 的终端，开始交互
```
