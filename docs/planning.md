# deepbox 项目实施计划

> 状态：Draft v1.1（Remote Connectivity Cut 已实现）
>
> 本计划从当前 P0 出发，按依赖关系把 deepbox 推进到可日常使用、可内部 Beta、再到可公开部署。
> 规划按 **Cut** 而不是日期组织：每个 Cut 必须有明确范围、测试和验收标准，前一层语义稳定后
> 才进入下一层。

---

## 0. 当前基线（2026-07）

### 已实现

- FastAPI + SQLite Server。
- 用户注册、登录和 signed cookie。
- Devbox 创建、一次性 token、token hash、轮换。
- 一名用户多 Devbox；一台 Devbox 多 Agent。
- connector runtime 探测和 WebSocket 连接。
- Windows pywinpty / POSIX pty 抽象。
- `mock` 与真实 Claude Code runtime。
- xterm.js 原生 TUI 流式交互、ANSI、光标、resize。
- `attach/detach/terminate` 协议 v2；detach 不杀 PTY。
- connector 断线内存 FIFO、重连、surviving session 上报。
- UI 优先 resume 最新 live session。
- `pyte.HistoryScreen` scrollback + viewport restore。
- asciicast v2 DVR 文件和下载 API。
- 基础单元测试与真实 Claude E2E 验证。
- production 环境配置、health/readiness、WS Origin 校验和 connector protocol 检查。
- Tailscale Serve 三机远程拓扑与 Windows 启动/诊断流程（`remote-deployment.md`）。

### 当前边界

- Session 状态没有完整持久化状态机。
- UI 没有完整 Session 列表、New/Resume/Terminate/Archive 工作流。
- 内存 FIFO 没有 seq/ACK，不能严格证明零丢失。
- connector 进程退出会杀 PTY。
- DVR 没有 Replay UI、checkpoint、retention。
- Private Alpha 已由 Tailscale Serve 提供 Tailnet 内 TLS，secret/cookie 已外置并 fail-closed；
  但密码 hash、rate limit、审计等仍不满足**公开互联网**生产安全要求。
- Hub/LiveRegistry 是单进程内存结构。
- SQLite 只适合当前单机开发。

---

## 1. 总体执行顺序

```text
Cut R  Remote Connectivity Baseline（已完成：Tailscale private alpha）
  ↓
Cut 1  Session Lifecycle RFC + DB State Machine
  ↓
Cut 2  Session Control Center UI/API
  ↓
Cut 3  Protocol v3 Durable Delivery
  ↓
Cut 4  DVR Replay + Retention
  ↓
Cut 5  Session Supervisor / Connector Service
  ↓
Cut 6  Security Baseline
  ↓
Cut 7  Runtime Registry + Codex/Copilot
  ↓
Cut 8  Workspace / Collaboration
  ↓
Cut 9  Scale-out Architecture
  ↓
Cut 10 Agent-native Jobs / Approvals / Notifications
```

原则：

- 先保证 Session 语义正确，再做可靠投递。
- 先让单机版本可证明正确，再横向扩展。
- 先完成真实用户主流程，再扩展更多 Runtime。
- 每个 Cut 同步更新文档、测试和迁移说明。

---

# Milestone A：Usable Local MVP

目标：单用户在自己的多台 Devbox 上，可以可靠地创建、恢复、结束和回看 Agent Session。

---

## Cut 1 — Session Lifecycle RFC 与持久状态机

### 目标

把 Session 从“一个临时 WS 路由 id”升级为完整的一等对象，并消除 live/inactive/ended 的歧义。

### 设计工作

新增 `docs/session-lifecycle.md`，锁定：

- 状态机和允许的转换。
- New、Attach、Detach、Terminate、Archive、Delete 语义。
- Server 重启、connector 重连、connector 重启的状态处理。
- 同一 Agent 多 Session 并发规则。
- stale connector/重复连接/旧 ready 事件处理。
- Session generation 或 PTY instance id。

### 数据模型

为 Session 增加：

```text
state
started_at
last_activity_at
ended_at
exit_code
cols / rows
connector_instance_id
pty_instance_id
recording_path
recording_bytes
archived_at
```

增加正式 DB migration 机制，建议 Alembic；不再依赖 `create_all()` 充当迁移。

