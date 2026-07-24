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

Run after one-time installation:
    set DEEPBOX_SERVER_URL=http://localhost:8077
    set DEEPBOX_TOKEN=hpc_box_...
    deepbox connect                         # all-in-one (default)
    deepbox connect --mode supervisor       # long-lived PTY owner (sessiond)
    deepbox connect --mode transport        # WS owner, reconnects to sessiond

From a source checkout, ``python -m connector`` remains the equivalent developer
entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

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
from .local_store import LocalProject, LocalProjectStore, open_local_store
from .runtime_probe import RuntimeProbeCache
from .runtimes import all_adapters
from .skills import (
    SkillBinding,
    SkillError,
    SkillManager,
)
from .supervisor import SessionSupervisor
from .spool import open_spool
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
    "websocket_connect_options",
    "main",
]


def websocket_connect_options() -> dict:
    """Keep both connector transports on the same conservative WS policy."""
    return {
        "open_timeout": 30,
        "ping_interval": 20,
        "ping_timeout": 60,
        "close_timeout": 5,
        "max_size": 16 * 1024 * 1024,
    }


class Connector:
    """Single-process composition of supervisor + transport.

    Retained for backwards compatibility and as the default deployment shape.
    Internally it owns a :class:`SessionSupervisor` and, per WS connection,
    attaches a :class:`TransportSession` over a fresh loopback channel.
    """

    def __init__(self, server_url: str, token: str, spool=None,
                 local_store: LocalProjectStore | None = None):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.local_store = local_store
        self.supervisor = SessionSupervisor(
            spool=spool, local_store=local_store)
        self.ws = None
        self.connect_count = 0
        self.last_heartbeat_ack = None
        self.runtime_probe_cache = RuntimeProbeCache()

    # -- compatibility shims (used by tests and older call sites) ---------

    @property
    def agents(self) -> dict[str, dict]:
        return self.supervisor.agents

    @agents.setter
    def agents(self, value: dict[str, dict]) -> None:
        self.supervisor.replace_agents(value)

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
        """Drain buffered frames straight to a websocket (legacy test seam).

        Control frames retain their send-boundary ACK. Protocol-v3 output cannot
        be released by this sender-only compatibility seam because it has no
        websocket receive path; runtime connections use ``TransportSession``.
        """
        while True:
            records = self.supervisor._spool.pending_records()
            if self.supervisor._controls:
                delivery_id, frame = self.supervisor._controls[0]
            elif records:
                delivery_id, frame = records[0]
            else:
                self.pending_event.clear()
                if (self.supervisor._controls or
                        self.supervisor._spool.pending_records()):
                    continue
                await self.pending_event.wait()
                continue
            await ws.send(json.dumps(frame))
            if frame.get("type") == "output":
                # Only an exact server durability ACK may release this row.
                await asyncio.Future()
            if (self.supervisor._controls and
                    self.supervisor._controls[0][0] == delivery_id):
                self.supervisor._controls.popleft()

    async def handle(self, raw: str):
        await self.supervisor.handle_control(json.loads(raw))

    async def open_pty(self, agent_id, session_id, cols=120, rows=30,
                       surface=None):
        await self.supervisor.open_pty(
            agent_id, session_id, cols, rows, surface=surface)

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
        self.supervisor.replace_agents(data["agents"])
        print(f"[connector] devbox={data['name']} agents={[a['handle'] for a in data['agents']]}")
        return data

    def probe_runtimes(self, devbox_id: str = "local", *,
                       force: bool = False) -> list[dict]:
        """Return capability-v2 reports for every registered runtime family.

        Missing CLIs are deliberately included so the browser can explain how
        to install them. The connector reports normalized states only: never
        executable paths, raw probe output, environment values, or credentials.
        """
        return self.runtime_probe_cache.probe_all(devbox_id, force=force)

    async def report_runtimes(self, devbox_id: str, caps: list[dict]):
        async with httpx.AsyncClient() as c:
            response = await c.post(
                f"{self.server_url}/api/devboxes/{devbox_id}/runtimes",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"capabilities": caps})
            response.raise_for_status()

    async def report_skills(self, devbox_id: str):
        """Publish sanitized skill metadata; local paths never leave the host."""
        if self.local_store is None:
            return
        skills = SkillManager(self.local_store).inventory(
            all_scopes=True, short_digest=False)
        async with httpx.AsyncClient() as c:
            response = await c.post(
                f"{self.server_url}/api/devboxes/{devbox_id}/skills",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"skills": skills})
            response.raise_for_status()

    async def report_projects(self, devbox_id: str):
        """Publish path-free project metadata and one-cycle cwd migrations."""
        if self.local_store is None:
            return
        migrations = self.supervisor.pending_project_migrations()
        async with httpx.AsyncClient() as c:
            response = await c.post(
                f"{self.server_url}/api/devboxes/{devbox_id}/projects",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "projects": self.local_store.public_projects(),
                    "migrations": migrations,
                })
            response.raise_for_status()
        self.supervisor.clear_project_migrations(migrations)

    # -- main run loop -----------------------------------------------------

    async def run(self):
        print(f"[connector] authenticating with {self.server_url} (protocol {PROTOCOL_VERSION})")
        me = await self.fetch_me()
        await self.report_projects(me["devbox_id"])
        await self.report_skills(me["devbox_id"])
        caps = self.probe_runtimes(me["devbox_id"])
        await self.report_runtimes(me["devbox_id"], caps)
        summary = [
            f"{cap['runtime']}:{cap['installation']['status']}"
            for cap in caps
        ]
        print(f"[connector] runtime capabilities: {summary}")
        print(f"[connector] opening WebSocket {ws_url(self.server_url)}")

        # New loopback channel per WS connection. Attaching/detaching the
        # transport never disturbs the supervisor's PTYs (Cut 4 invariant).
        sup_end, tx_end = LoopbackChannel.pair()
        self.supervisor.attach(sup_end)
        drain = asyncio.create_task(self.supervisor.drain_to(sup_end))
        control = asyncio.create_task(self._supervisor_control(sup_end))
        inventory = (
            asyncio.create_task(_watch_project_inventory(self, me["devbox_id"]))
            if self.local_store is not None
            else None
        )
        try:
            async with websockets.connect(
                    ws_url(self.server_url),
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    **websocket_connect_options()) as ws:
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
            tasks = [drain, control]
            if inventory is not None:
                tasks.append(inventory)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
                 endpoint: str | None = None, spool=None,
                 local_store: LocalProjectStore | None = None):
        self.supervisor = SessionSupervisor(
            agents, spool=spool, local_store=local_store)
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


