"""Session supervisor (``sessiond``): owns PTY/session lifecycle.

Cut 4 separates *session ownership* from *WebSocket transport*. This module is
the ownership half: it starts, feeds, resizes and kills PTYs and buffers their
output. It has **no knowledge of WebSockets or the server**. A transport (see
:mod:`connector.transport`) attaches over an IPC :class:`~connector.ipc.Channel`
and relays frames to/from the server.

Key invariant (the whole point of the split):
    Detaching or restarting the transport MUST NOT kill any PTY. PTYs live and
    keep producing output into an in-memory pending buffer; the next transport
    to attach drains that buffer in order. Durable/disk spooling is Cut 5 — the
    ``pending`` deque here is the seam it will replace.
"""
from __future__ import annotations

import asyncio
from collections import deque

from .ipc import Channel
from .pty_session import PtySession, resolve_cmd


class SessionSupervisor:
    """Owns every PtySession for this devbox, independent of any transport."""

    def __init__(self, agents: dict[str, dict] | None = None):
        self.agents: dict[str, dict] = agents or {}
        # key = (agent_id, session_id) -> PtySession
        self.ptys: dict[tuple[str, str], PtySession] = {}
        # Output/exit/ready frames buffered until a transport drains them.
        # Cut 5 replaces this in-memory deque with a durable spool.
        self.pending: deque[dict] = deque()
        self.pending_event = asyncio.Event()
        # A frame remains queued until the transport confirms WebSocket send.
        # This preserves the pre-split reconnect guarantee across the IPC seam.
        self._next_delivery_id = 1
        self._inflight_delivery_id: int | None = None
        self._delivery_ack = asyncio.Event()
        # The currently attached transport channel, or None when detached.
        self._channel: Channel | None = None

    # -- transport attach/detach ------------------------------------------

    def attach(self, channel: Channel) -> None:
        """Bind a transport channel. Existing PTYs are untouched.

        Any frames buffered while detached are re-signalled so the transport's
        drain loop resends them in order.
        """
        self._channel = channel
        if self.pending:
            self.pending_event.set()

    def detach(self) -> None:
        """Unbind the transport. PTYs keep running and buffering output."""
        self._channel = None

    @property
    def attached(self) -> bool:
        return self._channel is not None

    # -- outbound buffering ------------------------------------------------

    def emit(self, frame: dict) -> None:
        """Queue an outbound frame for the transport (never blocks on WS)."""
        self.pending.append(frame)
        self.pending_event.set()

    async def drain_to(self, channel: Channel) -> None:
        """Forward buffered frames to ``channel`` until cancelled.

        The supervisor removes a frame only after the transport acknowledges a
        successful WebSocket send. If the transport disappears before that
        acknowledgement, the frame remains queued for the next attach. A crash
        after the server accepted a frame but before the acknowledgement can
        duplicate it; Cut 5 adds durable sequence/resume semantics.
        """
        while True:
            if not self.pending:
                self.pending_event.clear()
                await self.pending_event.wait()
                continue
            frame = self.pending[0]
            delivery_id = self._next_delivery_id
            self._next_delivery_id += 1
            self._inflight_delivery_id = delivery_id
            self._delivery_ack.clear()
            await channel.send({
                "type": "ipc_delivery",
                "delivery_id": delivery_id,
                "frame": frame,
            })
            await self._delivery_ack.wait()
            if self.pending and self.pending[0] is frame:
                self.pending.popleft()
            self._inflight_delivery_id = None

    # -- control handling --------------------------------------------------

    async def handle_control(self, frame: dict) -> None:
        """Apply one control frame received from a transport."""
        t = frame.get("type")
        if t == "ipc_delivery_ack":
            if frame.get("delivery_id") == self._inflight_delivery_id:
                self._delivery_ack.set()
            return
        aid = frame.get("agent_id")
        sid = frame.get("session_id")
        if t == "open":
            await self.open_pty(aid, sid, frame.get("cols", 120), frame.get("rows", 30))
        elif t == "input":
            p = self.ptys.get((aid, sid))
            if p:
                p.write(frame.get("data", ""))
        elif t == "resize":
            p = self.ptys.get((aid, sid))
            if p:
                p.resize(frame.get("cols", 80), frame.get("rows", 24))
        elif t in ("close", "terminate"):
            p = self.ptys.pop((aid, sid), None)
            if p:
                p.kill()
        elif t == "list_sessions":
            self.emit(self.sessions_frame())

    def sessions_frame(self) -> dict:
        return {
            "type": "sessions",
            "sessions": [{"agent_id": aid, "session_id": sid}
                         for aid, sid in self.ptys.keys()],
        }

    async def open_pty(self, agent_id: str, session_id: str,
                       cols: int = 120, rows: int = 30) -> None:
        key = (agent_id, session_id)
        if key in self.ptys:
            self.emit({"type": "ready", "agent_id": agent_id, "session_id": session_id})
            return
        info = self.agents.get(agent_id, {})
        cmd = resolve_cmd(info.get("runtime", "mock"), info.get("launch_cmd"))

        async def on_output(data: str):
            self.emit({"type": "output", "agent_id": agent_id,
                       "session_id": session_id, "data": data})

        async def on_exit(code: int):
            self.ptys.pop(key, None)
            self.emit({"type": "exit", "agent_id": agent_id,
                       "session_id": session_id, "code": code})

        p = PtySession(cmd, info.get("cwd"), on_output, on_exit, cols=cols, rows=rows)
        try:
            await p.start()
        except Exception as e:  # pragma: no cover - real PTY spawn failure
            self.emit({"type": "exit", "agent_id": agent_id,
                       "session_id": session_id, "code": -1,
                       "data": f"\r\n[failed to start: {e}]\r\n"})
            return
        self.ptys[key] = p
        self.emit({"type": "ready", "agent_id": agent_id, "session_id": session_id})
        self.emit({"type": "presence", "agent_id": agent_id, "state": "online"})

    def status(self) -> dict:
        """Machine-readable supervisor status for CLI/doctor surfaces."""
        return {
            "attached": self.attached,
            "sessions": [{"agent_id": aid, "session_id": sid}
                         for aid, sid in self.ptys.keys()],
            "pending_frames": len(self.pending),
        }

    def shutdown(self) -> None:
        """Kill all PTYs. Only used on real supervisor exit, never on detach."""
        for p in list(self.ptys.values()):
            p.kill()
        self.ptys.clear()
