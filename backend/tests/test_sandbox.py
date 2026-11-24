"""H5: the code sandbox runner (sandbox/runner.py) and Enzo's client (agent/sandbox.py), over real HTTP.
The container limits (no network, read-only, rlimits) only exist on Linux in Docker: checked live on the NAS."""
import asyncio
import importlib.util
import threading
from pathlib import Path

import pytest

from agent import sandbox

spec = importlib.util.spec_from_file_location("sandbox_runner", Path(__file__).parent.parent / "sandbox" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(runner, "TOKEN", "t0ken-for-tests")
    srv = runner.ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SANDBOX_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("SANDBOX_TOKEN", "t0ken-for-tests")
    monkeypatch.setenv("ENZO_SECRET_FOR_TEST", "do-not-leak")
    yield srv
    srv.shutdown()


def test_code_runs_in_the_sandbox_with_an_empty_environment(server):
    out = asyncio.run(sandbox.run("import os\nprint(sum(range(10)))\nprint('ENZO_SECRET_FOR_TEST' in os.environ)"))
    assert out.splitlines() == ["45", "False"]
    assert "ZeroDivisionError" in asyncio.run(sandbox.run("1/0")) and asyncio.run(sandbox.run("1/0")).startswith("[ERROR]")


def test_a_loop_is_stopped_and_output_is_cut(server):
    out = asyncio.run(sandbox.run("while True: pass", timeout=2))
    assert out.startswith("[ERROR]: The code ran too long")
    big = runner.run("print('x' * 100000)")
    assert len(big["stdout"]) < runner.MAX_OUT + 100 and "[...cut...]" in big["stdout"]


def test_no_token_no_sandbox_no_code(server, monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "wrong")
    assert "refused" in asyncio.run(sandbox.run("print(1)"))
    monkeypatch.delenv("SANDBOX_URL")
    assert "No sandbox is set up" in asyncio.run(sandbox.run("print(1)"))


def test_run_python_only_uses_the_sandbox(monkeypatch):
    import inspect
    from agent import tools
    src = inspect.getsource(tools.run_python)
    assert "sandbox.run" in src and "execute(" not in src
    assert "run_python" not in tools.registry().funcs              # ENABLE_PYTHON_TOOL stays false by default
