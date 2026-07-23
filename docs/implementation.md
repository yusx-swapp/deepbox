# deepbox 实现说明（How it works）

> 本文解释 P0 骨架 + 真实 Claude CLI 接入是**如何实现**的，配合 `design.md`（设计）阅读。
> 目标读者：想读代码、想扩展或部署这套系统的人。

---

## 1. 全景：一条消息的完整旅程

Structured runtime 的主路径：

```text
Browser native chat
  -> input {data, generic options}
  -> FastAPI /ws/term（RBAC + keyboard lease + opaque relay）
  -> Connector StructuredAgentSession
  -> 本地 Claude Code/Copilot CLI
  -> canonical event
  -> connector spool + Server durable recording/ACK
  -> Browser reducer + semantic render
```

PTY runtime 的兼容路径：

```text
Browser xterm -> input bytes -> Server relay -> Connector PtySession -> 本地 CLI
Browser xterm <- output bytes <- Server relay/recording <- Connector PtySession
```

**一句话**：Server 是 runtime-agnostic durable switchboard。它不运行模型、不持有模型密钥，
也不解释 runtime/model/reasoning；它只执行身份与协作规则，并可靠转发/记录 terminal bytes 或
canonical event。智能（Claude/Copilot/Codex）100% 跑在用户机器上。

---

## 2. 服务端（`server/app/`）

### 2.1 `models.py`：持久化数据模型

SQLAlchemy Core 模型覆盖用户、内部 organization、workspace/membership、workspace invitation、Devbox、Agent、Session、参与者、PTY 输出、DVR recording、结构化消息、任务与 LocalProject。

- `User` 保留本地 `password_hash`，并增加 `email / auth_provider / external_tenant_id / external_subject / disabled_at`。部分唯一索引 `uq_user_external_identity` 只约束非空 Microsoft 外部身份三元组。
- `Workspace` 是用户可见的协作边界；`Membership` 对 `(workspace_id, user_id)` 唯一并保存 `viewer / operator / admin / owner`。`Devbox.workspace_id` 与 `Session.workspace_id` 把所有资源归到 workspace。
- `WorkspaceInvitation` 保存标准化邮箱、目标角色、SHA-256 token hash、短 preview、过期/接受/撤销时间与接受者；服务端不持久化明文邀请 token。
- `Invitation` 仍用于 deployment owner 创建本地密码账号；它与 workspace access 分离。
- `runtime_capabilities_json` 对 server 是 opaque JSON blob；LocalProject 保存 `path / label / is_default / capability_flags`，其中 `capability_flags` 也保持 opaque。
- `_migrate()` 以 append-only `ALTER TABLE` 补列，并单独创建 SQLite 无法通过 ALTER 添加的唯一索引；`_backfill_workspaces()` 为旧用户建个人 workspace，回填 Devbox/Session，并保证现有 owner membership。
- DVR 使用独立 `recording_chunk(session_id, seq, direction, data, created_at)` 保存双向原始字节；旧 `output_chunk` 继续兼容历史 PTY 文本。

### 2.1a Microsoft identity 与 workspace onboarding

`identity.py` 是纯解析模块：它只解析 Easy Auth 已验证后注入的 `X-MS-CLIENT-PRINCIPAL*`，规范化 email/tenant，并生成可读 username seed；不验证或保存 OAuth token。`main.py` 用 `(auth_provider, tenant, subject)` upsert 用户。allowlist 中的 Microsoft email 可一次性 claim 旧的 sole local owner；普通外部身份始终创建独立 member，再通过邀请加入共享 workspace。

Workspace invitation 的明文 token 只在创建响应的 `join_url` 返回一次；数据库仅存 hash/preview。preview 使用 `POST` JSON body，返回扁平的 `workspace_name / role / email_hint / expires_at`。accept 要求当前用户的规范化 email 精确匹配，并在唯一约束竞争后重查 membership，从而让双击/并发领取幂等。

### 2.2 `util.py` — 凭证与 id
- `new_id()`：`uuid4().hex`。
- `new_token()`：`hpc_box_` + 32 随机字节的 hex。返回 `(完整token, sha256, preview)`。
  **数据库只存 sha256**，完整 token 只在创建时返回一次。
