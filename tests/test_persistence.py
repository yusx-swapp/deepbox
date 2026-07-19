import asyncio
import json
import unittest
import uuid

import pyte

from connector.client import Connector, heartbeat_loop
from server.app.live import LiveSession, serialize_screen


class FailingWebSocket:
    async def send(self, _data):
        raise ConnectionError("server restarted")


class RecordingWebSocket:
    def __init__(self):
        self.frames = []

    async def send(self, data):
        self.frames.append(data)


class ConnectorBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_ws_send_keeps_frame_for_next_connection(self):
        connector = Connector("http://unused", "token")
        frame = {"type": "output", "session_id": "s1", "data": "important"}
        await connector.send(frame)

        with self.assertRaises(ConnectionError):
            await connector._sender(FailingWebSocket())
        self.assertEqual(list(connector.pending), [frame])

        healthy = RecordingWebSocket()
        task = asyncio.create_task(connector._sender(healthy))
        for _ in range(20):
            if not connector.pending:
                break
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(len(healthy.frames), 1)
        self.assertEqual(len(connector.pending), 0)

    async def test_idle_connection_emits_protocol_heartbeat(self):
        socket = RecordingWebSocket()
        task = asyncio.create_task(heartbeat_loop(socket, interval=0.001))
        for _ in range(20):
            if socket.frames:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(socket.frames)
        self.assertEqual(json.loads(socket.frames[0]), {"type": "heartbeat"})


class ScreenRestoreTests(unittest.TestCase):
    def test_restore_contains_scrollback_and_current_screen(self):
        screen = pyte.HistoryScreen(20, 3, history=100)
        stream = pyte.ByteStream(screen)
        stream.feed(b"first\r\nsecond\r\nthird\r\nfourth")

        restored = serialize_screen(screen)

        self.assertIn("first", restored)
        self.assertIn("fourth", restored)
        self.assertIn("\x1b[2J\x1b[H", restored)

    def test_live_session_records_output_and_restores_it(self):
        sid = "test-" + uuid.uuid4().hex
        live = LiveSession(sid, 40, 5)
        try:
            live.feed_output("persistent hello")
            self.assertIn("persistent hello", live.restore_bytes())
            self.assertIn("persistent hello", live.cast_path.read_text(encoding="utf-8"))
        finally:
            live.mark_ended(0)
            live.cast_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
