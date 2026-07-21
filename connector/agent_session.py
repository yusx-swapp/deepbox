"""Structured agent session: drive a coding agent in headless/streaming mode.

Where :class:`connector.pty_session.PtySession`投屏一个全屏 TUI 的原始终端字节流
(每次重绘、每个按键回显都被迫走网络往返), a :class:`StructuredAgentSession`
instead runs the agent in its **headless structured** mode and translates its
native protocol into a small, agent-agnostic *canonical event* stream. The
browser then renders a real chat UI (message bubbles, tool cards, a permission
prompt) instead of a terminal, so:

  * 用户"打完整段话再发送"——输入不再逐键往返;
  * agent 的真实反应(文本增量、工具调用、结果)以结构化事件流式到达;
  * 接入新 agent(Copilot CLI、Codex……)只需再写一个 translator。

For Claude Code the headless interface is::

    claude -p --output-format stream-json --input-format stream-json \
           --include-partial-messages --verbose [--permission-mode ...]

which speaks newline-delimited JSON on stdio.

Interface parity with :class:`PtySession`
-----------------------------------------
This class exposes the *exact* surface the supervisor already drives —
``start()``, ``write(str)``, ``resize(cols, rows)``, ``kill()``,
``is_alive()`` and the ``on_output`` / ``on_exit`` async callbacks — so the
supervisor only chooses which class to construct. ``on_output`` still receives
a ``str``; for a structured session that string is one canonical event encoded
as JSON, carried in a frame with ``kind="event"``. The server persists and
fans that frame out through the same durable spool / ACK / replay / fence
pipeline as terminal output (it never branches on ``kind``); the browser
demultiplexes on ``kind``.

Security invariants preserved:
  * argv is built + validated by :mod:`connector.runtimes` (no shell, no
    metacharacters); no secrets are ever placed on argv or emitted.
  * We never log or emit prompt/response *content* here beyond forwarding the
    canonical event to the same trusted output path terminal bytes already use.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Awaitable, Callable

IS_WIN = sys.platform == "win32"

# Canonical event names (agent-agnostic). The browser renders on these.
EV_STATUS = "status"          # session/init/system status
EV_MESSAGE_DELTA = "message.delta"   # assistant text increment
EV_MESSAGE = "message"        # a complete assistant message (fallback / final)
EV_TOOL_CALL = "tool.call"    # agent invoked a tool (name + input)
EV_TOOL_RESULT = "tool.result"  # a tool returned
EV_PERMISSION_ASK = "permission.ask"  # agent needs approval to use a tool
EV_TURN_END = "turn.end"      # one assistant turn finished (usage/cost)
EV_USER_ECHO = "user.echo"    # our own user message, replayed for ack
EV_ERROR = "error"


def _event(ev: str, **fields) -> dict:
    """Build one canonical event dict."""
    out = {"ev": ev}
    out.update(fields)
    return out


def translate_claude_event(obj: dict) -> list[dict]:
    """Translate one Claude ``stream-json`` object into canonical events.

    Pure function (no I/O) so it can be unit-tested with synthetic transcripts
    — no real ``claude`` process and no token spend. Returns zero or more
    canonical event dicts; unknown shapes yield ``[]`` (forward-compatible).

    Claude Code ``stream-json`` object shapes handled:
      * ``{"type":"system","subtype":"init", ...}``            -> status(init)
      * ``{"type":"stream_event","event":{...}}``  (partials)  -> message.delta / tool.call
      * ``{"type":"assistant","message":{content:[...]}}``     -> message / tool.call (non-partial)
      * ``{"type":"user","message":{content:[tool_result]}}``  -> tool.result
      * ``{"type":"result","subtype":..., ...}``               -> turn.end
      * ``{"type":"control_request", ... can_use_tool ...}``   -> permission.ask
    """
    t = obj.get("type")

    if t == "system":
        return [_event(EV_STATUS, subtype=obj.get("subtype"),
                       session_id=obj.get("session_id"),
                       model=obj.get("model"))]

    if t == "stream_event":
        # Anthropic Messages API streaming deltas (via --include-partial-messages).
        return _translate_stream_event(obj.get("event") or {})

    if t == "assistant":
        # A complete assistant message (arrives even without partials).
        msg = obj.get("message") or {}
        out: list[dict] = []
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                out.append(_event(EV_MESSAGE, text=block["text"],
                                  final=True))
            elif bt == "tool_use":
                out.append(_event(EV_TOOL_CALL,
                                  tool=block.get("name"),
                                  tool_id=block.get("id"),
                                  input=block.get("input")))
        return out

    if t == "user":
        # Tool results come back wrapped as a user message.
        msg = obj.get("message") or {}
        out = []
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    out.append(_event(EV_TOOL_RESULT,
                                      tool_id=block.get("tool_use_id"),
                                      is_error=bool(block.get("is_error")),
                                      content=_flatten_tool_result(
                                          block.get("content"))))
        return out

    if t == "result":
        return [_event(EV_TURN_END,
                       subtype=obj.get("subtype"),
                       is_error=bool(obj.get("is_error")),
                       cost_usd=obj.get("total_cost_usd"),
                       usage=obj.get("usage"),
                       result=obj.get("result"))]

    if t == "control_request":
        # A tool wants to run and the session isn't in an auto-approve mode.
        req = obj.get("request") or {}
        if req.get("subtype") in ("can_use_tool", "permission"):
            return [_event(EV_PERMISSION_ASK,
                           request_id=obj.get("request_id"),
                           tool=req.get("tool_name") or req.get("tool"),
                           input=req.get("input"))]
        return []

    return []


def _translate_stream_event(event: dict) -> list[dict]:
    et = event.get("type")
    if et == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [_event(EV_MESSAGE_DELTA, text=delta["text"])]
        if delta.get("type") == "input_json_delta" and delta.get("partial_json"):
            # Streaming tool-input; browser can ignore until the tool.call lands.
            return []
        return []
    if et == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            return [_event(EV_TOOL_CALL, tool=block.get("name"),
                           tool_id=block.get("id"), input=block.get("input"),
                           streaming=True)]
        return []
    if et == "message_stop":
        return [_event(EV_MESSAGE, final=True, text="")]
    return []


def _flatten_tool_result(content) -> str:
    """Reduce a tool_result content payload to a display string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return str(content)


