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

## 1. 实体层级（Workspace / Devbox / Agent）

```text
Human ── Membership(role) ──▶ Workspace ── owns ──▶ Devbox ── runs ──▶ Agent
```

- **Human**：浏览器用户；通过本地密码或 Azure App Service Easy Auth 的 Microsoft 身份登录，随后使用 deepbox 自己签发的限时 `deepbox_session` cookie。
- **Workspace**：用户可见的协作与授权边界。一个用户可加入多个 workspace；一个 workspace 可包含多个 Devbox。
- **Membership**：Human 在某个 Workspace 内的 `viewer / operator / admin / owner` 角色。workspace 成员可发现该空间下全部 Devbox 与 Agent；写操作继续按角色检查。
- **Devbox**：一台用户机器上的 connector/supervisor。连接时只带 Devbox bearer token，不带 Microsoft token 或模型 key。
- **Agent**：Devbox 上由 supervisor 管理的 CLI 进程；浏览器看到的层级固定为 **Workspace → Devbox → Agent**。Agent 可引用一个 path-free `local_project_id`。
- **LocalProject**：connector-local 项目；绝对路径只存在本机 `state.db`，Server 只持有同 ID 的 path-free `DevboxProject` metadata。
- **Skill**：用户提供的 `SKILL.md` package；connector 管理内容和 runtime bindings，Server 仅持有 path-free inventory。

部署级 `User.role`（`owner / member`）只控制全局用户管理等控制面能力；它与 workspace membership 分离。服务端仍然不运行模型，也不持有 Claude/Copilot/Codex 凭据。

---

## 2. 数据模型（SQLite / SQLAlchemy）

核心表（省略部分时间戳和辅助字段）：

```text
user(id, username, password_hash, display_name, role, email,
     auth_provider, external_tenant_id, external_subject, disabled_at)
organization(id, name, owner_user_id, is_personal)
workspace(id, organization_id, name, is_personal)
membership(id, workspace_id, user_id, role)
workspace_invitation(id, workspace_id, email, role, token_hash, token_preview,
                     created_by_user_id, expires_at, accepted_at,
                     accepted_by_user_id, revoked_at)
devbox(id, owner_user_id, name, last_seen_at, capabilities, skills, workspace_id)
devbox_project(id, devbox_id, name, runtime_config, created_at, updated_at)
token(id, devbox_id, hash, preview, created_at, last_used_at, revoked_at)
agent(id, devbox_id, handle, display_name, runtime, local_project_id,
      runtime_config, cwd, launch_cmd, presence, created_at)
session(id, user_id, agent_id, title, retention, workspace_id, created_at)
session_participant(id, session_id, user_id, role, joined_at)
message(id, session_id, author_kind, author_id, body, created_at)
recording_frame(id, session_id, pty_instance_id, seq, kind, data, payload_hash,
                elapsed, timestamp, created_at, redacted_at)
recording_checkpoint(id, session_id, frame_id, event_index, elapsed,
                     cols, rows, screen, created_at)
invitation(id, email, role, token_hash, token_preview, expires_at,
           max_uses, used_count, revoked_at)
```

`devbox.skills` 是 connector 上报的 sanitized JSON inventory；`devbox_project`
不含本机 path。`agent.cwd` 仅保留作 one-cycle legacy migration bridge。
Connector 本机另有 `local_project` 与 `local_skill` 两张 SQLite table；后者保存
scope、project ID、digest、family targets 与实际 binding paths，均不属于 Server schema。

关键约束：

- `uq_user_external_identity` 对非空 `(auth_provider, external_tenant_id, external_subject)` 建唯一索引，保证同一 Microsoft 主体只映射一个用户。
- `membership` 对 `(workspace_id, user_id)` 唯一；最后一个 workspace owner 不可降级或删除。
- workspace 邀请只持久化 SHA-256 token hash 与短 preview；链接明文只在创建响应出现一次。邀请按标准化邮箱绑定、单次接受、可过期/撤销；重新签发会撤销旧的未领取链接。
- 接受 workspace 邀请在同一事务内创建 membership 并标记 `accepted_at`；并发双击捕获唯一约束冲突后重查 membership，返回幂等成功。
- `runtime_capabilities_json` 与 `capability_flags` 对 server 都是 opaque JSON；服务端不解释 runtime/model 字符串。
- `_migrate()` 补列/补索引；`_backfill_workspaces()` 为旧用户建立 personal workspace，并回填 Devbox/Session 归属与 owner membership。

