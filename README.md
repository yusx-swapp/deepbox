# deepbox

一个 **agent 交换机 / 管理面**：把你本地 devbox 里的 agent CLI（Claude Code、
Copilot CLI、Codex CLI…）连接到 server，然后登录平台，就能像在本地终端里一样和它们交互。

> 我们是平台，不是 AI 产品。Server 永不跑模型、永不持有 key。智能与凭证都留在你的 devbox 上。

详见 [`docs/design.md`](docs/design.md)。

## 组件
- `server/` — FastAPI + WebSocket + SQLite。身份 / 频道 / 消息 / presence / 帧中继 /
  **Protocol v3 durable recording、checkpoint、asciicast 导出与 retention**。
- `connector/` — 用户自启进程，桥接本地 agent CLI ↔ server；一次安装后通过
  `deepbox connect` / `doctor` / `status` / `project` / `upgrade` 管理，日常连接不会刷新安装目录。
  connector-only runtime registry 为 Claude Code / Copilot CLI / Codex CLI / mock 统一构造并校验
  argv，capability 对 Server/Web 保持 opaque。
- `web/` — 面向远程 devbox / agent 的 **Terminal-first Switchboard** web UI：
  克制暗色 topbar + 左侧 Fleet 面板（online/total 汇总、搜索、紧凑 devbox/agent 清单）
  + 右侧 xterm 终端主区；**⌘K / Ctrl+K command palette**（筛选打开 agent、打开
  history、创建 devbox、进入 owner），自定义 modal/form 取代浏览器 prompt/alert；一次性 token
  只在内存/DOM 显示，并分别提供 **一次安装** 与 **`deepbox connect`** 的可复制命令；
  **自动重连 + 屏幕还原 + Session DVR 回放**。
  样式为 token 驱动（`web/styles.css`），状态一律「圆点+文字」不只靠颜色，含
  focus-visible、prefers-reduced-motion 与窄屏纵向堆叠响应式；DOM-free 纯逻辑在
  UMD 模块 `web/ui.js`，由 `web/ui.test.js`（node:test）覆盖。
- Workspace collaboration — 每个用户有 personal workspace 且可创建更多空间；左栏按
  **Workspace → Devbox → Agent** 展示，`viewer / operator / admin / owner` 四级角色约束全部资源。
- Microsoft / local sign-in — Azure 可由 App Service Easy Auth 接入组织 Entra 账号和个人 Microsoft
  账号；本地密码登录保留给开发环境和 hybrid 迁移。
- Invitations — workspace owner/admin 签发单次、过期、邮箱绑定的加入链接；deployment owner
  另行管理本地账号邀请、禁用和重新启用。
- Security baseline — Argon2id + 旧 hash 透明升级、生产 Origin allowlist、分层 rate limit、
  security headers、脱敏 JSON audit、凭证吊销即时断连、owner-only recording secure erase。

## 文档
- [`docs/product-design.md`](docs/product-design.md) — **产品定位、用户、对象模型、核心流程与设计原则**
- [`docs/planning.md`](docs/planning.md) — **从当前 P0 到 MVP、Internal Beta 和团队产品的实施计划**
- [`docs/remote-deployment.md`](docs/remote-deployment.md) — **三台 Windows 电脑通过 Tailscale 远程连接**
- [`docs/azure-deployment.md`](docs/azure-deployment.md) — **Azure App Service (Linux) 部署 server**
- [`docs/install.md`](docs/install.md) — `deepbox` 命令的一次安装、日常连接、显式升级与 Windows 安全刷新
- [`docs/design.md`](docs/design.md) — 整体技术架构
- [`docs/implementation.md`](docs/implementation.md) — 当前代码实现说明
- [`docs/onboarding.md`](docs/onboarding.md) — **首个 owner 引导、角色、邀请与成员生命周期（P1 Cut 1）**
- [`docs/operations.md`](docs/operations.md) — **运维手册：结构化日志、连接可见性、就绪检查、备份/恢复、容量告警、版本与冒烟检查（P1 Cut 3）**
- [`docs/persistence.md`](docs/persistence.md) — 会话持久化设计（平台的立身之本）

## 连接用户机器：安装一次，随时连接

先从浏览器或 [`docs/install.md`](docs/install.md) 复制对应平台的一次性安装命令。安装完成后，
每次只需设置浏览器签发的 server URL / devbox token，再运行：

```text
deepbox connect
```

升级是显式操作：`deepbox upgrade`。只有安装/升级会刷新 `~/.deepbox/app`；
`deepbox connect` 不下载、不重装，也不会触碰正在使用的安装目录。

## 快速开始（P0，mock agent 端到端）
```bat
cd C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
:: connector 机器另装（含 Windows-only pywinpty）：
::   .venv\Scripts\python -m pip install -r requirements-connector.txt
:: 1) 启动本地开发 server（默认 127.0.0.1:8077）
.venv\Scripts\python -m server
:: 2) 浏览器打开 http://localhost:8077，注册/登录，创建 Devbox 并复制一次性 token
:: 3) 启动 connector（新终端）；它会探测并上报本机可用 runtime
set DEEPBOX_SERVER_URL=http://localhost:8077
set DEEPBOX_TOKEN=hpc_box_...
.venv\Scripts\python -m connector
:: 4) 回到 Fleet 添加 Agent，从下拉框选择 mock，再打开终端交互
::    Agent 可直接删除；在线 connector 会立即同步新增和删除，无需重连
```

## Web UI 快捷键
- `Ctrl/Cmd + K` — 打开 command palette（筛选打开 agent、打开 history、创建
  devbox、进入 owner）。
- palette 内 `↑` / `↓` 移动选择，`Enter` 执行，`Esc` 关闭。
- Fleet 搜索框实时过滤 devbox 与 agent。
- 会话内 keyboard lease：`Request` / `Take keyboard` / `Release` / `Hand off`
  （Viewer 永远只读）。