### Server 行为

- 状态转换集中在纯函数/服务层，禁止路由里随意赋字符串。
- connector `ready/exit/sessions` 必须携带 PTY instance/generation。
- 旧连接发来的事件不能覆盖新实例状态。
- ended Session attach 只允许查看最终屏幕/回放，不允许重新起 PTY。

### 测试

- 状态转换表单元测试。
- Server 重启后状态恢复测试。
- 重复 ready/exit 测试。
- stale connector 测试。
- ended Session 不会被重新启动。

### 验收标准

- DB 是 Session 元数据的权威来源。
- 每个状态都有唯一明确含义。
- 任何路径都不会把旧 Session 静默变成新 PTY。
- 测试覆盖所有允许和禁止的状态转换。

---

## Cut 2 — Session Control Center

### 目标

把已有持久化能力暴露成用户真正能理解和操作的产品界面。

### API

```http
GET    /api/sessions
GET    /api/sessions/:id
GET    /api/agents/:id/sessions
POST   /api/agents/:id/sessions
PATCH  /api/sessions/:id
POST   /api/sessions/:id/terminate
POST   /api/sessions/:id/archive
DELETE /api/sessions/:id
```

支持过滤：

```text
state
agent_id
devbox_id
archived
last_activity
```

### UI

- Sessions 首页。
- Agent 下展示 live/disconnected/ended Sessions。
- 明确的 `New Session` 按钮。
- Resume / Replay / Terminate / Archive。
- 会话 rename。
- last activity、duration、cwd、runtime、recording 大小。
- 多 Session tab。
- 页面刷新后恢复当前打开的 Session。
- terminate/delete 二次确认。

### UX 决策

- 点击 Agent 默认进入 Session 列表，不做隐藏创建。
- 只有点击 New Session 才创建 PTY。
- live Session 进入 Terminal；ended Session 进入最终画面/Replay。
- disconnected 显示“等待 connector”，不能假装 live。

### 测试

- Session 查询/权限/API 测试。
- UI 状态映射纯函数测试。
- New 与 Resume 不混淆。
- 多 Session 并发。
- 刷新/重连浏览器验证。

### 验收标准

用户可以完整执行：

```text
登录 → 选 Devbox/Agent → 看 Session 列表
→ New 或 Resume → 使用 → Detach → 再 Resume
→ Terminate → Replay/Archive
```

---

# Milestone B：Provably Durable Sessions

目标：Server 崩溃和网络中断不能造成已经产生的终端输出缺失或重复。

---

## Cut 3 — Protocol v3：Sequence、ACK 与磁盘 Spool

### 问题

当前 connector 在 `ws.send()` 成功后移除内存帧，但 Server 可能在落盘前崩溃，因此仍有丢失窗口。

### 协议

output：

```json
{
  "type": "output",
  "session_id": "...",
  "pty_instance_id": "...",
  "seq": 1042,
  "data": "..."
}
```

ACK：

```json
{
  "type": "ack",
  "session_id": "...",
  "pty_instance_id": "...",
  "seq": 1042
}
```

### Connector

本地 SQLite spool：

```text
outbox(session_id, pty_instance_id, seq, payload, created_at)
ack_state(session_id, pty_instance_id, last_acked_seq)
```

流程：

1. PTY output 先 commit 到本地 SQLite。
2. sender 按 seq 发送。
3. Server 持久化并 ACK。
4. connector 收到 ACK 后清理已确认记录。
5. reconnect 从 last ACK 重发。

### Server

- `(session_id, pty_instance_id, seq)` 唯一约束/去重索引。
- 先持久化 recording，再返回 ACK。
- 重复帧不重复写 recording，但重新 ACK。
- gap 检测；发现 seq 跳跃时请求 resend 或暂缓推进 ACK。

### 输入可靠性

输入帧增加 `client_input_id`，Server/connector 返回 accepted/delivered，防止重连时重复 Enter。

### 故障注入测试

- send 成功但 Server 持久化前 kill。
- Server 持久化后 ACK 前断网。
- 重复发送同一 seq。
- 乱序、缺 seq。
- 离线积压大量输出后恢复。
- connector 重连多次。

### 验收标准