- `hash_password/verify_password`：`salt$sha256(salt+pw)`（P0 够用；生产应换 bcrypt/argon2）。

### 2.3 `hub.py` — 实时路由核心
一个进程内单例 `Hub`，维护两类连接和几张路由表：
```python
DevboxConn { ws, devbox_id, agent_ids, outbound, sender_task, retired }
                                                  # 一个 connector 的 WS
HumanConn  { ws, user_id, sessions }              # 一个浏览器的 WS

devboxes:         devbox_id  -> DevboxConn
agent_to_devbox:  agent_id   -> devbox_id     # 找 agent 属于哪台 devbox
session_watchers: session_id -> {HumanConn}   # 谁在看这个会话
```
关键方法：
- `to_devbox(agent_id, frame)`：把帧非阻塞排入 host 该 agent 的 connector 的有界队列。
- `sync_agents(devbox_id, agent_ids, directory)`：在 Hub 锁内原子替换在线路由并排入权威 `agents` 目录。
- `to_session_humans(session_id, frame)`：广播给所有正在看该会话的浏览器。

每条 DevboxConn 只有一个 sender task 调 `ws.send_json()`，保持帧顺序并隔离慢连接；队列上限 256，单帧发送超时 5 秒，溢出/失败会 retire 并以 `1011` 关闭。重复 devbox 连接会先 retire 旧连接并以 `4002` 关闭；`remove_devbox(expected=...)` 防止旧 receive loop 的迟到 `finally` 误删新连接。
> dataclass 加了 `eq=False`，让连接对象按**身份**可哈希（否则含可变字段的 dataclass 不能进 set）。

### 2.4 `main.py`：FastAPI 路由

**(a) 身份、账号与安全**

- `GET /api/auth/config` 公开返回 `local / hybrid / microsoft` 模式及可用登录方式。
- 本地 register/login 只在 `password_auth_enabled` 时开放；cookie 用 `URLSafeTimedSerializer` 验签并受 `DEEPBOX_SESSION_TTL_SECONDS` 限时。
- Microsoft start/callback/logout 经 `/.auth/login/aad` 与 `/.auth/logout`；callback 只消费 Easy Auth principal headers，映射外部身份后签发 Deepbox cookie，不保存 Microsoft token。
- deployment owner 继续管理本地账号 invitation、禁用/重新启用、bootstrap 与管理端点；禁用会主动断开该用户现有 WebSocket。
- `_security_baseline` 提供 production Origin allowlist、分层 rate limit、安全 header 与脱敏 audit。

**(b) Workspace / Devbox / Agent**

- `GET/POST /api/workspaces` 按 membership 列出/创建 workspace；创建者为 owner。
- members 端点支持列出、添加已有用户、改角色、删除；admin/owner 权限与“至少保留一个 owner”均在服务端校验。
- workspace invitation 端点支持创建、列表、撤销、POST preview 与原子 accept；邀请按 email 绑定、单次、过期，重新签发撤销旧链接，accept 对并发双击幂等。
- `GET /api/devboxes` 聚合当前用户所有 workspace；创建 Devbox 必须指定可管理的 workspace。Agent、Session、recording 与 project 路由都从 workspace membership 推导权限。
- connector bearer 仅作用于自己的 Devbox；动态 agent 热注册与 `runtime_capabilities_json` 上报不依赖 UI/服务端 runtime 分支。

**(c) Session 创建与编排**

- `_create_session` 持久化 session 与 creator participant，在线 agent 先走 structured `session_open`，仅 terminal-only capability 回退 PTY。
- HTTP message/task routes 与 WS `message` / `task_create` 共享校验与持久化路径；workspace viewer 只读，operator 及以上可写。
- reconnect 使用 cursor replay；DVR/legacy output、structured events、supervisor 路由与 retention/secure erase 保持兼容。

## 3. connector（`connector/`）—— 本设计的灵魂

用户在自己机器上自启的进程。**智能和 API key 都在这里，server 永远看不到。**

### 3.0 本地命令与安装边界

