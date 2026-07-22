# deepbox — 设计文档

> **一句话定位**：deepbox 是一个"agent 交换机 / 管理面"。用户把他们本地
> devbox 里的 agent CLI（Claude Code、GitHub Copilot CLI、Codex CLI …）连接到我们的
> server，然后**登录我们的平台，就能像在本地终端里一样**跟这些 agent 交互。
>
> **我们是平台，不是 AI 产品。** Server 永远不跑模型、不持有任何 API key、不安装任何
> CLI。智能与凭证 100% 留在用户的 devbox 上。我们只提供：身份、连接、频道/会话、
> 消息中继、presence、以及把用户输入和 agent 输出双向转发的"神经"。

---

## 0. 灵感来源

本设计直接借鉴姊妹项目 **deepradio** 的 **Computer Model**（`C:\Code\deepradio\docs`）。
核心思想完全一致：server 发出**不含内容的唤醒/中继信号**，真正的 agent 运行在用户机器上的
一个用户自启进程里。deepbox 把这套模型用 **Python** 重新实现，并针对
"HPC 式多 devbox / 多 agent 管理" + "把真实交互式 CLI 原样投射到 web" 做了强化。

deepradio 与 deepbox 的关键差异：

| 维度 | deepradio | deepbox |
|---|---|---|
| Agent 本体 | `link` 里的 LLM 循环 / 本地 CLI | **真实的交互式 CLI 进程**（claude/copilot/codex） |
| 中继粒度 | content-free wake + REST 拉取整条消息 | **持久 PTY 会话**，逐字节双向流式转发 |
| 用户体验目标 | 聊天室里多了个 AI 队友 | **与本地开终端用该 CLI 完全一致** |
| 技术栈 | TypeScript / Hono / SQLite | **Python / FastAPI / SQLite** |

---

## 1. 三个实体（Human / Devbox / Agent）

```
┌─────────────┐   owns   ┌──────────────────────────────┐
│   Human     │─────────▶│           Devbox             │  e.g. "Alex 的开发机 / 集群登录节点"
│ (user 登录) │          │  一台跑 connector 的机器     │
└─────────────┘          │  由一个 TOKEN 认证           │
                         └───────────────┬──────────────┘
                                         │ hosts (1:N)
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                     ▼
              ┌──────────┐        ┌──────────┐         ┌──────────┐
              │  Agent   │        │  Agent   │         │  Agent   │  每个 = 一个 agent principal
              │@claude   │        │@copilot  │         │@codex    │  绑定 devbox_id + runtime
              └──────────┘        └──────────┘         └──────────┘
```

| 实体 | 是什么 | 认证 |
|---|---|---|
| **Human** | 平台用户，浏览器登录 | session cookie（P0 用户名/密码；后续 OAuth） |
| **Devbox** | 一台跑 `connector` 的机器，认证单元，Human 拥有 | `hpc_box_<64hex>` bearer token，只存 hash |
| **Agent** | 一个可交互的 CLI 实例（runtime=claude-code/copilot-cli/codex-cli/mock），host 在某个 Devbox 上 | 继承其 Devbox 的 token，只能通过该 Devbox 说话 |

- **一个 Human 可拥有多个 Devbox**（多台开发机 / 多个集群节点）。
- **一个 Devbox 可 host 多个 Agent**（同一台机器上跑多个不同 CLI）。
- **Devbox 不是 principal**：它是基础设施，从不出现在消息作者里，只有它的 Agent 会。

---

## 2. 数据模型（SQLite / SQLAlchemy）

```
user           id, username, password_hash, display_name, created_at
devbox         id, owner_user_id, name, created_at, last_seen_at, capabilities(JSON)
token          id, devbox_id, hash, preview, created_at, last_used_at, revoked_at
agent          id, devbox_id, handle, display_name, runtime, cwd, launch_cmd,
               presence(offline|online|busy|error), created_at
session        id, user_id, agent_id, title, created_at            # 一次人-agent 的会话/频道
message        id, session_id, author_kind(user|agent|system),
               author_id, body, created_at
# 流式 CLI 输出不进 message 表逐条存；见 §5 的传输模型。
```

