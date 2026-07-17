# deepbox 实现说明（How it works）

> 本文解释 P0 骨架 + 真实 Claude CLI 接入是**如何实现**的，配合 `design.md`（设计）阅读。
> 目标读者：想读代码、想扩展或部署这套系统的人。

---

## 1. 全景：一条消息的完整旅程

```
┌── 浏览器 (web/) ─────────┐        ┌── server (server/app) ──┐      ┌── 用户 devbox (connector/) ──┐
│ xterm.js 终端            │        │ FastAPI + WebSocket      │      │ connector 进程                │
│                          │        │ + SQLite + Hub           │      │                               │
│ 用户敲键 ── onData ──────┼─WS────▶│ /ws/term 收 input        │      │                               │
│                          │        │   Hub.to_devbox()        │──WS─▶│ 收 input → 写 PTY stdin       │
│                          │        │                          │      │        claude.exe (真实 CLI)  │
│                          │        │                          │      │ PTY 输出 ── on_output ────────┤
│ term.write(data) ◀───────┼─WS─────┤ Hub.to_session_humans()  │◀─WS──┤ 发 output 帧                  │
│  (逐字节渲染 ANSI)        │        │ /ws/term 透传            │      │                               │
└──────────────────────────┘        └──────────────────────────┘      └───────────────────────────────┘
        HumanConn                          Hub (内存路由表)                    DevboxConn + PtySession
```

**一句话**：server 是一个纯粹的**字节流交换机**。它不解析、不理解、不存储 CLI 的输出内容，
只负责把"哪个用户的按键"送到"哪台 devbox 的哪个 PTY"，再把 PTY 的输出送回"正在看这个
会话的浏览器"。智能（Claude）100% 跑在用户机器上。

---

## 2. 服务端（`server/app/`）

### 2.1 `models.py` — 数据层
SQLAlchemy 2.0 声明式模型 + SQLite。六张表：
`user / devbox / token / agent / session / message`。
- 用 `mapped_column` 强类型；`init_db()` 建表并暴露 `SessionLocal` 工厂。
- 关系用 `cascade="all, delete-orphan"`：删 user 级联删它的 devbox/token/agent。

### 2.2 `util.py` — 凭证与 id
- `new_id()`：`uuid4().hex`。
- `new_token()`：`hpc_box_` + 32 随机字节的 hex。返回 `(完整token, sha256, preview)`。
  **数据库只存 sha256**，完整 token 只在创建时返回一次。
- `hash_password/verify_password`：`salt$sha256(salt+pw)`（P0 够用；生产应换 bcrypt/argon2）。

### 2.3 `hub.py` — 实时路由核心
一个进程内单例 `Hub`，维护两类连接和几张路由表：
```python
DevboxConn { ws, devbox_id, agent_ids }     # 一个 connector 的 WS
HumanConn  { ws, user_id, sessions }         # 一个浏览器的 WS

devboxes:         devbox_id  -> DevboxConn
agent_to_devbox:  agent_id   -> devbox_id     # 找 agent 属于哪台 devbox
session_watchers: session_id -> {HumanConn}   # 谁在看这个会话
```
关键方法：
- `to_devbox(agent_id, frame)`：把帧发给 host 该 agent 的那个 connector。
- `to_session_humans(session_id, frame)`：广播给所有正在看该会话的浏览器。
> dataclass 加了 `eq=False`，让连接对象按**身份**可哈希（否则含可变字段的 dataclass 不能进 set）。

### 2.4 `main.py` — FastAPI 应用
三块内容：

**(a) REST 管理面**（浏览器,cookie 认证）
`register/login/logout` → 用 `itsdangerous` 签名 cookie 存 `uid`。
`/api/devboxes`（增删查、轮换 token）、`/api/.../agents`（增删）、
`/api/agents/{id}/sessions`（开会话）、`/api/sessions/{id}/messages`。
每个受保护路由都调 `current_user()` 校验 cookie。

**(b) REST 运行时面**（connector,Bearer token 认证）
`GET /api/me`：connector 启动时拉取"我这台 devbox 要跑哪些 agent"。
`POST /api/devboxes/{id}/runtimes`：connector 上报本机探测到的可用 CLI。
用 `devbox_from_bearer()` 校验：查 token 的 sha256 → 定位 Devbox。

**(c) 两个 WebSocket**
- `/ws/devbox`（connector 用）：从 `Authorization: Bearer` header 取 token → 校验 →
  把该 devbox 所有 agent 置 `online` → 注册 `DevboxConn` 到 Hub → 下发 `hello`。
  之后循环收 connector 发来的 `output/ready/exit/presence` 帧，转发给对应会话的浏览器。
  断开时把 agent 置 `offline`。
- `/ws/term`（浏览器用）：从 cookie 取登录态 → 注册 `HumanConn`。
  收到 `open{session_id}` → `Hub.watch()` 订阅该会话 + 通知 connector 开 PTY；
  收到 `input/resize/close` → 补上 `agent_id` → `Hub.to_devbox()` 转给 connector。

---

## 3. connector（`connector/`）—— 本设计的灵魂

用户在自己机器上自启的进程。**智能和 API key 都在这里，server 永远看不到。**

### 3.1 `client.py` — 主循环
1. `GET /api/me`（带 Bearer token）→ 拿到要跑的 agent 名单（runtime/cwd/launch_cmd）。
2. `probe_runtimes()`：用 `shutil.which()` 探测本机装了哪些 CLI（claude/copilot/codex），
   `POST /runtimes` 上报（让 UI 显示 capabilities）。
