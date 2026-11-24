"""
agent/mcp_client.py - MCP connections (Hermes plan H4, docs/HERMES_PLAN.md): ready-made servers become Enzo tools.

Config: data/mcp_servers.json (The user's, gitignored), or the mcp_servers tool (asks first):
    {"servers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}},
                 "docs":   {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_KEY}"}},
                 ...       optional: "include": [tool names], "exclude": [...], "enabled": false, "local": true}}
    "${NAME}" is read from Enzo's environment (.env); keys never go through the chat.

Each tool appears as mcp__<server>__<tool> (64 characters at most). Safer than Hermes, which trusts every server
by default:
  - a tool the server does not mark read-only (readOnlyHint) asks Yes / Always / No before every call
    (approvals.RISKY), and asks again in a turn that read outside text, even after "Always";
  - every MCP result counts as outside text (facts.TAINT_TOOLS): it can't quietly trigger mail, files or code;
  - a stdio server gets a minimal environment (no .env secrets) plus only the variables its config names;
  - no sampling: a server can't spend Enzo's model budget;
  - results are cut like any tool result; descriptions are stripped of invisible characters and capped.
MCP tools go to the cloud brain; the local model's short list only gets servers marked "local": true.

One connection per server, kept open in its own task (reconnects with backoff up to 5 minutes). Calls wait up to
CALL_TIMEOUT seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from pathlib import Path

import anyio
from loguru import logger

CONFIG = Path(os.getenv("MCP_CONFIG") or Path(__file__).resolve().parent.parent / "data" / "mcp_servers.json")
CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "60"))
CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "30"))
MAX_DESC = 900
SAFE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TERM", "NODE_PATH", "PROGRAMFILES",
            "PROGRAMDATA")
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f]")


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("servers") or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"MCP: {CONFIG.name} unreadable: {e}")
        return {}


def save_config(servers: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"servers": servers}, indent=1), encoding="utf-8")


def _fill(value: str) -> str:
    """"${NAME}" -> Enzo's environment variable NAME (empty if it is not set)."""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.getenv(m.group(1), ""), str(value))


def child_env(cfg: dict) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    env.update({str(k): _fill(v) for k, v in (cfg.get("env") or {}).items()})
    return env


def tool_name(server: str, tool: str) -> str:
    clean = lambda s: re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
    return f"mcp__{clean(server)}__{clean(tool)}"[:64]


def _schema(raw) -> dict:
    s = dict(raw or {})
    if s.get("type") != "object":
        s = {"type": "object", "properties": {}}
    s["properties"] = s.get("properties") or {}
    s["required"] = [r for r in s.get("required") or [] if r in s["properties"]]
    return s


def _desc(server: str, text: str) -> str:
    text = " ".join(_INVISIBLE.sub("", text or "").split())
    return f"[{server} (outside service)] " + (text[:MAX_DESC] or "No description.")


def result_text(res) -> str:
    """A CallToolResult as plain text for the model."""
    parts = []
    for c in getattr(res, "content", None) or []:
        kind = getattr(c, "type", "")
        if kind == "text":
            parts.append(_INVISIBLE.sub("", c.text))
        elif kind == "resource":
            r = getattr(c, "resource", None)
            parts.append(_INVISIBLE.sub("", getattr(r, "text", "") or f"[resource {getattr(r, 'uri', '')}]"))
        else:
            parts.append(f"[{kind or 'content'} not shown]")
    if not parts and getattr(res, "structured_content", None) is not None:
        parts.append(json.dumps(res.structured_content, ensure_ascii=False)[:20000])
    text = "\n".join(p for p in parts if p).strip() or "(no output)"
    return f"[ERROR]: {text}" if getattr(res, "is_error", False) else text


