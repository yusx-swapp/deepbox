"""Cut 4 unit tests: IPC framing and the LoopbackChannel seam."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

import connector.ipc as ipc
from connector.ipc import (
    AuthError,
    FrameTooLarge,
    IS_WIN,
    LoopbackChannel,
    MAX_FRAME,
    cleanup_stale_endpoint,
    connect_channel,
    decode_frame,
    default_endpoint,
    encode_frame,
    ensure_secret,
    serve_channel,
)


class FrameCodecTests(unittest.TestCase):
    def test_roundtrip_preserves_frame(self):
        frame = {"type": "output", "agent_id": "a", "session_id": "s", "data": "hi\r\n"}
        self.assertEqual(decode_frame(encode_frame(frame)), frame)

    def test_frames_are_newline_delimited(self):
        line = encode_frame({"type": "heartbeat"})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)

    def test_default_endpoint_is_per_user(self):
        a = default_endpoint("alice")
        b = default_endpoint("bob")
        self.assertNotEqual(a, b)
        self.assertIn("alice", a)

    @unittest.skipUnless(IS_WIN, "Windows named-pipe presence probe")
    def test_missing_named_pipe_is_not_present(self):
        endpoint = default_endpoint("deepbox-test-pipe-that-does-not-exist")
        self.assertFalse(ipc.endpoint_exists(endpoint))


class FrameBoundsTests(unittest.TestCase):
    def test_encode_rejects_oversize_frame(self):
        # Payload guaranteed to blow past the 1 MiB cap once JSON-encoded.
        huge = {"type": "output", "data": "x" * (MAX_FRAME + 16)}
        with self.assertRaises(FrameTooLarge):
            encode_frame(huge)

    def test_decode_rejects_oversize_line(self):
        line = b"{" + b"a" * (MAX_FRAME + 1) + b"}\n"
        with self.assertRaises(FrameTooLarge):
            decode_frame(line)

    def test_decode_rejects_non_object_json(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            decode_frame(b"[]\n")

    def test_boundary_frame_is_accepted(self):
        # A modest frame well under the cap must round-trip cleanly.
        frame = {"type": "output", "data": "y" * 1000}
        self.assertEqual(decode_frame(encode_frame(frame)), frame)


class EndpointCleanupTests(unittest.TestCase):
    def test_custom_stale_unix_endpoint_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = os.path.join(root, "custom.sock")
            with open(endpoint, "wb") as handle:
                handle.write(b"stale")
            probe = mock.Mock()
            probe.connect.side_effect = ConnectionRefusedError
            with mock.patch("connector.ipc.IS_WIN", False), mock.patch(
                    "connector.ipc.socket.AF_UNIX", create=True), mock.patch(
                    "connector.ipc.socket.socket", return_value=probe):
                self.assertTrue(cleanup_stale_endpoint(endpoint=endpoint))
            probe.settimeout.assert_called_once_with(0.2)
            probe.close.assert_called_once_with()
            self.assertFalse(os.path.exists(endpoint))

    def test_ambiguous_probe_failure_does_not_remove_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            endpoint = os.path.join(root, "live.sock")
            with open(endpoint, "wb") as handle:
                handle.write(b"keep")
            probe = mock.Mock()
            probe.connect.side_effect = PermissionError
            with mock.patch("connector.ipc.IS_WIN", False), mock.patch(
                    "connector.ipc.socket.AF_UNIX", create=True), mock.patch(
                    "connector.ipc.socket.socket", return_value=probe):
                self.assertFalse(cleanup_stale_endpoint(endpoint=endpoint))
            self.assertTrue(os.path.exists(endpoint))


class _IpcServerFixture:
    """Spin up a real serve_channel echo server on a private endpoint."""

    def __init__(self, suffix, endpoint):
        self.suffix = suffix
        self.endpoint = endpoint
        self.server = None
        self.seen = []
        self.connections = 0

    async def _handler(self, channel):
        self.connections += 1
        while True:
            frame = await channel.recv()
            if frame is None:
                break
            self.seen.append(frame)
            await channel.send({"type": "echo", "data": frame.get("data")})
        await channel.close()

    async def start(self):
        self.server = await serve_channel(self._handler, endpoint=self.endpoint,
                                          user_suffix=self.suffix)

    async def stop(self):
        if self.server is not None:
            await self.server.close()


class LocalIpcTransportTests(unittest.IsolatedAsyncioTestCase):
    """Real OS-backed IPC: named pipe on Windows, Unix socket on POSIX."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="deepbox-ipc-test-")
        uniq = str(os.getpid()) + str(id(self))
        self._suffix = "iputest" + uniq
        os.environ["XDG_RUNTIME_DIR"] = self._tmp
        if IS_WIN:
            self._endpoint = r"\\.\pipe\deepbox-iputest-" + uniq
        else:
            self._endpoint = os.path.join(self._tmp, "sessiond-test.sock")

    def tearDown(self):
        os.environ.pop("XDG_RUNTIME_DIR", None)

    async def test_auth_handshake_and_reconnect(self):
        ensure_secret(self._suffix)
        fixture = _IpcServerFixture(self._suffix, self._endpoint)
        await fixture.start()
        try:
            # First transport connects, talks, and disconnects.
            ch1 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            await ch1.send({"type": "input", "data": "one"})
            reply = await ch1.recv()
            self.assertEqual(reply["data"], "one")
            await ch1.close()
            await asyncio.sleep(0.1)

            # Second transport reconnects to the SAME server endpoint.
            ch2 = await connect_channel(endpoint=self._endpoint,
                                        user_suffix=self._suffix)
            await ch2.send({"type": "input", "data": "two"})
            reply2 = await ch2.recv()
            self.assertEqual(reply2["data"], "two")
            await ch2.close()
            await asyncio.sleep(0.1)

            self.assertGreaterEqual(fixture.connections, 2)
        finally:
            await fixture.stop()

    @unittest.skipIf(IS_WIN, "POSIX socket 0600 permission check")
    async def test_unix_socket_mode_is_0600(self):
        import stat
        ensure_secret(self._suffix)
        fixture = _IpcServerFixture(self._suffix, self._endpoint)
        await fixture.start()
        try:
            mode = stat.S_IMODE(os.stat(self._endpoint).st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            await fixture.stop()

    async def test_wrong_secret_is_rejected(self):
        ensure_secret(self._suffix)
        fixture = _IpcServerFixture(self._suffix, self._endpoint)
        await fixture.start()
        try:
            # Corrupt the client's view of the secret by using a different
            # suffix that has its own (mismatched) secret file.
            other = self._suffix + "x"
            ensure_secret(other)
            with self.assertRaises((AuthError, ConnectionError, OSError)):
                ch = await connect_channel(endpoint=self._endpoint,
                                           user_suffix=other)
                # If connect returns, the server must drop us before any echo.
                await ch.send({"type": "input", "data": "nope"})
                self.assertIsNone(await ch.recv())
        finally:
            await fixture.stop()


class _MemoryWriter:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass


class HandshakeSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_rejects_bare_welcome_without_server_proof(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame({
            "type": "ipc_hello", "v": ipc.IPC_HANDSHAKE_VERSION,
            "nonce": "server-nonce",
        }))
        reader.feed_data(encode_frame({"type": "ipc_welcome", "ok": True}))
        reader.feed_eof()

        with self.assertRaisesRegex(AuthError, "mutual authentication"):
            await ipc._client_handshake(reader, _MemoryWriter(), "secret")

    async def test_silent_peer_hits_handshake_timeout(self):
        reader = asyncio.StreamReader()
        with mock.patch.object(ipc, "HANDSHAKE_TIMEOUT", 0.01):
            with self.assertRaisesRegex(AuthError, "timed out"):
                await ipc._handshake_read(reader, "closed")


class LoopbackChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_bidirectional_ordered_delivery(self):
        sup, tx = LoopbackChannel.pair()
        await sup.send({"type": "output", "data": "1"})
        await sup.send({"type": "output", "data": "2"})
        first = await tx.recv()
        second = await tx.recv()
        self.assertEqual(first["data"], "1")
        self.assertEqual(second["data"], "2")

    async def test_reverse_direction(self):
        sup, tx = LoopbackChannel.pair()
        await tx.send({"type": "input", "data": "x"})
        got = await sup.recv()
        self.assertEqual(got["type"], "input")

    async def test_close_yields_eof(self):
        sup, tx = LoopbackChannel.pair()
        await sup.close()
        self.assertIsNone(await tx.recv())
        with self.assertRaises(ConnectionError):
            await sup.send({"type": "x"})


if __name__ == "__main__":
    unittest.main()