def translate_copilot_event(obj: dict) -> list[dict]:
    """Translate one GitHub Copilot CLI ``--output-format json`` object.

    Pure function (no I/O), unit-tested with synthetic transcripts. Copilot
    emits newline-delimited JSON objects shaped ``{"type","data","id",...}``.
    Handled types:
      * ``assistant.message_delta`` -> message.delta  (data.deltaContent)
      * ``assistant.message``       -> message (final) + tool.call per toolRequest
      * ``assistant.turn_end`` / ``result`` -> turn.end
      * ``session.*`` (mcp/skills/tools loaded, status) -> status (or dropped)
      * ``user.message``            -> [] (our own echo; UI already showed it)
    Unknown/ephemeral shapes yield ``[]`` (forward-compatible).
    """
    t = obj.get("type")
    data = obj.get("data") or {}

    if t == "assistant.message_delta":
        txt = data.get("deltaContent")
        if txt:
            return [_event(EV_MESSAGE_DELTA, text=txt)]
        return []

    if t == "assistant.message":
        out: list[dict] = []
        # The streamed deltas already carried the text; emit a final marker so
        # the UI closes the bubble, then surface any tool requests.
        out.append(_event(EV_MESSAGE, final=True, text=data.get("content") or ""))
        for tr in data.get("toolRequests") or []:
            out.append(_event(EV_TOOL_CALL,
                              tool=tr.get("name") or tr.get("tool"),
                              tool_id=tr.get("id"),
                              input=tr.get("arguments") if "arguments" in tr
                              else tr.get("input")))
        return out

    if t == "tool.execution_started" or t == "tool.call":
        return [_event(EV_TOOL_CALL, tool=data.get("name"),
                       tool_id=data.get("id") or data.get("toolCallId"),
                       input=data.get("arguments") or data.get("input"))]

    if t in ("tool.execution_completed", "tool.result"):
        return [_event(EV_TOOL_RESULT,
                       tool_id=data.get("id") or data.get("toolCallId"),
                       is_error=bool(data.get("isError") or data.get("error")),
                       content=_flatten_tool_result(
                           data.get("result") if "result" in data
                           else data.get("content")))]

    if t in ("assistant.turn_end", "result"):
        return [_event(EV_TURN_END, subtype=t,
                       is_error=bool((data or {}).get("error")))]

    if t and t.startswith("session."):
        # Startup/system status; keep it lightweight and non-content.
        return [_event(EV_STATUS, subtype=t)]

    return []


