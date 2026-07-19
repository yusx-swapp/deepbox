"""Cut 4 transport delivery acknowledgement tests."""
import asyncio
import json
import unittest

from connector.ipc import LoopbackChannel
from connector.transport import TransportSession


class FakeWebSocket:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def send(self, payload):
        if self.error:
            raise self.error
        self.sent.append(json.loads(payload))


class TransportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_websocket_send_is_acknowledged(self):
        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        task = asyncio.create_task(transport._channel_to_ws(FakeWebSocket()))

        await supervisor_end.send({
            "type": "ipc_delivery",
            "delivery_id": 7,
            "frame": {"type": "output", "data": "ok"},
        })
        ack = await supervisor_end.recv()
        self.assertEqual(ack, {"type": "ipc_delivery_ack", "delivery_id": 7})

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_failed_websocket_send_is_not_acknowledged(self):
        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        task = asyncio.create_task(
            transport._channel_to_ws(FakeWebSocket(ConnectionError("closed")))
        )

        await supervisor_end.send({
            "type": "ipc_delivery",
            "delivery_id": 8,
            "frame": {"type": "output", "data": "retry"},
        })
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], ConnectionError)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor_end.recv(), timeout=0.01)


if __name__ == "__main__":
    unittest.main()
