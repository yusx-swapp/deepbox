"""Session supervisor (``sessiond``): owns PTY/session lifecycle.

Cut 4 separates *session ownership* from *WebSocket transport*. This module is
the ownership half: it starts, feeds, resizes and kills PTYs and buffers their
output. It has **no knowledge of WebSockets or the server**. A transport (see
:mod:`connector.transport`) attaches over an IPC :class:`~connector.ipc.Channel`
and relays frames to/from the server.

Key invariant (the whole point of the split):
    Detaching or restarting the transport MUST NOT kill any PTY. PTYs live and
    keep producing output into a durable spool; the next transport to attach
    drains that spool in order.

Cut 5 makes PTY output durable: each output frame is appended and fsynced to a
per-user disk spool *before* it is eligible to send, carries a per-PTY sequence
number, and is removed only after the server's exact durable ACK is persisted.
Restarting the supervisor replays un-acked output. Ephemeral control frames use
an in-memory queue and are never written to the durable spool. Unit constructors
may inject an :class:`~connector.spool.InMemorySpool`; the real CLI runtime
injects a durable :class:`~connector.spool.DiskSpool`.
"""
from __future__ import annotations

import asyncio
from collections import deque
from uuid import UUID, uuid4

from .ipc import Channel
from .pty_session import PtySession, resolve_cmd
from .spool import InMemorySpool, SpoolBase

# Cut 9 pipelining bounds: how many durable output frames (and their bytes) may
# be in flight to the transport before earlier ACKs return. The disk spool is
# still the durability source of truth; this only bounds send-ahead so a slow or
# disconnected server cannot create unbounded WebSocket / memory pressure.
MAX_INFLIGHT_FRAMES = 64
MAX_INFLIGHT_BYTES = 512 * 1024


class _PendingView:
    """List-like compatibility view over ephemeral controls + durable output."""

    def __init__(self, supervisor: "SessionSupervisor"):
        self._supervisor = supervisor

    def _frames(self) -> list[dict]:
        controls = [frame for _delivery_id, frame in self._supervisor._controls]
        outputs = [
            frame for _delivery_id, frame
            in self._supervisor._spool.pending_records()
        ]
        return controls + outputs

    def __len__(self) -> int:
        return len(self._frames())

    def __iter__(self):
        return iter(self._frames())

    def __getitem__(self, index):
        return self._frames()[index]

    def __bool__(self) -> bool:
        return bool(self._supervisor._controls or
                    self._supervisor._spool.pending_records())


