import unittest

from server.app.hub import DevboxConn, Hub, HumanConn


class FakeWebSocket:
    def __init__(self):
        self.close_codes = []

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


if __name__ == "__main__":
    unittest.main()