- Recording 无缺帧、无重复、顺序正确。
- Server 任意时刻被 kill 后可恢复。
- 未 ACK 数据始终保留在 connector 磁盘。
- 指标可观察 pending frames/bytes 和 last ACK。

---

## Cut 4 — DVR Replay、Checkpoint 与 Retention

### 目标

把已有 `.cast` 文件变成用户可见的 Session 历史能力。

### Replay UI

- 播放/暂停。
- 0.5x/1x/2x/8x。
- timeline 和 seek。
- 跳到开始/结束。
- 当前时间/总时长。
- 下载 asciicast。
- 最终屏幕静态预览。

### Checkpoint

长 Session 不能每次从头重播。定期保存：

```text
checkpoint(session_id, seq, time, serialized_screen)
```

seek：加载最近 checkpoint，再应用后续事件。

### Recording Store

抽象接口：

```python
RecordingStore:
    append()
    read_range()
    checkpoint()
    delete()
    metadata()
```

开发版可用本地文件；生产版可切对象存储。

### Retention

Workspace/Session 级：

- 不录制
- 7 天
- 30 天
- 永久
- ended 后自动删除/归档

### 验收标准

- 一小时 Session 可以快速打开和 seek。
- 删除 recording 后不可再通过任何 API 获取。
- 权限和 retention 一致执行。
- Replay 与 live Terminal 使用同一终端渲染语义。

---

# Milestone C：Durable Devbox Runtime

目标：connector 自身重启、升级或网络模块崩溃时，Agent 进程继续存在。

---

## Cut 5 — Session Supervisor 与 Connector Service

> **进度（P2 Cut 4 拆分 + P2 Cut 5 持久磁盘 spool 均已落地）**：
> supervisor（会话所有权）/ transport（WS）代码级拆分已完成——`connector/supervisor.py`、
> `connector/transport.py`、`connector/ipc.py`。默认 `python -m connector` 仍经进程内 `LoopbackChannel`
> 保留兼容的单进程形态；`--mode supervisor` / `--mode transport` 则启用真实双进程，transport 重启/断开不再 kill PTY。
> 本地 IPC 抽象已落地真实双进程传输：Windows 命名管道 / POSIX Unix socket（0600）、
> 长度受限 JSON 帧（永不 pickle）、本地当前用户鉴权握手，并新增 `--mode supervisor|transport|all-in-one`
> CLI（默认 all-in-one）；本机 Windows 命名管道 reconnect 、分离 transport detach/reconnect 下 FakePty
> 存活均已有单测实测。
>
> **P2 Cut 5 Protocol v3 durable delivery（已落地）**：connector 使用 SQLite WAL + `synchronous=FULL` 的 `outbox/ack_state/input_receipts`；每个 PTY 启动生成稳定 `pty_instance_id`，输出按 `(session_id, pty_instance_id)` 分配连续 seq 并先提交 spool。transport 只有收到 server 对同一三元组的 durable ACK 才释放队首；send 成功不算 ACK，persist-before-ACK 断线会安全重放，duplicate 会重新 ACK，gap 会请求精确 resend，冲突 fail closed。server 的 `recording_frames` 在 ACK 前提交并做 ownership、唯一键和内容哈希校验。输入使用 UUID `client_input_id` 本地持久去重，server 收到 `input_ack` 后才写 cast。控制帧保留 send-boundary ACK，但只进 sessiond 内存队列，不写 durable spool、不制造 PTY 输出 gap，也不在重启后陈旧重放。
> 真实 Windows 服务
> 形态下的 sessiond 长稳 + 真实 ConPTY / agent 长稳验证仍是真机验收门（需用户人工执行）——见
> implementation.md §8a 真机验收门。

### 架构拆分

```text
deepbox-sessiond
  ├── PTY 生命周期
  ├── 本地 Session registry
  └── durable spool

deepbox-connector
  ├── Server transport
  ├── auth
  └── protocol
```

本地 IPC：

- Windows named pipe。
- POSIX Unix domain socket。

### Windows

- ConPTY 长驻 daemon。
- Windows Service 安装/卸载。
- 开机启动。
- 日志和诊断命令。

### Linux/macOS

- systemd user service / launchd。
- 独立 PTY host 或受控 tmux backend。
- Unix socket 权限。

### CLI

