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

**一句话**：server 是一个纯粹的**字节流交换机 + durable recording control plane**。它不解析、
不理解 CLI 输出语义，也绝不运行模型或持有模型密钥；它只路由按键/PTY 字节，并把 Protocol v3
输出按 retention policy 持久化供恢复与回放。智能（Claude/Copilot/Codex）100% 跑在用户机器上。

---

## 2. 服务端（`server/app/`）

### 2.1 `models.py` — 数据层
SQLAlchemy 2.0 声明式模型 + SQLite。核心身份/运行表为
`user / devbox / token / agent / session / message / bootstrap_state / invitation`；DVR 使用
`recording_frame / recording_checkpoint`；Cut 8 增加 `organization / workspace / membership /
session_participant / keyboard_lease`。
- 用 `mapped_column` 强类型；`init_db()` 建表并暴露 `SessionLocal` 工厂。
- 关系用 `cascade="all, delete-orphan"`：删 user 级联删它的 devbox/token/agent。
- **P1 Cut 1 附加列/表**（对既有 SQLite 库用 `_migrate()` 做**加列式**迁移，不改数据）：
  - `user.role`（默认 `member`，取值 `owner`/`member`）、`user.disabled_at`（可空）。
  - `bootstrap_state`：单例行 `id=1`，与首个 owner **同一事务**插入；主键唯一性构成
    持久、并发安全的原子闩锁——并发首启只有一方提交成功。
  - `invitation`：仅存 `token_hash`（SHA-256），带 `expires_at / redeemed_at /
    revoked_at`；兑换是单条条件 `UPDATE`，保证一次性、过期/吊销即失效。

### 2.1a P1 Cut 1 路由（onboarding）
详见 `onboarding.md`。摘要：
- `GET /api/auth/bootstrap-status` → 安全布尔；`POST /api/auth/bootstrap` → 一次性建首个 owner，
  凭据按 SHA-256 比对，任何非法/不可用一律通用 `404`，从不回显 token/hash。
- `POST/GET/DELETE /api/invitations`（owner）：铸造（有界 TTL、明文只回一次）、列出元数据、吊销。
  浏览器生成 `/#invite=...` fragment 链接（不进入 HTTP/access log），首次加载立即从地址栏移除并仅在内存保留；
  注入登录表单前做 HTML attribute escaping。
- `POST /api/auth/register` 支持 `invite_code`：原子兑换、创建 member。开发自注册仍受
  `DEEPBOX_REGISTRATION_ENABLED` 控制，生产须保持 false。
- `GET /api/users`、`POST /api/users/{id}/disable|enable`（owner）：禁用/恢复成员；禁用用户无法登录，
  现有浏览器会话与 connector bearer token 失效，活跃 WebSocket 立即关闭；绝不禁用最后一个启用的 owner（含自锁）。

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
`register` 受 `DEEPBOX_REGISTRATION_ENABLED` 控制：production 默认 false，
关闭时路由返回 403（fail-closed，避免公网开放注册）。
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

### 3.0 P2 Cut 4：Supervisor / Transport 拆分
connector 拆成两半，二者只经 IPC 抽象（`ipc.py`）通信：
- **`supervisor.py`（sessiond）——会话所有权**：拥有全部 `PtySession` 生命周期；`detach()` 只断开 transport，绝不 kill PTY；只有 `shutdown()` 才结束 PTY。每次新 PTY 启动生成一个 UUID `pty_instance_id`，同一 PTY 的幂等 `open` 复用该值，PTY 退出或终止时清除。
- **`transport.py`——WebSocket 传输**：不拥有 PTY。`TransportSession` 在 IPC 与 `/ws/devbox` 之间转发帧，心跳和网络重连都不能改变 PTY 生命周期。
- **IPC**：split 模式使用 Windows named pipe / POSIX `0600` Unix socket；消息为限制 1 MiB 的 newline-JSON，并有 HMAC 握手。all-in-one 模式使用相同 `Channel` 接口的 `LoopbackChannel`。

### 3.0a P2 Cut 5：Protocol v3 durable spool、server ACK 与精确 resume

Protocol v3 的输出身份是 `(session_id, pty_instance_id, seq)`：

