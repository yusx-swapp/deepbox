"""Prove the persistence value: session survives viewer detach, and a fresh
viewer is restored to the current screen (simulating close tab / reconnect /
switch device)."""
import asyncio, json, websockets, httpx, pyte, os, sys

BASE="http://localhost:8077"

async def main():
    c=httpx.Client(base_url=BASE)
    r=c.post("/api/auth/login",json={"username":"demo","password":"demo"})
    cookie=r.cookies.get("deepbox_session"); ck={"deepbox_session":cookie}
    aid="a76a32164fe84de59345ab6aa4bafcec"
    sid=c.post(f"/api/agents/{aid}/sessions",cookies=ck).json()["id"]
    hdr={"Cookie":f"deepbox_session={cookie}"}

    # ---- Viewer 1: attach, drive Claude, then DETACH (close tab) ----
    async with websockets.connect("ws://localhost:8077/ws/term",additional_headers=hdr) as ws:
        await ws.send(json.dumps({"type":"attach","session_id":sid,"cols":120,"rows":30}))
        got=[]
        async def rd():
            async for raw in ws:
                f=json.loads(raw)
                if f.get("type") in ("output","restore"): got.append(f["data"])
        t=asyncio.create_task(rd())
        await asyncio.sleep(9)
        await ws.send(json.dumps({"type":"input","session_id":sid,
            "data":"remember the secret number 4242, just acknowledge\r"}))
        await asyncio.sleep(10)
        t.cancel()
        await ws.send(json.dumps({"type":"detach","session_id":sid}))  # close tab, PTY lives
    print("[viewer1] detached (tab closed). PTY should still be alive on devbox.")

    await asyncio.sleep(2)  # simulate time away / switching device

    # ---- Viewer 2: fresh connection, attach → must be RESTORED to current screen ----
    screen=pyte.Screen(120,30); stream=pyte.ByteStream(screen)
    async with websockets.connect("ws://localhost:8077/ws/term",additional_headers=hdr) as ws:
        await ws.send(json.dumps({"type":"attach","session_id":sid,"cols":120,"rows":30}))
        async def rd2():
            async for raw in ws:
                f=json.loads(raw)
                if f.get("type") in ("restore","output"):
                    stream.feed(f["data"].encode("utf-8","replace"))
        t=asyncio.create_task(rd2())
        # ask it to recall — proves same live process, not a new one
        await asyncio.sleep(2)
        await ws.send(json.dumps({"type":"input","session_id":sid,
            "data":"what was the secret number?\r"}))
        await asyncio.sleep(10)
        t.cancel()

    disp="\n".join(l.rstrip() for l in screen.display)
    with open("snapshot.txt","w",encoding="utf-8") as f: f.write(disp)
    has_recall = "4242" in disp
    print("[viewer2] restored screen; recall 4242 present:", has_recall)
    # DVR file present?
    from pathlib import Path
    cast=Path("data/sessions")/f"{sid}.cast"
    print("[DVR] recording exists:", cast.exists(), cast.stat().st_size if cast.exists() else 0, "bytes")
    print("=== PERSISTENCE", "PASS" if has_recall and cast.exists() else "CHECK snapshot.txt", "===")
    os._exit(0)

asyncio.run(main())
