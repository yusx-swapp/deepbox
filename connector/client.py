"""deepbox connector — connects a devbox to the server and bridges each agent's
interactive CLI (via PTY) to the platform.

Run:
    set DEEPBOX_SERVER_URL=http://localhost:8000
    set DEEPBOX_TOKEN=hpc_box_...
    python -m connector
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil

import httpx
import websockets

from .pty_session import PtySession, resolve_cmd, DEFAULT_CMDS


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
        self.ws = None

    async def fetch_me(self):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.server_url}/api/me",
                            headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()
            data = r.json()
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
        me = await self.fetch_me()
        caps = self.probe_runtimes()
        await self.report_runtimes(me["devbox_id"], caps)
        print(f"[connector] runtimes available: {caps}")

        async with websockets.connect(
                ws_url(self.server_url),
                additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            self.ws = ws
            hello = await ws.recv()
            print(f"[connector] connected: {hello}")
            async for raw in ws:
                await self.handle(raw)

    async def send(self, frame: dict):
        import json
        if self.ws:
            await self.ws.send(json.dumps(frame))

    async def handle(self, raw: str):
        import json
        frame = json.loads(raw)
        t = frame.get("type")
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
                                                            "http://localhost:8000"))
    ap.add_argument("--token", default=os.environ.get("DEEPBOX_TOKEN"))
    args = ap.parse_args()
    if not args.token:
        raise SystemExit("Set DEEPBOX_TOKEN or pass --token")
    c = Connector(args.server_url, args.token)
    while True:
        try:
            await c.run()
        except Exception as e:
            print(f"[connector] disconnected: {e}; retry in 3s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
