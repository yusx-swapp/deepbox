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
    def __init__(self) -> None:
        self.devboxes: dict[str, DevboxConn] = {}      # devbox_id -> conn
        self.agent_to_devbox: dict[str, str] = {}      # agent_id -> devbox_id
        self.humans: set[HumanConn] = set()
        # session_id -> set of HumanConn watching it
        self.session_watchers: dict[str, set[HumanConn]] = {}
        self._lock = asyncio.Lock()

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

    def watch(self, conn: HumanConn, session_id: str, agent_id: str):
        conn.sessions[session_id] = agent_id
        self.session_watchers.setdefault(session_id, set()).add(conn)

    def unwatch(self, conn: HumanConn, session_id: str):
        conn.sessions.pop(session_id, None)
        self.session_watchers.get(session_id, set()).discard(conn)

    async def to_devbox(self, agent_id: str, frame: dict) -> bool:
        conn = self.devbox_for_agent(agent_id)
        if not conn:
            return False
        await conn.ws.send_json(frame)
        return True

    async def to_session_humans(self, session_id: str, frame: dict):
        for conn in list(self.session_watchers.get(session_id, set())):
            try:
                await conn.ws.send_json(frame)
            except Exception:
                pass


hub = Hub()
