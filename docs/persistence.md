# 会话持久化设计（Session Persistence）— 平台的立身之本

> 本文档专门设计 deepbox 的**会话持久化 / 重连 / 回放**能力。
>
> **为什么这一步决定成败**：用户在本地开终端用 agent，关掉窗口，一切归零。
> 如果我们的平台只是"网页版终端"，那和本地没区别。**平台的全部价值在于：
> 会话活在 devbox 上、被服务器忠实记录、随时随地无损重连、还能回放历史。**
> 这份设计就是把这句话变成机制。

---

## 1. 本地 vs 平台：我们要兑现的 5 个差异

| 能力 | 本地终端 | deepbox 平台（本设计交付） |
|---|---|---|
| **会话生命周期** | 绑定终端窗口，关了就没 | 会话活在 devbox 的 PTY 里，**独立于任何观看者** |
| **断线恢复** | 网络抖动可能丢工作 | **零丢失重连**：自动重连 + 立即还原当前画面 |
| **跨设备** | 不可能 | 关笔记本 → 手机/另一台机器打开，**同一个 live 会话** |
| **历史** | 只有内存 scrollback，关了即失 | **完整 DVR 录制**，可回放、审计、（未来）搜索 |
| **协作** | 单人 | **多观看者**同时看同一 live 会话（天然支持） |

---

## 2. 核心难点：终端是"屏幕"不是"日志"

终端输出流里混着光标移动、清屏、颜色、以及 **alt-screen**（Claude/vim 这类全屏 TUI
会切到备用屏并不断重绘）。因此：

- ❌ **不能**"从会话开始重播所有字节"——长会话里 99% 是 TUI 的中间重绘帧，又慢又大。
- ✅ 必须区分两个需求，用两种机制：
  1. **"现在屏幕长什么样"**（重连时要立刻看到）→ 用**服务端无头终端模拟器**维护当前屏幕状态，
     序列化成一小段"重绘字节"发过去。**有界**（只和屏幕尺寸有关），对 TUI 完美。
  2. **"这个会话从头到尾发生了什么"**（回放/审计）→ 用 server SQLite 中的 Protocol v3
     **durable `RecordingFrame`** 记录；API 可导出 asciicast v2，也可返回 events + checkpoints 做随机 seek。

---

## 3. 三层架构：PTY / Recorder / Viewer 彻底解耦

```
┌── devbox (connector) ──┐   ┌────────── server ──────────┐   ┌── 多个浏览器/设备 ──┐
│                        │   │                            │   │                     │
│  PtySession (claude)   │   │  LiveSession (每会话一个)  │   │  Viewer A (笔记本)  │
│  ▲ 持久存活            │   │   ├ pyte 屏幕(权威当前态)  │◀─▶│  Viewer B (手机)    │
│  │ 不随浏览器关闭      │──▶│   ├ SQLite durable frames  │   │  Viewer C (同事)    │
│  │ 只随 connector 进程 │out│   └ subscribers 广播集合   │   │                     │
│  └ 或 terminate 结束    │   │                            │   │  attach → restore   │
└────────────────────────┘   └────────────────────────────┘   │        → live 流    │
      源头(source of truth      平台价值层(持久化/记录/广播)      └─────────────────────┘
      of the LIVE process)      (viewer 来去自由，会话不受影响)
```

**三条铁律：**
1. **PTY 是活进程的唯一源头**，住在 devbox 的 connector 进程里，**只随 connector 进程存亡或显式
   terminate 而结束**——浏览器开关、网络抖动都不影响它。
2. **Server 是持久化与广播层**：把 PTY 输出喂进 pyte（维护当前屏幕）+ 落盘（DVR）+ 广播给
   所有观看者。**这是平台相对本地的增值所在。**
3. **Viewer 完全无状态、可随意来去**：attach 时先收到一帧 `restore`（当前屏幕），随后收 live 流。

---

## 4. 生命周期语义：detach ≠ terminate（关键修正）

当前 P0 代码里"浏览器发 close → connector 杀 PTY"是**错的**，它让平台退化成本地终端。
新语义：

| 动作 | 触发 | 对 PTY 的影响 | 用途 |
|---|---|---|---|
| **attach** | 浏览器打开会话 | 确保 PTY 存在（幂等） | 开始观看 |
| **detach** | 浏览器关闭/离开 | **无**——PTY 继续活 | 离开但不结束（换设备、临时关闭） |
| **terminate** | 用户显式"结束会话" | 杀掉 CLI 进程 | 真的不要了 |
| （connector 进程死） | devbox 重启等 | PTY 随之消失，会话标记 ended | 不可抗力；未来可用 tmux 兜底 |

会话状态机：`starting → live →（detach 多次、attach 多次）→ ended`。

---

## 5. Server 端：LiveSession 与 Recorder

每个 `session_id` 对应一个内存 `LiveSession`（pyte 当前屏幕 + subscribers），持久事实源则是
SQLite `RecordingFrame`。connector 的 output 先按 `(session_id, pty_instance_id, seq)` durable
commit，server 才发送 ACK；随后同一字节更新 pyte 并广播给 live viewer。相同 seq + payload hash
可安全 re-ACK，冲突 payload fail closed。

**重连 restore**（attach 时）：`serialize_screen(screen)` 把 pyte 当前屏幕转成一段
"清屏 + 逐行带 SGR 颜色重绘 + 定位光标"的字节，发一帧 `restore`。server 进程重启后，
`LiveRegistry` 通过 `durable_events()` 重建 screen，再接收 connector 磁盘 spool 精确补发的缺失 seq。
connector transport 重启不杀 PTY；supervisor/sessiond 重启目前仍会结束其托管的 PTY。

