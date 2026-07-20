"""The realtime Hub: tracks human + devbox WS connections and relays frames.

A human opens a terminal on a session (user<->agent). Input frames from the
human are routed to the devbox connection that hosts the agent; output frames
from the devbox are routed back to every human watching that session.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from fastapi import WebSocket


@dataclass(eq=False)
class DevboxConn:
    ws: WebSocket
    devbox_id: str
    agent_ids: set[str] = field(default_factory=set)
    active_session_ids: set[str] = field(default_factory=set)


@dataclass(eq=False)
class HumanConn:
    ws: WebSocket
    user_id: str
    # session_id -> agent_id this human currently watches
    sessions: dict[str, str] = field(default_factory=dict)


class Hub:
    def __init__(
        self, human_send_timeout: float = 1.0, human_queue_size: int = 128
    ) -> None:
        self.devboxes: dict[str, DevboxConn] = {}      # devbox_id -> conn
        self.agent_to_devbox: dict[str, str] = {}      # agent_id -> devbox_id
        self.humans: set[HumanConn] = set()
        # session_id -> set of HumanConn watching it
        self.session_watchers: dict[str, set[HumanConn]] = {}
        self._lock = asyncio.Lock()
        self._human_send_timeout = human_send_timeout
        self._human_queue_size = human_queue_size
        self._human_queues: dict[HumanConn, asyncio.Queue[dict]] = {}
        self._human_sender_tasks: dict[HumanConn, asyncio.Task] = {}

    # ---- devbox side ----
    async def add_devbox(self, conn: DevboxConn):
        async with self._lock:
            self.devboxes[conn.devbox_id] = conn
            for aid in conn.agent_ids:
                self.agent_to_devbox[aid] = conn.devbox_id

    async def remove_devbox(self, devbox_id: str):
        async with self._lock:
            conn = self.devboxes.pop(devbox_id, None)
            if conn:
                for aid in conn.agent_ids:
                    self.agent_to_devbox.pop(aid, None)

    def devbox_for_agent(self, agent_id: str) -> DevboxConn | None:
        did = self.agent_to_devbox.get(agent_id)
        return self.devboxes.get(did) if did else None

    def is_agent_online(self, agent_id: str) -> bool:
        return agent_id in self.agent_to_devbox

    def is_session_active(self, agent_id: str, session_id: str) -> bool:
        conn = self.devbox_for_agent(agent_id)
        return bool(conn and session_id in conn.active_session_ids)

    # ---- human side ----
    def add_human(self, conn: HumanConn):
        self.humans.add(conn)

    def remove_human(self, conn: HumanConn):
        self.humans.discard(conn)
        for sid in list(conn.sessions):
            self.session_watchers.get(sid, set()).discard(conn)
        self._human_queues.pop(conn, None)
        task = self._human_sender_tasks.pop(conn, None)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if task is not None and task is not current_task:
            task.cancel()

    def _ensure_human_sender(self, conn: HumanConn) -> asyncio.Queue[dict]:
        queue = self._human_queues.get(conn)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._human_queue_size)
            self._human_queues[conn] = queue
            self._human_sender_tasks[conn] = asyncio.create_task(
                self._send_to_human(conn, queue)
            )
        return queue

    async def _send_to_human(
        self, conn: HumanConn, queue: asyncio.Queue[dict]
    ) -> None:
        try:
            while True:
                frame = await queue.get()
                await asyncio.wait_for(
                    conn.ws.send_json(frame), timeout=self._human_send_timeout
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.remove_human(conn)
            try:
                await asyncio.wait_for(
                    conn.ws.close(code=1011), timeout=self._human_send_timeout
                )
            except Exception:
                pass

    def watch(self, conn: HumanConn, session_id: str, agent_id: str):
        conn.sessions[session_id] = agent_id
        self.session_watchers.setdefault(session_id, set()).add(conn)
        self._ensure_human_sender(conn)

    def unwatch(self, conn: HumanConn, session_id: str):
        conn.sessions.pop(session_id, None)
        self.session_watchers.get(session_id, set()).discard(conn)

    async def disconnect_devbox(self, devbox_id: str, code: int = 4001) -> bool:
        """Immediately close and unregister one connector after credential revocation."""
        async with self._lock:
            conn = self.devboxes.pop(devbox_id, None)
            if conn is None:
                return False
            for agent_id in conn.agent_ids:
                self.agent_to_devbox.pop(agent_id, None)
        try:
            await conn.ws.close(code=code)
        except Exception:
            pass
        return True

    async def disconnect_user(
        self, user_id: str, devbox_ids: set[str], code: int = 4001
    ) -> tuple[int, int]:
        """Immediately close a disabled user's human and connector sockets."""
        async with self._lock:
            humans = [c for c in self.humans if c.user_id == user_id]
            devboxes = [
                c for did, c in self.devboxes.items() if did in devbox_ids
            ]
            for conn in humans:
                self.remove_human(conn)
            for conn in devboxes:
                self.devboxes.pop(conn.devbox_id, None)
                for agent_id in conn.agent_ids:
                    self.agent_to_devbox.pop(agent_id, None)
        for conn in [*humans, *devboxes]:
            try:
                await conn.ws.close(code=code)
            except Exception:
                pass
        return len(humans), len(devboxes)

    async def disconnect_user_sessions(
        self, user_id: str, session_ids: set[str], code: int = 4001
    ) -> int:
        """Close only one user's browser sockets attached to selected sessions."""
        async with self._lock:
            humans = [c for c in self.humans
                      if c.user_id == user_id
                      and not set(c.sessions).isdisjoint(session_ids)]
            for conn in humans:
                self.remove_human(conn)
        for conn in humans:
            try:
                await conn.ws.close(code=code)
            except Exception:
                pass
        return len(humans)

    async def to_devbox(self, agent_id: str, frame: dict) -> bool:
        conn = self.devbox_for_agent(agent_id)
        if not conn:
            return False
        await conn.ws.send_json(frame)
        return True

    async def to_session_humans(self, session_id: str, frame: dict):
        """Enqueue ordered fan-out without waiting on browser network I/O."""
        for conn in list(self.session_watchers.get(session_id, set())):
            queue = self._ensure_human_sender(conn)
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self.remove_human(conn)
                asyncio.create_task(self._close_stale_human(conn))

    async def _close_stale_human(self, conn: HumanConn) -> None:
        try:
            await asyncio.wait_for(
                conn.ws.close(code=1011), timeout=self._human_send_timeout
            )
        except Exception:
            pass


hub = Hub()