def _project_inventory_signature(local_store: LocalProjectStore) -> tuple:
    """Return a path-free change token for connector-local projects."""
    return tuple(
        (project.id, project.name, project.updated_at)
        for project in local_store.list_projects()
    )


def _skill_inventory_signature(local_store: LocalProjectStore) -> tuple:
    """Return a path-free token including each managed skill's health."""
    inventory = SkillManager(local_store).inventory(
        all_scopes=True, short_digest=False)
    return tuple(
        (
            item.get("id"), item.get("digest"), item.get("updated_at"),
            item.get("status"), tuple(item.get("targets") or ()),
        )
        for item in inventory
    )


async def _watch_project_inventory(connector: Connector, devbox_id: str,
                                   *, interval: float = 2.0) -> None:
    """Re-report project metadata when another local CLI process changes it."""
    reported = _project_inventory_signature(connector.local_store)
    reported_skills = _skill_inventory_signature(connector.local_store)
    while True:
        await asyncio.sleep(interval)
        current = _project_inventory_signature(connector.local_store)
        current_skills = _skill_inventory_signature(connector.local_store)
        if current != reported:
            try:
                await connector.report_projects(devbox_id)
            except Exception as exc:
                print(f"[sessiond] project metadata refresh failed: {exc}")
            else:
                reported = current
                print("[sessiond] local project metadata refreshed")
        if current_skills != reported_skills:
            try:
                await connector.report_skills(devbox_id)
            except Exception as exc:
                print(f"[sessiond] skill metadata refresh failed: {exc}")
            else:
                reported_skills = current_skills
                print("[sessiond] local skill metadata refreshed")