def encode_user_message(text: str) -> str:
    """Encode a user turn as one Claude ``stream-json`` stdin line."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def encode_permission_response(request_id: str, allow: bool) -> str:
    """Encode a control response approving/denying a tool-use request."""
    return json.dumps({
        "type": "control_response",
        "response": {
            "request_id": request_id,
            "subtype": "success" if allow else "error",
            "response": {"behavior": "allow" if allow else "deny"},
        },
    }) + "\n"


# Translator registry: pick by runtime id so adding an agent is one function
# plus one register() — no changes to the session machinery.
TRANSLATORS: dict = {}


def register_translator(runtime_id: str, fn):
    TRANSLATORS[runtime_id] = fn


register_translator("claude-code-structured", translate_claude_event)
register_translator("copilot-cli-structured", translate_copilot_event)


class StructuredAgentSession:
    """Drive one agent in headless structured mode, PtySession-compatible.

    ``spawn`` is injectable for tests: it must return an object exposing
    ``stdin`` (with ``write``/``drain``/``close``), ``stdout`` (an async line
    iterator via ``readline``), ``wait()`` and ``kill()`` — i.e. an
    ``asyncio.subprocess.Process``. The default spawns the real agent.

    Two drive modes cover the spread of headless agents:

    * **persistent** (default; Claude Code): one long-lived process; ``write``
      pushes a user turn onto its stdin via ``encode_user_message``.
    * **per_turn** (Copilot CLI ``-p``): the agent runs one prompt then exits,
      so each ``write(text)`` spawns a fresh process with the prompt appended
      to argv, sharing a stable ``--session-id`` for context. Process exit
      ends only that turn, not the server session.
    """

    def __init__(self, cmd: list[str], cwd: str | None,
                 on_output: Callable[[str], Awaitable[None]],
                 on_exit: Callable[[int], Awaitable[None]],
                 cols: int = 120, rows: int = 30,
                 spawn: Callable[..., Awaitable] | None = None,
                 translate=None, per_turn: bool = False,
                 prompt_argv=None):
        self.cmd = cmd
        self.cwd = cwd or None
        self.on_output = on_output
        self.on_exit = on_exit
        self.cols = cols
        self.rows = rows
        self._spawn = spawn or self._default_spawn
        self._translate = translate or translate_claude_event
        self._per_turn = per_turn
        # For per_turn: extra argv inserted before the prompt (e.g. the prompt
        # flag). The prompt text is appended as the final argv element.
        self._prompt_argv = list(prompt_argv or [])
        self._proc = None
        self._alive = False
        self._stderr_tail: list[str] = []


    async def _default_spawn(self, prompt: str | None = None):
        argv = list(self.cmd)
        if self._per_turn and prompt is not None:
            argv = argv + self._prompt_argv + [prompt]
        return await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def start(self):
        if self._per_turn:
            # No process until the first user turn; the session is "alive" in
            # the sense that the server session should stay open awaiting input.
            self._alive = True
            self._proc = None
            self._spawn_lock = asyncio.Lock()
            await self._emit(_event(EV_STATUS, subtype="ready"))
            return
        self._proc = await self._spawn()
        self._alive = True
        asyncio.create_task(self._read_stdout())
        if getattr(self._proc, "stderr", None) is not None:
            asyncio.create_task(self._read_stderr())

    async def _run_one_turn(self, prompt: str):
        """per_turn: spawn a fresh process for a single prompt and drain it."""
        async with self._spawn_lock:
            self._proc = await self._spawn(prompt)
            if getattr(self._proc, "stderr", None) is not None:
                asyncio.create_task(self._read_stderr())
            await self._drain_turn()

    async def _drain_turn(self):
        proc = self._proc
        stream = proc.stdout
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            except Exception:
                break
            if not line:
                break
            await self._handle_line(line)
        try:
            await proc.wait()
        except Exception:
            pass
        # Turn finished; ensure the UI closes the turn even if the agent didn't
        # emit an explicit terminal event.
        await self._emit(_event(EV_TURN_END, subtype="process_exit"))
        self._proc = None

    async def _read_stdout(self):
        proc = self._proc
        stream = proc.stdout
        while self._alive:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Overlong line: skip to next; never crash the reader.
                continue
            except Exception:
                break
            if not line:
                break
            await self._handle_line(line)
        self._alive = False
        code = 0
        try:
            code = await proc.wait()
        except Exception:
            pass
        await self.on_exit(int(code or 0))

    async def _read_stderr(self):
        stream = self._proc.stderr
        while True:
            try:
                line = await stream.readline()
            except Exception:
                break
            if not line:
                break
            # Keep only a short tail for diagnostics; never emit as content.
            try:
                self._stderr_tail.append(line.decode(errors="replace"))
                del self._stderr_tail[:-20]
            except Exception:
                pass

    async def _handle_line(self, raw: bytes):
        text = raw.decode(errors="replace").strip()
        if not text:
            return
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Non-JSON noise on stdout (banner etc.): forward as a status note
            # rather than dropping, but never as assistant content.
            await self._emit(_event(EV_STATUS, note=text[:500]))
            return
        for ev in self._translate(obj):
            await self._emit(ev)

    async def _emit(self, ev: dict):
        await self.on_output(json.dumps(ev))

    def is_alive(self) -> bool:
        if not self._alive:
            return False
        if self._per_turn:
            # Between turns there is no process, but the session stays open.
            return True
        if self._proc is None:
            return False
        rc = getattr(self._proc, "returncode", None)
        if rc is not None:
            self._alive = False
            return False
        return True

    def write(self, data: str):
        """Send one user turn. ``data`` is plain text (not terminal bytes)."""
        if not self.is_alive():
            return
        if self._per_turn:
            # One process per prompt (Copilot -p). Ignore overlapping turns
            # while one is still running (the lock also guards this).
            if self._proc is not None:
                return
            asyncio.create_task(self._run_one_turn(data))
            return
        if self._proc is None:
            return
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(encode_user_message(data).encode())
        except Exception:
            pass

    def respond_permission(self, request_id: str, allow: bool):
        """Answer a pending ``permission.ask`` via the control channel."""
        if not self.is_alive() or self._proc is None:
            return
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(encode_permission_response(request_id, allow).encode())
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        # Structured sessions have no terminal geometry; record for parity.
        self.cols = cols
        self.rows = rows

    def kill(self):
        self._alive = False
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except Exception:
            pass
