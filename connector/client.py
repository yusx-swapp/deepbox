"""deepbox connector - connects a devbox to the server and bridges each agent's
interactive CLI (via PTY) to the platform.

Cut 4 splits the connector into two halves:

  - :class:`~connector.supervisor.SessionSupervisor` (``sessiond``) owns PTY
    lifecycle. It never touches the network. Losing the transport does not kill
    PTYs.
  - :class:`~connector.transport.TransportSession` owns the WebSocket. It relays
    frames between the server and the supervisor over an IPC channel.

They talk over a :class:`~connector.ipc.Channel`. By default both halves run in
one process joined by an in-memory :class:`~connector.ipc.LoopbackChannel`
(``python -m connector`` / ``--mode all-in-one``), which keeps the historical
single-process API intact. The same seam also supports a real two-process split
over a Unix socket / Windows named pipe:

  - ``--mode supervisor`` (a.k.a. ``sessiond``) runs a long-lived process that
    owns the PTYs and serves the local IPC endpoint, accepting transport
    reconnects. Restarting the transport never kills a PTY.
  - ``--mode transport`` runs a process that owns the HTTP/WS side and connects
    to the local supervisor. It can restart independently.

The default remains all-in-one; the split is opt-in.

Run:
    set DEEPBOX_SERVER_URL=http://localhost:8077
    set DEEPBOX_TOKEN=hpc_box_...
    python -m connector                     # all-in-one (default)
    python -m connector --mode supervisor   # long-lived PTY owner (sessiond)
    python -m connector --mode transport    # WS owner, reconnects to sessiond
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil

import httpx
import websockets

from .diagnostics import explain_connection_error, run_doctor
from .ipc import (
    AuthError,
    LoopbackChannel,
    cleanup_stale_endpoint,
    connect_channel,
    default_endpoint,
    endpoint_exists,
    read_secret,
    serve_channel,
)
from .pty_session import DEFAULT_CMDS
from .supervisor import SessionSupervisor
from .transport import (
    HEARTBEAT_INTERVAL,
    PROTOCOL_VERSION,
    TransportSession,
    heartbeat_loop,
    ws_url,
)

__all__ = [
    "Connector",
    "heartbeat_loop",
    "ws_url",
    "PROTOCOL_VERSION",
    "HEARTBEAT_INTERVAL",
    "SupervisorService",
    "run_supervisor",
    "run_transport",
    "main",
]


class Connector:
    """Single-process composition of supervisor + transport.

    Retained for backwards compatibility and as the default deployment shape.
    Internally it owns a :class:`SessionSupervisor` and, per WS connection,
    attaches a :class:`TransportSession` over a fresh loopback channel.
    """

    def __init__(self, server_url: str, token: str):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.supervisor = SessionSupervisor()
        self.ws = None
        self.connect_count = 0
        self.last_heartbeat_ack = None

    # -- compatibility shims (used by tests and older call sites) ---------

    @property
    def agents(self) -> dict[str, dict]:
        return self.supervisor.agents

    @agents.setter
    def agents(self, value: dict[str, dict]) -> None:
        self.supervisor.agents = value

    @property
    def ptys(self):
        return self.supervisor.ptys

    @property
    def pending(self):
        return self.supervisor.pending

    @property
    def pending_event(self):
        return self.supervisor.pending_event

    async def send(self, frame: dict):
        """Queue a frame without coupling PTY readers to WS availability."""
        self.supervisor.emit(frame)

    async def _sender(self, ws):
        """Drain buffered frames straight to a websocket (legacy test seam)."""
        while True:
            if not self.pending:
                self.pending_event.clear()
                await self.pending_event.wait()
                continue
            frame = self.pending[0]
            await ws.send(json.dumps(frame))
            self.pending.popleft()

    async def handle(self, raw: str):
        await self.supervisor.handle_control(json.loads(raw))

    async def open_pty(self, agent_id, session_id, cols=120, rows=30):
        await self.supervisor.open_pty(agent_id, session_id, cols, rows)

    # -- HTTP bootstrap ----------------------------------------------------

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
        self.supervisor.agents = {a["id"]: a for a in data["agents"]}
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

    # -- main run loop -----------------------------------------------------

    async def run(self):
        print(f"[connector] authenticating with {self.server_url} (protocol {PROTOCOL_VERSION})")
        me = await self.fetch_me()
        caps = self.probe_runtimes()
        await self.report_runtimes(me["devbox_id"], caps)
        print(f"[connector] runtimes available: {caps}")
        print(f"[connector] opening WebSocket {ws_url(self.server_url)}")

        # New loopback channel per WS connection. Attaching/detaching the
        # transport never disturbs the supervisor's PTYs (Cut 4 invariant).
        sup_end, tx_end = LoopbackChannel.pair()
        self.supervisor.attach(sup_end)
        drain = asyncio.create_task(self.supervisor.drain_to(sup_end))
        control = asyncio.create_task(self._supervisor_control(sup_end))
        try:
            async with websockets.connect(
                    ws_url(self.server_url),
                    additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
                self.ws = ws
                self.connect_count += 1
                hello = await ws.recv()
                print(f"[connector] connected (attempt #{self.connect_count}): {hello}")
                transport = TransportSession(tx_end)
                await transport.run(ws)
                self.last_heartbeat_ack = transport.last_heartbeat_ack
        finally:
            self.ws = None
            self.supervisor.detach()
            for task in (drain, control):
                task.cancel()
            await asyncio.gather(drain, control, return_exceptions=True)

    async def _supervisor_control(self, channel):
        """Apply control frames the transport relays from the server."""
        while True:
            frame = await channel.recv()
            if frame is None:
                return
            await self.supervisor.handle_control(frame)

    def status(self) -> dict:
        return {
            "server_url": self.server_url,
            "connect_count": self.connect_count,
            "last_heartbeat_ack": self.last_heartbeat_ack,
            **self.supervisor.status(),
        }


class SupervisorService:
    """Long-lived ``sessiond``: owns PTYs and serves the local IPC endpoint.

    Accepts one transport connection at a time. When a transport disconnects the
    PTYs keep running and buffering; the next transport to connect drains the
    buffer in order. Only :meth:`shutdown` (process exit) kills PTYs.
    """

    def __init__(self, agents: dict[str, dict] | None = None,
                 endpoint: str | None = None):
        self.supervisor = SessionSupervisor(agents)
        self.endpoint = endpoint or default_endpoint()
        self._server = None
        self._busy = asyncio.Lock()  # enforces one transport at a time
        self._stop = asyncio.Event()

    async def _on_channel(self, channel) -> None:
        # One transport at a time: reject a second concurrent connection rather
        # than corrupt the single supervisor buffer/ack state machine.
        if self._busy.locked():
            try:
                await channel.send({"type": "ipc_busy",
                                    "detail": "another transport is attached"})
                await channel.close()
            except (ConnectionError, OSError):
                pass
            return
        async with self._busy:
            self.supervisor.attach(channel)
            await channel.send({"type": "ipc_attached"})
            drain = asyncio.create_task(self.supervisor.drain_to(channel))
            try:
                while True:
                    frame = await channel.recv()
                    if frame is None:
                        break
                    await self.supervisor.handle_control(frame)
            finally:
                self.supervisor.detach()
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
                try:
                    await channel.close()
                except (ConnectionError, OSError):
                    pass

    async def serve(self) -> None:
        self._server = await serve_channel(self._on_channel, endpoint=self.endpoint)
        print(f"[sessiond] serving IPC at {self.endpoint}")
        try:
            await self._stop.wait()
        finally:
            if self._server is not None:
                await self._server.close()
            self.supervisor.shutdown()

    def stop(self) -> None:
        self._stop.set()


async def run_supervisor(server_url: str, token: str,
                         endpoint: str | None = None) -> None:
    """Run a standalone sessiond that also bootstraps agents from the server."""
    # Fail closed if the catalogue cannot be loaded. An empty catalogue would
    # silently resolve real agent IDs to the mock runtime.
    bootstrap = Connector(server_url, token)
    me = await bootstrap.fetch_me()
    caps = bootstrap.probe_runtimes()
    await bootstrap.report_runtimes(me["devbox_id"], caps)
    print(f"[sessiond] runtimes available: {caps}")

    address = endpoint or default_endpoint()
    if endpoint_exists(address):
        # A previous supervisor may have died leaving a stale POSIX socket.
        if cleanup_stale_endpoint(endpoint=address):
            print(f"[sessiond] removed stale endpoint state for {address}")
    service = SupervisorService(dict(bootstrap.agents), endpoint=address)
    await service.serve()


async def run_transport(server_url: str, token: str,
                        endpoint: str | None = None) -> None:
    """Run a standalone transport that connects to a local sessiond.

    Owns the HTTP/WS side and reconnects to both the server and the supervisor
    without ever killing PTYs. Restarting this process leaves sessiond's PTYs
    untouched.
    """
    server_url = server_url.rstrip("/")
    address = endpoint or default_endpoint()
    connect_count = 0
    while True:
        try:
            channel = await connect_channel(endpoint=address)
        except (AuthError, ConnectionError, OSError, ValueError) as exc:
            print(f"[transport] cannot reach sessiond at {address}: {exc}; retry in 3s")
            await asyncio.sleep(3)
            continue
        try:
            attached = await channel.recv()
            if attached is None or attached.get("type") != "ipc_attached":
                detail = (attached or {}).get("detail", "sessiond closed during attach")
                raise ConnectionError(detail)
            print(f"[transport] attached to sessiond at {address}")
            async with websockets.connect(
                    ws_url(server_url),
                    additional_headers={"Authorization": f"Bearer {token}"}) as ws:
                connect_count += 1
                hello = await ws.recv()
                print(f"[transport] connected (attempt #{connect_count}): {hello}")
                session = TransportSession(channel)
                await session.run(ws)
        except Exception as exc:
            print(f"[transport] disconnected: {explain_connection_error(exc)}; retry in 3s")
            await asyncio.sleep(3)
        finally:
            try:
                await channel.close()
            except (ConnectionError, OSError):
                pass


def _status_payload(server_url: str, mode: str) -> dict:
    endpoint = default_endpoint()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "server_url": server_url.rstrip("/"),
        "mode": mode,
        "ipc_endpoint": endpoint,
        "ipc_endpoint_present": endpoint_exists(endpoint),
        "supervisor_secret_present": read_secret() is not None,
    }


async def main():
    ap = argparse.ArgumentParser("deepbox-connector")
    ap.add_argument("--server-url", default=os.environ.get("DEEPBOX_SERVER_URL",
                                                            "http://localhost:8077"))
    ap.add_argument("--token", default=os.environ.get("DEEPBOX_TOKEN"))
    ap.add_argument("--mode", choices=["all-in-one", "supervisor", "transport"],
                    default=os.environ.get("DEEPBOX_MODE", "all-in-one"),
                    help="all-in-one (default): supervisor+transport in one process; "
                         "supervisor: long-lived sessiond owning PTYs; "
                         "transport: WS owner that reconnects to a local sessiond")
    ap.add_argument("--endpoint", default=os.environ.get("DEEPBOX_IPC_ENDPOINT"),
                    help="override the local IPC endpoint (advanced)")
    ap.add_argument("--doctor", action="store_true",
                    help="check URL, TLS, health, protocol, and authentication, then exit")
    ap.add_argument("--status", action="store_true",
                    help="print connector/IPC configuration as JSON, then exit")
    args = ap.parse_args()

    mode_label = {
        "all-in-one": "all-in-one (supervisor+transport via loopback)",
        "supervisor": "supervisor (sessiond; owns PTYs, serves IPC)",
        "transport": "transport (owns WS; connects to sessiond)",
    }[args.mode]

    if args.status:
        print(json.dumps(_status_payload(args.server_url, mode_label), indent=2))
        raise SystemExit(0)

    if args.doctor:
        checks = await asyncio.to_thread(run_doctor, args.server_url, args.token or "",
                                         PROTOCOL_VERSION)
        for check in checks:
            print(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        endpoint = args.endpoint or default_endpoint()
        print(f"[INFO] mode: {mode_label}")
        print(f"[INFO] ipc endpoint: {endpoint}")
        print(f"[INFO] ipc endpoint present: {endpoint_exists(endpoint)}")
        print(f"[INFO] supervisor secret present: {read_secret() is not None}")
        raise SystemExit(0 if all(check.ok for check in checks) else 1)

    if not args.token:
        raise SystemExit("Set DEEPBOX_TOKEN or pass --token")

    if args.mode == "supervisor":
        await run_supervisor(args.server_url, args.token, args.endpoint)
        return
    if args.mode == "transport":
        await run_transport(args.server_url, args.token, args.endpoint)
        return

    c = Connector(args.server_url, args.token)
    while True:
        try:
            await c.run()
        except Exception as exc:
            print(f"[connector] disconnected: {explain_connection_error(exc)}; retry in 3s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
