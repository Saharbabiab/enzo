"""
pc_helper.py — run this on your Windows PC so Enzo (on the NAS) can use it.

    python pc_helper.py                 # first run asks for your Enzo password once
    python pc_helper.py --url https://enzo.example.com:4444

It connects OUT to Enzo (nothing on this PC is opened to the network), keeps the link up
(auto-reconnect) and runs only the whitelisted PC tools when Enzo asks — lock, open apps,
media keys, files, read-only commands, screenshots. Risky ones were already approved by you
in the Enzo app before they get here. Stop it with Ctrl+C; "python pc_helper.py --forget"
deletes the saved login, and revoking it works too: python sessions.py list / revoke <id>.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TOKEN_FILE = ROOT / "data" / "pc_helper.json"
DEFAULT_URL = os.getenv("ENZO_URL", "https://enzo.example.com:4444")

# What this helper is willing to run (the server has the same list).
ALLOWED = {
    "run_command", "list_files", "read_file", "write_file", "create_folder",
    "open_app", "open_url", "media_control", "lock_pc",
    "claude_code",  # Claude Code jobs in the work folder (pc_code_jobs.py)
}


def _load_token() -> str:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("token", "")
    except (OSError, ValueError):
        return ""


def _save_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")


def _login(base: str) -> str:
    if sys.stdin is None or not sys.stdin.isatty():
        sys.exit("Saved login is missing or was revoked. Run 'python pc_helper.py' once in a terminal to sign in.")
    import httpx
    print(f"Signing this PC in to {base}")
    password = getpass.getpass("Enzo password: ")
    r = httpx.post(f"{base}/api/login", timeout=20, json={
        "password": password, "role": "helper",
        "device": f"PC helper ({platform.node()})",
    })
    if r.status_code != 200:
        sys.exit(f"Login failed ({r.status_code}): {r.text}")
    token = r.json()["token"]
    _save_token(token)
    print("Saved. You won't be asked again.")
    return token


def _screenshot_b64() -> str:
    from agent import tools
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = tools._capture_screen(path)
        if not r.get("success") or not os.path.getsize(path):
            return f"[ERROR]: Couldn't capture the screen: {r.get('error')}"
        return base64.b64encode(Path(path).read_bytes()).decode()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _run_tool(tool: str, args: dict) -> str:
    if tool == "screenshot":
        return await asyncio.to_thread(_screenshot_b64)
    if tool not in ALLOWED:
        return f"[ERROR]: The PC helper doesn't run '{tool}'."
    if tool == "claude_code":
        import pc_code_jobs
        return await asyncio.to_thread(pc_code_jobs.handle, args or {})
    from agent import tools
    func = getattr(tools, tool, None)
    if func is None:
        return f"[ERROR]: Unknown tool '{tool}'."
    import inspect
    clean = {k: v for k, v in (args or {}).items() if k in inspect.signature(func).parameters}
    out = await func(**clean) if inspect.iscoroutinefunction(func) else await asyncio.to_thread(func, **clean)
    return "" if out is None else str(out)


async def _session(base: str, token: str) -> str:
    """One connection. Returns 'auth' if the server rejected our token, else 'closed'."""
    import websockets
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/pc"
    async with websockets.connect(ws_url, max_size=32 * 2**20, ping_interval=20, ping_timeout=30) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except websockets.ConnectionClosed as e:
            # Only an explicit "unauthorized" (4401) means the login is bad. Anything else
            # (server restarting, proxy hiccup) must just be retried - never ask for a password.
            if e.rcvd is not None and e.rcvd.code == 4401:
                return "auth"
            raise
        if hello.get("type") != "auth_ok":
            return "auth"
        print("Connected. Enzo can use this PC now. (Ctrl+C to stop)")

        async def handle(msg: dict) -> None:
            tool = msg.get("tool", "")
            print(f"  -> {tool}")
            try:
                result, ok = await asyncio.wait_for(_run_tool(tool, msg.get("args") or {}), 55), True
            except Exception as e:  # noqa: BLE001 - report every failure back to Enzo
                result, ok = f"[ERROR]: {tool} failed on the PC: {e}", False
            await ws.send(json.dumps({"type": "result", "id": msg.get("id"), "ok": ok, "result": result}))

        async def report_jobs() -> None:
            """Finished code jobs go to Enzo until it acknowledges them (they survive a reconnect)."""
            import pc_code_jobs
            while True:
                for r in await asyncio.to_thread(pc_code_jobs.unsent):
                    await ws.send(json.dumps({"type": "job_done", "job": r}))
                await asyncio.sleep(10)

        tasks: set[asyncio.Task] = {asyncio.create_task(report_jobs())}
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "job_ack":
                    import pc_code_jobs
                    pc_code_jobs.ack(str(msg.get("id", "")))
                elif msg.get("type") == "call":
                    t = asyncio.create_task(handle(msg))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            for t in tasks:
                t.cancel()
        if ws.close_code == 4000:
            return "replaced"
    return "closed"


async def main_async(base: str) -> None:
    token = _load_token() or _login(base)
    delay = 2
    while True:
        try:
            outcome = await _session(base, token)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network errors: just retry
            print(f"Connection problem ({type(e).__name__}); retrying in {delay}s...")
            outcome = "error"
        if outcome == "replaced":
            print("Another PC helper connected to Enzo - this one is stopping.")
            return
        if outcome == "auth":
            print("Enzo didn't accept the saved login (revoked or expired).")
            token = _login(base)
            delay = 2
            continue
        delay = 2 if outcome == "closed" else min(delay * 2, 60)
        await asyncio.sleep(delay)


def main() -> None:
    ap = argparse.ArgumentParser(description="Let Enzo on the NAS use this PC.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Enzo address (default {DEFAULT_URL})")
    ap.add_argument("--forget", action="store_true", help="delete the saved login and exit")
    a = ap.parse_args()
    if a.forget:
        TOKEN_FILE.unlink(missing_ok=True)
        print("Saved login deleted.")
        return
    if platform.system() != "Windows":
        sys.exit("This helper is for the Windows PC (it uses PowerShell tools).")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if sys.stdout is None:  # started without a console (pythonw / at Windows login)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(TOKEN_FILE.parent / "pc_helper.log", "a", buffering=1, encoding="utf-8")
    try:
        asyncio.run(main_async(a.url.rstrip("/")))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