```text
deepbox connect <token>
deepbox status
deepbox sessions
deepbox logs
deepbox doctor
deepbox disconnect
```

### 验收标准

- kill connector transport，Claude 不退出。
- 升级 connector，Claude 不退出。
- transport 恢复后续接同一 PTY 和 spool。
- 用户无需保持一个可见命令行窗口。

> 整台 Devbox 重启后自动恢复 Agent 进程属于后续可选能力，不在本 Cut 默认承诺中。

---

# Milestone D：Internal Beta Readiness

目标：满足可信内部用户使用，而不是只在本机 Demo。

---

## Cut 6 — Security Baseline

### 必做

- Argon2id 密码哈希。
- Server secret 通过环境变量/secret manager。
- Secure/HttpOnly/SameSite cookie。
- CSRF 防护。
- WebSocket Origin allowlist。
- HTTPS/WSS 部署。
- 登录、token、API rate limit。
- Token 轮换和吊销后立即断开连接。
- 审计日志。
- Recording 访问鉴权、删除和 retention。
- 安全 headers。
- 配置分开发/测试/生产。

### 威胁模型

专门文档覆盖：

- token 泄露。
- 跨 Devbox 冒充。
- Session 越权。
- 恶意 Viewer 输入。
- CSRF/WS hijacking。
- Recording 泄露。
- connector 被降级/替换。
- replay/duplicate frame 攻击。

### 验收标准

- 自动安全测试覆盖权限边界。
- 不存在硬编码生产 secret。
- 未授权用户无法读取 live output、recording 或发送 input。
- 公网只暴露 TLS 入口。

---

## Cut 7 — Runtime Adapter Registry 与真实多 Runtime

### 目标

新增 Runtime 不需要修改 Server/Web 的业务逻辑。

### Adapter

```python
RuntimeAdapter:
    id
    label
    probe
    launch_command
    capabilities
    environment
```

### 首批验证

- Claude Code。
- Codex CLI。
- GitHub Copilot CLI。
- mock。

### Capability

```json
{
  "runtime": "claude-code",
  "installed": true,
  "version": "2.1.119",
  "path": "...",
  "features": {}
}
```

Server 只把 capability 当 opaque JSON blob 存储/转发。

### 验收标准

- 每个 Runtime 有 probe/command 单元测试。
- 新增 Runtime = 一个 adapter 文件 + 一个 registry entry。
- Server API 和通用 Web UI 不做 runtime-specific 分支。

---

# Milestone E：Team Product

---

## Cut 8 — Workspace、权限与协作

### 数据模型

```text
Organization
Workspace
Membership
Role
SessionParticipant
KeyboardLease
```

### 角色

- Owner
- Admin
- Operator
- Viewer

### 协作规则

- 多人可同时观看。
- 一个 Session 同时只有一个 keyboard lease。
- 请求、移交、超时释放控制权。
- Viewer 无输入/terminate 权限。
- 所有控制权变化写审计日志。

### 验收标准

- 两个 Viewer 可同步观看。
- 只有 lease owner 可以输入。
- 断线后 lease 自动释放或转移。
- 权限覆盖 Session、Recording、Devbox、Token。

---

## Cut 9 — 横向扩展

只有在单机生命周期和协议稳定后执行。

### 目标架构

```text
PostgreSQL      → 控制面元数据
Redis/NATS      → presence、WS 节点路由、短暂事件
Object Storage  → recordings/checkpoints
API/WS Nodes    → 多实例无状态入口
```

### 关键问题

- connector 连接路由。
- Viewer 与 connector 位于不同 WS 节点。
- Session owner/lease。
- recording 单写者。
- presence TTL。
- Server 节点故障转移。

### 验收标准

- 任一 API/WS 节点退出不终止 Session。
- Viewer 可经任意节点 attach。
- 同一 output 不被多个节点重复持久化。
- presence 有明确 TTL 和最终一致语义。

---

# Milestone F：Agent-native Differentiation

---

## Cut 10 — Jobs、Approvals、Notifications、Artifacts

### Job

把长任务提升为结构化对象：

```text
queued
running
waiting_for_input
waiting_for_approval
completed
failed
cancelled
```

### Approval Inbox

统一展示：