### Token 规则

- 明文格式：`hpc_box_<urlsafe>`
- 只在创建/轮换响应中出现一次
- SQLite 只保存 `SHA-256(token)` 和短 `preview`
- connector WebSocket 用 `Authorization: Bearer hpc_box_...`

### LocalProject

- `DevboxProject` 归属一个 `Devbox`
- 唯一键是 `(devbox_id, path)`
- `is_default=true` 表示该 Devbox 的默认工作目录；同一 Devbox 最多保留一个默认项目
- `capability_flags` 是 connector 上报的 opaque JSON
- workspace 成员可读项目；只有 `admin/owner` 可改项目目录或 capability
- connector 可通过 `project_sync` 消息同步本机项目清单；server 不探测本机文件系统

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
                   载入 D 的 projects + agents（路径仍只在 connector 本机）
                   把所有 host 的 agent presence 置为 online
                   touch devbox.last_seen_at
                   conn = DevboxConn(..., outbound=Queue(maxsize=256))
                   注册路由；同 Devbox 的旧连接 retire + close(4002)
                   先排 hello，再从 fresh DB snapshot 排权威 projects + agents 目录
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

## 5. 核心架构：Structured-first，PTY fallback

### 5.1 两条本地执行路径

支持 structured 的 adapter 使用原生 chat 路径：

```text
Browser composer
  -> generic input + options
  -> Server opaque relay
  -> Connector RuntimeAdapter / StructuredAgentSession
  -> 本地 Claude Code 或 Copilot CLI
  -> canonical events
  -> Server durable relay
  -> Browser semantic reducer/render
```

其他 adapter 保留 terminal fallback：

```text
Browser xterm <-> Server byte relay/recording <-> Connector PTY <-> 本地 CLI
```

模型计算、provider 登录态和模型凭证始终留在用户机器。Server 不启动 CLI，也不解释 runtime ID、
model、effort 或 attachment；它只做身份/RBAC/keyboard lease、opaque frame 转发、可靠记录和协作广播。

### 5.2 RuntimeAdapter 与 capability facts

Connector 的 registry 是扩展边界。每个 adapter 描述：

- runtime ID/label、probe 与本地 command；
- PTY 或 structured 模式，以及 persistent/per-turn 进程策略；
- model、permission 和 CLI argv 映射；
- generic `select` / `file` controls 的 scope、choices、default 与 bounds。

Probe 后 connector 上报 display-safe capability object。稳定 revision 忽略 probe 时间戳；动态发现的 model
catalogue 会投影到各 surface 的 model control。若已安装 runtime 的 live discovery 没有得到 model ID，
connector 使用该 family 的 adapter static catalog，并标记 `models.status=partial`、`models.source=adapter`；
有 runtime 结果时标记 `complete/runtime`。只有可靠的非交互 authentication status probe 才能阻断启动，
无法安全探测时状态为 `unknown`。Server 将 capability 作为 opaque JSON 保存；浏览器仅根据
`features.structured` 选择 chat surface，并从 `features.controls` 生成 model/reasoning/file widgets。
UI 始终提供 `Runtime default`，且仅在 model control 声明 `allow_custom=true` 时提供可编辑 model ID combobox。
connector-local executable path 不上报，浏览器也没有 runtime-ID 特判。

扩展原则仍是：**新增 runtime = 一个 connector adapter；Server 和 Browser 不改。**

### 5.3 Canonical event contract

Structured adapter 只向上游发送统一事件：

- `status`
- `session.config`
- `user.echo`
- `message.delta` / `message`
- `tool.call` / `tool.result`
- `permission.ask`
- `turn.end`
- `error`