class Server:
    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.tools: dict[str, dict] = {}          # enzo name -> {"tool", "description", "schema", "read_only"}
        self.ready = asyncio.Event()
        self.error = ""
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None

    def target(self):
        from mcp import StdioServerParameters
        if self.cfg.get("url"):
            return _fill(self.cfg["url"])           # headers: see _client()
        return StdioServerParameters(command=self.cfg["command"], args=[_fill(a) for a in self.cfg.get("args") or []],
                                     env=child_env(self.cfg), cwd=self.cfg.get("cwd"))

    def _client(self):
        from mcp import Client
        if self.cfg.get("url") and self.cfg.get("headers"):
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            http = httpx2.AsyncClient(headers={k: _fill(v) for k, v in self.cfg["headers"].items()}, timeout=CALL_TIMEOUT)
            return Client(streamable_http_client(_fill(self.cfg["url"]), http_client=http), read_timeout_seconds=CALL_TIMEOUT)
        target = self.target()
        if isinstance(target, str):
            return Client(target, read_timeout_seconds=CALL_TIMEOUT)
        from mcp.client.stdio import stdio_client
        # The server's own messages go to data/logs/mcp_<name>.log (Enzo's stderr may not be a real file, e.g. in tests).
        log_dir = CONFIG.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if getattr(self, "_errlog", None) is None:
            self._errlog = open(log_dir / f"mcp_{self.name}.log", "a", encoding="utf-8")
        return Client(stdio_client(target, errlog=self._errlog), read_timeout_seconds=CALL_TIMEOUT)

    def _accept(self, tool) -> bool:
        inc, exc = self.cfg.get("include"), self.cfg.get("exclude") or []
        return (not inc or tool.name in inc) and tool.name not in exc

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self._client() as c:
                    with anyio.fail_after(CONNECT_TIMEOUT):     # not asyncio.wait_for: a new task breaks MCP's anyio scopes
                        listed = await c.list_tools()
                    self.tools = {}
                    for t in listed.tools:
                        if self._accept(t):
                            ann = getattr(t, "annotations", None)
                            self.tools[tool_name(self.name, t.name)] = {
                                "tool": t.name, "description": _desc(self.name, t.description or t.title or ""),
                                "schema": _schema(t.input_schema), "read_only": bool(getattr(ann, "read_only_hint", False))}
                    self.error, backoff = "", 1.0
                    self.ready.set()
                    logger.info(f"🔌 MCP {self.name}: {len(self.tools)} tools")
                    while True:
                        tool, args, fut = await self.queue.get()
                        if fut.done():
                            continue
                        try:
                            with anyio.fail_after(CALL_TIMEOUT):
                                res = await c.call_tool(tool, args)
                            fut.set_result(result_text(res))
                        except TimeoutError:
                            fut.set_result(f"[ERROR]: {self.name} did not answer within {CALL_TIMEOUT:.0f} s.")
                        except Exception as e:
                            fut.set_result(f"[ERROR]: {self.name} failed: {e.__class__.__name__}: {str(e)[:200]}")
                            if not isinstance(e, ValueError):
                                raise               # the connection may be gone: reconnect
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ready.clear()
                inner = e
                while getattr(inner, "exceptions", None):          # anyio wraps the real error in groups
                    inner = inner.exceptions[0]
                self.error = f"{inner.__class__.__name__}: {str(inner)[:160]}"
                logger.warning(f"MCP {self.name} disconnected ({self.error}); retrying in {backoff:.0f} s")
                await asyncio.sleep(backoff)
                backoff = min(300.0, backoff * 2)

    async def call(self, tool: str, args: dict) -> str:
        if not self.ready.is_set():
            return f"[ERROR]: {self.name} is not connected right now ({self.error or 'starting'})."
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((tool, args, fut))
        try:
            return await asyncio.wait_for(fut, CALL_TIMEOUT + 5)
        except asyncio.TimeoutError:
            return f"[ERROR]: {self.name} did not answer within {CALL_TIMEOUT:.0f} s."


_servers: dict[str, Server] = {}
_started_loop = None


async def start(wait: float = CONNECT_TIMEOUT) -> None:
    """Connect to every enabled server (once per event loop), wait up to `wait` seconds, then add their tools."""
    global _started_loop
    loop = asyncio.get_running_loop()
    if _started_loop is loop:
        return
    _started_loop = loop
    await sync(wait)


async def sync(wait: float = CONNECT_TIMEOUT) -> None:
    """Start servers that are new in the config, stop removed ones, refresh the tool registry."""
    want = {n: c for n, c in config().items() if c.get("enabled", True) and (c.get("command") or c.get("url"))}
    for name in list(_servers):
        if name not in want or _servers[name].cfg != want[name]:
            s = _servers.pop(name)
            if s.task:
                s.task.cancel()
    new = []
    for name, cfg in want.items():
        if name not in _servers:
            s = Server(name, cfg)
            s.task = asyncio.create_task(s.run(), name=f"mcp-{name}")
            _servers[name] = s
            new.append(s)
    if new:
        try:
            await asyncio.wait_for(asyncio.gather(*(s.ready.wait() for s in new)), wait)
        except asyncio.TimeoutError:
            logger.warning(f"MCP: not connected yet: {', '.join(s.name for s in new if not s.ready.is_set())}")
    from agent.tools import registry
    registry().reload()


def tools() -> dict[str, dict]:
    """Every connected MCP tool: enzo name -> info (+ "server")."""
    out = {}
    for s in _servers.values():
        for n, t in s.tools.items():
            out[n] = {**t, "server": s.name, "local": bool(s.cfg.get("local"))}
    return out


def hidden_from_local() -> set[str]:
    return {n for n, t in tools().items() if not t["local"]} | {"mcp_servers"}   # setting up connections: a cloud-brain job


def make_func(name: str, info: dict):
    """An async tool function for the registry, with the server's JSON schema."""
    async def call_mcp(**kwargs) -> str:
        s = _servers.get(info["server"])
        if not s:
            return f"[ERROR]: The {info['server']} connection was removed."
        return await s.call(info["tool"], kwargs)
    call_mcp.__name__ = name
    call_mcp.__doc__ = info["description"]
    call_mcp._schema = {"type": "function", "function": {"name": name, "description": info["description"],
                                                         "parameters": info["schema"]}}
    call_mcp._mcp = info
    return call_mcp


def status() -> str:
    cfg = config()
    if not cfg:
        return "No MCP connections are set up."
    lines = []
    for name, c in cfg.items():
        s = _servers.get(name)
        state = ("off" if not c.get("enabled", True) else "connected" if s and s.ready.is_set()
                 else f"not connected ({s.error or 'starting'})" if s else "not started")
        lines.append(f"- {name}: {state}, {len(s.tools) if s else 0} tools" + (f" ({', '.join(t['tool'] for t in s.tools.values())[:300]})" if s and s.tools else ""))
    return "\n".join(lines)


def parse_command(command: str) -> tuple[str, list[str]]:
    parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p     # Windows mode keeps the quotes
             for p in shlex.split(command, posix=os.name != "nt")]
    return (parts[0], parts[1:]) if parts else ("", [])
