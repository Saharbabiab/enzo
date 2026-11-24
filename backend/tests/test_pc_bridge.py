"""PC helper bridge: forwards PC tools from the NAS to pc_helper.py."""

import asyncio

import pytest

from agent import pc_bridge


class FakeWS:
    """Stands in for the helper's WebSocket."""
    def __init__(self):
        self.sent, self.inbox = [], asyncio.Queue()

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self, code=1000, reason=""):
        await self.inbox.put(RuntimeError("closed"))


@pytest.fixture(autouse=True)
def clean_bridge():
    pc_bridge._helper = None
    pc_bridge._pending.clear()
    yield
    pc_bridge._helper = None
    pc_bridge._pending.clear()


def test_offline_returns_friendly_error():
    out = asyncio.run(pc_bridge.call("lock_pc"))
    assert out.startswith("[ERROR]") and "pc_helper.py" in out


def test_roundtrip_and_disconnect():
    async def scenario():
        ws = FakeWS()
        server = asyncio.create_task(pc_bridge.serve(ws))
        await asyncio.sleep(0)
        assert pc_bridge.online()

        task = asyncio.create_task(pc_bridge.call("open_app", {"name": "spotify"}))
        await asyncio.sleep(0.05)
        req = ws.sent[0]
        assert req["tool"] == "open_app" and req["args"] == {"name": "spotify"}
        await ws.inbox.put({"type": "result", "id": req["id"], "ok": True, "result": "[OK]: opened"})
        assert await task == "[OK]: opened"

        # a call that is pending when the helper drops gets a clear error
        pending = asyncio.create_task(pc_bridge.call("lock_pc"))
        await asyncio.sleep(0.05)
        await ws.inbox.put(RuntimeError("gone"))
        assert (await pending).startswith("[ERROR]")
        await server
        assert not pc_bridge.online()

    asyncio.run(scenario())


def test_timeout_and_unknown_tool():
    async def scenario():
        ws = FakeWS()
        server = asyncio.create_task(pc_bridge.serve(ws))
        await asyncio.sleep(0)
        assert "didn't answer" in await pc_bridge.call("lock_pc", timeout=0.1)
        assert "not a PC tool" in await pc_bridge.call("format_disk")
        await ws.inbox.put(RuntimeError("bye"))
        await server

    asyncio.run(scenario())


def test_should_route(monkeypatch):
    import shell_executor
    monkeypatch.setattr(shell_executor, "IS_WINDOWS", False)
    assert pc_bridge.should_route("lock_pc") and pc_bridge.should_route("screenshot")
    assert not pc_bridge.should_route("calendar_events")          # not a PC tool
    monkeypatch.setenv("PC_HELPER_MODE", "off")
    assert not pc_bridge.should_route("lock_pc")
    monkeypatch.setenv("PC_HELPER_MODE", "auto")
    monkeypatch.setattr(shell_executor, "IS_WINDOWS", True)       # on the PC itself: run locally
    assert not pc_bridge.should_route("lock_pc")


def test_registry_forwards_after_approval(monkeypatch):
    from agent import tools, approvals
    calls = []

    async def fake_request(name, args):
        calls.append(("approval", name))
        return True

    async def fake_call(tool, args=None, timeout=60):
        calls.append(("pc", tool, args))
        return "[OK]: PC locked."

    monkeypatch.setattr(approvals, "request", fake_request)
    monkeypatch.setattr(pc_bridge, "should_route", lambda name: name in pc_bridge.PC_TOOLS)
    monkeypatch.setattr(pc_bridge, "call", fake_call)
    out = asyncio.run(tools.registry().call("lock_pc", {}))
    assert out == "[OK]: PC locked."
    assert calls == [("approval", "lock_pc"), ("pc", "lock_pc", {})]   # approval FIRST, then the PC


def test_helper_whitelist_matches_server():
    import pc_helper
    assert pc_helper.ALLOWED == pc_bridge.PC_TOOLS - {"screenshot"}


def test_websocket_routes_come_before_the_web_app_mount():
    """A Mount('/') swallows every route registered after it (this broke /pc once)."""
    import server
    from starlette.routing import Mount
    routes = server.app.router.routes
    mount_at = next((i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == ""), len(routes))
    for path in ("/ws", "/pc", "/api/login", "/api/health"):
        assert next(i for i, r in enumerate(routes) if getattr(r, "path", None) == path) < mount_at, path


def test_quick_steps_route_to_pc_on_the_nas(monkeypatch):
    seen = []

    async def fake_call(tool, args=None, timeout=60):
        seen.append((tool, args))
        return "[OK]"

    async def local(step):
        seen.append(("local", step["action"]))
        return "local"

    monkeypatch.setattr(pc_bridge, "call", fake_call)
    monkeypatch.setattr(pc_bridge, "should_route", lambda name: True)
    assert asyncio.run(pc_bridge.execute_step({"action": "list_files", "input": "desktop"}, local)) == "[OK]"
    assert asyncio.run(pc_bridge.execute_step({"action": "open_app", "input": "spotify"}, local)) == "[OK]"
    assert asyncio.run(pc_bridge.execute_step({"action": "write_file", "input": "a.txt|hello|there"}, local)) == "[OK]"
    assert seen == [("list_files", {"folder": "desktop"}), ("open_app", {"name": "spotify"}),
                    ("write_file", {"path": "a.txt", "content": "hello|there"})]
    # steps that aren't PC actions stay local; on the PC itself everything stays local
    assert asyncio.run(pc_bridge.execute_step({"action": "web_search", "input": "x"}, local)) == "local"
    monkeypatch.setattr(pc_bridge, "should_route", lambda name: False)
    assert asyncio.run(pc_bridge.execute_step({"action": "list_files", "input": "d"}, local)) == "local"
