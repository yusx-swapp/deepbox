"""Protocol v3 transport durability acknowledgement tests."""
import asyncio
import json
import unittest

from connector.ipc import LoopbackChannel
from connector.transport import ProtocolError, TransportSession


OUTPUT = {
    "type": "output",
    "session_id": "s1",
    "pty_instance_id": "p1",
    "seq": 1,
    "data": "ok",
}


class FakeWebSocket:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def send(self, payload):
        if self.error:
            raise self.error
        self.sent.append(json.loads(payload))


class TransportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def _start_delivery(self, frame=None, delivery_id=7):
        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        ws = FakeWebSocket()
        task = asyncio.create_task(transport._channel_to_ws(ws))
        await supervisor_end.send({
            "type": "ipc_delivery",
            "delivery_id": delivery_id,
            "frame": dict(frame or OUTPUT),
        })
        await asyncio.sleep(0)
        return supervisor_end, transport, ws, task

    async def test_send_without_server_ack_does_not_release_delivery(self):
        supervisor, _, ws, task = await self._start_delivery()
        self.assertEqual(ws.sent, [OUTPUT])
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_exact_server_ack_releases_delivery(self):
        supervisor, transport, _, task = await self._start_delivery()
        await transport._server_events.put({
            "type": "ack", "session_id": "s1",
            "pty_instance_id": "p1", "seq": 1,
        })
        ack = await asyncio.wait_for(supervisor.recv(), timeout=0.2)
        self.assertEqual(ack, {"type": "ipc_delivery_ack", "delivery_id": 7})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_stale_ack_is_ignored_until_exact_ack(self):
        supervisor, transport, _, task = await self._start_delivery()
        await transport._server_events.put({
            "type": "ack", "session_id": "s1",
            "pty_instance_id": "old", "seq": 1,
        })
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        await transport._server_events.put({
            "type": "ack", "session_id": "s1",
            "pty_instance_id": "p1", "seq": 1,
        })
        self.assertEqual((await asyncio.wait_for(supervisor.recv(), 0.2))["delivery_id"], 7)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_matching_resend_retries_same_row_before_ack(self):
        supervisor, transport, ws, task = await self._start_delivery()
        await transport._server_events.put({
            "type": "resend", "session_id": "s1",
            "pty_instance_id": "p1", "expected_seq": 1,
        })
        await asyncio.sleep(0)
        self.assertEqual(ws.sent, [OUTPUT, OUTPUT])
        await transport._server_events.put({
            "type": "ack", "session_id": "s1",
            "pty_instance_id": "p1", "seq": 1,
        })
        await asyncio.wait_for(supervisor.recv(), 0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_resume_mismatch_fails_closed_without_local_ack(self):
        supervisor, transport, _, task = await self._start_delivery()
        await transport._server_events.put({
            "type": "resend", "session_id": "s1",
            "pty_instance_id": "p1", "expected_seq": 2,
        })
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], ProtocolError)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)

    async def test_reconnect_resends_unreleased_output(self):
        supervisor, _, first_ws, first_task = await self._start_delivery()
        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
        # A fresh transport receives the same still-pending delivery from sessiond.
        supervisor2, transport_end2 = LoopbackChannel.pair()
        transport2 = TransportSession(transport_end2)
        second_ws = FakeWebSocket()
        second_task = asyncio.create_task(transport2._channel_to_ws(second_ws))
        await supervisor2.send({"type": "ipc_delivery", "delivery_id": 7,
                                "frame": dict(OUTPUT)})
        await asyncio.sleep(0)
        self.assertEqual(first_ws.sent, [OUTPUT])
        self.assertEqual(second_ws.sent, [OUTPUT])
        second_task.cancel()
        await asyncio.gather(second_task, return_exceptions=True)

    async def test_control_frame_keeps_send_boundary_ack(self):
        frame = {"type": "ready", "session_id": "s1"}
        supervisor, _, ws, task = await self._start_delivery(frame=frame)
        self.assertEqual(ws.sent, [frame])
        self.assertEqual((await asyncio.wait_for(supervisor.recv(), 0.2))["delivery_id"], 7)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_failed_websocket_send_is_not_acknowledged(self):
        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        task = asyncio.create_task(
            transport._channel_to_ws(FakeWebSocket(ConnectionError("closed")))
        )
        await supervisor_end.send({"type": "ipc_delivery", "delivery_id": 8,
                                   "frame": dict(OUTPUT)})
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], ConnectionError)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor_end.recv(), timeout=0.02)


if __name__ == "__main__":
    unittest.main()
