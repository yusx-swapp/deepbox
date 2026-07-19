"""WebSocket transport half of the connector.

Cut 4 separates *transport* from *session ownership*. This module owns the
network side only: authenticate, open ``/ws/devbox``, relay control frames from
the server to the supervisor over an IPC :class:`~connector.ipc.Channel`, and
forward the supervisor's buffered output frames to the server.

Restarting or losing the transport does NOT touch PTYs — those are owned by the
:class:`~connector.supervisor.SessionSupervisor`, which keeps buffering output
while detached. :class:`TransportSession` models exactly one WS connection; the
outer reconnect loop lives in :mod:`connector.client`.
"""
from __future__ import annotations

import asyncio
import json

from .ipc import Channel

PROTOCOL_VERSION = 2
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


class TransportSession:
    """Bridges one WebSocket to a supervisor via an IPC channel.

    ``channel`` is the transport end of a :class:`~connector.ipc.Channel`. The
    supervisor holds the other end. On POSIX/Windows deployments this becomes a
    Unix socket / named pipe; in-process it is a
    :class:`~connector.ipc.LoopbackChannel`.
    """

    def __init__(self, channel: Channel):
        self.channel = channel
        self.last_heartbeat_ack = None

    async def run(self, ws) -> None:
        """Pump frames both ways until any direction ends, then return."""
        # Ask the supervisor to re-advertise live sessions so the server can
        # resume the same PTYs after a transport restart.
        await self.channel.send({"type": "list_sessions"})
        ws_to_channel = asyncio.create_task(self._ws_to_channel(ws))
        channel_to_ws = asyncio.create_task(self._channel_to_ws(ws))
        heartbeat = asyncio.create_task(heartbeat_loop(ws))
        done, pending = await asyncio.wait(
            {ws_to_channel, channel_to_ws, heartbeat},
            return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc

    async def _ws_to_channel(self, ws) -> None:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") == "heartbeat_ack":
                self.last_heartbeat_ack = frame.get("ts")
                continue
            await self.channel.send(frame)

    async def _channel_to_ws(self, ws) -> None:
        while True:
            envelope = await self.channel.recv()
            if envelope is None:
                return
            if envelope.get("type") == "ipc_delivery":
                await ws.send(json.dumps(envelope["frame"]))
                # Once WebSocket send returns, ensure the local acknowledgement
                # reaches the supervisor even if this task is cancelled next.
                await asyncio.shield(self.channel.send({
                    "type": "ipc_delivery_ack",
                    "delivery_id": envelope["delivery_id"],
                }))
            else:  # custom Channel compatibility during the Cut 4 transition
                await ws.send(json.dumps(envelope))