`scripts/install.ps1` / `install.sh` 只负责首次安装和显式升级：刷新
`~/.deepbox/app`、维护独立 venv，并在 `~/.deepbox/bin` 安装一个稳定的 `deepbox`
shim。installer 用 venv 内的 `deepbox-app.pth` 指向同级 `app`；shim 默认从自身 `bin`
位置解析安装根目录（因此自定义 `DEEPBOX_HOME` 不必在后续 shell 重复设置），再从调用者当前目录用
venv Python 的 isolated mode（`-I -m connector.cli`）启动，因此不会从 caller cwd 或
`PYTHONPATH` 注入同名 package。只有首个参数严格为 `upgrade` 时才重新下载 installer，
普通 `deepbox connect` 绝不触碰 app 目录。

`connector/cli.py` 是用户命令 dispatcher：`connect` 去掉命令名后转给
`client.main(argv)`；`doctor` / `status` 转成原有 flag；`project` 保留子命令 argv。
installer 仍写出旧 `deepbox-connect.cmd` / `.sh`，但它只委托 `deepbox connect`，从而兼容
旧快捷方式而不再把安装与连接耦合。Windows installer 仅在安装/升级刷新前停止本 venv 的
`-m connector` / `-m connector.cli` 进程树；连接路径不会调用该逻辑。

### 3.0a P2 Cut 4：Supervisor / Transport 拆分
connector 拆成两半，二者只经 IPC 抽象（`ipc.py`）通信：
- **`supervisor.py`（sessiond）——会话所有权**：拥有全部 `PtySession` / `StructuredAgentSession` 生命周期；`detach()` 只断开 transport，绝不 kill PTY；显式 `terminate`、权威 `agents` 目录删除对应 agent 或 `shutdown()` 才结束 PTY。`agents` 帧必须是元素完整合法的列表才会原子替换目录（空列表是合法的 clear-all）；任一畸形元素使整帧保持 no-op。每次新 PTY 启动生成一个 UUID `pty_instance_id`，同一且仍存活的 local session 的幂等 `open` 复用该值；如果本地子进程已被外部终止但 reader 尚未完成清理，`open` 会剔除 stale handle 并启动新实例。旧 reader 的迟到 `exit` 以对象身份校验隔离，不能误删或关闭替代它的新 PTY。
- **`transport.py`——WebSocket 传输**：不拥有 PTY。`TransportSession` 在 IPC 与 `/ws/devbox` 之间转发帧，心跳和网络重连都不能改变 PTY 生命周期。`run()` 同时监督四个子任务，任一完成即在 `finally` 取消并 `gather(..., return_exceptions=True)` 回收全部任务，再重抛首个非取消异常，避免 `_channel_to_ws()` 等后台异常变成 “Task exception was never retrieved”。
- **IPC**：split 模式使用 Windows named pipe / POSIX `0600` Unix socket；消息为限制 1 MiB 的 newline-JSON，并有 HMAC 握手。all-in-one 模式使用相同 `Channel` 接口的 `LoopbackChannel`。

### 3.0b P2 Cut 5：Protocol v3 durable spool、server ACK 与精确 resume

Protocol v3 的输出身份是 `(session_id, pty_instance_id, seq)`：

