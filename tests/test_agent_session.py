"""Offline tests for the structured agent session (Cut 10).

No real ``claude`` process and no token spend: we feed synthetic Claude
``stream-json`` transcripts through the pure translator and through a fake
subprocess to exercise :class:`connector.agent_session.StructuredAgentSession`.
"""
import asyncio
import json

import pytest

from connector import agent_session as A


def _evs(obj):
    return A.translate_claude_event(obj)


def test_system_init_becomes_status():
    out = _evs({"type": "system", "subtype": "init",
                "session_id": "s1", "model": "sonnet"})
    assert out == [{"ev": A.EV_STATUS, "subtype": "init",
                    "session_id": "s1", "model": "sonnet"}]


def test_text_delta_becomes_message_delta():
    out = _evs({"type": "stream_event",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": "Hel"}}})
    assert out == [{"ev": A.EV_MESSAGE_DELTA, "text": "Hel"}]


def test_assistant_text_and_tool_use():
    out = _evs({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}},
    ]}})
    assert out[0] == {"ev": A.EV_MESSAGE, "text": "hi", "final": True}
    assert out[1]["ev"] == A.EV_TOOL_CALL
    assert out[1]["tool"] == "Bash"
    assert out[1]["tool_id"] == "t1"
    assert out[1]["input"] == {"command": "ls"}


def test_tool_result_from_user_message():
    out = _evs({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
         "content": [{"type": "text", "text": "file1\nfile2"}]},
    ]}})
    assert out == [{"ev": A.EV_TOOL_RESULT, "tool_id": "t1",
                    "is_error": False, "content": "file1\nfile2"}]


def test_result_becomes_turn_end():
    out = _evs({"type": "result", "subtype": "success", "is_error": False,
                "total_cost_usd": 0.01, "usage": {"input_tokens": 10},
                "result": "done"})
    assert out[0]["ev"] == A.EV_TURN_END
    assert out[0]["cost_usd"] == 0.01
    assert out[0]["result"] == "done"


def test_permission_ask():
    out = _evs({"type": "control_request", "request_id": "r1",
                "request": {"subtype": "can_use_tool", "tool_name": "Write",
                            "input": {"path": "/x"}}})
    assert out == [{"ev": A.EV_PERMISSION_ASK, "request_id": "r1",
                    "tool": "Write", "input": {"path": "/x"}}]


def test_unknown_shape_is_empty():
    assert _evs({"type": "mystery"}) == []
    assert _evs({}) == []


def test_encode_user_message_roundtrip():
    line = A.encode_user_message("hello world")
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["content"][0]["text"] == "hello world"


def test_encode_permission_response():
    allow = json.loads(A.encode_permission_response("r1", True))
    assert allow["response"]["request_id"] == "r1"
    assert allow["response"]["response"]["behavior"] == "allow"
    deny = json.loads(A.encode_permission_response("r1", False))
    assert deny["response"]["response"]["behavior"] == "deny"


# --- Fake-subprocess integration ------------------------------------------

class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeStdin:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, out_lines):
        self.stdout = _FakeStream(out_lines)
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin()
        self.returncode = None
        self._killed = False

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self._killed = True
        self.returncode = -9


async def _co_session_emits():
    transcript = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s1"}).encode() + b"\n",
        json.dumps({"type": "stream_event",
                    "event": {"type": "content_block_delta",
                              "delta": {"type": "text_delta",
                                        "text": "Hi"}}}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success",
                    "is_error": False}).encode() + b"\n",
    ]
    got = []
    exited = []

    async def on_output(s):
        got.append(json.loads(s))

    async def on_exit(code):
        exited.append(code)

    sess = A.StructuredAgentSession(
        ["claude"], None, on_output, on_exit,
        spawn=lambda: _spawn_fake(transcript))
    await sess.start()
    # Let the reader task drain the transcript.
    for _ in range(50):
        if exited:
            break
        await asyncio.sleep(0.01)
    evs = [e["ev"] for e in got]
    assert A.EV_STATUS in evs
    assert A.EV_MESSAGE_DELTA in evs
    assert A.EV_TURN_END in evs
    assert exited == [0]

def test_session_emits_canonical_events_from_transcript_sync():
    asyncio.run(_co_session_emits())


async def _spawn_fake(lines):
    return _FakeProc(lines)


async def _co_write_encodes():
    sess = A.StructuredAgentSession(
        ["claude"], None, _noop, _noop,
        spawn=lambda: _spawn_fake([]))
    await sess.start()
    # Ensure alive before write (reader hasn't hit EOF wait yet in this tick).
    sess._alive = True
    sess.write("do the thing")
    assert b"do the thing" in sess._proc.stdin.buf

def test_write_encodes_user_turn_sync():
    asyncio.run(_co_write_encodes())


async def _co_respond_perm():
    sess = A.StructuredAgentSession(
        ["claude"], None, _noop, _noop,
        spawn=lambda: _spawn_fake([]))
    await sess.start()
    sess._alive = True
    sess.respond_permission("r1", True)
    assert b"control_response" in sess._proc.stdin.buf
    assert b"allow" in sess._proc.stdin.buf

def test_respond_permission_writes_control_sync():
    asyncio.run(_co_respond_perm())


async def _noop(*a, **k):
    pass