事件只包含 UI 和恢复所需的 display-safe 字段，不包含 raw provider payload、chain-of-thought、token、
模型凭证或工作站路径。每个逻辑 turn 最多记录一个 `turn.end`；同一 `tool_id` 的流式开始与完整快照在
reducer 中更新同一张工具卡。connector 会抑制 provider 在 delta 之后重复发送的完整文本快照。
live frame 与 restore JSONL 使用同一个 reducer。

### 5.4 Frame protocol v3

| 方向 | Frame | 语义 |
|---|---|---|
| Browser -> Server -> Connector | `input {data, options, client_input_id}` | PTY bytes 或 structured turn；options 对 Server opaque |
| Browser -> Server -> Connector | `resize` / `terminate` | terminal resize 或显式结束本地 session；`terminate` 仅接受持有 keyboard lease 的 operator |
| Server -> Connector | `open` | 幂等确保本地 PTY/structured process 存在 |
| Connector -> Server | `output {seq, pty_instance_id, kind, data}` | `kind` 为 `output` 或 `event`；durable commit 后 ACK |
| Server -> Browser | `restore {kind?, data}` | terminal screen bytes，或 `kind:event` 的 canonical-event JSONL |
| Server -> Browser | `output {kind, data}` | live terminal bytes 或单个 canonical event |

Structured options 和附件在 connector 按 adapter descriptor 二次验证；Server 不把 capability blob 变成
业务 schema。输出可靠性仍由 connector spool、单调 seq、Server ACK、带 `expected_seq` 的 `resend`、旧 instance 的
`fence` 和 payload hash 冲突 fail-closed 提供。

### 5.5 恢复与重连

- Terminal attach：Server 从 pyte/recording 恢复当前 screen，再接 live bytes。
- Structured attach：Browser 先由 capability 进入 chat；Server 返回最新的、最多 4 MiB 的完整 durable event JSONL
  tail，再接 live events。该 tail 是当前有界 replay window 的权威快照：browser 先 reset 再逐行 fold；单个坏行不会破坏
  后续 timeline。lazy chat mount 以 view epoch 隔离旧视图，并以 single-flight 合并 cold-start event burst。
- Connector WebSocket 使用 30s open timeout、20s ping、60s pong tolerance、5s close timeout 和
  16 MiB frame bound；异常断线后外层 loop 继续退避重连及 spool 续传。
- model/reasoning 等 session-scoped controls 在 session 已配置或出现首个 chat item 后锁定。`New chat`
  发送 `terminate`、创建空 persisted session 并重新开放 controls；旧历史不删除。

### 5.6 LocalProject 与用户 Skills

- `deepbox project add <path> --name <name>` 将 canonical absolute path 写入 connector-state `state.db`；Windows
  默认 root 为 `%LOCALAPPDATA%/deepbox`，macOS/Linux 为 `${XDG_STATE_HOME:-~/.local/state}/deepbox`。Server 的
  `DevboxProject` 只含 ID、name 与非敏感 `runtime_config`。
- Add-agent 每次打开会刷新 runtime/project inventory，可选择 `local_project_id`。其 **Add a local project**
  只生成可复制命令，浏览器与 Server 不浏览或修改本机文件系统。
- Skill root 必须含 UTF-8 `SKILL.md`；YAML frontmatter 用 `yaml.safe_load` 解析，要求 lower-kebab-case `name`
  与 string `description`，且目录 basename 等于 `name`。tree 上限 256 个 regular files / 10 MiB；traversal、
  symlink/junction/reparse、读期间变化均拒绝；脚本只标记 `contains_scripts`，Deepbox 从不执行。
- scope 为 `personal` 或已注册 LocalProject。`--project` 可按 ID、唯一 case-insensitive name、exact normalized
  path 解析；无值等价 `--project .`，以 `commonpath` 的最长包含项目为准。
- adapter family 声明 personal/project skill roots；一个 family target 可对应多个 roots。Claude Code 同时绑定
  `.claude/skills` 与 `.agents/skills` family roots；Copilot/Codex 使用 `.agents/skills`。重发现 roots 时 merge
  旧 bindings，不留下 orphan。
