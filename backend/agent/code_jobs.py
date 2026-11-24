"""
agent/code_jobs.py - Enzo hands coding jobs to Claude Code on the user's PC (the code_job tool).

Flow: code_job(start) asks him -> the job is queued here (data/code_jobs.json) -> sent to pc_helper.py as soon as
the PC is connected (pc_code_jobs.py runs Claude Code on a git branch of the project) -> the helper reports back
("job_done" over the /pc link, resent until acknowledged) -> he is told the result -> code_job(apply) asks him again
and merges the branch, or discard throws it away. Nothing changes in his project until he approves apply, and
"Always" never covers these questions (he asked to be asked every time).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from pathlib import Path

from loguru import logger

FILE = Path(__file__).parent.parent / "data" / "code_jobs.json"
KEEP = 50
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _store(jobs: dict) -> None:
    keep = dict(sorted(jobs.items(), key=lambda kv: kv[1].get("created", 0))[-KEEP:])
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def jobs() -> dict:
    with _lock:
        return _load()


def get(job_id: str) -> dict | None:
    return jobs().get(str(job_id).strip())


def _update(job_id: str, **changes) -> dict | None:
    with _lock:
        all_ = _load()
        if job_id not in all_:
            return None
        all_[job_id].update(changes)
        _store(all_)
        return all_[job_id]


def add(folder: str, task: str) -> dict:
    job = {"id": secrets.token_hex(4), "folder": folder.strip(), "task": task.strip(), "state": "queued",
           "created": time.time()}
    with _lock:
        all_ = _load()
        all_[job["id"]] = job
        _store(all_)
    return job


async def send_queued() -> None:
    """Send queued jobs to the PC (one at a time; the PC says BUSY while one runs). Called every minute and on start."""
    from agent import pc_bridge
    if not pc_bridge.online():
        return
    for job in sorted((j for j in jobs().values() if j["state"] == "queued"), key=lambda j: j["created"]):
        out = await pc_bridge.call("claude_code", {"action": "start", "id": job["id"], "folder": job["folder"],
                                                "task": job["task"]}, timeout=60)
        if out.startswith("[OK]"):
            _update(job["id"], state="running", sent=time.time())
        elif out.startswith("[BUSY]") or "isn't connected" in out or "didn't answer" in out:
            return                                          # try again next minute
        else:
            _update(job["id"], state="failed", error=out.removeprefix("[ERROR]: "))
            await _tell(f"Sir, I could not start the code job in {job['folder']}: {out.removeprefix('[ERROR]: ')}")


async def _tell(text: str) -> None:
    try:
        from scheduler import notify
        await notify(text)
    except Exception as e:
        logger.debug(f"code job notify: {e}")


def message(job: dict) -> str:
    """What he is told when a job finishes."""
    if job["state"] == "failed":
        return f"Sir, the code job in {job['folder']} failed: {job.get('error', 'no reason given')}"
    if not job.get("changed"):
        return f"Sir, Claude Code looked at {job['folder']} and changed nothing. It said: {job.get('summary', '')[:500]}"
    files = job.get("diffstat", "").splitlines()
    return (f"Sir, the code job in {job['folder']} is done. {job.get('summary', '')[:600]}\n"
            f"Changes: {files[-1].strip() if files else 'see the diff'}. Nothing is applied yet: say apply job "
            f"{job['id']} to keep it, or discard job {job['id']}.")


async def finished(result: dict) -> bool:
    """The helper's report (sent again until acknowledged, so a repeat is ignored). True = acknowledge it."""
    job_id = str(result.get("id", ""))
    job = get(job_id)
    if job is None:
        return True                                         # not ours (or long gone): just let the helper drop it
    if job["state"] in ("done", "failed", "applied", "discarded"):
        return True
    fields = {k: result.get(k) for k in ("summary", "diffstat", "diff", "changed", "branch", "error", "cost_usd")
              if result.get(k) is not None}
    job = _update(job_id, state="failed" if result.get("state") == "failed" else "done", finished=time.time(), **fields)
    logger.info(f"🧑‍💻 Code job {job_id} {job['state']} ({job['folder']})")
    asyncio.get_running_loop().create_task(_tell(message(job)))
    asyncio.get_running_loop().create_task(send_queued())
    return True


def describe(limit: int = 6) -> str:
    items = sorted(jobs().values(), key=lambda j: j["created"], reverse=True)[:limit]
    if not items:
        return "No code jobs yet."
    lines = []
    for j in items:
        line = f"- job {j['id']} in {j['folder']}: {j['state']} - {j['task'][:120]}"
        if j["state"] == "done":
            line += f"\n  result: {j.get('summary', '')[:300]}" + (f"\n  changes: {j.get('diffstat', '').splitlines()[-1].strip()}"
                                                                    if j.get("diffstat") else " (no changes)")
        if j["state"] == "failed":
            line += f"\n  error: {j.get('error', '')[:300]}"
        lines.append(line)
    return "\n".join(lines)


def describe_one(j: dict) -> str:
    out = [f"{j['state']} in {j['folder']}: {j['task'][:300]}"]
    if j.get("summary"):
        out.append(f"Result: {j['summary'][:1500]}")
    if j.get("error"):
        out.append(f"Error: {j['error'][:500]}")
    if j.get("diff"):
        out.append(f"Changes:\n{j.get('diffstat', '')}\n{j['diff'][:3000]}")
    return "\n".join(out)


def mark(job_id: str, state: str) -> None:
    _update(job_id, state=state, closed=time.time())