- Agent 等待运行命令批准。
- Agent 等待文件写入批准。
- Agent 等待用户问题回答。
- Agent 需要重新认证。

### Notifications

- 浏览器通知。
- Email。
- Slack/Teams。
- 后续移动推送。

### Artifacts

Session/Job 关联：

- diff/patch。
- 测试结果。
- 日志。
- 生成文件。
- PR/commit 链接。

### 设计约束

优先从 Runtime adapter 的结构化 sideband 获取状态；不要让 Server 通过解析 ANSI 文本猜测 Agent
语义。Terminal 原始流始终保留作为通用 fallback。

---

## 2. 跨 Cut 工程规范

每个实现 Cut 都必须：

1. 先读现有代码和文档，不凭记忆修改。
2. 同一 pass 更新文档，确保文档与代码一致。
3. 新增功能必须有自动化测试。
4. 提供明确 migration/rollback 方案。
5. 做 Windows connector 实测。
6. 做 mock runtime 确定性 E2E。
7. 涉及 lifecycle/transport 时做故障注入。
8. 不触碰用户真实 Devbox/Agent，除非用户明确要求。
9. 不把 API key、CLI 登录态或完整 token 写入仓库/日志。

---

## 3. 建议的近期执行包

### Planning Pack 1：Session Foundation

包含：

- Cut 1 Session Lifecycle RFC。
- DB migration 基础设施。
- Session 状态服务和测试。
- Connector generation/PTY instance id。

完成后再进入 UI。

### Planning Pack 2：User-visible Sessions

包含：

- Cut 2 Session API。
- Session Control Center。
- New/Resume/Terminate/Archive。
- 多 Session tab。

### Planning Pack 3：Durability

包含：

- Cut 3 protocol v3。
- connector SQLite spool。
- Server ACK/去重。
- 故障注入测试工具。

---

## 4. 决策门槛

### 进入 Internal Beta 前

必须完成：

- Cut 1–6。
- 至少 Claude Code + 一个额外 Runtime。
- 安全威胁模型和权限测试。
- connector 安装/诊断流程。
- recording retention/delete。

### 进入 Public Beta 前

必须完成：

- Workspace 权限。
- 可观测性和告警。
- PostgreSQL/对象存储。
- 备份与恢复演练。
- connector 自动升级策略。
- 明确隐私政策和 recording 默认策略。

---

## 5. 风险登记

| 风险 | 影响 | 当前缓解 | 后续动作 |
|---|---|---|---|
| Server ACK 前崩溃丢帧 | Session 历史不完整 | P2 Cut 5 持久磁盘 spool（seq/ACK/fsync/resume）已落地 | Cut 3 端到端 server 侧 seq 对账 |
| connector 退出杀 PTY | Agent 上下文消失 | 自动重连仅覆盖网络 | Cut 5 session supervisor |
| Recording 含 secret | 高隐私风险 | 仅 owner API | Cut 4 retention + Cut 6 加密/权限 |
| Session 状态漂移 | UI 错误恢复/误启动 | surviving session 上报 | Cut 1 generation + DB 状态机 |
| 多 Viewer 同时输入 | 命令混乱/危险 | 当前单用户 | Cut 8 keyboard lease |
| 单进程 Hub | 无法横向扩展 | 单机开发 | Cut 9 Redis/NATS 路由 |
| Runtime TUI 差异 | 兼容性问题 | PTY 原样透传 | Cut 7 adapter + E2E matrix |
| Windows ConPTY 行为差异 | Session 稳定性 | pywinpty 实测 | Cut 5 daemon + Windows CI |

---

## 6. 下一步建议

立即开始 **Cut 1：Session Lifecycle RFC 与持久状态机**，而不是继续扩展 Runtime 或视觉 UI。

原因：

1. Session 是所有后续功能的依赖核心。
2. 状态语义不稳定会让 Durable Delivery、Replay、权限和扩展性反复返工。
3. 当前用户已经能看到“新建 vs 恢复”混淆带来的产品问题。
4. 完成 Cut 1 后，Cut 2 才能做出可靠、可理解的 Session Control Center。

建议下一个可交付件：

```text
docs/session-lifecycle.md
+ Alembic migration
+ SessionState/transition pure functions
+ lifecycle 单元测试
+ Server/connector generation 协议设计
```