> 命名保持"无聊耐用"：代码里就叫 `devbox/agent/session/message`。产品/营销层面可以再包装
> HPC 术语（"接入节点 / 作业 / 会话"）。

Token 规则（照搬 deepradio，已验证过的做法）：
- 格式 `hpc_box_` + 64 hex（32 随机字节）。
- 只存 `sha256(token)`，按 hash 查。
- `preview` = `hpc_box_` + 随机部分前 6 位 + `…`，供 UI 显示"是哪一个 token"。
- 创建时**完整 token 只返回一次**，之后不可再取。
- 可轮换（发新的）、可吊销（`revoked_at`，hub 立即断开在用的连接）。

---

## 3. 连接模型（两种 WebSocket）

Server 维护一个 **Hub**，管理两类连接：

```python
Conn =
  | HumanConn   { ws, user_id }                       # 浏览器
  | DevboxConn  { ws, devbox_id, agent_ids: set,      # connector
                  outbound: Queue[dict], sender_task, retired }
```

### Human 连接（浏览器）
`GET /ws?session=<cookie>` → 校验登录 → 订阅该用户可见的 session 事件。

### Devbox 连接（connector）
```
connector ──WS upgrade, header: Authorization: Bearer hpc_box_...──▶ server
server: 校验 token
        ├─ 无效/吊销 → close(4001)
        └─ 有效 → 解析 Devbox D
                   载入 D 的 agents (agent WHERE devbox_id = D.id)
                   把所有 host 的 agent presence 置为 online
                   touch devbox.last_seen_at
                   conn = DevboxConn(..., outbound=Queue(maxsize=256))
                   注册路由；同 Devbox 的旧连接 retire + close(4002)
                   先排 hello，再从 fresh DB snapshot 排权威 agents 目录
```
断开时：仅当该连接仍是 Hub 当前映射才清路由并把 agent 置为 offline；被替换旧连接的迟到 `finally` 不会覆盖新连接状态。

所有 server → connector 帧只做非阻塞入队，由每条连接唯一的 sender task 保序写 WebSocket。单帧发送超时（5 秒）、失败或 256 帧队列溢出会 retire 并 close(1011)，所以并发增删 agent 的 HTTP 请求不会卡在慢连接网络 I/O 上。`hello {devbox_id, agent_ids, protocol_version: 3}` 在连接可接收并发目录更新前入队，严格保持首帧语义。

> **为什么用 header 而不是 `?token=`**：connector 是本地进程，能设置 WS upgrade header，
> 把密钥挡在 URL / 访问日志之外。浏览器不能设 WS header —— 没关系，人类不用 token。

---

## 4. 认证与写入规则

| 请求来源 | 判定 | 允许的作者 |
|---|---|---|
| 带 `Authorization: Bearer hpc_box_...` | 校验 → 解析 Devbox D | author 必须是 `devbox_id == D.id` 的 agent（否则 403） |
| 无 token（浏览器 session） | 视为已登录 Human | author 必须是该 Human 自己（否则 403） |

要点：**无 token 的请求不能以 agent 身份说话**；**一个 token 只能扮演它那台 Devbox
上的 agent** —— 杜绝跨 Devbox 冒充。

---

## 5. 传输模型 —— 本设计的核心（"感知无差别"）

用户要求：**通过平台用 agent，和在本地终端里用，体验完全一样。**

因此 connector 不是"每条消息起一次 CLI"，而是为每个 agent 维持一个
**持久的交互式 PTY 会话**（就像用户自己开着那个终端常驻）。

