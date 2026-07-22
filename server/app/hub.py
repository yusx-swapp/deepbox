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
    outbound: asyncio.Queue[dict] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256), repr=False
    )
    sender_task: asyncio.Task | None = field(default=None, repr=False)
    retired: bool = False


@dataclass(eq=False)
class HumanConn:
    ws: WebSocket
    user_id: str
    # session_id -> agent_id this human currently watches
    sessions: dict[str, str] = field(default_factory=dict)


class Hub:
    def __init__(
        self,
        human_send_timeout: float = 1.0,
        human_queue_size: int = 128,
        devbox_send_timeout: float = 5.0,
        devbox_close_timeout: float = 1.0,
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
        self._devbox_send_timeout = devbox_send_timeout
        self._devbox_close_timeout = devbox_close_timeout

    # ---- devbox side ----
    @staticmethod
    def _consume_sender_result(task: asyncio.Task) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _sender_done(self, conn: DevboxConn, task: asyncio.Task) -> None:
        if conn.sender_task is task:
            conn.sender_task = None
        self._consume_sender_result(task)

    def _cancel_devbox_sender(self, conn: DevboxConn) -> None:
        task = conn.sender_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _close_devbox_socket(self, conn: DevboxConn, code: int) -> None:
        task = conn.sender_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await asyncio.wait_for(
                conn.ws.close(code=code), timeout=self._devbox_close_timeout
            )
        except Exception:
            pass

    async def _send_devbox_frames(self, conn: DevboxConn) -> None:
        try:
            while not conn.retired:
                frame = await conn.outbound.get()
                await asyncio.wait_for(
                    conn.ws.send_json(frame), timeout=self._devbox_send_timeout
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed writer retires only this connection. Closing its socket
            # wakes the receive loop, whose finally block removes the mapping.
            conn.retired = True
            await self._close_devbox_socket(conn, 1011)

    def _enqueue_devbox(self, conn: DevboxConn, frame: dict) -> bool:
        if conn.retired:
            return False
        try:
            conn.outbound.put_nowait(frame)
        except asyncio.QueueFull:
            conn.retired = True
            self._cancel_devbox_sender(conn)
            closer = asyncio.create_task(self._close_devbox_socket(conn, 1011))
            closer.add_done_callback(self._consume_sender_result)
            return False
        task = conn.sender_task
        if task is None or task.done():
            task = asyncio.create_task(self._send_devbox_frames(conn))
            conn.sender_task = task
            task.add_done_callback(lambda done, c=conn: self._sender_done(c, done))
        return True

    def send_devbox(self, conn: DevboxConn, frame: dict) -> bool:
        if self.devboxes.get(conn.devbox_id) is not conn:
            return False
        return self._enqueue_devbox(conn, frame)

    async def add_devbox(
        self, conn: DevboxConn, initial_frames: tuple[dict, ...] = ()
    ):
        if (conn.outbound.maxsize > 0
                and conn.outbound.qsize() + len(initial_frames)
                > conn.outbound.maxsize):
            raise ValueError("initial devbox frames exceed outbound queue capacity")
        old = None
        async with self._lock:
            old = self.devboxes.get(conn.devbox_id)
            if old is not None and old is not conn:
                old.retired = True
                self._cancel_devbox_sender(old)
                for aid in old.agent_ids:
                    if self.agent_to_devbox.get(aid) == conn.devbox_id:
                        self.agent_to_devbox.pop(aid, None)
            conn.retired = False
            self.devboxes[conn.devbox_id] = conn
            for aid in conn.agent_ids:
                self.agent_to_devbox[aid] = conn.devbox_id
            # Queue protocol bootstrap frames before exposing the connection to
            # a concurrent agent-directory push. The connector consumes hello
            # itself before starting TransportSession, so ordering is strict.
            for frame in initial_frames:
                if not self._enqueue_devbox(conn, frame):
                    raise RuntimeError("failed to queue initial devbox frame")
        if old is not None and old is not conn:
            await self._close_devbox_socket(old, 4002)

    async def remove_devbox(
        self, devbox_id: str, expected: DevboxConn | None = None
    ) -> bool:
        async with self._lock:
            conn = self.devboxes.get(devbox_id)
            if conn is None or (expected is not None and conn is not expected):
                return False
            self.devboxes.pop(devbox_id, None)
            conn.retired = True
            self._cancel_devbox_sender(conn)
            for aid in conn.agent_ids:
                if self.agent_to_devbox.get(aid) == devbox_id:
                    self.agent_to_devbox.pop(aid, None)
            return True

    def is_devbox_online(self, devbox_id: str) -> bool:
        conn = self.devboxes.get(devbox_id)
        return conn is not None and not conn.retired

    def devbox_for_agent(self, agent_id: str) -> DevboxConn | None:
        did = self.agent_to_devbox.get(agent_id)
        conn = self.devboxes.get(did) if did else None
        return conn if conn is not None and not conn.retired else None

    def is_agent_online(self, agent_id: str) -> bool:
        return self.devbox_for_agent(agent_id) is not None

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
            conn.retired = True
            self._cancel_devbox_sender(conn)
            for agent_id in conn.agent_ids:
                if self.agent_to_devbox.get(agent_id) == devbox_id:
                    self.agent_to_devbox.pop(agent_id, None)
        await self._close_devbox_socket(conn, code)
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
                conn.retired = True
                self._cancel_devbox_sender(conn)
                for agent_id in conn.agent_ids:
                    if self.agent_to_devbox.get(agent_id) == conn.devbox_id:
                        self.agent_to_devbox.pop(agent_id, None)
        for conn in humans:
            try:
                await conn.ws.close(code=code)
            except Exception:
                pass
        for conn in devboxes:
            await self._close_devbox_socket(conn, code)
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

    async def retire_agent_sessions(
        self, agent_id: str, session_ids: set[str]
    ) -> None:
        """Notify browser watchers and forget sessions removed with an agent."""
        if not session_ids:
            return
        for session_id in session_ids:
            await self.to_session_humans(session_id, {
                "type": "exit", "agent_id": agent_id,
                "session_id": session_id, "code": 0,
                "reason": "agent_deleted",
            })
        async with self._lock:
            for conn in self.devboxes.values():
                conn.active_session_ids.difference_update(session_ids)
            for session_id in session_ids:
                for human in self.session_watchers.pop(session_id, set()):
                    human.sessions.pop(session_id, None)

    async def to_devbox(self, agent_id: str, frame: dict) -> bool:
        conn = self.devbox_for_agent(agent_id)
        if not conn:
            return False
        return self._enqueue_devbox(conn, frame)

    async def sync_agents(
        self, devbox_id: str, agent_ids: set[str], directory: list[dict]
    ) -> bool:
        """Live-update an online devbox's routes and queue its new directory.

        The database remains authoritative. The connector receives the same
        directory after every WebSocket connect, so an offline mutation is
        reconciled on reconnect. The per-connection writer is the only task
        that touches ``send_json``; HTTP mutations never wait on network I/O.
        """
        async with self._lock:
            conn = self.devboxes.get(devbox_id)
            if conn is None or conn.retired:
                return False
            # Drop stale routes for this devbox, then install the new set.
            for aid in list(conn.agent_ids):
                if self.agent_to_devbox.get(aid) == devbox_id:
                    self.agent_to_devbox.pop(aid, None)
            conn.agent_ids = set(agent_ids)
            for aid in conn.agent_ids:
                self.agent_to_devbox[aid] = devbox_id
            return self._enqueue_devbox(
                conn, {"type": "agents", "agents": directory}
            )

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