**DVR 回放**：`GET /api/sessions/{id}/recording` 从 durable rows 动态导出 asciicast v2；
`GET /api/sessions/{id}/replay` 返回 events、checkpoint 和 metadata。web seek 先恢复目标时间之前最近的
checkpoint，再应用 cursor 更大的 output event，因而相同时间戳的帧也不会漏放。Session retention
支持 `none/7d/30d/permanent`：过期 payload 被清空且 checkpoint 删除，但 seq/hash ledger 保留。

### 5.1 Cut 8 workspace 与协作状态

- `organization(id, name, is_personal, owner_user_id, created_at)`：workspace 的组织容器；personal organization
  通过 owner 关联用户。
- `workspace(id, org_id, name, is_personal, created_at)` 与
  `membership(id, workspace_id, user_id, role, created_at)`：Membership 对 `(workspace_id,user_id)` 唯一，角色为
  `viewer/operator/admin/owner`。Devbox 与 Session 各增加 nullable `workspace_id`；nullable 只用于无损迁移窗口，
  登录后的 workspace bootstrap 会把旧 Devbox 和其 Session 回填到 owner 的 personal workspace。
- `session_participant(id, session_id, user_id, role, joined_at, last_seen_at)`：对 `(session_id,user_id)` 唯一，
  attach 时 upsert，用于展示参与者；WebSocket 是否在线仍以进程内 Hub 为准。
- `keyboard_lease(session_id, holder_user_id, acquired_at, expires_at, version)`：每个 Session 至多一行。
  acquire/renew/release/handoff 在事务中检查 workspace role、TTL 和 version；过期行可被下一位 controller 原子接管。
  这是短期协作控制状态，不进入 recording 内容，也不改变 Server 不持有模型凭证的边界。

`_migrate()` 仅做 additive nullable columns/new tables，并调用幂等 personal workspace backfill；不重写
recording ledger 或 connector spool。SQLite 文件备份因此同时包含 workspace 元数据和 lease 状态。

---

## 6. 帧协议（browser attach + connector protocol v3）

浏览器 → server：
- `attach {session_id}`（原 `open`）
- `input {session_id, data}`
- `resize {session_id, cols, rows}`
- `detach {session_id}`（原 `close`，**不再杀 PTY**）
- `terminate {session_id}`（显式结束；Cut 8 要求有效 keyboard holder）
- `keyboard_acquire / keyboard_renew / keyboard_release {session_id}`
- `keyboard_handoff {session_id, target_user_id}`（仅当前 holder）

server → 浏览器：
- `collaboration {session_id, participants, keyboard}`（角色、在线参与者与 lease 快照）
- `keyboard_request {session_id, requester_user_id, requester_username}`（忙时通知当前 holder）
- `restore {session_id, data}`（attach 后立即，当前屏幕重绘字节）
- `output {session_id, data}`（live 流）
- `status {session_id, state}`（live/ended/offline）
- `exit {session_id, code}`

server ↔ connector：
- `open`（幂等，确保 PTY 在）
- `output {session_id, pty_instance_id, seq, kind, data}` → durable commit 后 `ack`
- gap → `resend_from`；相同 seq/hash → duplicate re-ACK；不同 payload → fail closed
- `input {client_input_id}` → connector 去重并返回 `input_ack`
- `resize` / `terminate`；detach **不**下发任何东西给 connector

---

## 7. 浏览器端：自动重连 + 无缝还原

- WS 断开 → 指数退避自动重连 → 重连后自动 `attach` → 收 `restore` 重画 → 继续。
- 点击 agent 时先查询其 session 列表；若 connector 上报了仍存活的 session，则打开最新的
  `live` session，而不是每次点击都静默创建新 session。
- restore 使用 `pyte.HistoryScreen`：先重建有限 scrollback，再精确重画当前 viewport 与光标。
- 用户视角：**网络抖一下，画面顿一下就回来了，server 离线期间输出也会由 connector 补发**。
- 换设备：登录 → 打开同一会话 → attach → restore，看到的就是另一台设备上的当前进度。
- 会话已 ended：显示最终画面 + "回放历史"入口（DVR）。

---

## 8. 有界性与成本

- **restore 快照有界**：只和屏幕尺寸有关（~几十 KB），与会话时长无关。
- **pyte 内存有界**：屏幕 + 有限 scrollback。
- **durable recording**：SQLite rows 随时长线性增长，且位于 ACK 路径以提供 delivery 语义；
  默认 30d retention，也可选 none/7d/permanent。清理 payload 不删除 dedup identity row。
- checkpoint interval 有界 seek 重放量；checkpoint 含完整屏幕，因此 retention 清理同步删除相关 checkpoint。
- Owner 可调用 `DELETE /api/sessions/{id}/recording` 执行 secure erase：每个 durable frame
  保留 `(session_id, pty_instance_id, seq)`、kind、原 `payload_hash` 与时间戳以维持 Protocol v3
  dedup/hash 账本，但 `data` 被固定 redaction marker 替换并写入 `redacted_at`；所有 checkpoint
  被物理删除。操作幂等，非 owner/跨租户目标返回 opaque 404。

---

## 9. 验收标准（这一步做没做好，就看这几条）

1. 打开 Claude,让它输出一屏 → **关掉浏览器标签** → 重新打开会话 → **立刻看到关闭前的
   完整画面**,并能继续对话。（会话没死、无损重连）
2. 拔网/刷新 → 自动重连 → 画面无缝恢复,输入不丢。
3. 两个浏览器窗口同开一个会话 → 在 A 里打字,B 里**实时**看到。（多观看者）
4. 结束会话后,能**回放**整个过程。（DVR）
5. 全程 server 不理解内容、不持有 key —— 只是忠实的记录者与广播者。
