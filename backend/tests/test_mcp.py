"""H4: MCP connections (agent/mcp_client.py) against a real stdio server (tests/fixtures/mcp_test_server.py)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent import approvals, facts, mcp_client, traces
from agent.tools import registry, risk

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_test_server.py")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_client, "CONFIG", path)
    monkeypatch.setattr(mcp_client, "_servers", {})
    monkeypatch.setattr(mcp_client, "_started_loop", None)
    monkeypatch.setenv("ENZO_TEST_SECRET", "s3cret")
    monkeypatch.setenv("TEST_TOKEN_FOR_MCP", "tok")
    mcp_client.save_config({"testsrv": {"command": sys.executable, "args": [SERVER],
                                        "env": {"MY_TOKEN": "${TEST_TOKEN_FOR_MCP}"}}})
    yield path
    for s in list(mcp_client._servers.values()):
        if s.task:
            s.task.cancel()
    mcp_client._servers.clear()
    registry().reload()


def test_names_schemas_and_results_are_made_safe():
    assert mcp_client.tool_name("My Server", "get-Issue") == "mcp__my_server__get_issue"
    assert len(mcp_client.tool_name("s" * 40, "t" * 40)) == 64
    assert mcp_client._schema({"type": "object", "properties": {"a": {}}, "required": ["a", "ghost"]})["required"] == ["a"]
    assert mcp_client._schema(None) == {"type": "object", "properties": {}, "required": []}
    assert "​" not in mcp_client._desc("x", "hid​den") and mcp_client._desc("x", "y" * 5000).count("y") == mcp_client.MAX_DESC


def test_a_real_server_connects_and_its_tools_follow_enzos_rules(cfg, monkeypatch):
    asked = []

    async def confirm(question, tool):
        asked.append(question)
        return "no"
    monkeypatch.setattr(approvals, "_confirm", confirm)
    monkeypatch.setattr(approvals, "APPROVALS_ON", True)

    async def main():
        await mcp_client.start(wait=60)
        reg = registry()
        names = {n for n in reg.funcs if n.startswith("mcp__testsrv__")}
        assert names == {"mcp__testsrv__add", "mcp__testsrv__write_note", "mcp__testsrv__env_report"}
        schema = next(s for s in reg.schemas if s["function"]["name"] == "mcp__testsrv__add")["function"]
        assert set(schema["parameters"]["properties"]) == {"a", "b"} and schema["description"].startswith("[testsrv")
        traces.start("add two numbers")
        try:
            # read-only: runs at once; its result counts as outside text
            assert await reg.call("mcp__testsrv__add", {"a": 2, "b": 3, "junk": 1}) == "5"
            assert "mcp__testsrv__add" in facts.TAINT_TOOLS and facts.turn_is_tainted()
            assert risk("mcp__testsrv__add") == ("read", {"network"})
            # not marked read-only: asks first, and "no" means it never reaches the server
            assert (await reg.call("mcp__testsrv__write_note", {"text": "hi"})).startswith("[DECLINED]")
            assert asked and "testsrv's write_note" in asked[0]
            assert risk("mcp__testsrv__write_note")[0] == "outbound"
            assert "needs argument" in await reg.call("mcp__testsrv__add", {"a": 1})
            # the server's process sees only safe variables and the ones its config names
            env = (await reg.call("mcp__testsrv__env_report", {})).split(",")
            assert "MY_TOKEN" in env and "ENZO_TEST_SECRET" not in env and "ANTHROPIC_API_KEY" not in env
        finally:
            traces._current.set(None)
        # the local model's short list does not get them (cloud brain only)
        from agent import agent_loop
        _, local = agent_loop.shared_prefix()
        assert not any(t["function"]["name"].startswith("mcp__") for t in local)
        from agent import big_brain
        assert "mcp__testsrv__add" in {t["name"] for t in big_brain._anthropic_tools()}
        assert "testsrv: connected, 3 tools" in mcp_client.status()
    asyncio.run(main())


def test_the_tool_adds_and_removes_servers_only_after_asking(cfg, monkeypatch):
    from agent import tools
    answers = ["no", "yes", "yes"]

    async def confirm(question, tool):
        return answers.pop(0)
    monkeypatch.setattr(approvals, "_confirm", confirm)
    monkeypatch.setattr(approvals, "APPROVALS_ON", True)
    mcp_client.save_config({})

    async def main():
        add = dict(action="add", name="Test Srv", command=f'"{sys.executable}" "{SERVER}"')
        assert (await tools.mcp_servers(**add)).startswith("[DECLINED]") and not mcp_client.config()
        out = await tools.mcp_servers(**add)
        assert out.startswith("[OK]: Connected test_srv: 3 tools"), out
        assert "mcp__test_srv__add" in registry().funcs
        assert "http" in await tools.mcp_servers(action="add", name="web", url="http://plain.example/mcp")
        assert (await tools.mcp_servers(action="remove", name="test_srv")).startswith("[OK]")
        assert "mcp__test_srv__add" not in registry().funcs and not mcp_client.config()
    asyncio.run(main())