3. 开 `/ws/devbox` WS（header 带 Bearer token），收 `hello`。
4. 事件循环处理 server 帧：
   - `open`  → `open_pty(agent_id, session_id)`：起一个 `PtySession`。
   - `input` → `pty.write(data)`：把用户按键写进 CLI 的 stdin。
   - `resize`→ `pty.resize(cols, rows)`：同步终端尺寸。
   - `close` → `pty.kill()`。
5. PTY 有输出就发 `output` 帧;进程退出发 `exit` 帧。
6. 断线自动重连（外层 `while True` + 3s 退避）。PTY 输出先进入 connector 内存 FIFO，
   sender 只有在 WS 发送成功后才弹出队首；server 离线时 reader 不会死、帧不会被移除，重连后
   按序补发。connector 还会重新上报存活的 session，让 UI resume 同一 PTY。

`resolve_cmd(runtime, launch_cmd)`：把 runtime 名映射到实际命令。
`launch_cmd` 优先（可自定义任意命令），否则用默认表：
```
mock         → python -m connector.mockcli   (无需任何真实 CLI 即可测全链路)
claude-code  → claude
copilot-cli  → copilot
codex-cli    → codex
```

### 3.2 `pty_session.py` — 跨平台伪终端
**为什么必须用 PTY**：Claude Code/Copilot/Codex 是**交互式 TUI**，会检测"是不是真终端"
来决定渲染彩色框、光标定位、快捷键。普通管道（subprocess.PIPE）会让它们退化或拒绝运行。
PTY（伪终端）让 CLI 以为自己连着真终端,于是输出完整的原生界面。

- **Windows**：`pywinpty`（封装 ConPTY）。`PtyProcess.spawn(cmd, cwd, dimensions=(rows,cols))`。
  用后台线程 `run_in_executor` 阻塞读，读到就 `await on_output()`。
- **POSIX**：内置 `pty.fork()` + `os.execvp`，子进程跑 CLI；父进程 `os.read(fd)` 读输出，
  `ioctl(TIOCSWINSZ)` 设尺寸。
- **初始尺寸很关键**：Claude 的 TUI 需要合理的 cols/rows 才能正确布局，所以 `PtySession`
  构造时就带默认 `120x30`,浏览器连上后再用第一个 `resize` 帧校准。

### 3.3 `mockcli.py` — 测试替身
一个假 CLI：读 stdin 行，回 `you said: ...`。让整条链路（WS 协议、Hub 路由、PTY 转发）
不依赖任何真实 agent 就能端到端测试。

---

## 4. web（`web/`）—— 极简 SPA

### 4.1 `index.html`
挂载 xterm.js（CDN）、xterm-addon-fit,加一点深色主题 CSS。

### 4.2 `app.js`
- **认证**：登录/注册 → 后端设 cookie → `boot()` 拉 `/api/me/user`。
- **管理界面**：左侧列出 Devbox（在线绿点）、每个 Devbox 下的 agent（presence 绿点）、
  创建 Devbox（弹出一次性 token）、建 agent（选 runtime/cwd）、轮换 token、删除。
- **终端**：`setupTerm()` 建 xterm 实例 + FitAddon。
  点某个 agent → `POST .../sessions` 建会话 → 连 `/ws/term`：
  - `term.onData` → 发 `input` 帧（用户每次按键）。
  - 收 `output` 帧 → `term.write()`（逐字节渲染，ANSI 序列由 xterm 解释）。
  - `window.onresize` → `fit.fit()` + 发 `resize` 帧。
  于是浏览器里的终端 = devbox 上那个 CLI 的实时镜像。

---

## 5. 真实 Claude 接入是怎么跑通的

零特殊代码。步骤只是：
1. 建 agent 时 `runtime="claude-code"`,connector 的 `resolve_cmd` 把它解析成 `claude`。
2. connector 收到 `open` → `PtySession(['claude'], cwd, ...)` 起真实进程。
3. Claude 检测到 PTY → 渲染完整 TUI → 字节流经 `output` 帧 → server 透传 → xterm 渲染。
4. 用户输入 → `input` 帧 → 写进 Claude 的 stdin → Claude 正常响应。

验证：`tests_claude.py`（E2E PASS）、`snapshot.py`（用 pyte 把流还原成文本快照，
肉眼确认欢迎框 + `● Hello!` 回复都在）。

---

## 6. 端到端测试与工具

| 文件 | 作用 |
|---|---|
| `tests_e2e.py` | mock runtime 全链路（注册→建 agent→connector→WS→输入→回显）|
| `tests_claude.py` | 真实 Claude CLI 全链路 |
| `snapshot.py` | 用 pyte 终端模拟器把 PTY 流渲染成文本快照（= 浏览器所见）|
| `provision_demo.py` | 建 demo/demo 账号 + Devbox + Claude agent,打印 token |

---

## 7. 现在的边界（P0 已知未做）

- 会话字节流**不落库**：刷新浏览器会重开一屏（Claude 会重画,但历史滚动丢失）。
- 密码 hash 用 sha256（应升级 bcrypt/argon2）。`SECRET` 硬编码（应走环境变量）。
- 单进程内存 Hub：不支持多实例横向扩展（需外部 pub/sub 如 Redis）。
- 无权限分享模型：agent 只有 owner 能用。
- 无 TLS（本地开发 http/ws）。

这些都在 `design.md` 的 Roadmap（P1–P3）里。
