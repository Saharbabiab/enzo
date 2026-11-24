"""
agent/sandbox.py - Enzo's side of the code sandbox (Hermes plan H5; the container is sandbox/, set up with
sandbox/compose.example.yml).

run_python sends code here and nowhere else: no sandbox configured (SANDBOX_URL + SANDBOX_TOKEN) means no code runs.
The tool itself is only offered when ENABLE_PYTHON_TOOL=true, and every run still asks the user (approvals.RISKY).
Hermes, by comparison, runs code on the host by default and skips approval inside its containers while their
network stays on; Enzo's sandbox has no network at all.
"""

from __future__ import annotations

import os

import httpx

TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "20"))


def configured() -> bool:
    return bool(os.getenv("SANDBOX_URL", "").strip() and os.getenv("SANDBOX_TOKEN", "").strip())


async def run(code: str, timeout: int = TIMEOUT) -> str:
    """The text for the model: what the code printed, its errors, or why nothing ran."""
    if not configured():
        return ("[ERROR]: No sandbox is set up, so no code can run (Enzo never runs code outside it). "
                "Tell the user the sandbox container needs to be added first.")
    try:
        async with httpx.AsyncClient(timeout=timeout + 15) as c:
            r = await c.post(os.environ["SANDBOX_URL"].rstrip("/") + "/run", json={"code": code, "timeout": timeout},
                             headers={"X-Sandbox-Token": os.environ["SANDBOX_TOKEN"]})
        if r.status_code != 200:
            return f"[ERROR]: The sandbox refused the code ({r.status_code}: {r.text[:200]})."
        d = r.json()
    except Exception as e:
        return f"[ERROR]: The sandbox is not reachable ({e.__class__.__name__})."
    out = (d.get("stdout") or "").strip()
    err = (d.get("stderr") or "").strip()
    if d.get("timed_out"):
        return f"[ERROR]: The code ran too long and was stopped after {timeout} s.\n{out}".strip()
    if not d.get("ok"):
        return f"[ERROR]: The code failed (exit {d.get('exit')}):\n{err[-3000:]}\n{out[-1000:]}".strip()
    return (out or "(the code printed nothing)") + (f"\n[stderr]: {err[-1000:]}" if err else "")