- source-of-truth 为 `<connector-state-root>/skills/store/<digest>/<name>/`。安装先双重验证/hash，再 staged
  atomic replace 全部 destinations；失败 rollback。`list`/`inspect` 先验证 store，再验证 bindings，返回
  `installed` / `drifted` / `missing`；install/remove 遇 drift 必须显式 `--force`。最后引用移除后 GC store digest。
- Connector 只上报 `{id,name,description,digest,scope,project_id,targets,contains_scripts,status}`。Server 不接收
  任何 path；仍有 skill 引用时，本机与 Server report reconciliation 都拒绝删除项目。

## 6. REST API（核心）

| Method | Path | Auth | 说明 |
|---|---|---|---|
| `GET` | `/api/auth/config` | 无 | 返回启用的本地/Microsoft 登录方式 |
| `POST` | `/api/auth/login` | 无 | `local/hybrid` 的密码登录 |
| `GET` | `/api/auth/microsoft/start` | 无 | 跳转 `/.auth/login/aad` |
| `GET` | `/api/auth/microsoft/callback` | Easy Auth headers | tenant+subject upsert，签发 Deepbox cookie |
| `GET` | `/api/auth/microsoft/logout` | 无 | 清 cookie 并跳转 `/.auth/logout` |
| `GET` | `/api/me` | Cookie | 当前用户资料 |
| `GET/POST` | `/api/workspaces` | Cookie | 列出 membership / 创建 workspace（创建者为 owner） |
| `GET/POST` | `/api/workspaces/{id}/members` | Cookie + workspace role | 列出成员 / 添加已有用户 |
| `PATCH/DELETE` | `/api/workspaces/{id}/members/{user_id}` | Cookie + admin/owner | 改角色 / 删除成员，保护最后 owner |
| `GET/POST` | `/api/workspaces/{id}/invitations` | Cookie + admin | 列出 / 创建邮箱绑定邀请；admin 不可授予 admin |
| `DELETE` | `/api/workspaces/{id}/invitations/{invite_id}` | Cookie + admin | 撤销未领取邀请 |
| `POST` | `/api/workspace-invitations/preview` | 无 | body 中提交 token，返回 workspace/角色/掩码邮箱 |
| `POST` | `/api/workspace-invitations/accept` | Cookie | 邮箱严格匹配后原子接受，重复提交幂等 |
| `GET/POST` | `/api/devboxes` | Cookie + workspace role | 聚合用户所有 workspace 的 Devbox / 在 workspace 创建 |
| `POST` | `/api/devboxes/{id}/tokens` | Cookie + workspace admin | 签发新 connector token |
| `DELETE` | `/api/devboxes/{id}/tokens/{token_id}` | Cookie + workspace admin | 撤销 connector token |
| `GET/POST` | `/api/devboxes/{id}/agents` | Cookie + workspace role | 列出 / 创建 Agent |
| `GET/POST` | `/api/agents/{id}/sessions` | Cookie + workspace role | 列出 / 创建共享会话 |
| `GET/POST` | `/api/sessions/{id}/messages` | Cookie + participant/role | 列出 / 发送结构化消息 |
| `GET/POST` | `/api/sessions/{id}/tasks` | Cookie + participant/role | 列出 / 创建结构化任务 |
| `POST` | `/api/devboxes/{id}/projects` | Connector Token | 替换 path-free LocalProject metadata；处理 one-cycle legacy migration，并拒绝删除仍被 skill 引用的项目 |
| `POST` | `/api/devboxes/{id}/skills` | Connector Token | 替换 sanitized skill inventory（最多 256 项，不接收 path） |

Microsoft 登录的信任边界在 Azure App Service Easy Auth：平台先验证 OAuth/OIDC，再注入 `X-MS-CLIENT-PRINCIPAL*`。应用不接收浏览器 Microsoft bearer token，不存 access/refresh token；非 App Service 或未正确启用 Easy Auth 的部署必须保持 `DEEPBOX_AUTH_MODE=local`。`microsoft` 模式还要求明确的 owner 邮箱 allowlist 与 `DEEPBOX_PUBLIC_URL`。