async def run_supervisor(server_url: str, token: str,
                         endpoint: str | None = None,
                         state_path: str | None = None) -> None:
    """Run a standalone sessiond that also bootstraps agents from the server."""
    # Fail closed if the catalogue cannot be loaded. An empty catalogue would
    # silently resolve real agent IDs to the mock runtime.
    local_store = open_local_store(state_path)
    try:
        bootstrap = Connector(server_url, token, local_store=local_store)
        me = await bootstrap.fetch_me()
        await bootstrap.report_projects(me["devbox_id"])
        await bootstrap.report_skills(me["devbox_id"])
        caps = bootstrap.probe_runtimes()
        await bootstrap.report_runtimes(me["devbox_id"], caps)
        print("[sessiond] runtimes available: " + ", ".join(
            str(cap.get("runtime", "unknown")) for cap in caps))

        address = endpoint or default_endpoint()
        if endpoint_exists(address):
            # A previous supervisor may have died leaving a stale POSIX socket.
            if cleanup_stale_endpoint(endpoint=address):
                print(f"[sessiond] removed stale endpoint state for {address}")
        service = SupervisorService(
            dict(bootstrap.agents), endpoint=address,
            spool=open_spool(server_url, token), local_store=local_store)
        project_watcher = asyncio.create_task(
            _watch_project_inventory(bootstrap, me["devbox_id"]))
        try:
            await service.serve()
        finally:
            project_watcher.cancel()
            await asyncio.gather(project_watcher, return_exceptions=True)
    finally:
        local_store.close()


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
                    additional_headers={"Authorization": f"Bearer {token}"},
                    **websocket_connect_options()) as ws:
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