- **先落盘再发送**：`SessionSupervisor.emit()` 对 `output` 调用 `enqueue_output()`。`connector/spool.py` 的真实运行时 `DiskSpool` 使用 stdlib `sqlite3`，启用 WAL 和 `synchronous=FULL`；`outbox` 以三元组唯一约束保存 payload，`ack_state` 保存各 PTY 实例最后连续 ACK，`input_receipts` 保存输入去重 ID。单测可注入 `InMemorySpool`。
- **序号域**：`seq` 在 `BEGIN IMMEDIATE` 事务中按 PTY 实例分配，取 `max(last_acked_seq, outbox max seq)+1`，从 1 开始且不复用。ready/presence/exit/input_ack 等控制帧只进进程内队列，既不占 PTY 输出序号，也不会在 sessiond 重启后陈旧重放。
- **本地 FIFO**：`drain_to()` 优先发送进程内控制帧，再发送最旧 durable `ord`，并记录当前 `delivery_id`。只有匹配当前 in-flight 的 `ipc_delivery_ack` 才会推进；durable output 还必须仍是 spool 队首，SQLite ACK 只允许 `last_acked_seq+1`，事务提交后才删除 outbox 行。
- **ACK 不是 `send()` 成功**：transport 在 `ws.send(output)` 前后都不会释放 output。它等待 server 返回完全匹配的 `{type:"ack", session_id, pty_instance_id, seq}`，然后才把本地 `ipc_delivery_ack` 发给 supervisor。陈旧/错配 ACK 被忽略；`resend.expected_seq` 等于当前 seq 时重发同一行，不一致或 server error 时 fail closed，保留 spool 给下一次连接。非 output 控制帧仍以 `ws.send()` 完成为本地发送边界。
- **server 先持久化再 ACK**：`server/app/recording.py` 的 `RecordingStore` 把输出写入 `recording_frames` 并 commit 后才允许 `/ws/devbox` 回 ACK。相同三元组+相同 payload 是幂等 duplicate（重新 ACK、不重写）；payload 冲突返回 error；gap 返回 `resend(expected_seq)`，不推进 ACK。server 按 devbox/agent ownership 校验，不能跨机器写历史。
- **断线恢复**：transport 在 server ACK 前崩溃、WebSocket 在 persist 后 ACK 前断开、或整机重启，outbox 行都仍在。CLI 用 `sha256(server_url + "
" + token)[:16]` 选择用户私有 spool 路径，路径不包含 token；重连按 `ord` 重放，server 去重后精确 ACK。
- **输入幂等**：browser input 缺少 ID 时 server 生成 UUID `client_input_id`；supervisor 在写 PTY 前通过 `input_receipts` 原子登记，同一 ID 重放不会二次写入。首次和 duplicate 都返回 `input_ack(status="delivered")`。server 只在收到 delivery ACK 后把该输入写入 cast，并把 ACK 转发给 session browser。
- **可观测性**：`SessionSupervisor.status()` 返回 `pending_frames`、`pending_bytes`、各 PTY 实例 `last_acked_seq/next_seq` 以及当前 `pty_instance_id`。

`open_spool(server_url, token)` 只在真实 CLI 模式注入；普通 `Connector(...)` / `SessionSupervisor(...)` 构造默认使用内存 spool，不会在单测或库调用时创建用户文件。server 仍只持有终端记录和非 secret 元数据，绝不接触模型或本地 API key。

### 3.1 `client.py` — 主循环（组合 supervisor + transport）
1. `GET /api/me`（带 Bearer token）→ 拿到要跑的 agent 名单（runtime/cwd/launch_cmd）。
2. `probe_runtimes()`：用 `shutil.which()` 探测本机装了哪些 CLI（claude/copilot/codex），
   `POST /runtimes` 上报（让 UI 显示 capabilities）。
3. （all-in-one）每个 WS 连接新建一对 `LoopbackChannel`，`supervisor.attach()`，开 drain / control 两个 task；双进程下改由 `SupervisorService.serve()` 接受 transport 连接，`run_transport()` 连本地 sessiond。
4. 开 `/ws/devbox` WS（header 带 Bearer token），收 `hello`，交给 `TransportSession.run(ws)`。
5. server 帧经 transport→channel→`supervisor.handle_control()`；PTY 输出经
   `supervisor.emit()`→`pending`→`drain_to()`→transport→WS。
