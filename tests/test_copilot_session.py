"""Offline tests for the Copilot CLI structured adapter (per-turn mode).

No real ``copilot`` process and no token spend: synthetic
``--output-format json`` transcripts are fed through the pure translator and
through a fake subprocess to exercise the per-turn drive mode of
:class:`connector.agent_session.StructuredAgentSession`.
"""
import asyncio
import json

from connector import agent_session as A


def _evs(obj):
    return A.translate_copilot_event(obj)


def test_message_delta_becomes_message_delta():
    out = _evs({"type": "assistant.message_delta",
                "data": {"deltaContent": "Hel"}})
    assert out == [{"ev": A.EV_MESSAGE_DELTA, "text": "Hel"}]


def test_empty_delta_dropped():
    assert _evs({"type": "assistant.message_delta", "data": {}}) == []


def test_assistant_message_final_and_tool_requests():
    out = _evs({"type": "assistant.message", "data": {
        "content": "done",
        "toolRequests": [
            {"id": "t1", "name": "shell", "arguments": {"cmd": "ls"}},
        ],
    }})
    assert out[0] == {"ev": A.EV_MESSAGE, "final": True, "text": "done"}
    assert out[1]["ev"] == A.EV_TOOL_CALL
    assert out[1]["tool"] == "shell"
    assert out[1]["tool_id"] == "t1"
    assert out[1]["input"] == {"cmd": "ls"}


def test_tool_completed_becomes_tool_result():
    out = _evs({"type": "tool.execution_completed", "data": {
        "id": "t1", "isError": False,
        "result": [{"type": "text", "text": "ok"}],
    }})
    assert out == [{"ev": A.EV_TOOL_RESULT, "tool_id": "t1",
                    "is_error": False, "content": "ok"}]


def test_turn_end_and_result():
    assert _evs({"type": "assistant.turn_end", "data": {}})[0]["ev"] \
        == A.EV_TURN_END
    assert _evs({"type": "result", "data": {}})[0]["ev"] == A.EV_TURN_END


def test_session_events_become_status():
    out = _evs({"type": "session.tools_loaded", "data": {"count": 3}})
    assert out == [{"ev": A.EV_STATUS, "subtype": "session.tools_loaded"}]


def test_user_message_and_unknown_dropped():
    assert _evs({"type": "user.message", "data": {"content": "hi"}}) == []
    assert _evs({"type": "mystery"}) == []
    assert _evs({}) == []


def test_registry_maps_copilot_translator():
    assert A.TRANSLATORS["copilot-cli-structured"] is A.translate_copilot_event


# --- Fake-subprocess per-turn integration ---------------------------------

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

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _turn_transcript(text):
    return [
        json.dumps({"type": "session.status",
                    "data": {"state": "ready"}}).encode() + b"\n",
        json.dumps({"type": "assistant.message_delta",
                    "data": {"deltaContent": text}}).encode() + b"\n",
        json.dumps({"type": "assistant.message",
                    "data": {"content": text}}).encode() + b"\n",
        json.dumps({"type": "result", "data": {}}).encode() + b"\n",
    ]


async def _co_per_turn():
    got = []
    exited = []
    spawned_prompts = []

    async def on_output(s):
        got.append(json.loads(s))

    async def on_exit(code):
        exited.append(code)

    async def fake_spawn(prompt=None):
        spawned_prompts.append(prompt)
        return _FakeProc(_turn_transcript("Hi"))

    sess = A.StructuredAgentSession(
        ["copilot"], None, on_output, on_exit,
        spawn=fake_spawn, translate=A.translate_copilot_event,
        per_turn=True, prompt_argv=["-p"])
    await sess.start()
    # Session is alive with no process yet (per-turn defers spawn).
    assert sess.is_alive() is True
    assert sess._proc is None

    sess.write("hello")
    for _ in range(100):
        if len(got) >= 4:
            break
        await asyncio.sleep(0.01)

    # on_exit must NOT fire for a per-turn process finishing; the session stays.
    assert exited == []
    assert sess.is_alive() is True
    assert spawned_prompts == ["hello"]
    evs = [e["ev"] for e in got]
    assert A.EV_MESSAGE_DELTA in evs
    assert A.EV_TURN_END in evs


def test_per_turn_session_drives_one_process_per_turn_sync():
    asyncio.run(_co_per_turn())