class SessionSupervisor:
    """Owns every PtySession for this devbox, independent of any transport."""

    def __init__(self, agents: dict[str, dict] | None = None,
                 spool: SpoolBase | None = None):
        self.agents: dict[str, dict] = agents or {}
        # key = (agent_id, session_id) -> PtySession and stable process identity.
        self.ptys: dict[tuple[str, str], PtySession] = {}
        self.pty_instances: dict[tuple[str, str], str] = {}
        # Durable, sequence-numbered store of un-acked PTY output. Unit tests
        # may inject an InMemorySpool; the CLI injects a DiskSpool.
        self._spool: SpoolBase = spool if spool is not None else InMemorySpool()
        # Control frames are deliberately ephemeral: stale ready/presence/exit
        # frames must not be replayed after a supervisor restart.
        self._controls: deque[tuple[str, dict]] = deque()
        self._next_control_id = 0
        self.pending = _PendingView(self)
        self.pending_event = asyncio.Event()
        # A frame remains queued until the transport confirms WebSocket send.
        # The IPC delivery_id carried to the transport IS the durable seq, so an
        # ACK maps exactly back to the persisted record.
        # Cut 9: bounded pipelining. Multiple durable frames may be in flight
        # to the transport at once instead of one-frame-per-RTT stop-and-wait.
        # ``_inflight_ids`` maps every delivery_id currently handed to the
        # transport but not yet acknowledged (control:N ids and spool ``ord``
        # ints) to its payload byte size. A frame is never re-sent while its id
        # is in this map; on attach the map is cleared so a fresh transport
        # replays the whole backlog in order.
        self._inflight_ids: dict[int | str, int] = {}
        self._inflight_bytes = 0
        # The currently attached transport channel, or None when detached.
        self._channel: Channel | None = None
        # Un-acked frames recovered from a prior run are immediately eligible.
        if self._spool.pending_records():
            self.pending_event.set()

    # -- transport attach/detach ------------------------------------------

    def attach(self, channel: Channel) -> None:
        """Bind a transport channel. Existing PTYs are untouched.

        Any frames buffered while detached are re-signalled so the transport's
        drain loop resends them in order.
        """
        self._channel = channel
        # A fresh transport has no memory of what the previous one sent. Clear
        # the in-flight window so drain_to replays every un-acked frame in order.
        self._inflight_ids.clear()
        self._inflight_bytes = 0
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
        """Queue an outbound frame without blocking on WebSocket I/O.

        PTY output is committed to the durable spool first. Control frames are
        kept only in memory because replaying stale lifecycle state after a
        supervisor restart would be incorrect.
        """
        if frame.get("type") == "output":
            self._spool.enqueue_output(frame)
        else:
            self._next_control_id += 1
            delivery_id = f"control:{self._next_control_id}"
            self._controls.append((delivery_id, dict(frame)))
        self.pending_event.set()

    async def drain_to(self, channel: Channel) -> None:
        """Forward buffered frames to ``channel`` with bounded pipelining.

        Cut 9: instead of one-frame-per-RTT stop-and-wait, up to
        ``MAX_INFLIGHT_FRAMES`` / ``MAX_INFLIGHT_BYTES`` durable frames may be in
        flight to the transport before their ACKs return. Durable outputs carry
        their spool row ``ord`` as ``delivery_id``; ephemeral controls carry a
        process-local ``control:N`` ID. A frame is never re-sent while its id is
        already in ``_inflight_ids``; the spool stays the durability source of
        truth, so any un-acked frame replays in order on the next attach.
        """
        while True:
            sent_any = False
            # Controls take priority (lifecycle / input_ack) and are cheap.
            for delivery_id, frame in list(self._controls):
                if delivery_id in self._inflight_ids:
                    continue
                if len(self._inflight_ids) >= MAX_INFLIGHT_FRAMES:
                    break
                self._inflight_ids[delivery_id] = 0
                await channel.send({
                    "type": "ipc_delivery",
                    "delivery_id": delivery_id,
                    "frame": frame,
                })
                sent_any = True
            # Durable outputs, strictly in global ``ord`` order.
            for delivery_id, frame in self._spool.pending_records():
                if delivery_id in self._inflight_ids:
                    continue
                size = len(str(frame.get("data", "")))
                if self._inflight_ids and (
                    len(self._inflight_ids) >= MAX_INFLIGHT_FRAMES
                    or self._inflight_bytes + size > MAX_INFLIGHT_BYTES
                ):
                    # Window full: stop scanning; an ACK will free room and
                    # re-set pending_event so we resume from the same tail.
                    break
                self._inflight_ids[delivery_id] = size
                self._inflight_bytes += size
                await channel.send({
                    "type": "ipc_delivery",
                    "delivery_id": delivery_id,
                    "frame": frame,
                })
                sent_any = True
            if sent_any:
                # More rows may now fit (or new frames arrived); re-scan.
                continue
            self.pending_event.clear()
            if self._has_sendable():
                continue
            await self.pending_event.wait()

    def _has_sendable(self) -> bool:
        """True if any pending frame is not yet in the in-flight window."""
        if len(self._inflight_ids) >= MAX_INFLIGHT_FRAMES:
            return False
        for delivery_id, _ in self._controls:
            if delivery_id not in self._inflight_ids:
                return True
        for delivery_id, _ in self._spool.pending_records():
            if delivery_id not in self._inflight_ids:
                return True
        return False

    def _release_inflight(self, delivery_id) -> None:
        """Drop one delivery_id from the window and re-arm the sender."""
        size = self._inflight_ids.pop(delivery_id, None)
        if size:
            self._inflight_bytes -= size
        self.pending_event.set()

    def _reconcile_inflight_after_fence(self) -> None:
        """Drop in-flight durable ids whose spool rows a fence just purged."""
        valid = {ordv for ordv, _ in self._spool.pending_records()}
        for delivery_id in list(self._inflight_ids):
            if isinstance(delivery_id, str) and delivery_id.startswith("control:"):
                continue
            if delivery_id not in valid:
                size = self._inflight_ids.pop(delivery_id, 0)
                if size:
                    self._inflight_bytes -= size


    # -- control handling --------------------------------------------------

    async def handle_control(self, frame: dict) -> None:
        """Apply one control frame received from a transport."""
        t = frame.get("type")
        if t == "ipc_delivery_ack":
            self._apply_ack(frame.get("delivery_id"))
            return
        if t == "fence":
            # The server ruled this pty_instance's durable output stream forked.
            # Purge its spool tail so the single-inflight delivery loop stops
            # retrying poison rows, and release any inflight delivery that was
            # waiting on one of the purged rows so newer output can drain.
            sid_f = frame.get("session_id")
            pid_f = frame.get("pty_instance_id")
            if sid_f and pid_f:
                self._spool.fence(sid_f, pid_f)
                # Drop any in-flight durable ids the fence just purged so the
                # window frees up and newer output can drain.
                self._reconcile_inflight_after_fence()
                self.pending_event.set()
            return
        aid = frame.get("agent_id")
        sid = frame.get("session_id")
        if t == "open":
            await self.open_pty(aid, sid, frame.get("cols", 120), frame.get("rows", 30))
        elif t == "input":
            p = self.ptys.get((aid, sid))
            client_input_id = frame.get("client_input_id")
            try:
                client_input_id = str(UUID(str(client_input_id)))
            except (TypeError, ValueError, AttributeError):
                return
            if p:
                first_delivery = self._spool.record_input_once(client_input_id)
                if first_delivery:
                    p.write(frame.get("data", ""))
                self.emit({
                    "type": "input_ack",
                    "agent_id": aid,
                    "session_id": sid,
                    "client_input_id": client_input_id,
                    "status": "delivered",
                })
        elif t == "resize":
            p = self.ptys.get((aid, sid))
            if p:
                p.resize(frame.get("cols", 80), frame.get("rows", 24))
        elif t in ("close", "terminate"):
            key = (aid, sid)
            p = self.ptys.pop(key, None)
            if p:
                p.kill()
            self.pty_instances.pop(key, None)
        elif t == "list_sessions":
            self.emit(self.sessions_frame())

    def _apply_ack(self, delivery_id) -> None:
        """Advance the exact acknowledged control or durable output row.

        With bounded pipelining several ids are in flight at once, so the ACK
        need not match a single gate; it must match an id we actually sent
        (present in ``_inflight_ids``). Spool advancement stays strict and
        per-stream contiguous — ``spool.ack`` only removes the row when its seq
        is the smallest AND ``last_acked + 1`` — so a stale or out-of-order ACK
        can never delete the wrong row.
        """
        if delivery_id is None or delivery_id not in self._inflight_ids:
            return
        if isinstance(delivery_id, str) and delivery_id.startswith("control:"):
            # Controls ACK in order; only release when it is the head control.
            if not self._controls or self._controls[0][0] != delivery_id:
                return
            self._controls.popleft()
            self._release_inflight(delivery_id)
            return
        # Durable output: let the spool enforce contiguity. It returns False if
        # this ord is not the next deletable row, leaving the spool untouched.
        if self._spool.ack(delivery_id):
            self._release_inflight(delivery_id)

    def sessions_frame(self) -> dict:
        return {
            "type": "sessions",
            "sessions": [{
                "agent_id": aid,
                "session_id": sid,
                "pty_instance_id": self.pty_instances[(aid, sid)],
            } for aid, sid in self.ptys.keys()],
        }

    async def open_pty(self, agent_id: str, session_id: str,
                       cols: int = 120, rows: int = 30) -> None:
        key = (agent_id, session_id)
        existing = self.ptys.get(key)
        if existing and existing.is_alive():
            self.emit({"type": "ready", "agent_id": agent_id,
                       "session_id": session_id,
                       "pty_instance_id": self.pty_instances[key]})
            return
        if existing:
            # A child can be killed outside the supervisor while the platform PTY
            # reader is still blocked. Do not advertise that stale handle as ready.
            self.ptys.pop(key, None)
            self.pty_instances.pop(key, None)
        pty_instance_id = str(uuid4())
        info = self.agents.get(agent_id, {})
        cmd = resolve_cmd(info.get("runtime", "mock"), info.get("launch_cmd"))

        async def on_output(data: str):
            self.emit({"type": "output", "agent_id": agent_id,
                       "session_id": session_id,
                       "pty_instance_id": pty_instance_id,
                       "data": data})

        async def on_exit(code: int):
            # A stale reader may finish after open_pty has replaced its dead PTY.
            # Only the currently registered instance may close the server session.
            if self.ptys.get(key) is not p:
                return
            self.ptys.pop(key, None)
            self.emit({"type": "exit", "agent_id": agent_id,
                       "session_id": session_id,
                       "pty_instance_id": pty_instance_id,
                       "code": code})
            self.pty_instances.pop(key, None)

        p = PtySession(cmd, info.get("cwd"), on_output, on_exit, cols=cols, rows=rows)
        try:
            await p.start()
        except Exception as e:  # pragma: no cover - real PTY spawn failure
            self.emit({"type": "exit", "agent_id": agent_id,
                       "session_id": session_id, "code": -1,
                       "data": f"\r\n[failed to start: {e}]\r\n"})
            return
        self.ptys[key] = p
        self.pty_instances[key] = pty_instance_id
        self.emit({"type": "ready", "agent_id": agent_id,
                   "session_id": session_id,
                   "pty_instance_id": pty_instance_id})
        self.emit({"type": "presence", "agent_id": agent_id, "state": "online"})

    def status(self) -> dict:
        """Machine-readable supervisor status for CLI/doctor surfaces."""
        spool_status = self._spool.status()
        return {
            "attached": self.attached,
            "sessions": [{
                "agent_id": aid,
                "session_id": sid,
                "pty_instance_id": self.pty_instances[(aid, sid)],
            } for aid, sid in self.ptys.keys()],
            **spool_status,
        }

    def shutdown(self) -> None:
        """Kill all PTYs. Only used on real supervisor exit, never on detach."""
        for p in list(self.ptys.values()):
            p.kill()
        self.ptys.clear()
        self.pty_instances.clear()
        self._spool.close()