6. 断线自动重连（外层 `while True` + 3s 退避）。WS 断开时 `supervisor.detach()`，
   PTY 继续跑、output 继续进 `pending`，重连后新 transport 按序补发并 resume 同一 PTY。

`connector/runtimes.py` 是 runtime 单一事实来源。`RuntimeAdapter` 描述稳定 id、label、
`base_argv`、model flag/allowlist、permission mode argv、非机密 environment 和探测提示；
注册表内置 `mock`、`claude-code`、`copilot-cli`、`codex-cli`。`client.probe_runtimes()`
遍历注册表并把 install/version/path/features 作为 opaque capability JSON 上报，Server/Web
不解析 runtime-specific 字段。

`resolve_cmd(runtime, launch_cmd, model, permission_mode)`：显式 `launch_cmd` 仍优先，但只用
`shlex.split` 拆成 argv；否则由共享 `build_command()` 构造 argv。两条路径都会拒绝空 token、
控制字符和 shell 元字符，并以 argv 直接 spawn（不经过 shell）。未知 runtime 为兼容旧数据
回退到 `mock`。新增 runtime 只需定义并 `register()` 一个 adapter，无需修改 supervisor、Server
或 Web。

### 3.2 `pty_session.py` — 跨平台伪终端
**为什么必须用 PTY**：Claude Code/Copilot/Codex 是**交互式 TUI**，会检测"是不是真终端"
来决定渲染彩色框、光标定位、快捷键。普通管道（subprocess.PIPE）会让它们退化或拒绝运行。
PTY（伪终端）让 CLI 以为自己连着真终端,于是输出完整的原生界面。

- **Windows**：`pywinpty`（封装 ConPTY）。`PtyProcess.spawn(cmd, cwd, dimensions=(rows,cols))`。仅 connector 需要，安装自 `requirements-connector.txt`（`sys_platform=="win32"` 门控）；根 `requirements.txt` 只含 server 依赖，保持 Linux/Oryx 可装。
  用后台线程 `run_in_executor` 阻塞读，读到就 `await on_output()`。
- **POSIX**：内置 `pty.fork()` + `os.execvp`，子进程跑 CLI；父进程 `os.read(fd)` 读输出，
  `ioctl(TIOCSWINSZ)` 设尺寸。
- **初始尺寸很关键**：Claude 的 TUI 需要合理的 cols/rows 才能正确布局，所以 `PtySession`
  构造时就带默认 `120x30`,浏览器连上后再用第一个 `resize` 帧校准。

### 3.3 `mockcli.py` — 测试替身
一个假 CLI：读 stdin 行，回 `you said: ...`。让整条链路（WS 协议、Hub 路由、PTY 转发）
不依赖任何真实 agent 就能端到端测试。

---

## 4. web（`web/`）—— Terminal-first Switchboard SPA

### 4.1 `index.html`
挂载 xterm.js（CDN）、xterm-addon-fit，含最小内联 reset。**此文件保持不变**；
`app.js` 在运行时注入 `<link rel="stylesheet" href="/static/styles.css">`（在内联
reset 之后加载，故外部主题为唯一事实来源）。

### 4.2 `app.js`
- **认证**：登录/注册/首 owner bootstrap → 后端设 cookie → `boot()` 拉 `/api/me/user`。
- **主 shell**：克制品牌 topbar（搜索/⌘K 入口、owner 入口、用户、退出）+ 左侧
  **Fleet 面板**（标题、online/total 汇总、搜索、紧凑 devbox/agent 清单）+ 右侧
  **Terminal stage**；未选 agent 时显示空状态与快捷提示。devbox/agent 状态均为
  「圆点 + 文字」。
- **Command palette**（`Ctrl/Cmd+K`）：overlay（不引入路由），筛选打开 agent、打开
  history、创建 devbox、进入 owner（仅 owner）；`↑/↓` 导航、`Enter` 执行、`Esc` 关闭。
- **模态**：createDevbox / createAgent / 删除确认 / 错误提示都用 app 内自定义
  modal/form 取代浏览器 `prompt/alert/confirm`。**一次性 token 只渲染进内存中的
  modal DOM，绝不写 storage/cookie/URL/日志。** modal 提供 Copy token 和 Copy command；完整 Windows
  命令由 `web/ui.js::windowsConnectorCommand()` 纯函数生成，剪贴板 API 不可用时回退到临时 textarea。
