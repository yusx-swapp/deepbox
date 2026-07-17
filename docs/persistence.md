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
  2. **"这个会话从头到尾发生了什么"**（回放/审计）→ 用**磁盘录制**（asciicast，带时间戳），
     完整、不丢、可离线回放。

---

## 3. 三层架构：PTY / Recorder / Viewer 彻底解耦

```
┌── devbox (connector) ──┐   ┌────────── server ──────────┐   ┌── 多个浏览器/设备 ──┐
│                        │   │                            │   │                     │
│  PtySession (claude)   │   │  LiveSession (每会话一个)  │   │  Viewer A (笔记本)  │
│  ▲ 持久存活            │   │   ├ pyte 屏幕(权威当前态)  │◀─▶│  Viewer B (手机)    │
│  │ 不随浏览器关闭      │──▶│   ├ 磁盘录制 .cast (DVR)   │   │  Viewer C (同事)    │
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

每个 `session_id` 对应一个内存 `LiveSession`：

```python
LiveSession:
    screen   : pyte.Screen        # 权威"当前屏幕"，含颜色/光标，尺寸 = cols×rows
    stream   : pyte.Stream        # 把字节喂进 screen
    cast_fp  : 追加写 data/sessions/<id>.cast  (asciicast v2)
    subscribers : set[HumanConn]  # 正在看这个会话的所有浏览器
    dims, start_time, ended, exit_code
```

**输出到达时**（connector `output` 帧）：
```
feed(data):
    screen 消费 data        → 更新当前屏幕（供重连 restore）
    cast 追加 [t,"o",data]  → 落盘（供 DVR 回放）
    broadcast output 给所有 subscribers → live 观看
```

**重连 restore**（attach 时）：`serialize_screen(screen)` 把 pyte 当前屏幕转成一段
"清屏 + 逐行带 SGR 颜色重绘 + 定位光标"的字节，发一帧 `restore`。浏览器写进一个干净的
xterm 就**瞬间还原到当前画面**——无论离开了 1 秒还是 1 小时。

**服务器重启也不丢**由三段机制共同保证：
1. connector 的 PTY reader 只写入本地 FIFO；WS 不可用时输出帧留在队列，重连后按序补发，
   不再让网络异常杀死 PTY reader。
2. connector 重连后上报仍存活的 `(agent_id, session_id)`，server/UI 因而知道应该 **resume
   同一进程**，而不是误建新 session。
3. `get_or_create` 若发现 `.cast` 已存在，把历史 `o` 事件重播进 `pyte.HistoryScreen`，重建
   当前屏幕和有限 scrollback；随后接上 connector 补发的离线期间输出。

当前 FIFO 在 connector 内存中，因此覆盖 **server 重启 / 网络中断（connector 进程仍在）**；
connector 自己重启时 PTY 也会消失。未来若要覆盖整台 devbox/connector 重启，需要把 PTY
托管给 tmux/ConPTY 守护进程，并把 FIFO 做成本地磁盘 spool。

**DVR 回放**：`GET /api/sessions/{id}/recording` 吐出 `.cast` 文件；web 的"回放"模式按时间戳
播放（或秒进）。这是本地终端完全没有的能力——审计、复盘、分享一段 agent 工作过程。

---

## 6. 帧协议变更（protocol v2）

浏览器 → server：
- `attach {session_id}`（原 `open`）
- `input {session_id, data}`
- `resize {session_id, cols, rows}`
- `detach {session_id}`（原 `close`，**不再杀 PTY**）
- `terminate {session_id}`（新增，显式结束）

server → 浏览器：
- `restore {session_id, data}`（attach 后立即，当前屏幕重绘字节）
- `output {session_id, data}`（live 流）
- `status {session_id, state}`（live/ended/offline）
- `exit {session_id, code}`

server ↔ connector：
- `open`（幂等，确保 PTY 在）
- `input` / `resize`
- `terminate`（杀 PTY）——注意 detach **不**下发任何东西给 connector

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
- **.cast 磁盘**：随时长线性增长（纯文本，可压缩/轮转）；这是"完整历史"的必要成本，
  且不参与 live 路径，不影响实时性。
- 未来可加：`.cast` 大小上限 + 轮转；idle 会话回收策略（默认**不回收**以保证持久性）。

---

## 9. 验收标准（这一步做没做好，就看这几条）

1. 打开 Claude,让它输出一屏 → **关掉浏览器标签** → 重新打开会话 → **立刻看到关闭前的
   完整画面**,并能继续对话。（会话没死、无损重连）
2. 拔网/刷新 → 自动重连 → 画面无缝恢复,输入不丢。
3. 两个浏览器窗口同开一个会话 → 在 A 里打字,B 里**实时**看到。（多观看者）
4. 结束会话后,能**回放**整个过程。（DVR）
5. 全程 server 不理解内容、不持有 key —— 只是忠实的记录者与广播者。