def _resolve_skill_project(
    store: LocalProjectStore, selector: str | None
) -> LocalProject | None:
    if selector is None:
        return None
    project = store.get(selector)
    if project is not None:
        return project

    projects = store.list_projects()
    if selector == ".":
        current = os.path.abspath(os.getcwd())
        matches = []
        for item in projects:
            try:
                if os.path.commonpath((current, item.path)) == item.path:
                    matches.append(item)
            except ValueError:  # Different Windows drives have no common path.
                continue
        if matches:
            return max(matches, key=lambda item: len(item.path))
    else:
        normalized = os.path.realpath(os.path.abspath(os.path.expanduser(selector)))
        project = next((item for item in projects if item.path == normalized), None)
        if project is not None:
            return project

    named = [item for item in projects if item.name.casefold() == selector.casefold()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise SkillError(f"project name is ambiguous: {selector}")
    raise SkillError(
        f"local project is not registered: {selector}; run 'deepbox project add' first"
    )


def _skill_bindings(project: LocalProject | None) -> dict[str, SkillBinding]:
    roots: dict[str, list[str]] = {}
    project_path = project.path if project is not None else None
    for adapter in all_adapters():
        family = adapter.family_id
        for root in adapter.skill_roots(project_path):
            values = roots.setdefault(family, [])
            if root not in values:
                values.append(root)
    return {
        family: SkillBinding(family, tuple(values))
        for family, values in roots.items() if values
    }


def _skill_json(manager: SkillManager, skill) -> dict:
    payload = skill.public_json()
    payload["status"] = manager.status(skill)
    return payload


async def _skill_command(args) -> None:
    if not args.skill_action:
        raise SystemExit("choose a skill action: install, list, inspect, or remove")
    store = open_local_store(args.state_path)
    try:
        manager = SkillManager(store)
        project = _resolve_skill_project(store, args.project)
        scope = "project" if project is not None else "personal"
        project_id = project.id if project is not None else None
        if args.skill_action == "install":
            result = manager.install(
                args.source,
                project=project,
                bindings=_skill_bindings(project),
                force=args.force,
            )
            print(json.dumps(_skill_json(manager, result.skill), indent=2))
        elif args.skill_action == "list":
            skills = manager.list(project)
            print(json.dumps([_skill_json(manager, item) for item in skills], indent=2))
        elif args.skill_action == "inspect":
            skill = store.get_skill(args.name, scope, project_id)
            if skill is None:
                raise SkillError(f"unknown {scope} skill: {args.name}")
            print(json.dumps(manager.inspect(skill), indent=2))
        elif args.skill_action == "remove":
            if not manager.remove(
                args.name,
                project=project,
                force=args.force,
            ):
                raise SkillError(f"unknown {scope} skill: {args.name}")
            print(f"[connector] skill removed: {args.name} ({scope})")
        else:
            raise SkillError("choose a skill action: install, list, inspect, or remove")
    except SkillError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


async def _project_command(args) -> None:
    store = open_local_store(args.state_path)
    try:
        project = None
        if args.project_action == "add":
            project = store.add(args.path, args.name)
            print(f"[connector] project added: {project.name} ({project.id}) -> {project.path}")
        elif args.project_action == "remove":
            if not store.remove(args.project_id):
                raise SystemExit(f"unknown project id: {args.project_id}")
            print(f"[connector] project removed: {args.project_id}")
        elif args.project_action == "list":
            projects = store.list_projects()
            if not projects:
                print("No local projects registered.")
            for item in projects:
                print(f"{item.id}  {item.name}  {item.path}")
        elif args.project_action != "sync":
            raise SystemExit("choose a project action: add, remove, list, or sync")

        should_sync = args.project_action == "sync" or (
            args.project_action in {"add", "remove"} and bool(args.token))
        if should_sync:
            if not args.token:
                raise SystemExit("DEEPBOX_TOKEN or --token is required to sync projects")
            connector = Connector(
                args.server_url, args.token, local_store=store)
            me = await connector.fetch_me()
            await connector.report_projects(me["devbox_id"])
            print("[connector] project metadata synced (local paths stayed on this machine)")
        elif args.project_action in {"add", "remove"}:
            print("[connector] local change will sync the next time the connector starts")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


async def main(argv: list[str] | None = None):
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
    ap.add_argument("--state-path", default=None, help=argparse.SUPPRESS)
    command_parsers = ap.add_subparsers(dest="command")
    project_parser = command_parsers.add_parser(
        "project", help="manage connector-local project paths")
    project_actions = project_parser.add_subparsers(dest="project_action")
    project_add = project_actions.add_parser("add", help="register a local directory")
    project_add.add_argument("path")
    project_add.add_argument("--name", default=None)
    project_remove = project_actions.add_parser("remove", help="remove a project id")
    project_remove.add_argument("project_id")
    project_actions.add_parser("list", help="list local projects")
    project_actions.add_parser("sync", help="sync path-free metadata to the server")

    skill_parser = command_parsers.add_parser(
        "skill", help="install and manage connector-local Agent Skills")
    skill_actions = skill_parser.add_subparsers(dest="skill_action")
    skill_install = skill_actions.add_parser("install", help="install a local skill directory")
    skill_install.add_argument("source")
    skill_install.add_argument("--force", action="store_true")
    skill_list = skill_actions.add_parser("list", help="list managed skills")
    skill_inspect = skill_actions.add_parser("inspect", help="inspect a managed skill")
    skill_inspect.add_argument("name")
    skill_remove = skill_actions.add_parser("remove", help="remove a managed skill")
    skill_remove.add_argument("name")
    skill_remove.add_argument("--force", action="store_true")
    for parser in (skill_install, skill_list, skill_inspect, skill_remove):
        parser.add_argument(
            "--project", nargs="?", const=".", default=None,
            metavar="ID_OR_NAME",
            help="use project scope; omit a value to resolve the current directory")
    ap.add_argument("--doctor", action="store_true",
                    help="check URL, TLS, health, protocol, and authentication, then exit")
    ap.add_argument("--status", action="store_true",
                    help="print connector/IPC configuration as JSON, then exit")
    args = ap.parse_args(argv)

    mode_label = {
        "all-in-one": "all-in-one (supervisor+transport via loopback)",
        "supervisor": "supervisor (sessiond; owns PTYs, serves IPC)",
        "transport": "transport (owns WS; connects to sessiond)",
    }[args.mode]

    if args.command == "project":
        await _project_command(args)
        return
    if args.command == "skill":
        await _skill_command(args)
        return

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
        await run_supervisor(
            args.server_url, args.token, args.endpoint, args.state_path)
        return
    if args.mode == "transport":
        await run_transport(args.server_url, args.token, args.endpoint)
        return

    local_store = open_local_store(args.state_path)
    c = Connector(
        args.server_url, args.token,
        spool=open_spool(args.server_url, args.token),
        local_store=local_store)
    try:
        while True:
            try:
                await c.run()
            except Exception as exc:
                print(f"[connector] disconnected: {explain_connection_error(exc)}; retry in 3s")
                await asyncio.sleep(3)
    finally:
        local_store.close()


if __name__ == "__main__":
    asyncio.run(main())