- **终端**：`setupTerm()` 建 xterm 实例（主题对齐 UI token）+ FitAddon。
  点某个 agent → 优先 resume 仍存活的 live PTY，否则 `POST .../sessions` 建会话 →
  连 `/ws/term`：`term.onData` 发 `input` 帧；收 `output`/`restore` 帧 `term.write()`；
  `window.onresize` → `fit.fit()` + `resize` 帧；断线指数退避重连。
- 所有服务端字符串（name/handle/runtime/capabilities…）经 `esc()` HTML 转义；
  capability blob 视为 opaque，不按固定结构解析。

### 4.3 `ui.js` + `ui.test.js`
DOM-free 的纯逻辑抽到 UMD 模块 `web/ui.js`：fleet 汇总、devbox/agent 过滤、
command 生成/筛选、runtime label、initials、状态映射、`escapeHtml`。`app.js` 用与
`replay.js`/`collaboration.js` 相同的缓存 Promise 动态加载它。`web/ui.test.js`
（node:test）覆盖上述关键逻辑。

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
| `provision_demo.py` | **仅开发**：调用公开注册 API 建 demo/demo 账号 + Devbox + Claude agent,打印 token。非自动 seed；`DEEPBOX_ENV=production` 时拒绝运行 |
| `tests/test_persistence.py` | connector FIFO、scrollback restore、DVR 回归测试 |
| `tests/test_collaboration.py` | Cut 8 角色排序、workspace 隔离、lease 竞争/续租/释放/超时/CAS 移交 |
| `tests/test_collaboration_routes.py` | Cut 8 workspace REST 共享与 owner 不变量、Viewer opaque REST 拒绝、WS 只读拒绝 |
| `tests/test_models_migration.py` | 旧 SQLite schema 的 Cut 8 加列、建表与 personal workspace 回填 |
| `tests/test_connector_ipc.py` | **P2 Cut 4** IPC 帧编解码（仅 JSON object）+ `MAX_FRAME` 边界 + `LoopbackChannel` 有序/EOF/背压；真实本地 IPC（本机 Windows 命名管道）鉴权握手 + reconnect + 错误密钥拒绝 + POSIX 0600；自定义陈旧 Unix endpoint 清理 |
| `tests/test_connector_supervisor.py` | **P2 Cut 4** supervisor/transport 拆分：transport 重启不 kill PTY、detach 期间 output 缓冲、按序补发、close 只 kill 目标 PTY；另含真实双进程 IPC 仿真：detach/reconnect 下 FakePty 存活、第二个 transport 被 `ipc_busy` 拒绝（用 FakePty，不起真实 agent/ConPTY）|
| `tests/test_config.py` | production 配置和 Origin allowlist 测试 |
| `tests/test_connector_diagnostics.py` | connector URL/TLS/DNS 诊断消息测试 |

---

## 7. 远程部署配置

`server/app/config.py` 从环境变量/`.env` 加载 Server 配置。`python -m server` 根据配置启动
Uvicorn。production 模式会拒绝开发默认 secret、空 Origin allowlist 和非 Secure cookie。

推荐三机部署使用 Tailscale Serve：Uvicorn 只监听 `127.0.0.1:8077`，Tailscale 负责 Tailnet
内的 HTTPS/WSS。浏览器 `/ws/term` 校验 Origin；connector `/ws/devbox` 只接受 Authorization
header token，不接受 query-string token。详见 `remote-deployment.md`。

健康检查：

- `GET /api/health`：进程存活和协议版本。
- `GET /api/ready`：额外检查 DB 和 recording 数据目录。

---

## 7a. 最小生产运维（P1 Cut 3）

详见 [`docs/operations.md`](operations.md)。实现分布：

- `server/app/logging.py` — 结构化 JSON 日志（每行一个对象，`ts/level/logger/message` +
  `event` 字段）。`configure_logging()` 幂等安装 handler，`log_event()` 丢弃 `None`
  字段以避免泄露未设置的密钥。`main.py` 在导入时调用一次，`DEEPBOX_LOG_LEVEL` 控制级别。
- connector 心跳：`connector/client.py` 每 20s 发送 `{"type":"heartbeat"}`，服务端刷新
  `last_seen_at` 并回 `heartbeat_ack`；`connect_count` 让重连可见。服务端把
  online/offline 记为结构化事件。