- **先落盘再发送**：`SessionSupervisor.emit()` 对 `output` 调用 `enqueue_output()`。`connector/spool.py` 的真实运行时 `DiskSpool` 使用 stdlib `sqlite3`，启用 WAL 和 `synchronous=FULL`；`outbox` 以三元组唯一约束保存 payload，`ack_state` 保存各 PTY 实例最后连续 ACK，`input_receipts` 保存输入去重 ID。单测可注入 `InMemorySpool`。
- **序号域**：`seq` 对 `output`/`event` frame 在 `BEGIN IMMEDIATE` 事务中按 PTY 实例分配，取 `max(last_acked_seq, outbox max seq)+1`，从 1 开始且不复用。ready/presence/exit/input_ack 等控制帧只进进程内队列，既不占 durable output 序号，也不会在 sessiond 重启后陈旧重放。
- **本地有界流水线**：`drain_to()` 不再单-inflight 停等，而是维护一个按 `ord` 有序、受 `MAX_INFLIGHT_FRAMES` 帧数与 `MAX_INFLIGHT_BYTES` 字节数双重约束的 in-flight 窗口（`_inflight_ids: {delivery_id -> bytes}`）。它优先发送进程内控制帧，再按顺序发送尚未 in-flight 的 durable `ord`，在窗口未满且未到 backpressure 上限前**连续发送多帧**，无需等待前面帧的 ACK——这消除了「一帧一 Azure RTT」的吞吐上限。窗口满时停止扫描，任一 ACK/fence/新 output 都会 `set` `pending_event` 让发送环从同一队尾续发。`ord` 已在 `_inflight_ids` 中的帧永不重发；spool 仍是 durability 真源，attach 时清空 `_inflight_ids`/`_inflight_bytes`，新 transport 按序重放全部未 ACK 行。
- **精确、乱序安全的 ACK 推进**：`_apply_ack(delivery_id)` 只处理确实在 `_inflight_ids` 中的 id。控制帧按序从 `_controls` 队首 pop；durable output 交由 spool 强制 per-stream 连续性——`spool.ack(ord)` 仅当该 seq 同时是本流最小且等于 `last_acked_seq+1` 时才删行并返回 True，因此**陈旧或乱序的 ACK 绝不会删错行**（先到的中间 ACK 只留在窗口里，等队首 ACK 到达才真正推进）。释放后从窗口移除该 id 并回补字节额度、`set` `pending_event`。fence 走 `_reconcile_inflight_after_fence()`：按 fence 后的 `pending_records()` 与当前 `_controls` 重算，丢掉已被清走的 durable 或 control in-flight id，仍在队列中的控制 id 保留。
- **ACK 不是 `send()` 成功**：transport 把「发送」与「durable ACK 处理」解耦为两条独立任务。`_channel_to_ws` 对 output 帧**发送即返回**，只把身份 `(session_id, pty_instance_id, seq)→(delivery_id, frame)` 按序记入 `_outstanding`（`OrderedDict`），不阻塞、可连续发多帧。独立的 `_process_server_events` 消费 server 事件：`ack` 精确弹出对应 `_outstanding` 行并回本地 `ipc_delivery_ack`（陈旧/未知 ACK 释放不了任何行，忽略）；`resend(expected_seq)` 重发该 seq 及其后本流所有 outstanding 行（保序恢复连续尾），若请求 seq 不在 outstanding 则 fail closed；server `error` 抛 `ProtocolError`；**fence 恢复**：若 server 判定某 `(session_id, pty_instance_id)` durable 流已分叉（connector 重启 PTY 却仍在重发旧 spool 尾），它回 `{type:"fence", ...}` 而非 terminal error，`_handle_fence` 清掉该流全部 outstanding 行并把 `{type:"fence"}` 转给 supervisor（不 raise、不重连），后续更新的 local session 实例得以继续 drain。非 output 控制帧仍以 `ws.send()` 完成为本地发送边界（发送后立即回 `ipc_delivery_ack`）。
- **server 两阶段：先 fan-out 再持久化再 ACK（回显不等 fsync）**：`server/app/recording.py` 的 `RecordingStore` 把 `persist_output()` 拆成纯内存的 `classify_output()`（读 ledger 判定 NEW/DUPLICATE/GAP/CONFLICT/INVALID，NEW 时构造**未 commit** 的 `RecordingFrame`）和 durable 的 `commit_new()`（`db.add`+`db.commit`，即 ACK 边界）。`/ws/devbox` 热路径对 NEW 帧**先** `feed_live_output()` + `Hub.to_session_humans()` 把 frame 广播给浏览器，**再**用 `await asyncio.to_thread(recording_store.commit_new, ...)` 落盘，commit 成功后才回 ACK 并同样在线程池里 `maybe_checkpoint()`。这样 browser 端的按键回显只经过内存分类 + 非阻塞入队，**绝不等待 network-disk 的 fsync**；而同步 commit 移出 asyncio 事件循环后，落盘期间事件循环继续把已入队的广播真正发出、也不阻塞后续输入路由。durable ACK 仍只表示 server 已持久化（连接对同一 session 串行处理，`s` 不跨线程并发）。相同三元组+相同 payload 是幂等 duplicate（重新 ACK、不重写）；gap 返回 `resend(expected_seq)`，不推进 ACK。**分叉流走 fence 而非 error**：相同三元组但 payload 冲突（CONFLICT），或 seq 低于持久化前沿且查无此行的陈旧尾（INVALID「below persisted frontier」），都由纯函数 `recording.output_ack_response()` 映射成可恢复的 `fence`（帧已展示，`commit_new` 若丢 unique-key race 也会重分类为 DUPLICATE/CONFLICT/GAP 供 connector 恢复）；只有真正 malformed / 非本 devbox 拥有的 INVALID 才仍是 terminal `error`。server 按 devbox/agent ownership 校验，不能跨机器写历史。`Hub.to_session_humans()` 只把 frame 非阻塞地 `put_nowait` 进每个 watcher 独立的 128-frame 有界队列；per-watcher sender 保序发送，每次发送默认限时 1 秒。队列满、发送失败或超时的 stale watcher 会从全部索引移除并以 1011 关闭，因此 refresh/resume 遗留连接或慢浏览器不能阻塞其他 viewer，更不能卡住 connector 的严格 FIFO spool 或延迟 durable ACK。
- **server 侧 SQLite 调优（去掉每帧 fsync 的网络往返）**：production DB 在 Azure App Service 的 `/home`（网络盘）上，SQLite 默认 `journal_mode=DELETE`+`synchronous=FULL` 会让每次 commit 付多次 fsync＝多次网络往返。`server/app/models.py::init_db()` 注册 SQLite 连接 PRAGMA 监听器（仅 SQLite URL），设 `journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`wal_autocheckpoint=1000`：WAL 把每帧 commit 收敛成一次顺序追加、把 sync 推迟到 checkpoint，仍然崩溃安全（WAL 下 NORMAL 只在掉电时可能丢最后几条已提交事务，而这些帧 connector 的 durable spool 会在重连时重发，故仍可恢复）。`tests/test_db_pragmas.py` 断言实际生效的 PRAGMA。

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

## 4. web（`web/`）—— Native chat + Terminal fallback SPA

### 4.1 `index.html`
挂载 xterm.js（CDN）、xterm-addon-fit，含最小内联 reset。**此文件保持不变**；
`app.js` 在运行时注入 `<link rel="stylesheet" href="/static/styles.css">`（在内联
reset 之后加载，故外部主题为唯一事实来源）。server 显式注册 `.js` 的 `application/javascript` MIME，避免 Windows MIME 注册表与 `nosniff` 组合阻断 SPA 启动。

### 4.2 `web/app.js`：UI controller

- 启动时先取 `GET /api/auth/config`，只显示当前模式允许的本地密码或 Microsoft 登录。workspace invite fragment 会暂存到 `sessionStorage`，因此 Easy Auth redirect 后仍可 preview/accept。
- `renderFleet()` 按 **Workspace → Devbox → Agent** 渲染左栏；workspace 切换由 `web/ui.js` 的纯函数保持有效 selection，并只展示该空间的 Devbox/Agent。
- workspace manager 支持创建空间、添加已有用户、改/删成员、创建/撤销邮箱邀请；UI 仅做 affordance，最终角色检查都在 server。
- token modal 仍一次显示 plaintext Devbox token/command，关闭后不保留；Microsoft logout 同时清 Deepbox cookie 并走 Easy Auth logout。
- Session 内容区保留 structured Chat/Task 与 terminal fallback；浏览器不持有 connector token 或任何模型 key。

### 4.3 `ui.js` + `ui.test.js`
DOM-free 的纯逻辑抽到 UMD 模块 `web/ui.js`：fleet 汇总、devbox/agent 过滤、command 生成/筛选、
runtime label/options、`supportsStructuredChat()`、opaque agent API path、initials、状态映射、
`escapeHtml`。`web/chat.js` 另外承载 canonical reducer、JSONL parser、generic controls/options 与
semantic render。`app.js` 用缓存 Promise 动态加载这些模块；`web/ui.test.js` 和 `web/chat.test.js`
（node:test）覆盖关键纯逻辑。

---

## 5. 真实 Claude PTY fallback 是怎么跑通的

这条历史 fallback 仍无需 Server 特殊代码。步骤是：
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
| `tests/test_connector_supervisor.py` | **P2 Cut 4** supervisor/transport 拆分：transport 重启不 kill PTY、detach 期间 output 缓冲、按序补发、close 只 kill 目标 PTY；覆盖外部终止后的 stale PTY 替换与迟到 exit 隔离；权威 agents 热添加、畸形整帧拒绝，以及删除 PTY/pty_instance/durable spool/待发控制帧清理；另含真实双进程 IPC 仿真：detach/reconnect 下 FakePty 存活、第二个 transport 被 `ipc_busy` 拒绝（用 FakePty，不起真实 agent/ConPTY）|
| `tests/test_connector_transport.py` | 四子任务 FIRST_COMPLETED 后全部 cancel/gather，后台异常被取回并重抛，不留下悬空 task |
| `tests/test_hub.py` | devbox 有界发送队列、hello 首帧顺序、重复连接 retire、权威目录路由增删、agent 删除时 watcher exit/presence 清理与离线 no-op |
| `tests/test_agent_lifecycle.py` | 同一在线 devbox 并发添加 claude-code + codex 后串行 fresh-snapshot 校准，并验证删除 agent 的 session 级联与实时 reconcile |
| `tests/test_db_pragmas.py` | SQLite 每连接 `PRAGMA foreign_keys=ON` |
| `web/ui.test.js` | runtime options 清洗/去重、opaque agent ID URL 编码及其他 DOM-free UI helpers |
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

## 7.2 Cut 8 + identity/workspace layer：Workspace collaboration

- 每个现有/新建用户都有 personal workspace；用户也可创建多个 workspace，并通过 membership 加入其他 workspace。Devbox/Session 均有 `workspace_id`，旧数据在 migration/backfill 后归属明确。
- 左栏是 `Workspace → Devbox → Agent`；任一成员可发现空间内全部 agent，`viewer < operator < admin < owner` 决定读写和管理能力，最后一个 owner 受保护。
- workspace manager 可添加已有账号，也可签发 email-bound、single-use、expiring invitation。token 只在 hash fragment 与 sessionStorage 短暂存在，preview 走 POST body，accept email 精确匹配且并发幂等。
- Microsoft identity 由 App Service Easy Auth 验证；Deepbox 只保存 provider/tenant/subject/email 映射并签自己的限时 cookie。`local / hybrid / microsoft` 三种模式让本地开发与 Azure 迁移共存。
- `GET /api/devboxes`、Agent/Session/recording/project 路由按所有 membership 聚合并授权，不再把 deployment owner 当作 workspace owner。

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
- 浏览器把 xterm 输入按 24 ms 空闲窗口、80 ms 最大等待合并后再发往 WSS；Server 每帧仍会读取并校验
  60 秒 lease，但不再在输入热路径续租和提交 SQLite。持有者由独立的 20 秒 heartbeat 续租，失效或他人
  持有的 lease 仍会在任何输入转发前 fail closed。
- 应用本身不终止 TLS；Private Alpha 由 Tailscale Serve 提供 Tailnet 内 HTTPS/WSS，不能使用
  Funnel 或直接暴露 Uvicorn 到公网。

后续顺序见 `planning.md`。


## 9. 结构化 Agent 与原生聊天界面（Cut 10-11，2026-07-22）

### 9.1 方向与边界

对支持 headless/JSON 输出的 runtime，主路径不再是浏览器复刻全屏 TUI，而是 connector 在用户机器上
运行 CLI、翻译为统一事件，浏览器渲染自己的聊天界面。PTY/xterm 继续作为不支持 structured runtime
的兼容回退。Server 只转发、持久化 opaque frame；不运行模型、不读取模型凭证，也不解释 runtime、
model 或 reasoning 值。

### 9.2 Connector 事件协议与进程模型

- `connector/agent_session.py` 定义 display-safe canonical events：`status`、`session.config`、
  `user.echo`、`message.delta`、`message`、`tool.call`、`tool.result`、`permission.ask`、
  `turn.end`、`error`。
- Claude Code adapter 把 stream-json 翻译为上述事件，并在一个 live session 中保留长生命周期进程；
  Copilot CLI adapter 读取 JSONL，每个 turn 启动一个进程。
- `connector/supervisor.py` 对 structured session 调用 `write_turn(text, options)`，对 PTY session
  继续调用 `write(data)`；两条路径共用 session/open/terminate 与可靠 output transport。
- Structured output 使用 `kind: "event"`，仍带 `(session_id, pty_instance_id, seq)`，先进入 connector
  本地 spool，再由 Server durable commit/ACK。这里的 `pty_instance_id` 是协议兼容字段，不表示一定有 PTY。

### 9.3 Runtime family、surface negotiation 与 composer controls

- `connector/runtime_probe.py` 输出 capability schema v2。每个 family item 包含 `runtime`、`label`、
  `installation`、`compatibility`、`authentication`、`models`、`surfaces` 与排除 `probed_at`
  时间戳的稳定内容哈希 `revision`；未安装的 runtime 也会上报，以便 UI 给出 adapter 声明的安装命令/文档。
  Server 不读取 runtime/model 字符串，
  只把整个数组作为 opaque JSON 保存；executable path、probe 原始输出和 CLI 凭据都不会上传。
- 一个 family 可以注册多个 `RuntimeAdapter` surface。`surfaces[]` descriptor 携带内部 adapter ID、
  `terminal` / `structured` 名称、default 位与 generic features/controls。新 runtime family 仍只需 registry
  entry/adapter，不要求 Server 或 browser 添加 runtime-specific 分支。
- Browser 打开 agent 时按 family capability 选择 default surface（Claude/Copilot 当前为 `structured`），
  attach frame 显式发送 `surface`。Connector 用 `session.ready.surface` 确认；找不到或无法启动时返回
  `runtime.unavailable`（含 installation/compatibility/authentication 与 available surfaces），绝不静默
  回退到 terminal。
- Probe 可运行 adapter 声明的安全模型枚举 argv/parser；发现结果同时更新 family catalogue 与各 surface 的
  model choices。若 CLI 没有稳定枚举接口，`models.source` 为 `catalogue` 且 `complete:false`。浏览器把发现/目录
  模型渲染为建议，同时允许自定义 model；Connector 最终拒绝控制字符、shell metacharacter 和不允许 custom
  model 的 adapter 值。只有 adapter 提供可靠的非交互 status argv 时，authentication probe 才会参与 spawn gate；
  Copilot 仅提供交互式 `/login`，因此上报 `unknown` 而不是制造阻断启动的 false negative。
- Claude structured 暴露 model、effort 和 file controls；Copilot structured 暴露 model、reasoning
  effort（`low|medium|high|xhigh|max`）和 attachment controls。当前 generic control kind 只有 `select` 与 `file`；
  descriptor 携带 key、
  label、scope、choices/default 或文件数量/总字节上限，浏览器不按 runtime ID 分支。
- Connector 按 adapter allowlist 验证每个 option；session-scope select 在首个 turn 后锁定，per-turn
  select 每轮应用。agent `runtime_config.permission_mode` 同样先按 adapter allowlist 清洗，再进入每轮实际 argv。
  `session.config` 只回显 connector 已确认的 scalar 值，浏览器据此校正控件状态。

### 9.4 文件输入

- 浏览器依据 file descriptor 生成隐藏 file input、附件 chips 和错误提示，并在发送前检查数量与总大小。
- 文件通过 `FileReader` 转为 base64；Claude adapter 上限为最多 4 个、合计 1 MiB，Copilot adapter 为最多 4 个、合计 4 MiB。
- Connector 再次验证 count、声明/解码大小、base64、文件名与 adapter mode。Claude 只接受可解码的
  UTF-8 文本并嵌入 prompt；Copilot 写入 connector-owned 临时目录，以 `--attachment` 传给本地 CLI。per-turn
  child 完全 reap 且 stderr reader 退出后才删除目录；Windows 短暂文件锁会有界重试，不会让 turn task 异常退出。
- `user.echo` 和 durable history 只保留文件名/type/size，不保留 base64 正文或临时路径。

### 9.5 Browser chat、tab re-attach 与 restore

- `web/ui.js` 先用 family ID（并兼容旧 adapter ID）定位 capability，再由 default surface 决定 chat 或
  terminal；`web/chat.js` 独立负责事件解析、reducer、generic control normalization/options 和 semantic
  HTML；`web/app.js` 负责 DOM/WS/FileReader 接线。
- 打开 structured agent 时，在第一帧到达前就进入 chat surface，避免短暂落入 xterm 后停在
  “resumed live session”。lazy mount 使用 per-view epoch 丢弃旧 agent/view 的延迟结果，并用 single-flight 合并 cold-start
  event burst。live `event` 与 restore `event` 走同一个 reducer。
- `LiveRegistry.event_restore()` 从 durable recording 反向截取最新的、最多 4 MiB 的完整 `kind: event` 行，
  验证每行是 JSON object 后按原顺序组成 JSONL。浏览器把该有界 replay window 当作权威快照，先 reset state
  再逐行独立解析/fold；坏行不会吞掉后面的有效事件。
- reducer 合并 `session.config`、`user.echo`、assistant message、tool、permission、turn 和 error 状态；
  optimistic user turn 在收到 canonical `user.echo` 时不会重复显示。同一 `tool_id` 的完整 tool snapshot 更新已有
  streaming card；connector 对同一 turn 的重复 `turn.end` 去重，并在 Claude partial delta 后抑制重复完整文本，
  因此 live 与 restore 都不会出现重复气泡/工具卡。非 JSON native stdout 只产生通用 `error`，原文不会进入 relay/recording。

### 9.6 LocalProject 持久化与隐私边界

- `connector/local_store.py` 的 `LocalProjectStore` 使用 `~/.deepbox/state.db`，表为
  `local_project(id, name, path, created_at, updated_at)`。SQLite 开启 WAL、`synchronous=NORMAL`、
  `busy_timeout` 和 `foreign_keys`；跨进程 mutation 用相邻 `.lock` 文件串行化。新目录/数据库分别尝试
  `0700`/`0600` 权限。`add()` 只接受已存在的目录、存为绝对路径，展开 `~` 但不展开路径中合法的环境变量语法；
  canonical path 重复添加会复用原 ID，显式名称只更新 metadata。
- `deepbox project add|remove|list|sync`（由 `connector.cli` 委托）与常驻 connector 共用该 store。每次连接和 mutation 后，
  `Connector.report_projects()` 向 `/api/devboxes/{id}/projects` 只发送 `public_projects()` 的
  `{id,name}`（以及 legacy migration 的 `{agent_id,local_project_id}`）；绝不发送 `path`。
- Server 的 `DevboxProject` 只保存 path-free metadata。Agent 通过 `local_project_id` 外键引用 project，删除
  project 时外键置空；authoritative report 也会清理已消失 project 和悬空引用，再推送新的 agent directory。
  创建 agent 时 Server 校验 project 属于同一 devbox，`runtime_config` 必须是对象且不超过 16 KiB。
- `resolve_agents()` 在 connector 本机把 `local_project_id` 解析成 `cwd`。缺失 ID/目录产生 `project_error`，
  supervisor 的 attach 返回结构化 `runtime.unavailable(code=project_unavailable)`。旧 directory 中的 `cwd`
  会被导入为 LocalProject；首次成功 report 提交 migration 并清空 Server 上所有 legacy absolute cwd。

### 9.7 Connector WebSocket 稳定性

`Connector.run()` 与 `run_transport()` 统一使用：`open_timeout=30s`、`ping_interval=20s`、
`ping_timeout=60s`、`close_timeout=5s`、`max_size=16 MiB`。这降低短时调度/SNAT 抖动造成的误断，
但不会掩盖断线；外层 connector loop 仍在 abnormal close 后退避重连并从 durable spool 续传。
生产 B1 App Service 的 SNAT 上限仍是平台容量风险，参数硬化不是扩容的替代品。

### 9.8 当前限制与验证

- Claude structured 默认使用 `--permission-mode acceptEdits`，其他声明模式映射到对应 `--permission-mode`，仅
  `bypassPermissions` 使用 `--dangerously-skip-permissions`；Copilot structured 使用 `--allow-all-tools`。workspace
  role/keyboard lease 仍限制谁能提交输入，但这两个 adapter 当前不提供逐 tool 的 runtime approval。
- Copilot 每 turn 新进程，因此不保留跨 turn context；Claude 的 context 跟随 live structured process。
- Canonical event 不转发 raw provider payload、chain-of-thought、connector token、模型凭证或工作站路径。
- `tests/test_connector_runtimes.py`、`tests/test_copilot_session.py`、
  `tests/test_connector_transport.py`、`tests/test_server_recording.py` 覆盖 adapter/options/附件/restore/WS；
  `web/chat.test.js` 与 `web/ui.test.js` 覆盖 JSONL 容错、reducer、controls、render 与 surface selection。
