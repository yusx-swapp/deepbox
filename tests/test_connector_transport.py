
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


def _output(seq, data="ok", session_id="s1", pty="p1"):
    return {
        "type": "output",
        "session_id": session_id,
        "pty_instance_id": pty,
        "seq": seq,
        "data": data,
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
    async def _start(self, ws=None):
        """Start both the sender and the server-event processor tasks."""
        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        ws = ws or FakeWebSocket()
        sender = asyncio.create_task(transport._channel_to_ws(ws))
        events = asyncio.create_task(transport._process_server_events(ws))
        return supervisor_end, transport, ws, (sender, events)

    async def _deliver(self, supervisor, frame, delivery_id=7):
        await supervisor.send({
            "type": "ipc_delivery",
            "delivery_id": delivery_id,
            "frame": dict(frame),
        })
        await asyncio.sleep(0)

    async def _stop(self, tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_send_without_server_ack_does_not_release_delivery(self):
        supervisor, _, ws, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        self.assertEqual(ws.sent, [OUTPUT])
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        await self._stop(tasks)

    async def test_exact_server_ack_releases_delivery(self):
        supervisor, transport, _, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        await transport._server_events.put({
            "type": "ack", "session_id": "s1",
            "pty_instance_id": "p1", "seq": 1,
        })
        ack = await asyncio.wait_for(supervisor.recv(), timeout=0.2)
        self.assertEqual(ack, {"type": "ipc_delivery_ack", "delivery_id": 7})
        await self._stop(tasks)

    async def test_pipelines_many_frames_before_first_ack(self):
        # Core Cut 9 property: several output frames leave the transport before
        # any ACK returns, instead of one-frame-per-RTT stop-and-wait.
        supervisor, transport, ws, tasks = await self._start()
        for seq in range(1, 6):
            await self._deliver(supervisor, _output(seq), delivery_id=seq)
        self.assertEqual([f["seq"] for f in ws.sent], [1, 2, 3, 4, 5])
        # No ACK has been delivered yet, so nothing is released.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        # ACKs may even arrive out of order; each releases exactly its row.
        for seq in (3, 1, 2, 5, 4):
            await transport._server_events.put({
                "type": "ack", "session_id": "s1",
                "pty_instance_id": "p1", "seq": seq,
            })
        released = set()
        for _ in range(5):
            ack = await asyncio.wait_for(supervisor.recv(), timeout=0.2)
            released.add(ack["delivery_id"])
        self.assertEqual(released, {1, 2, 3, 4, 5})
        await self._stop(tasks)

    async def test_stale_ack_is_ignored_until_exact_ack(self):
        supervisor, transport, _, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
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
        await self._stop(tasks)

    async def test_matching_resend_retries_same_row_before_ack(self):
        supervisor, transport, ws, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
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
        await self._stop(tasks)

    async def test_resend_retransmits_whole_tail_in_order(self):
        # A resend for an earlier seq must replay that row and every later
        # outstanding row on the stream, in seq order.
        supervisor, transport, ws, tasks = await self._start()
        for seq in (1, 2, 3):
            await self._deliver(supervisor, _output(seq), delivery_id=seq)
        self.assertEqual([f["seq"] for f in ws.sent], [1, 2, 3])
        await transport._server_events.put({
            "type": "resend", "session_id": "s1",
            "pty_instance_id": "p1", "expected_seq": 2,
        })
        await asyncio.sleep(0)
        self.assertEqual([f["seq"] for f in ws.sent], [1, 2, 3, 2, 3])
        await self._stop(tasks)

    async def test_fence_for_same_stream_purges_tail_and_releases(self):
        supervisor, transport, _, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        await transport._server_events.put({
            "type": "fence", "session_id": "s1",
            "pty_instance_id": "p1", "seq": 1,
        })
        msg = await asyncio.wait_for(supervisor.recv(), timeout=0.2)
        self.assertEqual(msg, {
            "type": "fence", "session_id": "s1", "pty_instance_id": "p1",
        })
        # The processor released the tail without raising, so both tasks live.
        await asyncio.sleep(0)
        self.assertFalse(any(t.done() for t in tasks))
        await self._stop(tasks)

    async def test_fence_for_other_stream_does_not_release(self):
        supervisor, transport, _, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        await transport._server_events.put({
            "type": "fence", "session_id": "s1",
            "pty_instance_id": "other", "seq": 1,
        })
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        await self._stop(tasks)

    async def test_resume_mismatch_fails_closed_without_local_ack(self):
        supervisor, transport, _, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        await transport._server_events.put({
            "type": "resend", "session_id": "s1",
            "pty_instance_id": "p1", "expected_seq": 2,
        })
        # The server-event processor raises ProtocolError; the sender keeps
        # running but no local ACK is emitted.
        result = await asyncio.gather(tasks[1], return_exceptions=True)
        self.assertIsInstance(result[0], ProtocolError)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(supervisor.recv(), timeout=0.02)
        await self._stop((tasks[0],))

    async def test_reconnect_resends_unreleased_output(self):
        supervisor, _, first_ws, tasks = await self._start()
        await self._deliver(supervisor, OUTPUT)
        await self._stop(tasks)
        # A fresh transport receives the same still-pending delivery.
        supervisor2, transport_end2 = LoopbackChannel.pair()
        transport2 = TransportSession(transport_end2)
        second_ws = FakeWebSocket()
        second_task = asyncio.create_task(transport2._channel_to_ws(second_ws))
        await supervisor2.send({"type": "ipc_delivery", "delivery_id": 7,
                                "frame": dict(OUTPUT)})
        await asyncio.sleep(0)
        self.assertEqual(first_ws.sent, [OUTPUT])
        self.assertEqual(second_ws.sent, [OUTPUT])
        await self._stop((second_task,))

    async def test_control_frame_keeps_send_boundary_ack(self):
        frame = {"type": "ready", "session_id": "s1"}
        supervisor, _, ws, tasks = await self._start()
        await self._deliver(supervisor, frame)
        self.assertEqual(ws.sent, [frame])
        self.assertEqual((await asyncio.wait_for(supervisor.recv(), 0.2))["delivery_id"], 7)
        await self._stop(tasks)

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


class TransportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_retrieves_all_child_failures_and_leaves_no_tasks(self):
        class ClosingWebSocket:
            def __init__(self):
                self.closing = asyncio.Event()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await self.closing.wait()
                raise ConnectionError("receive closed")

            async def send(self, _payload):
                self.closing.set()
                raise ConnectionError("send closed")

        supervisor_end, transport_end = LoopbackChannel.pair()
        transport = TransportSession(transport_end)
        await supervisor_end.send({"type": "sessions", "sessions": []})
        loop = asyncio.get_running_loop()
        contexts = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        before = set(asyncio.all_tasks())
        try:
            with self.assertRaises(ConnectionError):
                await transport.run(ClosingWebSocket())
            await asyncio.sleep(0)
            leaked = [task for task in asyncio.all_tasks()
                      if task not in before and not task.done()]
            self.assertEqual(leaked, [])
            self.assertEqual(contexts, [])
        finally:
            loop.set_exception_handler(previous_handler)


if __name__ == "__main__":
    unittest.main()