- `server/app/version.py` — 版本与 Git 构建来源。`/api/version` 仅公开
  `{version, commit}`（短哈希，公开安全）；`/api/admin/version`（owner）附完整
  commit 与 `dirty`。部署产物用 `DEEPBOX_GIT_COMMIT` 注入。
- `server/app/capacity.py` — 纯函数阈值判定（数据库越大越差、磁盘剩余越小越差），
  `/api/admin/capacity`（owner）返回 ok/warn/alert。阈值经 `config.py` 校验。
- `server/ops/backup.py` — SQLite 在线备份（`integrity_check` 校验）与恢复（校验 +
  live-server 守卫，`--force` 才能覆盖运行中的库；`.pre-restore` 侧车 + `os.replace`
  原子替换）。
- `server/ops/smoke.py` — 重启后冒烟：命中 `/api/health`、`/api/ready`、`/api/version`，
  失败非零退出，可作为部署门禁。

---

## 7.1 P2 Cut 6：Replay、Checkpoint 与 Retention

- `server/app/recording.py` 以 `RecordingFrame.id` 作为跨 PTY stream 的 durable cursor；
  `durable_events()` 生成按 cursor 排序的 replay event，`maybe_checkpoint()` 周期性保存完整终端屏幕，
  `metadata()` 只返回回放所需统计，不解析 runtime/model。
- `GET /api/sessions/{id}/recording` 输出兼容 asciicast v2；`GET /api/sessions/{id}/replay`
  返回 header、events、checkpoints、duration 与 metadata，且两者都执行 session owner 隔离。
- `PATCH /api/sessions/{id}/retention` 只接受 `none/7d/30d/permanent`，策略与执行在
  `RecordingStore.set_retention()` 同一操作完成。清理时 payload 清空、`redacted_at` 置位并删除可能含
  已过期内容的 checkpoint；seq/hash ledger 不删，因此 ACK 丢失后的 identical duplicate 仍可安全 re-ACK。
  `none` 对后续新 output 也在 `persist_output()` 返回（即 server ACK）前清空持久 payload。
- `web/replay.js` 是可独立测试的纯 replay helper；`web/app.js` 动态加载它，并提供 Session 历史列表、
  播放/暂停、0.5x/1x/2x/8x、timeline seek、首尾跳转、最终屏幕、asciicast 下载与 retention selector。
  seek 恢复 `time <= target` 的最近 checkpoint，再严格应用 `cursor > checkpoint.cursor` 的 output event；
  replay mode 禁用 xterm stdin，返回 live 时重建 terminal/WS 状态。
- 回归覆盖 retention 即时执行、未来 `none` 帧、redaction 后 duplicate ACK、checkpoint 清理、
  server 字段到 browser replay 字段的归一化，以及相同时间戳下的 cursor seek。

## 7.2 Cut 8：Workspace、角色与协作

- `models.py` 用 `Organization → Workspace → Membership` 表达共享边界；Membership 角色按
  `viewer < operator < admin < owner` 排序。`Devbox.workspace_id` 和 `Session.workspace_id` 把资源固定到
  workspace；新用户首次读取 workspace 时自动得到 personal organization/workspace，旧库中的 nullable
  Devbox 会事务性回填到 owner 的 personal workspace，Session 继承其 Devbox workspace。
- `/api/workspaces` 提供 workspace 创建、成员列表/新增/改角/移除。Admin 可管理低于 owner 的角色，
  只有 owner 可授予/撤销 owner；最后一个 owner 永远不能被移除或降级。成员被改角或移除时，Server 会关闭
  该用户在该 workspace Session 上的现有 socket，使权限撤销立即生效且不影响其他 workspace。Devbox、Token、Agent、Session、
  Recording/Replay/Retention 的 REST 授权统一经 `_devbox_role()` / `_session_role()`，越权目标继续返回 opaque 404。
- `Hub.session_watchers` 保留同一 Session 的多个 browser 连接。`SessionParticipant` 保存已加入的用户和
  last-seen；server 广播 `collaboration` frame（participants + keyboard state），浏览器显示只读/可请求/持有状态。
