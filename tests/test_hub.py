import asyncio
import unittest

from server.app.hub import DevboxConn, Hub, HumanConn


class FakeWebSocket:
    def __init__(self):
        self.close_codes = []

    async def close(self, code=1000):
        self.close_codes.append(code)


class FanoutWebSocket:
    def __init__(self, stalled=False):
        self.stalled = stalled
        self.sent = []
        self.close_codes = []
        self._never = asyncio.Event()

    async def send_json(self, frame):
        if self.stalled:
            await self._never.wait()
        self.sent.append(frame)

    async def close(self, code=1000):
        self.close_codes.append(code)


class HubUserDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_user_closes_only_owned_connections(self):
        hub = Hub()
        target_human_ws = FakeWebSocket()
        other_human_ws = FakeWebSocket()
        target_devbox_ws = FakeWebSocket()
        other_devbox_ws = FakeWebSocket()

        target_human = HumanConn(ws=target_human_ws, user_id="target")
        other_human = HumanConn(ws=other_human_ws, user_id="other")
        hub.add_human(target_human)
        hub.add_human(other_human)
        await hub.add_devbox(DevboxConn(
            ws=target_devbox_ws, devbox_id="target-box", agent_ids={"target-agent"}
        ))
        await hub.add_devbox(DevboxConn(
            ws=other_devbox_ws, devbox_id="other-box", agent_ids={"other-agent"}
        ))

        counts = await hub.disconnect_user("target", {"target-box"})

        self.assertEqual(counts, (1, 1))
        self.assertEqual(target_human_ws.close_codes, [4001])
        self.assertEqual(target_devbox_ws.close_codes, [4001])
        self.assertEqual(other_human_ws.close_codes, [])
        self.assertEqual(other_devbox_ws.close_codes, [])
        self.assertNotIn(target_human, hub.humans)
        self.assertNotIn("target-box", hub.devboxes)
        self.assertNotIn("target-agent", hub.agent_to_devbox)
        self.assertIn(other_human, hub.humans)
        self.assertIn("other-box", hub.devboxes)
        self.assertEqual(hub.agent_to_devbox["other-agent"], "other-box")

    async def test_disconnect_user_sessions_is_scoped_to_selected_sessions(self):
        hub = Hub()
        selected_ws = FakeWebSocket()
        other_session_ws = FakeWebSocket()
        other_user_ws = FakeWebSocket()
        selected = HumanConn(ws=selected_ws, user_id="target", sessions={"s1": "a1"})
        other_session = HumanConn(ws=other_session_ws, user_id="target", sessions={"s2": "a2"})
        other_user = HumanConn(ws=other_user_ws, user_id="other", sessions={"s1": "a1"})
        for conn in (selected, other_session, other_user):
            hub.add_human(conn)

        count = await hub.disconnect_user_sessions("target", {"s1"})

        self.assertEqual(count, 1)
        self.assertEqual(selected_ws.close_codes, [4001])
        self.assertEqual(other_session_ws.close_codes, [])
        self.assertEqual(other_user_ws.close_codes, [])
        self.assertNotIn(selected, hub.humans)
        self.assertIn(other_session, hub.humans)
        self.assertIn(other_user, hub.humans)

    async def test_stalled_resumed_watcher_cannot_block_live_fanout(self):
        hub = Hub(human_send_timeout=0.01)
        healthy_ws = FanoutWebSocket()
        stalled_ws = FanoutWebSocket(stalled=True)
        healthy = HumanConn(ws=healthy_ws, user_id="healthy")
        stalled = HumanConn(ws=stalled_ws, user_id="stale")
        hub.add_human(healthy)
        hub.add_human(stalled)
        hub.watch(healthy, "s1", "a1")
        hub.watch(stalled, "s1", "a1")

        frames = [
            {"type": "output", "session_id": "s1", "seq": 388},
            {"type": "output", "session_id": "s1", "seq": 389},
        ]
        for frame in frames:
            await asyncio.wait_for(hub.to_session_humans("s1", frame), timeout=0.2)
        await asyncio.sleep(0.03)

        self.assertEqual(healthy_ws.sent, frames)
        self.assertIn(healthy, hub.humans)
        self.assertNotIn(stalled, hub.humans)
        self.assertEqual(stalled_ws.close_codes, [1011])
        self.assertNotIn(stalled, hub.session_watchers.get("s1", set()))
        hub.remove_human(healthy)

    async def test_full_watcher_queue_evicts_without_blocking_producer(self):
        hub = Hub(human_send_timeout=1.0, human_queue_size=1)
        stalled_ws = FanoutWebSocket(stalled=True)
        stalled = HumanConn(ws=stalled_ws, user_id="stale")
        hub.add_human(stalled)
        hub.watch(stalled, "s1", "a1")

        await hub.to_session_humans("s1", {"seq": 1})
        await asyncio.sleep(0)  # Let the sender consume seq 1 and stall.
        await hub.to_session_humans("s1", {"seq": 2})
        await asyncio.wait_for(
            hub.to_session_humans("s1", {"seq": 3}), timeout=0.1
        )
        await asyncio.sleep(0.01)

        self.assertNotIn(stalled, hub.humans)
        self.assertNotIn(stalled, hub.session_watchers.get("s1", set()))
        self.assertEqual(stalled_ws.close_codes, [1011])


if __name__ == "__main__":
    unittest.main()
