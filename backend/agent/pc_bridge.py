"""
pc_bridge.py — lets Enzo (running on the NAS) use the user's PC.

The program `pc_helper.py` runs on the PC and connects OUT to /pc on the Enzo server
(nothing on the PC is exposed). When a PC-only tool is called (lock, open app, media keys,
files, read-only shell, screenshot), ToolRegistry first asks the user for approval as usual
and then forwards the call here; the helper runs it and sends the result back.

Only one helper is connected at a time (a new connection replaces the old one).
"""

from __future__ import annotations

import asyncio
import itertools
import os

from fastapi import WebSocket
from loguru import logger

# Tools that act on the PC. The helper has its own copy of this whitelist.
PC_TOOLS = {
    "run_command", "list_files", "read_file", "write_file", "create_folder",
    "open_app", "open_url", "media_control", "lock_pc",
    "screenshot",  # raw PNG (base64) for look_at_screen
    "claude_code", # Claude Code jobs (agent/code_jobs.py; pc_code_jobs.py on the PC). Not a tool name: the code_job tool asks first
}

OFFLINE = ("[ERROR]: Your PC isn't connected to me right now. "
           "Start the helper on the PC (python pc_helper.py) and try again.")

_helper: WebSocket | None = None
_pending: dict[int, asyncio.Future] = {}
_ids = itertools.count(1)


def online() -> bool:
    return _helper is not None


def should_route(tool_name: str) -> bool:
    """True when this tool must run on the PC helper instead of on this machine."""
    from shell_executor import IS_WINDOWS
    mode = os.getenv("PC_HELPER_MODE", "auto").lower()  # auto | on | off
    if mode == "off" or tool_name not in PC_TOOLS:
        return False
    return mode == "on" or not IS_WINDOWS


def _fail_pending(reason: str) -> None:
    for fut in _pending.values():
        if not fut.done():
            fut.set_result({"ok": False, "result": reason})
    _pending.clear()


async def serve(ws: WebSocket) -> None:
    """Keep one helper connection and deliver its results to waiting callers."""
    global _helper
    if _helper is not None:
        try:
            await _helper.close(code=4000, reason="replaced by a new helper")
        except Exception:
            pass
        _fail_pending("[ERROR]: The PC helper reconnected; try again.")
    _helper = ws
    logger.info("🖥️  PC helper connected")
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "result":
                fut = _pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif isinstance(msg, dict) and msg.get("type") == "job_done" and isinstance(msg.get("job"), dict):
                from agent import code_jobs
                if await code_jobs.finished(msg["job"]):
                    await ws.send_json({"type": "job_ack", "id": msg["job"].get("id")})
    except Exception:
        pass
    finally:
        if _helper is ws:
            _helper = None
            _fail_pending(OFFLINE)
        logger.info("🖥️  PC helper disconnected")


async def call(tool: str, args: dict | None = None, timeout: float = 60) -> str:
    """Run `tool` on the PC and return its text result (or an [ERROR] string)."""
    ws = _helper
    if ws is None:
        return OFFLINE
    if tool not in PC_TOOLS:
        return f"[ERROR]: '{tool}' is not a PC tool."
    call_id = next(_ids)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[call_id] = fut
    try:
        await ws.send_json({"type": "call", "id": call_id, "tool": tool, "args": args or {}})
        msg = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return f"[ERROR]: The PC didn't answer within {timeout:g} s."
    except Exception as e:
        return f"[ERROR]: Couldn't reach the PC: {e}"
    finally:
        _pending.pop(call_id, None)
    return str(msg.get("result", ""))


# "Quick action" steps ({"action": ..., "input": ...}) that touch the PC, and the tool each one maps to.
_STEP_TO_TOOL = {
    "list_files":    ("list_files", "folder"),
    "read_file":     ("read_file", "path"),
    "open_app":      ("open_app", "name"),
    "open_url":      ("open_url", "url"),
    "create_folder": ("create_folder", "path"),
    "shell":         ("run_command", "command"),
}


async def execute_step(step: dict, local_execute) -> str:
    """Run a quick-intent step: on the PC helper when Enzo runs on the NAS, else locally."""
    action = step.get("action", "")
    value = str(step.get("input", "") or "")
    if action == "write_file" and should_route("write_file"):
        path, _, content = value.partition("|")
        return await call("write_file", {"path": path, "content": content})
    mapped = _STEP_TO_TOOL.get(action)
    if mapped and should_route(mapped[0]):
        return await call(mapped[0], {mapped[1]: value})
    return await local_execute(step)