- `KeyboardLease` 以 `session_id` 为主键，60 秒 TTL，handoff 通过 `version` 做 compare-and-swap。Operator/Admin/Owner 可
  `keyboard_acquire/renew/release`；忙时请求广播给 holder，holder 以 `keyboard_handoff {target_user_id}` 原子移交。
  lease 到期比较先把 SQLite round-trip 后的 naive UTC 与 timezone-aware 时间统一归一化，避免 terminal WebSocket
  因 `can't compare offset-naive and offset-aware datetimes` 异常断开并持续 reconnect。
  只有有效 holder 可发送 `input`/`resize`/`terminate`；每次输入自动续租，浏览器持有期间每 20 秒续租，
  holder 断开最后一个该用户连接时释放。Viewer 的控制 frame 返回 `read_only`。
- `web/collaboration.js` 是可独立测试的权限/租约 UI 状态 helper；`styles.css` 由 `app.js` 动态加载，
  无需更改 `index.html`。权限边界、共享资源、WebSocket 只读拒绝、租约竞争/移交和迁移均有回归测试。

---

## 8. 现在的边界

- output 由 server Protocol v3 `RecordingFrame` durable store 记录，并可导出 asciicast v2 / JSON replay；
  connector 断线时使用**内存** FIFO 作前置去抖，持久性由 **P2 Cut 5 磁盘 spool**（seq/ACK/fsync/resume）保证：
  每帧落盘后才可发送、server durable commit 后才 ACK、connector 精确 seq ACK 后才移除。
- **P2 Cut 4 已拆分** supervisor（会话所有权）/ transport（WS）：transport 重启/断开不再 kill PTY。
  拆分既可跑在**进程内** `LoopbackChannel`（`python -m connector` 默认 all-in-one），
  也可跑真实**双进程**：`--mode supervisor` 长驻拥有 PTY 并经命名管道 / Unix socket（`0600`）
  serve IPC，`--mode transport` 拥有 WS 并可独立重连（本机 proactor 命名管道 reconnect 已单测实测通过）。
  依然：默认 all-in-one 进程整体退出仍 `shutdown()` kill 其托管 PTY；真实 Windows 服务形态
  下的 sessiond 长稳 + 真实 ConPTY 长稳验证尚未执行（见下方真机验收门）。
- **P2 Cut 5 已落地** `SessionSupervisor.pending` 由持久磁盘 spool 支撑（`connector/spool.py`，见 §3.0a）：
  emit 落盘 fsync、单调 seq、精确 seq ACK 持久化后移除、重启按序重放未 ACK 帧、尾部半条截断 / 内部损坏 fail closed。

### 8a. P2 Cut 4 真机验收门（尚未执行）

以下必须在真实机器上人工验证并记录后，才能声明 Cut 4“生产就绪”：
1. Windows：真实 ConPTY（pywinpty）起真实 CLI，命名管道 `\\.\pipe\deepbox-sessiond-<user>`
   实现为独立 sessiond 进程；杀掉 transport 进程后 PTY 存活、重连补发无丢字。
2. POSIX：Unix socket（`0600`）双进程；同样验证 transport 重启 PTY 存活。
3. Windows 服务形态下 sessiond 长稳（≥数小时、多次 transport 重连）。
本轮已落地：长度受限 JSON 帧与鉴权握手、真实本地 IPC（本机 Windows 命名管道 reconnect 单测）、
双进程拆分的 CLI 模式，以及“分离 transport detach/reconnect 下 FakePty 存活”仿真测试。
但上述 1、2、3 需要真实 ConPTY / agent / Windows 服务，仍需用户在真机人工验证；
在此之前，代码中的真实 ConPTY / Windows 服务路径不得当作已验证。

- 密码 hash 仍是 salted SHA-256，应在公开部署前升级 Argon2id。
- 单进程内存 Hub/LiveRegistry 不支持多实例横向扩展。
- Cut 8 已支持 Workspace、四级角色、多 Viewer 与单 holder keyboard lease；当前 Hub/lease 仍是单 Server
  实例语义，跨实例路由与共享 lease backend 属于 Cut 9。
- 应用本身不终止 TLS；Private Alpha 由 Tailscale Serve 提供 Tailnet 内 HTTPS/WSS，不能使用
  Funnel 或直接暴露 Uvicorn 到公网。

后续顺序见 `planning.md`。