```
┌────────── 用户的 devbox（connector 进程）──────────┐
│                                                     │
│  Agent @claude  ── PTY ──▶  `claude`  (常驻交互进程)│
│       ▲  │ stdin              │ stdout/stderr       │
│       │  └──────── 写入 ──────┘                     │
│       │                       │ 逐块读取            │
│       │                       ▼                     │
│   client.py ◀── WS 双向流 ──▶ server Hub            │
└─────────────────────────────────────────────────────┘
                                 ▲   │
                     input 帧    │   │ output 帧（流式）
                                 │   ▼
                          浏览器 xterm.js 终端视图
```

**帧协议（WS，JSON，`protocol_version=3`；持久化语义详见 `persistence.md`）**

Server → Devbox：
- `hello` `{devbox_id, agent_ids, protocol_version}`
- `open` `{agent_id, session_id, cols, rows}` —— 幂等地确保该 session 的 PTY 就绪
- `input` `{agent_id, session_id, data}` —— 用户键入（转发到 PTY stdin）
- `resize` `{agent_id, session_id, cols, rows}` —— 终端尺寸变化
- `terminate` `{agent_id, session_id}` —— 用户显式结束会话时才杀 PTY
- `agents` `{agents:[{id,handle,runtime,cwd,launch_cmd}]}` —— 云端新增/删除 agent 后按 devbox 串行**热推** fresh committed snapshot；connector 完整校验后将其作为权威目录原子替换，删除项的 PTY 同时终止；任一畸形元素令整帧 no-op，合法空数组明确 clear-all

Devbox → Server：
- `sessions` `{sessions:[{agent_id,session_id}]}` —— 重连时上报仍存活的 PTY
- `ready` `{agent_id, session_id}`
- `output` `{agent_id, session_id, data}` —— PTY 输出（流式，逐块；WS 离线时 connector 排队）
- `exit` `{agent_id, session_id, code}` —— CLI 进程退出
- `presence` `{agent_id, state}` —— busy/online/error
- `runtimes` `{capabilities:[…]}` —— 可选的 WS capability 刷新；当前 connector 通常在建 WS 前探测并走 REST 上报

Human → Server：`attach/input/resize/detach/terminate`。`detach` 只取消观看，绝不杀 PTY。
Server → Human：`restore/output/status/ready/exit/presence`；attach 后先还原 scrollback + 当前屏幕，
随后衔接 live 输出。详见 `persistence.md`。

> **为什么走 PTY 而不是 `-p` 一次性调用**：Claude Code / Copilot / Codex 这类 CLI 的
> 价值在于**多轮、有记忆、可批准工具调用的交互式会话**。一次性调用会丢掉上下文、丢掉
> 审批流、丢掉 TUI。PTY 转发让"平台里的终端"就是"本地那个终端"。
>
> **消息 vs 流**：结构化会话元信息落 `message`/`session` 表；高频字节流写入每个 session
> 的 asciicast v2 DVR 文件，同时进入 `pyte.HistoryScreen` 维护有限 scrollback 与当前屏幕。
> 刷新、换设备或 server 重启后可 restore；实现细节见 `persistence.md`。

跨平台注意：PTY 在 Linux/macOS 用 `pty`，Windows 用 `pywinpty`（ConPTY）。connector
按平台选择实现；server 与协议完全平台无关。

---

## 6. REST 面（管理 + 运行时）

**管理（浏览器 / 登录 Human）**
| 方法 · 路径 | 作用 |
|---|---|
| `POST /api/auth/register` `{username,password}` | 注册 |
| `POST /api/auth/login` | 登录，设 session cookie |
| `POST /api/devboxes` `{name}` | 创建 Devbox + 首个 token，**完整 token 返回一次** |
| `GET /api/devboxes` | 列出我的 Devbox（含 agents、在线状态） |
| `DELETE /api/devboxes/:id` | 删除（级联 token；其 agent 一并移除） |
| `POST /api/devboxes/:id/agents` `{handle,display_name,runtime,cwd?,launch_cmd?}` | 新建一个 host 的 agent |
| `DELETE /api/agents/:id` | 删除 agent；SQLite 外键级联 session/message，浏览器 watcher 收到 exit，在线 connector 立即收到新权威目录并终止 PTY/丢弃该 agent 的待发帧 |
| `POST /api/devboxes/:id/tokens` | 轮换：发新 token（返回一次） |
| `DELETE /api/tokens/:id` | 吊销 token |
| `GET /api/agents/:id/sessions` | 列出该 agent 的会话及 live/inactive/ended 状态 |
| `POST /api/agents/:id/sessions` | 开一个新会话，返回 session_id |
| `GET /api/sessions/:id/messages` | 会话历史（结构化消息） |
| `GET /api/sessions/:id/recording` | 下载 asciicast v2 DVR 录制 |

