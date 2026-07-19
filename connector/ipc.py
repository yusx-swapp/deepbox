r"""Local IPC abstraction between the session supervisor (sessiond) and the
WebSocket transport.

Cut 4 goal: give the supervisor and transport a seam they can talk across so
that they can run either in one process or as two separate OS processes. Two
concrete channels implement the shared Channel contract: LoopbackChannel for
the single-process default (in-memory queues), and StreamChannel over a real
OS transport for the two-process split.

The two-process transport is implemented; real ConPTY + Windows-service
validation remains user-gated (see the acceptance gate in
docs/implementation.md):

- Windows: a named pipe at ``\\.\pipe\deepbox-sessiond-<user>``. The
  supervisor is the pipe server; each transport instance is a client.
- POSIX: a Unix domain socket at ``$XDG_RUNTIME_DIR/deepbox/sessiond-<user>.sock``
  (or ``~/.deepbox/deepbox/sessiond-<user>.sock``) created with 0600 so only the
  owning user can connect.

Both transports share the same wire contract (encode_frame / decode_frame,
bounded by MAX_FRAME, never pickle) and a local current-user auth handshake
keyed on a 0600 per-user secret file. The supervisor and transport depend only
on the Channel interface.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import sys
import time
from typing import Awaitable, Callable, Protocol

IS_WIN = sys.platform == "win32"

# Maximum size (in bytes) of a single encoded frame, including the trailing
# newline. Frames are never pickled; only length-bounded JSON crosses the wire.
# 1 MiB comfortably fits PTY output chunks while bounding memory per read.
MAX_FRAME = 1 << 20

# Protocol tag for the local IPC auth handshake.
IPC_HANDSHAKE_VERSION = 1
HANDSHAKE_TIMEOUT = 5.0


class FrameTooLarge(ValueError):
    """Raised when an encoded/received frame exceeds :data:`MAX_FRAME`."""


class AuthError(ConnectionError):
    """Raised when the local IPC auth handshake fails."""


# Wire framing ---------------------------------------------------------------


def encode_frame(frame: dict) -> bytes:
    """Serialize one control/data frame as length-bounded newline JSON.

    Raises :class:`FrameTooLarge` rather than emitting an oversize frame so a
    single runaway payload can never be pushed onto the wire.
    """
    data = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_FRAME:
        raise FrameTooLarge(f"frame of {len(data)} bytes exceeds MAX_FRAME={MAX_FRAME}")
    return data


def decode_frame(line: bytes) -> dict:
    """Parse one newline-delimited JSON object, enforcing the size bound."""
    if len(line) > MAX_FRAME:
        raise FrameTooLarge(f"frame of {len(line)} bytes exceeds MAX_FRAME={MAX_FRAME}")
    frame = json.loads(line.decode("utf-8"))
    if not isinstance(frame, dict):
        raise ValueError("IPC frame must be a JSON object")
    return frame


# Endpoint addressing --------------------------------------------------------


def default_endpoint(user_suffix: str | None = None) -> str:
    """Return the platform-appropriate IPC endpoint address.

    This only *computes* the address; it does not bind or connect. Both the
    Windows named-pipe and POSIX Unix-socket paths are namespaced per user so
    multiple accounts on one host never collide.
    """
    suffix = user_suffix or _default_user_suffix()
    if IS_WIN:
        return rf"\\.\pipe\deepbox-sessiond-{suffix}"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = runtime_dir if runtime_dir else os.path.join(os.path.expanduser("~"), ".deepbox")
    return os.path.join(base, "deepbox", f"sessiond-{suffix}.sock")


def _default_user_suffix() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return "".join(ch for ch in value if ch.isalnum()) or "default"
    return "default"


def secret_path(user_suffix: str | None = None) -> str:
    """Return the path of the per-user shared-secret file for the handshake.

    The secret authorizes a local transport to attach to the local supervisor.
    It lives beside the endpoint in a user-private directory. On POSIX the file
    is created with ``0600``; on Windows it relies on the user profile ACL.
    """
    suffix = user_suffix or _default_user_suffix()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = runtime_dir if runtime_dir else os.path.join(os.path.expanduser("~"), ".deepbox")
    return os.path.join(base, "deepbox", f"sessiond-{suffix}.secret")


def _endpoint_dir(endpoint: str) -> str | None:
    """Directory that must exist to bind ``endpoint`` (None for Windows pipes)."""
    if IS_WIN and endpoint.startswith("\\\\"):
        return None
    return os.path.dirname(endpoint)


def ensure_secret(user_suffix: str | None = None) -> str:
    """Create (if needed) and return the per-user shared secret, 0600 on POSIX."""
    path = secret_path(user_suffix)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if not IS_WIN:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    existing = read_secret(user_suffix)
    if existing:
        return existing
    token = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another sessiond won creation. Wait briefly for its small atomic write
        # instead of truncating or rotating the live process's credential.
        for _ in range(20):
            existing = read_secret(user_suffix)
            if existing:
                return existing
            time.sleep(0.01)
        raise RuntimeError(f"IPC secret exists but is empty: {path}")
    try:
        payload = token.encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    if not IS_WIN:
        os.chmod(path, 0o600)
    return token


def read_secret(user_suffix: str | None = None) -> str | None:
    """Read the shared secret written by a running supervisor, or None."""
    path = secret_path(user_suffix)
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


# Auth handshake -------------------------------------------------------------
#
# The mutual handshake proves both peers can read the per-user secret file. It
# uses the same framed JSON wire, with role-separated MAC inputs:
#   server -> client: ipc_hello(server_nonce)
#   client -> server: ipc_auth(client_nonce, MAC("client", server_nonce))
#   server -> client: ipc_welcome(MAC("server", client_nonce))
# Mutual proof prevents a process squatting the named-pipe path from receiving
# terminal input. Every handshake read is bounded to avoid stalled local peers.


def _mac(secret: str, role: str, nonce: str) -> str:
    message = f"{role}:{nonce}".encode("ascii")
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


async def _handshake_read(reader: asyncio.StreamReader, closed_message: str) -> dict:
    try:
        line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
    except TimeoutError as exc:
        raise AuthError("IPC handshake timed out") from exc
    if not line:
        raise AuthError(closed_message)
    return decode_frame(line)


async def _server_handshake(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter, secret: str) -> None:
    server_nonce = secrets.token_hex(16)
    writer.write(encode_frame({"type": "ipc_hello", "v": IPC_HANDSHAKE_VERSION,
                               "nonce": server_nonce}))
    await writer.drain()
    frame = await _handshake_read(reader, "peer closed during handshake")
    if frame.get("type") != "ipc_auth" or frame.get("v") != IPC_HANDSHAKE_VERSION:
        raise AuthError("bad auth frame")
    expected = _mac(secret, "client", server_nonce)
    if not hmac.compare_digest(frame.get("mac", ""), expected):
        writer.write(encode_frame({"type": "ipc_welcome", "ok": False}))
        await writer.drain()
        raise AuthError("secret mismatch")
    client_nonce = frame.get("nonce", "")
    if not isinstance(client_nonce, str) or not client_nonce:
        raise AuthError("missing client nonce")
    writer.write(encode_frame({"type": "ipc_welcome", "ok": True,
                               "mac": _mac(secret, "server", client_nonce)}))
    await writer.drain()


async def _client_handshake(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter, secret: str) -> None:
    hello = await _handshake_read(reader, "no hello from supervisor")
    if hello.get("type") != "ipc_hello" or hello.get("v") != IPC_HANDSHAKE_VERSION:
        raise AuthError("bad hello frame")
    server_nonce = hello.get("nonce", "")
    if not isinstance(server_nonce, str) or not server_nonce:
        raise AuthError("missing server nonce")
    client_nonce = secrets.token_hex(16)
    writer.write(encode_frame({"type": "ipc_auth", "v": IPC_HANDSHAKE_VERSION,
                               "nonce": client_nonce,
                               "mac": _mac(secret, "client", server_nonce)}))
    await writer.drain()
    welcome = await _handshake_read(reader, "supervisor closed during handshake")
    expected = _mac(secret, "server", client_nonce)
    if (welcome.get("type") != "ipc_welcome" or not welcome.get("ok")
            or not hmac.compare_digest(welcome.get("mac", ""), expected)):
        raise AuthError("supervisor failed mutual authentication")


# Channel interface ----------------------------------------------------------


class Channel(Protocol):
    """Bidirectional frame channel used by supervisor and transport.

    A real OS-backed channel (named pipe / Unix socket) and the in-memory
    :class:`LoopbackChannel` both satisfy this Protocol, so the supervisor and
    transport never import platform code directly.
    """

    async def send(self, frame: dict) -> None: ...

    async def recv(self) -> dict | None: ...

    async def close(self) -> None: ...


class LoopbackChannel:
    """In-process channel: two ends share a pair of asyncio queues.

    ``LoopbackChannel.pair()`` returns the (supervisor_end, transport_end) tuple.
    Everything a real pipe/socket needs to do -- ordered delivery, backpressure,
    close/EOF -- is modeled here so the split has real test coverage today.
    """

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue):
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False

    @classmethod
    def pair(cls) -> tuple["LoopbackChannel", "LoopbackChannel"]:
        a: asyncio.Queue = asyncio.Queue()
        b: asyncio.Queue = asyncio.Queue()
        # supervisor writes to a / reads from b; transport writes to b / reads a
        supervisor_end = cls(inbox=b, outbox=a)
        transport_end = cls(inbox=a, outbox=b)
        return supervisor_end, transport_end

    async def send(self, frame: dict) -> None:
        if self._closed:
            raise ConnectionError("channel closed")
        # Round-trip through the wire codec so tests exercise real framing.
        await self._outbox.put(encode_frame(frame))

    async def recv(self) -> dict | None:
        if self._closed:
            return None
        line = await self._inbox.get()
        if line is None:
            return None
        return decode_frame(line)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._outbox.put(None)


class StreamChannel:
    """A :class:`Channel` backed by an asyncio reader/writer stream pair.

    Used for both the Windows named-pipe and POSIX Unix-socket transports; the
    only platform-specific part is how the stream is created (see
    :func:`serve_channel` / :func:`connect_channel`). Framing is the shared
    length-bounded newline JSON codec.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._send_lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        if self._closed:
            raise ConnectionError("channel closed")
        data = encode_frame(frame)
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def recv(self) -> dict | None:
        if self._closed:
            return None
        try:
            line = await self._reader.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return None
        except asyncio.LimitOverrunError as exc:
            raise FrameTooLarge(str(exc)) from exc
        except (ConnectionError, OSError):
            return None
        if not line:
            return None
        return decode_frame(line)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# OS-backed serve/connect ----------------------------------------------------


