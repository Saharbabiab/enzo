"""Coding jobs for Claude Code on the PC: pc_code_jobs.py (real git, a fake `claude`) and Enzo's side
(agent/code_jobs.py + the code_job tool: asks every time, queues while the PC is away)."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pc_code_jobs
from agent import code_jobs, pc_bridge
from agent.context import new_session, session

FAKE_CLAUDE = r'''
import json, os, sys
task = sys.argv[sys.argv.index("-p") + 1]
if "LOGIN" in task:
    print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in - Please run /login"}))
    sys.exit(1)
if "fix" in task:
    with open("calc.py", "w") as f:
        f.write("def add(a, b):\n    return a + b\n")
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.01,
                  "result": "Fixed add. secret seen: " + str("ENZO_SECRET_FOR_TEST" in os.environ)}))
'''


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def work(tmp_path, monkeypatch):
    root = tmp_path / "work"
    repo = root / "proj"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    (root / "notes").mkdir()
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    monkeypatch.setattr(pc_code_jobs, "WORK_ROOT", root.resolve())
    monkeypatch.setattr(pc_code_jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pc_code_jobs, "claude_cmd", lambda: [sys.executable, str(fake)])
    monkeypatch.setenv("ENZO_SECRET_FOR_TEST", "do-not-leak")
    return repo


def wait_result(job_id):
    for _ in range(200):
        for r in pc_code_jobs.unsent():
            if r["id"] == job_id:
                return r
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_job_runs_on_a_branch_and_apply_merges_it(work):
    assert pc_code_jobs.folders() == ["proj"]
    assert pc_code_jobs.start("abc123", "proj", "fix add").startswith("[OK]")
    r = wait_result("abc123")
    assert r["state"] == "done" and r["changed"] and "calc.py" in r["diffstat"] and "a + b" in r["diff"]
    assert "secret seen: False" in r["summary"]                       # the child gets a minimal environment
    assert "a - b" in (work / "calc.py").read_text()                  # his checkout is untouched until apply
    assert not (pc_code_jobs.WORK_ROOT / ".enzo_jobs" / "abc123").exists()
    pc_code_jobs.ack("abc123")
    assert pc_code_jobs.unsent() == []
    assert pc_code_jobs.apply("abc123", "proj").startswith("[OK]")
    assert "a + b" in (work / "calc.py").read_text()
    assert pc_code_jobs.apply("abc123", "proj").startswith("[ERROR]")  # the branch is gone


def test_discard_no_changes_and_login_failure(work):
    pc_code_jobs.start("disc0001", "proj", "fix add")
    assert wait_result("disc0001")["changed"]
    assert pc_code_jobs.discard("disc0001", "proj").startswith("[OK]")
    assert "a - b" in (work / "calc.py").read_text()
    pc_code_jobs.start("look0001", "proj", "just look")
    r = wait_result("look0001")
    assert r["state"] == "done" and r["changed"] is False
    pc_code_jobs.start("login001", "proj", "LOGIN please")
    r = wait_result("login001")
    assert r["state"] == "failed" and "setup-token" in r["error"]


def test_only_git_projects_inside_the_work_folder(work):
    assert "outside" in pc_code_jobs.start("bad00001", "../..", "fix add")
    assert "not a git project" in pc_code_jobs.start("bad00002", "notes", "fix add")
    assert "no project" in pc_code_jobs.start("bad00003", "nope", "fix add")
    assert pc_code_jobs.start("../evil", "proj", "fix add").startswith("[ERROR]")


@pytest.fixture
def jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "FILE", tmp_path / "code_jobs.json")
    told = []

    async def tell(text):
        told.append(text)
    monkeypatch.setattr(code_jobs, "_tell", tell)
    pc_bridge._helper = None
    return told


def test_start_asks_every_time_and_queues_while_the_pc_is_away(jobs_file):
    from agent import tools
    new_session()
    asked = []

    async def confirm(q):
        asked.append(q)
        return "always" if len(asked) == 1 else "no"
    session().confirm = confirm
    out = asyncio.run(tools.code_job("start", "proj", "fix the add function"))
    assert "queued" in out and "not connected" in out and "fix the add function" in asked[0]
    out = asyncio.run(tools.code_job("start", "proj", "another job"))     # "Always" did not stick
    assert "[DECLINED]" in out and len(asked) == 2
    assert [j["state"] for j in code_jobs.jobs().values()] == ["queued"]


def test_a_finished_job_is_told_once(jobs_file):
    job = code_jobs.add("proj", "fix add")
    code_jobs.mark(job["id"], "running")
    result = {"id": job["id"], "state": "done", "changed": True, "summary": "Fixed add.",
              "diffstat": " calc.py | 2 +-\n 1 file changed", "diff": "-a - b\n+a + b"}

    async def run():
        assert await code_jobs.finished(result)
        assert await code_jobs.finished(result)                          # the helper resends until acknowledged
        await asyncio.sleep(0)
    asyncio.run(run())
    assert len(jobs_file) == 1 and "apply job " + job["id"] in jobs_file[0] and "1 file changed" in jobs_file[0]
    assert code_jobs.get(job["id"])["state"] == "done"
    assert "a + b" in code_jobs.describe_one(code_jobs.get(job["id"]))


def test_push_sends_the_current_branch_to_origin(work, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(work, "remote", "add", "origin", str(remote))
    out = pc_code_jobs.push("proj")
    assert out.startswith("[OK]") and "main" in out
    assert subprocess.run(["git", "-C", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
    assert "no project" in pc_code_jobs.push("nope")


def test_push_asks_every_time(jobs_file, monkeypatch):
    from agent import tools
    new_session()
    asked, sent = [], []

    async def confirm(q):
        asked.append(q)
        return "always" if len(asked) == 1 else "no"

    async def call(tool, args, timeout=0):
        sent.append(args)
        return "[OK]: Pushed main of proj to origin."
    session().confirm = confirm
    monkeypatch.setattr(pc_bridge, "online", lambda: True)
    monkeypatch.setattr(pc_bridge, "call", call)
    job = code_jobs.add("proj", "fix add")
    code_jobs.mark(job["id"], "applied")
    assert asyncio.run(tools.code_job("push")).startswith("[OK]")           # the latest applied job's project
    assert sent == [{"action": "push", "folder": "proj"}] and "proj" in asked[0]
    assert "[DECLINED]" in asyncio.run(tools.code_job("push", "proj"))       # "Always" did not stick
    assert len(sent) == 1