**运行时（connector / Bearer token）**
| 方法 · 路径 | 作用 |
|---|---|
| `GET /api/me` | 返回本 Devbox + 它要跑的 agent 名单（handle/runtime/cwd/launch_cmd） |
| `POST /api/devboxes/:id/runtimes` `{capabilities}` | connector 在建 WS 前上报本机可用 CLI；Fleet Add agent 下拉框只列已上报 runtime |

> **配置切分（保护凭证）**：非机密的 per-agent 配置（handle、runtime、cwd、启动命令）存在
> server，`GET /api/me` 下发。**任何 API key / 登录态** 只存在 connector 本地环境，
> server 永不经手。这就是整个设计的意义所在。

---

## 7. connector 包（`connector/`，Python）

用户自启进程。启动：
```
python -m connector --server-url http://localhost:8077 --token hpc_box_...
# 远程三机部署使用 Tailscale Serve HTTPS URL；详见 remote-deployment.md
# 或环境变量 DEEPBOX_SERVER_URL / DEEPBOX_TOKEN
```
启动流程：
1. `GET /api/me` → 拿到要跑的 agent 名单（每个的 runtime / cwd / launch_cmd）。
2. 探测本机可用 CLI（`where claude` / `where copilot` …）→ `POST /runtimes`。
3. 开 WS（Bearer header），收 `hello`。
4. 收到 `open` → 用 pty/pywinpty 起该 agent 的交互进程；`input`→写 stdin；
   PTY 输出→`output` 帧回传；`resize`→调 PTY 尺寸；进程退出→`exit`。

---

## 8. web 客户端（`web/`）

P0 极简：登录页 + Devbox/Agent 管理页 + 聊天/终端页（**xterm.js** 渲染 PTY 流，
输入回传 server）。后续可换 React。用户感知：打开某个 agent = 打开一个"云端终端"，
和本地那个 CLI 一模一样。

---

## 9. Roadmap

- **P0（本次骨架）**：user 注册/登录；创建 Devbox + token；创建 agent（先支持 `mock`
  runtime，一个假装是 CLI 的回显进程）；connector 连上；WS 帧协议打通；web 上开一个
  终端和 mock agent 交互。**目标：端到端链路跑通，不接真实 CLI。**
- **P1**：接真实 CLI（claude-code / copilot-cli / codex-cli），Windows 用 pywinpty、
  *nix 用 pty；presence 精细化；会话流可选落盘回放。
- **P2**：真正的 Human 认证（OAuth）、多用户共享 agent 的权限、审计。
- **P3**：作业式管理（把长任务当"作业"排队/追踪）、文件传输、通知。

---

## 10. 一图记住 —— 核心回路

```
用户在 web 终端敲了一行  ──WS input──▶  Server Hub
                                          │ 找到 host 该 agent 的 Devbox 连接
                                          ▼
                              ──WS input──▶ connector（用户的 devbox）
                                          │ 写入常驻 CLI 的 PTY stdin
                                          │ CLI 交互式产出（流式）
                              ──WS output──┘
Server Hub  ◀────────────────────────────
   │ 透传给该 session 的浏览器
   ▼
web 终端逐字节渲染 ── 和本地开终端一模一样
```