def _new_reader(loop: asyncio.AbstractEventLoop) -> asyncio.StreamReader:
    return asyncio.StreamReader(limit=MAX_FRAME, loop=loop)


async def serve_channel(
        on_channel: Callable[[StreamChannel], Awaitable[None]],
        *, endpoint: str | None = None, user_suffix: str | None = None):
    """Bind the local IPC endpoint and invoke ``on_channel`` per connection.

    Enforces the auth handshake before ``on_channel`` runs. Returns an object
    with an ``async close()`` used to stop serving. Only local, current-user
    peers can complete the handshake (named-pipe/Unix-socket + secret file).
    """
    address = endpoint or default_endpoint(user_suffix)
    secret = ensure_secret(user_suffix)
    directory = _endpoint_dir(address)
    if directory:
        os.makedirs(directory, exist_ok=True)
        if not IS_WIN:
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    async def _wrap(reader, writer):
        try:
            await _server_handshake(reader, writer, secret)
            await on_channel(StreamChannel(reader, writer))
        except (AuthError, FrameTooLarge, ValueError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    if IS_WIN:
        return await _serve_pipe(address, _wrap)
    return await _serve_unix(address, _wrap)


async def _serve_unix(address: str, handler):
    # The caller may remove a verified-stale socket first. Never unlink here:
    # doing so would let a second sessiond steal a live supervisor's endpoint.
    server = await asyncio.start_unix_server(handler, path=address)
    os.chmod(address, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    bound = os.stat(address)
    bound_identity = (bound.st_dev, bound.st_ino)

    class _UnixServer:
        async def close(self):
            server.close()
            await server.wait_closed()
            try:
                current = os.stat(address)
                if (current.st_dev, current.st_ino) == bound_identity:
                    os.unlink(address)
            except OSError:
                pass

    return _UnixServer()


async def _serve_pipe(address: str, handler):
    loop = asyncio.get_event_loop()

    def factory():
        reader = _new_reader(loop)
        return asyncio.StreamReaderProtocol(reader, handler, loop=loop)

    servers = await loop.start_serving_pipe(factory, address)

    class _PipeServer:
        async def close(self):
            for server in servers:
                server.close()

    return _PipeServer()


async def connect_channel(*, endpoint: str | None = None,
                          user_suffix: str | None = None) -> StreamChannel:
    """Connect to a running supervisor and complete the auth handshake."""
    address = endpoint or default_endpoint(user_suffix)
    secret = read_secret(user_suffix)
    if not secret:
        raise AuthError(f"no supervisor secret found (is sessiond running?): "
                        f"{secret_path(user_suffix)}")
    if IS_WIN:
        reader, writer = await _connect_pipe(address)
    else:
        reader, writer = await asyncio.open_unix_connection(path=address)
    try:
        await _client_handshake(reader, writer, secret)
    except BaseException:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        raise
    return StreamChannel(reader, writer)


async def _connect_pipe(address: str):
    loop = asyncio.get_event_loop()
    handle = await loop._proactor.connect_pipe(address)
    reader = _new_reader(loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    waiter = loop.create_future()
    transport = loop._make_duplex_pipe_transport(handle, protocol, waiter)
    await waiter
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


def endpoint_exists(endpoint: str | None = None, user_suffix: str | None = None) -> bool:
    """Best-effort check whether a supervisor endpoint appears present."""
    address = endpoint or default_endpoint(user_suffix)
    if IS_WIN and address.startswith("\\\\"):
        # os.path.exists() is not a reliable named-pipe probe. WaitNamedPipeW
        # distinguishes a missing path from an existing but currently busy pipe.
        try:
            import ctypes
            wait_named_pipe = ctypes.WinDLL("kernel32", use_last_error=True).WaitNamedPipeW
            wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            wait_named_pipe.restype = ctypes.c_int
            if wait_named_pipe(address, 0):
                return True
            return ctypes.get_last_error() in (121, 231)  # timeout / pipe busy
        except (AttributeError, OSError):
            return False
    return os.path.exists(address)


def cleanup_stale_endpoint(user_suffix: str | None = None, *,
                           endpoint: str | None = None) -> bool:
    """Remove a stale POSIX socket without disturbing a live supervisor.

    The auth secret is retained: rotating it before a new bind succeeds can
    lock transports out of an already-running sessiond. Windows named pipes
    disappear with their owning process and need no cleanup.
    """
    address = endpoint or default_endpoint(user_suffix)
    if IS_WIN or not os.path.exists(address):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(address)
    except (ConnectionRefusedError, FileNotFoundError):
        try:
            os.unlink(address)
            return True
        except OSError:
            return False
    except OSError:
        # Timeout, access denial, and unexpected probe failures do not prove the
        # endpoint stale. Prefer a bind failure over stealing a live endpoint.
        return False
    finally:
        probe.close()
    return False
