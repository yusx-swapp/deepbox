"""Cut 4 supervisor/transport split tests.

These verify the central invariant of the split: a transport
restart/detach must NOT close PTYs, and buffered output survives to the next
transport attach. Uses a fake PTY so no real process/ConPTY is spawned.
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

import connector.client as client_mod
from connector.client import SupervisorService
from connector.ipc import IS_WIN, LoopbackChannel, connect_channel, ensure_secret
from connector.supervisor import SessionSupervisor
import connector.supervisor as supervisor_mod


class FakePty:
    instances = []

    def __init__(self, cmd, cwd, on_output, on_exit, cols=120, rows=30):
        self.cmd = cmd
        self.on_output = on_output
        self.on_exit = on_exit
        self.killed = False
        self.alive = True
        self.written = []
        FakePty.instances.append(self)

    async def start(self):
        await self.on_output("hello")

    def write(self, data):
        self.written.append(data)

    def resize(self, cols, rows):
        self.size = (cols, rows)

    def is_alive(self):
        return self.alive

    def kill(self):
        self.killed = True
        self.alive = False


class SupervisorSplitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakePty.instances = []
        self._orig = supervisor_mod.PtySession
        supervisor_mod.PtySession = FakePty
        supervisor_mod.resolve_cmd = lambda runtime, launch, **kw: ["fake"]

    def tearDown(self):
        supervisor_mod.PtySession = self._orig

    async def test_open_pty_emits_ready_and_output(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        types = [f["type"] for f in sup.pending]
        self.assertIn("output", types)
        self.assertIn("ready", types)
        self.assertIn(("a", "s"), sup.ptys)
        durable_types = [
            frame["type"] for _delivery_id, frame in sup._spool.pending_records()
        ]
        self.assertEqual(durable_types, ["output"])

    async def test_agents_frame_refreshes_directory_for_hot_added_agent(self):
        # Agent 'b' does not exist at startup; a pushed 'agents' frame should
        # make it resolvable so a subsequent open uses its runtime.
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        self.assertNotIn("b", sup.agents)
        await sup.handle_control({"type": "agents", "agents": [
            {"id": "a", "runtime": "mock"},
            {"id": "b", "runtime": "mock", "launch_cmd": None},
        ]})
        self.assertIn("b", sup.agents)
        self.assertEqual(sup.agents["b"]["runtime"], "mock")
        await sup.handle_control({"type": "open", "agent_id": "b", "session_id": "s"})
        self.assertIn(("b", "s"), sup.ptys)

    async def test_agents_frame_ignores_malformed_payload(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "agents", "agents": "nonsense"})
        self.assertEqual(set(sup.agents), {"a"})
        await sup.handle_control({
            "type": "agents", "agents": [{"id": "a"}, {}]})
        self.assertEqual(set(sup.agents), {"a"})

    async def test_agents_frame_kills_sessions_for_removed_agent(self):
        class FakePty:
            def __init__(self):
                self.killed = False

            def kill(self):
                self.killed = True

        removed_pty = FakePty()
        kept_pty = FakePty()
        sup = SessionSupervisor({
            "removed": {"id": "removed", "runtime": "mock"},
            "kept": {"id": "kept", "runtime": "mock"},
        })
        sup.ptys = {
            ("removed", "s1"): removed_pty,
            ("kept", "s2"): kept_pty,
        }
        sup.pty_instances = {
            ("removed", "s1"): "old-removed",
            ("kept", "s2"): "old-kept",
        }
        sup.emit({
            "type": "output", "agent_id": "removed", "session_id": "s1",
            "pty_instance_id": "old-removed", "seq": 0, "data": "stale",
        })
        sup.emit({"type": "presence", "agent_id": "removed",
                  "session_id": "s1", "state": "online"})
        removed_control_id = sup._controls[-1][0]
        sup._inflight_ids[removed_control_id] = 0

        await sup.handle_control({
            "type": "agents",
            "agents": [{"id": "kept", "runtime": "mock"}],
        })

        self.assertEqual(set(sup.agents), {"kept"})
        self.assertNotIn(("removed", "s1"), sup.ptys)
        self.assertNotIn(("removed", "s1"), sup.pty_instances)
        self.assertFalse(any(
            frame.get("agent_id") == "removed"
            for _delivery_id, frame in sup._spool.pending_records()
        ))
        self.assertFalse(any(
            frame.get("agent_id") == "removed"
            for _delivery_id, frame in sup._controls
        ))
        self.assertNotIn(removed_control_id, sup._inflight_ids)
        self.assertIn(("kept", "s2"), sup.ptys)
        self.assertEqual(sup.pty_instances[("kept", "s2")], "old-kept")
        self.assertTrue(removed_pty.killed)
        self.assertFalse(kept_pty.killed)

    async def test_transport_restart_does_not_kill_pty(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        pty = FakePty.instances[0]

        # Transport goes away.
        sup.detach()
        self.assertFalse(sup.attached)
        self.assertFalse(pty.killed)
        self.assertIn(("a", "s"), sup.ptys)

        # Output produced while detached is buffered, not lost.
        await pty.on_output("while-detached")
        self.assertTrue(any(f.get("data") == "while-detached" for f in sup.pending))

        # New transport attaches and drains the backlog in order.
        sup2, tx2 = LoopbackChannel.pair()
        sup.attach(sup2)
        drain = asyncio.create_task(sup.drain_to(sup2))
        received = []
        for _ in range(len(sup.pending)):
            envelope = await tx2.recv()
            received.append(envelope["frame"])
            await sup.handle_control({
                "type": "ipc_delivery_ack",
                "delivery_id": envelope["delivery_id"],
            })
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        self.assertTrue(any(f.get("data") == "while-detached" for f in received))
        self.assertFalse(pty.killed)

    async def test_unacknowledged_delivery_stays_pending_for_next_transport(self):
        sup = SessionSupervisor()
        sup.emit({"type": "output", "session_id": "s",
                  "pty_instance_id": "p", "data": "keep-me"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))

        envelope = await tx_end.recv()
        self.assertEqual(envelope["frame"]["data"], "keep-me")
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)

        self.assertEqual(len(sup.pending), 1)
        self.assertEqual(sup.pending[0]["data"], "keep-me")

    async def test_control_arriving_during_output_does_not_block_exact_ack(self):
        sup = SessionSupervisor()
        sup.emit({"type": "output", "session_id": "s",
                  "pty_instance_id": "p", "data": "durable"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))
        try:
            output = await tx_end.recv()
            self.assertEqual(output["frame"]["type"], "output")

            # A browser input ACK can be emitted while this output waits for its
            # server durable ACK. The later control must not invalidate the
            # already selected in-flight delivery.
            sup.emit({"type": "input_ack", "session_id": "s",
                      "client_input_id": "11111111-1111-4111-8111-111111111111",
                      "status": "delivered"})
            await sup.handle_control({
                "type": "ipc_delivery_ack",
                "delivery_id": output["delivery_id"],
            })

            control = await asyncio.wait_for(tx_end.recv(), timeout=1.0)
            self.assertEqual(control["frame"]["type"], "input_ack")
            self.assertEqual(len(sup._spool.pending_records()), 0)
            await sup.handle_control({
                "type": "ipc_delivery_ack",
                "delivery_id": control["delivery_id"],
            })
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_fence_control_purges_forked_instance_and_unblocks(self):
        sup = SessionSupervisor()
        # Two output frames for a forked pty_instance sit in the spool.
        sup.emit({"type": "output", "session_id": "s",
                  "pty_instance_id": "old", "data": "poison-1"})
        sup.emit({"type": "output", "session_id": "s",
                  "pty_instance_id": "old", "data": "poison-2"})
        # A newer instance has fresh output that must be able to drain.
        sup.emit({"type": "output", "session_id": "s",
                  "pty_instance_id": "new", "data": "fresh"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))
        try:
            # Cut 9: with bounded pipelining all three frames drain without
            # waiting for ACKs; the forked tail and the fresh output are both
            # sent (the server, not the connector, decides the fork).
            delivered = []
            for _ in range(3):
                env = await asyncio.wait_for(tx_end.recv(), timeout=1.0)
                delivered.append(env["frame"]["pty_instance_id"])
            self.assertEqual(delivered, ["old", "old", "new"])

            # Server fences the forked instance instead of erroring. The fence
            # purges the poison rows from the spool and drops their in-flight
            # window entries so their ACKs are no longer required.
            await sup.handle_control({
                "type": "fence", "session_id": "s", "pty_instance_id": "old",
            })

            pending = sup._spool.records()
            self.assertTrue(all(r.pty_instance_id != "old" for r in pending))
            # The fresh instance's row survives the fence and stays deliverable.
            self.assertTrue(any(r.pty_instance_id == "new" for r in pending))
            # No stale in-flight ids remain for the fenced stream.
            self.assertEqual(sup._inflight_bytes,
                             sum(sup._inflight_ids.values()))
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_pipelines_multiple_frames_before_any_ack(self):
        # Core Cut 9 property at the supervisor layer: with a bounded window,
        # many durable frames drain before the first ACK returns.
        sup = SessionSupervisor()
        for i in range(5):
            sup.emit({"type": "output", "session_id": "s",
                      "pty_instance_id": "p", "data": f"chunk-{i}"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))
        try:
            got = []
            for _ in range(5):
                env = await asyncio.wait_for(tx_end.recv(), timeout=1.0)
                got.append(env["frame"]["data"])
            # All five sent with zero ACKs consumed yet.
            self.assertEqual(got, [f"chunk-{i}" for i in range(5)])
            self.assertEqual(len(sup._inflight_ids), 5)
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_window_bounds_inflight_frames(self):
        # The in-flight window is bounded by frame count; the sender stops
        # after MAX_INFLIGHT_FRAMES until ACKs free capacity.
        from connector.supervisor import MAX_INFLIGHT_FRAMES
        sup = SessionSupervisor()
        total = MAX_INFLIGHT_FRAMES + 5
        for i in range(total):
            sup.emit({"type": "output", "session_id": "s",
                      "pty_instance_id": "p", "data": f"c{i}"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))
        try:
            for _ in range(MAX_INFLIGHT_FRAMES):
                await asyncio.wait_for(tx_end.recv(), timeout=1.0)
            # Window full: no further frame until an ACK frees a slot.
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(tx_end.recv(), timeout=0.05)
            self.assertEqual(len(sup._inflight_ids), MAX_INFLIGHT_FRAMES)
            # ACK the oldest row; exactly one more frame drains.
            oldest = min(d for d in sup._inflight_ids
                         if not isinstance(d, str))
            await sup.handle_control({"type": "ipc_delivery_ack",
                                      "delivery_id": oldest})
            env = await asyncio.wait_for(tx_end.recv(), timeout=1.0)
            self.assertEqual(env["frame"]["type"], "output")
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_out_of_order_ack_only_releases_its_own_row(self):
        # A non-contiguous ACK must not delete a different spool row; the spool
        # keeps strict per-stream contiguity.
        sup = SessionSupervisor()
        for i in range(3):
            sup.emit({"type": "output", "session_id": "s",
                      "pty_instance_id": "p", "data": f"d{i}"})
        sup_end, tx_end = LoopbackChannel.pair()
        sup.attach(sup_end)
        drain = asyncio.create_task(sup.drain_to(sup_end))
        try:
            envs = [await asyncio.wait_for(tx_end.recv(), 1.0) for _ in range(3)]
            ids = [e["delivery_id"] for e in envs]
            before = len(sup._spool.pending_records())
            # ACK the middle row first: spool must not drop anything yet
            # (seq is not last_acked+1), and the id stays in-flight.
            await sup.handle_control({"type": "ipc_delivery_ack",
                                      "delivery_id": ids[1]})
            self.assertEqual(len(sup._spool.pending_records()), before)
            # ACK the head row: now the first (and only the first) row leaves.
            await sup.handle_control({"type": "ipc_delivery_ack",
                                      "delivery_id": ids[0]})
            self.assertEqual(len(sup._spool.pending_records()), before - 1)
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def test_close_control_kills_only_that_pty(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s1"})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s2"})
        p1 = FakePty.instances[0]
        await sup.handle_control({"type": "close", "agent_id": "a", "session_id": "s1"})
        self.assertTrue(p1.killed)
        self.assertNotIn(("a", "s1"), sup.ptys)
        self.assertIn(("a", "s2"), sup.ptys)

    async def test_input_and_resize_reach_pty(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        p = FakePty.instances[0]
        await sup.handle_control({"type": "input", "agent_id": "a", "session_id": "s",
                                  "client_input_id": "11111111-1111-4111-8111-111111111111",
                                  "data": "ls\n"})
        await sup.handle_control({"type": "resize", "agent_id": "a", "session_id": "s", "cols": 80, "rows": 24})
        self.assertEqual(p.written, ["ls\n"])
        self.assertEqual(p.size, (80, 24))

    async def test_duplicate_input_id_writes_once_and_acks_each_delivery(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        p = FakePty.instances[0]
        frame = {"type": "input", "agent_id": "a", "session_id": "s",
                 "client_input_id": "22222222-2222-4222-8222-222222222222",
                 "data": "once"}
        await sup.handle_control(dict(frame))
        await sup.handle_control(dict(frame))
        self.assertEqual(p.written, ["once"])
        acks = [f for f in sup.pending if f.get("type") == "input_ack"]
        self.assertEqual(len(acks), 2)
        self.assertTrue(all(f["status"] == "delivered" for f in acks))

    async def test_pty_instance_is_stable_and_output_seq_increments(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        open_frame = {"type": "open", "agent_id": "a", "session_id": "s"}
        await sup.handle_control(open_frame)
        instance_id = sup.pty_instances[("a", "s")]
        await sup.handle_control(open_frame)
        self.assertEqual(sup.pty_instances[("a", "s")], instance_id)
        self.assertEqual(len(FakePty.instances), 1)
        await FakePty.instances[0].on_output("second")
        outputs = [f for f in sup.pending if f.get("type") == "output"]
        self.assertEqual([f["seq"] for f in outputs], [1, 2])
        self.assertTrue(all(f["pty_instance_id"] == instance_id for f in outputs))

    async def test_open_replaces_dead_pty_and_ignores_late_stale_exit(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        open_frame = {"type": "open", "agent_id": "a", "session_id": "s"}
        await sup.handle_control(open_frame)
        stale = FakePty.instances[0]
        stale_instance_id = sup.pty_instances[("a", "s")]
        stale.alive = False

        await sup.handle_control(open_frame)
        current = FakePty.instances[1]
        current_instance_id = sup.pty_instances[("a", "s")]
        self.assertIs(sup.ptys[("a", "s")], current)
        self.assertNotEqual(current_instance_id, stale_instance_id)

        await stale.on_exit(9)
        self.assertIs(sup.ptys[("a", "s")], current)
        self.assertEqual(sup.pty_instances[("a", "s")], current_instance_id)
        stale_exits = [
            f for f in sup.pending
            if f.get("type") == "exit"
            and f.get("pty_instance_id") == stale_instance_id
        ]
        self.assertEqual(stale_exits, [])

    async def test_status_reports_sessions_and_pending(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        st = sup.status()
        self.assertEqual(st["sessions"][0]["agent_id"], "a")
        self.assertEqual(st["sessions"][0]["session_id"], "s")
        self.assertTrue(st["sessions"][0]["pty_instance_id"])
        self.assertGreater(st["pending_frames"], 0)

    async def test_shutdown_kills_all(self):
        sup = SessionSupervisor({"a": {"runtime": "mock"}})
        await sup.handle_control({"type": "open", "agent_id": "a", "session_id": "s"})
        p = FakePty.instances[0]
        sup.shutdown()
        self.assertTrue(p.killed)
        self.assertEqual(len(sup.ptys), 0)


class SupervisorBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_failure_does_not_bind_empty_supervisor(self):
        bootstrap = mock.Mock()
        bootstrap.fetch_me = mock.AsyncMock(side_effect=RuntimeError("offline"))
        with mock.patch.object(client_mod, "Connector", return_value=bootstrap), \
                mock.patch.object(client_mod, "SupervisorService") as service:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await client_mod.run_supervisor("https://example.test", "token")
        service.assert_not_called()


class TwoProcessSplitTests(unittest.IsolatedAsyncioTestCase):
    """Real OS-IPC split: a detached transport reconnects and the FakePty
    survives across the transport disconnect. Proves the sessiond process can
    outlive a transport restart without spawning a real agent."""

    def setUp(self):
        FakePty.instances = []
        self._orig = supervisor_mod.PtySession
        supervisor_mod.PtySession = FakePty
        self._orig_resolve = supervisor_mod.resolve_cmd
        supervisor_mod.resolve_cmd = lambda runtime, launch, **kw: ["fake"]
        self._tmp = tempfile.mkdtemp(prefix="deepbox-split-test-")
        uniq = str(os.getpid()) + str(id(self))
        self._suffix = "splittest" + uniq
        os.environ["XDG_RUNTIME_DIR"] = self._tmp
        if IS_WIN:
            self._endpoint = r"\\.\pipe\deepbox-splittest-" + uniq
        else:
            self._endpoint = os.path.join(self._tmp, "sessiond-split.sock")

    def tearDown(self):
        supervisor_mod.PtySession = self._orig
        supervisor_mod.resolve_cmd = self._orig_resolve
        os.environ.pop("XDG_RUNTIME_DIR", None)

    async def _recv_until(self, channel, predicate, acks_to, limit=50):
        for _ in range(limit):
            frame = await asyncio.wait_for(channel.recv(), timeout=2.0)
            if frame is None:
                return None
            if frame.get("type") == "ipc_delivery":
                await acks_to.send({"type": "ipc_delivery_ack",
                                    "delivery_id": frame["delivery_id"]})
                inner = frame.get("frame", {})
                if predicate(inner):
                    return inner
            elif predicate(frame):
                return frame
        return None

    async def test_transport_reconnect_over_real_ipc_keeps_pty(self):
        ensure_secret(self._suffix)
        service = SupervisorService(endpoint=self._endpoint)
        service.supervisor.agents = {"a": {"runtime": "mock"}}
        # Serve the endpoint but let us drive user_suffix via env.
        service._server = None

        async def serve():
            from connector.ipc import serve_channel
            service._server = await serve_channel(
                service._on_channel, endpoint=self._endpoint,
                user_suffix=self._suffix)
            await service._stop.wait()
            await service._server.close()

        serve_task = asyncio.create_task(serve())
        await asyncio.sleep(0.1)
        try:
            # Transport #1 connects and opens a PTY session.
            ch1 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            self.assertEqual((await ch1.recv())["type"], "ipc_attached")
            await ch1.send({"type": "open", "agent_id": "a", "session_id": "s"})
            got = await self._recv_until(
                ch1, lambda f: f.get("data") == "hello", ch1)
            self.assertIsNotNone(got)
            self.assertEqual(len(FakePty.instances), 1)
            pty = FakePty.instances[0]

            # Transport #1 disconnects; PTY must survive.
            await ch1.close()
            await asyncio.sleep(0.2)
            self.assertFalse(pty.killed)
            self.assertIn(("a", "s"), service.supervisor.ptys)

            # Produce output while detached; it must be buffered.
            await pty.on_output("while-detached")

            # Transport #2 reconnects and drains the buffered output.
            ch2 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            self.assertEqual((await ch2.recv())["type"], "ipc_attached")
            got2 = await self._recv_until(
                ch2, lambda f: f.get("data") == "while-detached", ch2)
            self.assertIsNotNone(got2)
            self.assertFalse(pty.killed)
            await ch2.close()
        finally:
            service.stop()
            await asyncio.gather(serve_task, return_exceptions=True)
            service.supervisor.shutdown()

    async def test_second_transport_is_refused_while_one_attached(self):
        ensure_secret(self._suffix)
        service = SupervisorService(endpoint=self._endpoint)

        async def serve():
            from connector.ipc import serve_channel
            service._server = await serve_channel(
                service._on_channel, endpoint=self._endpoint,
                user_suffix=self._suffix)
            await service._stop.wait()
            await service._server.close()

        serve_task = asyncio.create_task(serve())
        await asyncio.sleep(0.1)
        try:
            ch1 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            self.assertEqual((await ch1.recv())["type"], "ipc_attached")
            await asyncio.sleep(0.1)
            ch2 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            frame = await asyncio.wait_for(ch2.recv(), timeout=2.0)
            self.assertEqual(frame.get("type"), "ipc_busy")
            await ch1.close()
            await ch2.close()
        finally:
            service.stop()
            await asyncio.gather(serve_task, return_exceptions=True)
            service.supervisor.shutdown()


if __name__ == "__main__":
    unittest.main()