workspace 邀请 token 放在 URL fragment `#workspace-invite=...`，不会随首个 HTTP 请求发送；前端仅为跨 OAuth redirect 暂存到 `sessionStorage`，preview 使用 POST body，避免 token 出现在查询字符串和常规访问日志。

## 7. connector 包（`connector/`，Python）

用户自启进程。正式使用先安装一次本地 `deepbox` command；日常启动：
```text
DEEPBOX_SERVER_URL=http://localhost:8077 DEEPBOX_TOKEN=hpc_box_... deepbox connect
# PowerShell 使用对应的 $env:... 赋值
```

`deepbox` 的稳定 shim 位于 `~/.deepbox/bin`，从调用者当前目录启动
`connector.cli`。只有 `deepbox upgrade` 会重新运行 installer 并刷新
`~/.deepbox/app`；`deepbox connect` 不执行安装逻辑。源码开发仍可直接运行
`python -m connector`。

启动流程：
1. `GET /api/me` 获取权威 agent 与 path-free project 目录，并拒绝 protocol version mismatch。
2. 从本机 `state.db` 上报 projects 与 sanitized skills；registry probe runtime family，生成 display-safe
   capability object 并 `POST /runtimes`。
3. 启动 inventory watcher；外部 CLI 修改 project/skill 后自动重报 metadata。
4. 建立带 Bearer token 的 WS，收 `hello`/权威目录。
5. `open` 时按 adapter 创建 `StructuredAgentSession` 或 `PtySession`；只有 connector 将 `local_project_id`
   解析为 launch `cwd`。
6. Structured input 进入 `write_turn(data, options)`；PTY input 写 stdin。两种 output 都先进入本地
   durable spool，再等 Server commit ACK；断线后精确补发。
7. 两个 WS entry point 共用 30s open、20s ping、60s pong、5s close 与 16 MiB frame 策略。

---

## 8. web 客户端（`web/`）

单页 switchboard 提供 Fleet、session/协作、native chat 与 terminal fallback：

- capability 报 `features.structured` 时，在第一帧前进入 chat；canonical event 驱动 reducer/render；
- generic `select`/`file` descriptors 生成 model、reasoning 和附件 widgets；session lock 后用 `New chat` 重开；
- Add-agent 刷新 runtime/project inventory，选择 LocalProject，并只生成 copyable `deepbox project add ...`；
- Skills modal 只展示 path-free inventory 与 connector-local CLI 命令；
- tab re-attach fold durable event JSONL，再继续 live event；
- 非 structured runtime 继续由 xterm.js 渲染原始 PTY bytes。

---

## 9. Roadmap

- **已完成骨架与可靠性**：本地账号生命周期、Microsoft Easy Auth 身份映射、邮箱绑定 workspace 邀请、
  Workspace → Devbox → Agent 导航、connector hot registration、Protocol v3 durable spool/ACK/resend/fence、
  DVR/retention、workspace RBAC/keyboard lease 与 Azure 部署。
- **当前主线**：headless structured adapters + 自有聊天 UI；capability-driven controls、LocalProject 与
  connector-managed user skills 已落地，继续补齐附件、真实多机验证和 connector transport 稳定性。PTY/xterm 只做兼容 fallback。
- **下一阶段**：真实多机 E2E、更多 adapter、可审计的 runtime permission、长任务/通知与生产容量治理。

---

## 10. 一图记住 —— 核心回路

```text
Human input/options
  -> Server（身份、协作、opaque relay）
  -> Connector（adapter validation + local model CLI）
  -> terminal bytes 或 canonical event
  -> Connector spool
  -> Server durable commit + ACK + broadcast
  -> xterm fallback 或 native chat
```

Server 永不运行模型、读取模型 key、接收 LocalProject/Skill path、执行 Skill 文件，或解析 capability 中的 model/reasoning 业务含义。
