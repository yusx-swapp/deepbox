"""deepbox connector — connects a devbox to the server and bridges each agent's
interactive CLI (via PTY) to the platform.

Run:
    set DEEPBOX_SERVER_URL=http://localhost:8077
    set DEEPBOX_TOKEN=hpc_box_...
    python -m connector
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from collections import deque

import httpx
import websockets

from .diagnostics import explain_connection_error, run_doctor
from .pty_session import PtySession, resolve_cmd, DEFAULT_CMDS

PROTOCOL_VERSION = 2

# How often the connector proves liveness to the server while otherwise idle.
HEARTBEAT_INTERVAL = 20.0


async def heartbeat_loop(websocket, interval: float = HEARTBEAT_INTERVAL) -> None:
    """Send protocol heartbeats until the connection task is cancelled."""

    while True:
        await asyncio.sleep(interval)
        await websocket.send(json.dumps({"type": "heartbeat"}))


def ws_url(server_url: str) -> str:
    u = server_url.rstrip("/")
    if u.startswith("https"):
        return "wss" + u[5:] + "/ws/devbox"
    if u.startswith("http"):
        return "ws" + u[4:] + "/ws/devbox"
    return u + "/ws/devbox"


class Connector:
    def __init__(self, server_url: str, token: str):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.agents: dict[str, dict] = {}       # agent_id -> agent info
        # key = (agent_id, session_id) -> PtySession
        self.ptys: dict[tuple[str, str], PtySession] = {}
        # PTY readers must never depend on server availability. Frames accumulate
        # here while WS is down and are drained, in order, after reconnect.
        self.pending: deque[dict] = deque()
        self.pending_event = asyncio.Event()
        self.ws = None
        # Reconnect bookkeeping so operators can see stability over time.
        self.connect_count = 0
        self.last_heartbeat_ack = None

    async def fetch_me(self):
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{self.server_url}/api/me",
                            headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()
            data = r.json()
        server_protocol = data.get("protocol_version")
        if server_protocol != PROTOCOL_VERSION:
            raise RuntimeError(
                f"protocol mismatch: connector={PROTOCOL_VERSION}, server={server_protocol}")
        self.agents = {a["id"]: a for a in data["agents"]}
        print(f"[connector] devbox={data['name']} agents={[a['handle'] for a in data['agents']]}")
        return data

    def probe_runtimes(self) -> list[str]:
        caps = []
        for rt, cmd in DEFAULT_CMDS.items():
            if rt == "mock":
                caps.append(rt)
                continue
            if shutil.which(cmd[0]):
                caps.append(rt)
        return caps

    async def report_runtimes(self, devbox_id: str, caps: list[str]):
        async with httpx.AsyncClient() as c:
            await c.post(f"{self.server_url}/api/devboxes/{devbox_id}/runtimes",
                         headers={"Authorization": f"Bearer {self.token}"},
                         json={"capabilities": caps})

    async def run(self):
        print(f"[connector] authenticating with {self.server_url} (protocol {PROTOCOL_VERSION})")
        me = await self.fetch_me()
        caps = self.probe_runtimes()
        await self.report_runtimes(me["devbox_id"], caps)
        print(f"[connector] runtimes available: {caps}")
        print(f"[connector] opening WebSocket {ws_url(self.server_url)}")

        async with websockets.connect(
                ws_url(self.server_url),
                additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            self.ws = ws
            self.connect_count += 1
            hello = await ws.recv()
            print(f"[connector] connected (attempt #{self.connect_count}): {hello}")
            # Re-advertise PTYs that survived a server restart. This lets the
            # platform distinguish "resume the same process" from "start new".
            await self.send({
                "type": "sessions",
                "sessions": [{"agent_id": aid, "session_id": sid}
                             for aid, sid in self.ptys.keys()],
            })
            receiver = asyncio.create_task(self._receiver(ws))
            sender = asyncio.create_task(self._sender(ws))
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            done, pending = await asyncio.wait(
                {receiver, sender, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self.ws = None
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

    async def _receiver(self, ws):
        async for raw in ws:
            await self.handle(raw)

    async def _heartbeat(self, ws):
        # Periodic liveness ping. Keeps the WS path warm through idle NATs and
        # gives the server a fresh last_seen even when no session is active.
        await heartbeat_loop(ws)

    async def _sender(self, ws):
        while True:
            if not self.pending:
                self.pending_event.clear()
                await self.pending_event.wait()
            # Keep the frame at the head until send succeeds. On disconnect it
            # remains queued and the next WS connection retries it.
            frame = self.pending[0]
            await ws.send(json.dumps(frame))
            self.pending.popleft()

    async def send(self, frame: dict):
        """Queue a frame without coupling PTY readers to WS availability."""
        self.pending.append(frame)
        self.pending_event.set()

    async def handle(self, raw: str):
        import json
        frame = json.loads(raw)
        t = frame.get("type")
        aid = frame.get("agent_id")
        sid = frame.get("session_id")
        if t == "heartbeat_ack":
            self.last_heartbeat_ack = frame.get("ts")
            return
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

    async def open_pty(self, agent_id: str, session_id: str, cols: int = 120, rows: int = 30):
        key = (agent_id, session_id)
        if key in self.ptys:
            await self.send({"type": "ready", "agent_id": agent_id,
                             "session_id": session_id})
            return
        info = self.agents.get(agent_id, {})
        cmd = resolve_cmd(info.get("runtime", "mock"), info.get("launch_cmd"))

        async def on_output(data: str):
            await self.send({"type": "output", "agent_id": agent_id,
                             "session_id": session_id, "data": data})

        async def on_exit(code: int):
            self.ptys.pop(key, None)
            await self.send({"type": "exit", "agent_id": agent_id,
                             "session_id": session_id, "code": code})

        p = PtySession(cmd, info.get("cwd"), on_output, on_exit, cols=cols, rows=rows)
        try:
            await p.start()
        except Exception as e:
            await self.send({"type": "exit", "agent_id": agent_id,
                             "session_id": session_id, "code": -1,
                             "data": f"\r\n[failed to start: {e}]\r\n"})
            return
        self.ptys[key] = p
        await self.send({"type": "ready", "agent_id": agent_id,
                         "session_id": session_id})
        await self.send({"type": "presence", "agent_id": agent_id, "state": "online"})


async def main():
    ap = argparse.ArgumentParser("deepbox-connector")
    ap.add_argument("--server-url", default=os.environ.get("DEEPBOX_SERVER_URL",
                                                            "http://localhost:8077"))
    ap.add_argument("--token", default=os.environ.get("DEEPBOX_TOKEN"))
    ap.add_argument("--doctor", action="store_true",
                    help="check URL, TLS, health, protocol, and authentication, then exit")
    args = ap.parse_args()

    if args.doctor:
        checks = await asyncio.to_thread(run_doctor, args.server_url, args.token or "",
                                         PROTOCOL_VERSION)
        for check in checks:
            print(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        raise SystemExit(0 if all(check.ok for check in checks) else 1)

    if not args.token:
        raise SystemExit("Set DEEPBOX_TOKEN or pass --token")
    c = Connector(args.server_url, args.token)
    while True:
        try:
            await c.run()
        except Exception as exc:
            print(f"[connector] disconnected: {explain_connection_error(exc)}; retry in 3s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
