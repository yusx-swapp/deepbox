"""WebSocket transport half of the connector.

The transport never owns a PTY. Protocol v3 output remains in the supervisor's
spool until the server confirms that the exact output identity is durable.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict

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
    """Bridge one WebSocket to a supervisor without owning its PTYs.

    Cut 9: sending and durable-ACK handling are decoupled so many output
    frames can be in flight to the server at once. ``_channel_to_ws`` sends
    every frame immediately and, for durable output, records its identity in
    the ordered ``_outstanding`` map without blocking. A separate
    ``_process_server_events`` task consumes ack/resend/error/fence events and
    releases, resends, fails, or fences the matching row. The spool remains the
    durability source of truth, so any un-released row replays on reconnect.
    """

    def __init__(self, channel: Channel):
        self.channel = channel
        self.last_heartbeat_ack = None
        self._server_events: asyncio.Queue[dict] = asyncio.Queue()
        # identity -> (delivery_id, frame), insertion order == send order.
        self._outstanding: "OrderedDict[tuple[str, str, int], tuple[object, dict]]" = (
            OrderedDict()
        )

    async def run(self, ws) -> None:
        """Pump frames both ways until any direction ends."""
        await self.channel.send({"type": "list_sessions"})
        ws_to_channel = asyncio.create_task(self._ws_to_channel(ws))
        channel_to_ws = asyncio.create_task(self._channel_to_ws(ws))
        server_events = asyncio.create_task(self._process_server_events(ws))
        heartbeat = asyncio.create_task(heartbeat_loop(ws))
        done, pending = await asyncio.wait(
            {ws_to_channel, channel_to_ws, server_events, heartbeat},
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
            if frame_type in {"ack", "resend", "error", "fence"}:
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

    async def _channel_to_ws(self, ws) -> None:
        """Send frames as they arrive; durable output is tracked, not awaited."""
        while True:
            envelope = await self.channel.recv()
            if envelope is None:
                return
            if envelope.get("type") != "ipc_delivery":
                await ws.send(json.dumps(envelope))
                continue

            frame = envelope["frame"]
            delivery_id = envelope["delivery_id"]
            if frame.get("type") == "output":
                identity = self._identity(frame)
                if not identity[0] or not identity[1] or identity[2] <= 0:
                    raise ProtocolError(
                        "output frame has no valid protocol-v3 identity")
                # Record before send so a fast server ACK always finds the row.
                self._outstanding[identity] = (delivery_id, frame)
                await ws.send(json.dumps(frame))
                # No local ACK here: the server durability ACK (processed by
                # _process_server_events) is what releases this spool row.
                continue
            # Controls have no server durability ACK; their local send boundary
            # retains the Cut 4 behavior — ack immediately after the WS send.
            await ws.send(json.dumps(frame))
            await asyncio.shield(self.channel.send({
                "type": "ipc_delivery_ack",
                "delivery_id": delivery_id,
            }))

    async def _process_server_events(self, ws) -> None:
        """Release / resend / fail / fence outstanding rows from server events."""
        while True:
            event = await self._server_events.get()
            event_type = event.get("type")
            if event_type == "ack":
                try:
                    ack_identity = self._identity(event)
                except (TypeError, ValueError):
                    continue
                pair = self._outstanding.pop(ack_identity, None)
                if pair is None:
                    # Late/stale/unknown ACK: cannot release any spool row.
                    continue
                await self.channel.send({
                    "type": "ipc_delivery_ack",
                    "delivery_id": pair[0],
                })
            elif event_type == "resend":
                await self._handle_resend(ws, event)
            elif event_type == "error":
                raise ProtocolError(
                    str(event.get("detail") or "server rejected output"))
            elif event_type == "fence":
                await self._handle_fence(event)

    async def _handle_resend(self, ws, event: dict) -> None:
        expected = event.get("expected_seq")
        sess = str(event.get("session_id", ""))
        pty = str(event.get("pty_instance_id", ""))
        # Resend the requested row and every later outstanding row on that
        # stream, in seq order, to restore a contiguous durable tail.
        stream = sorted(
            (ident for ident in self._outstanding
             if ident[0] == sess and ident[1] == pty),
            key=lambda ident: ident[2])
        if not any(ident[2] == expected for ident in stream):
            raise ProtocolError(
                f"server resume mismatch for {sess}/{pty}: "
                f"expected {expected}, outstanding "
                f"{[ident[2] for ident in stream]}")
        for ident in stream:
            if ident[2] >= expected:
                await ws.send(json.dumps(self._outstanding[ident][1]))

    async def _handle_fence(self, event: dict) -> None:
        # The server ruled this pty_instance's durable stream forked
        # (CONFLICT / stale-tail). Raising would wedge reconnect into a
        # poison-frame loop, so purge every outstanding row for the stream and
        # tell the supervisor to drop its spool tail, then let newer output flow.
        sess = str(event.get("session_id", ""))
        pty = str(event.get("pty_instance_id", ""))
        removed = [ident for ident in self._outstanding
                   if ident[0] == sess and ident[1] == pty]
        if not removed:
            # A fence for some other stream cannot release these rows.
            return
        for ident in removed:
            self._outstanding.pop(ident, None)
        await self.channel.send({
            "type": "fence",
            "session_id": sess,
            "pty_instance_id": pty,
        })

