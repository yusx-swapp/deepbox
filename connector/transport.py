"""WebSocket transport half of the connector.

The transport never owns a PTY. Protocol v3 output remains in the supervisor's
spool until the server confirms that the exact output identity is durable.
"""
from __future__ import annotations

import asyncio
import json

from .ipc import Channel

PROTOCOL_VERSION = 3
HEARTBEAT_INTERVAL = 20.0


class ProtocolError(RuntimeError):
    """The peer requested a resume state incompatible with the in-flight row."""


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
    """Bridge one WebSocket to a supervisor without owning its PTYs."""

    def __init__(self, channel: Channel):
        self.channel = channel
        self.last_heartbeat_ack = None
        self._server_events: asyncio.Queue[dict] = asyncio.Queue()
        self._inflight_identity: tuple[str, str, int] | None = None

    async def run(self, ws) -> None:
        """Pump frames both ways until either direction ends."""
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
            frame_type = frame.get("type")
            if frame_type == "heartbeat_ack":
                self.last_heartbeat_ack = frame.get("ts")
                continue
            if frame_type in {"ack", "resend", "error"}:
                await self._server_events.put(frame)
                continue
            await self.channel.send(frame)

    @staticmethod
    def _identity(frame: dict) -> tuple[str, str, int]:
        return (
            str(frame.get("session_id", "")),
            str(frame.get("pty_instance_id", "")),
            int(frame.get("seq", 0)),
        )

    async def _await_durable_ack(self, ws, frame: dict) -> None:
        identity = self._identity(frame)
        if not identity[0] or not identity[1] or identity[2] <= 0:
            raise ProtocolError("output frame has no valid protocol-v3 identity")
        self._inflight_identity = identity
        try:
            while True:
                event = await self._server_events.get()
                event_type = event.get("type")
                if event_type == "ack":
                    try:
                        ack_identity = self._identity(event)
                    except (TypeError, ValueError):
                        continue
                    if ack_identity == identity:
                        return
                    # A late/stale ACK cannot release a different spool row.
                    continue
                if event_type == "resend":
                    expected = event.get("expected_seq")
                    same_stream = (
                        str(event.get("session_id", "")) == identity[0]
                        and str(event.get("pty_instance_id", "")) == identity[1]
                    )
                    if same_stream and expected == identity[2]:
                        await ws.send(json.dumps(frame))
                        continue
                    raise ProtocolError(
                        f"server resume mismatch for {identity[0]}/{identity[1]}: "
                        f"expected {expected}, in-flight {identity[2]}")
                if event_type == "error":
                    raise ProtocolError(str(event.get("detail") or "server rejected output"))
        finally:
            self._inflight_identity = None

    async def _channel_to_ws(self, ws) -> None:
        while True:
            envelope = await self.channel.recv()
            if envelope is None:
                return
            if envelope.get("type") != "ipc_delivery":
                await ws.send(json.dumps(envelope))
                continue

            frame = envelope["frame"]
            await ws.send(json.dumps(frame))
            if frame.get("type") == "output":
                await self._await_durable_ack(ws, frame)
            # Controls have no server durability ACK; their local send boundary
            # retains the Cut 4 behavior. Output reaches here only after exact ACK.
            await asyncio.shield(self.channel.send({
                "type": "ipc_delivery_ack",
                "delivery_id": envelope["delivery_id"],
            }))
