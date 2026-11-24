"""
agent/approvals.py — "Ask before acting" for risky tools.

Before a risky tool runs (writing files, running code, ...), Enzo asks the user
and waits for an answer:
  • Web app: Yes / Always / No buttons, or just say/type "yes" / "no".
  • "Always" approves that tool for the rest of the session.
  • No answer within APPROVAL_TIMEOUT seconds → treated as "no".
  • No way to ask (no UI attached) → denied, unless APPROVALS=off.

The channel (web socket, Telegram, ...) registers how to ask via
session().confirm = async (question: str) -> "yes" | "always" | "no".
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from agent.context import session

APPROVALS_ON = os.getenv("APPROVALS", "on").lower() != "off"
APPROVAL_TIMEOUT = float(os.getenv("APPROVAL_TIMEOUT", "90"))
AUDIT_FILE = Path(os.getenv("AUDIT_FILE") or Path(__file__).resolve().parent.parent / "data" / "audit.jsonl")


def _short(v, n=80) -> str:
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _reply_question(a: dict) -> str:
    who = ""
    try:
        from agent import google_services as g
        who = g.describe_email(str(a.get("message_id", ""))) if g.connected() else ""
    except Exception:
        pass
    return f"Send this reply{' to ' + _short(who, 220) if who else ''}? \"{_short(a.get('body'), 200)}\""


def _skill_question(a: dict) -> str:
    """'Install skill X? It adds: fn1 — first docstring line; fn2 — …'"""
    import ast
    name = a.get("name") or a.get("skill_name") or "?"
    parts = []
    try:
        for node in ast.parse(a.get("code") or "").body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                doc = (ast.get_docstring(node) or "").split("\n")[0]
                parts.append(f"{node.name} — {_short(doc, 70)}" if doc else node.name)
    except SyntaxError:
        pass
    adds = "; ".join(parts) or "new code"
    return (f"Teach myself a new skill '{_short(name, 40)}'? It adds: {adds}. It will run on your PC. "
            "Its tests run first; it goes live only if they pass.")


# tool name → function(args) -> question shown/spoken to the user
RISKY: dict = {
    "write_file":         lambda a: f"Write the file {_short(a.get('path'))}? It will be created or overwritten.",
    "run_python":         lambda a: f"Run this Python code in the sandbox? {_short(a.get('code'), 120)}",
    "generate_and_write": lambda a: f"Create the file {_short(str(a.get('input', '')).split('|')[0])}?",
    "delete_file":        lambda a: f"Delete {_short(a.get('input'))}? This can't be undone.",
    "move_file":          lambda a: f"Move {_short(a.get('input'))}?",
    "create_skill":       lambda a: _skill_question(a),
    "remove_skill":       lambda a: f"Delete the self-written skill or playbook '{_short(a.get('name'))}'?",
    "save_playbook":      lambda a: (f"Save the routine '{_short(a.get('name'), 40)}' ({_short(a.get('when'), 80)}): "
                                     f"{_short(a.get('steps'), 200)}?"),
    "lock_pc":            lambda a: "Lock your PC now?",
    "phone_call":         lambda a: f"Call {_short(a.get('who') or a.get('number'), 40)} from your phone now?",
    "phone_message":      lambda a: (f"Write to {_short(a.get('who') or a.get('number'), 40)} on "
                                     f"{'SMS' if str(a.get('app', '')).lower() in ('sms', 'text') else 'WhatsApp'}: "
                                     f"'{_short(a.get('message'), 160)}' - {'I will send it' if str(a.get('app', '')).lower() not in ('sms', 'text') else 'you will press send'}."),
    "calendar_add_event": lambda a: (f"Add '{_short(a.get('title'), 60)}' to your calendar at "
                                     f"{_short(a.get('start'), 30)} ({a.get('duration_minutes', 60)} min)?"),
    "email_reply":        lambda a: _reply_question(a),
    "email_send":         lambda a: (f"Send an email to {_short(a.get('to'), 60)} — subject "
                                     f"'{_short(a.get('subject'), 60)}': {_short(a.get('body'), 160)}"),
}

_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead", "confirm", "approve", "y",
        "כן", "בטח", "סבבה", "אוקיי", "יאללה", "תעשה את זה", "קדימה", "מאשר", "כן בבקשה"}
_ALWAYS = {"always", "always allow", "yes always", "don't ask again", "dont ask again", "תמיד", "כן תמיד", "אל תשאל שוב"}


def parse_answer(text: str) -> str:
    """Free text ('yes please', 'nope') → 'yes' | 'always' | 'no'."""
    import re
    t = re.sub(r"\s+", " ", re.sub(r"[.,!?;:]", " ", (text or "").lower())).strip()
    if t in _ALWAYS or "always" in t or "תמיד" in t:
        return "always"
    if t in _YES or any(t.startswith(w + " ") for w in ("yes", "yeah", "sure", "ok", "okay", "go", "כן", "בטח", "סבבה")):
        return "yes"
    return "no"


ASKS_ITSELF = {"calendar_delete_event", "calendar_update_event", "mcp_servers", "browser_click", "browser_type", "code_job", "phone_screen"}   # they call ask() once they know what they change
TAINT_NOTE = "I read a web page, an email, a notification or your screen for this request, so I am checking first. "


def _tainted() -> bool:
    from agent import facts
    return facts.turn_is_tainted()


def _audit(tool: str, args: dict, answer: str, tainted: bool, question: str = "") -> None:
    """One record per risky action decision: Mongo "audit" (file data/audit.jsonl if Mongo is down). Never blocks."""
    import json
    import threading
    import time
    from agent import traces
    doc = {"ts": time.time(), "tool": tool, "args": traces.redact(args)[:400], "answer": answer,
           "tainted": tainted, "question": traces.redact(question)[:300], "trace": traces.current().id}

    def write():
        try:
            from shared import memory_store
            if getattr(memory_store, "_ok", False) and os.getenv("AUDIT_STORE", "").lower() != "file":
                from datetime import datetime, timezone
                memory_store.db["audit"].insert_one({**doc, "at": datetime.fromtimestamp(doc["ts"], tz=timezone.utc)})
                return
        except Exception as e:
            logger.debug(f"audit to Mongo failed: {e}")
        try:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"audit not written: {e}")

    threading.Thread(target=write, daemon=True).start()


async def _confirm(question: str, label: str) -> str:
    s = session()
    if s.confirm is None:
        logger.warning(f"🔒 {label} denied — no way to ask the user ({question})")
        return "no (could not ask)"
    try:
        answer = await s.confirm(question)
    except Exception as e:
        logger.warning(f"🔒 Approval failed for {label}: {e}")
        return "no (ask failed)"
    logger.info(f"🔒 Approval for {label}: {answer}")
    return answer


def _mcp_asks(tool: str) -> bool:
    """An MCP tool the server does not mark read-only asks every time (agent/mcp_client.py)."""
    from agent import mcp_client
    info = mcp_client.tools().get(tool)
    return not (info and info["read_only"])


def _mcp_question(tool: str, args: dict) -> str:
    from agent import mcp_client
    info = mcp_client.tools().get(tool) or {}
    return f"Use {info.get('server', 'an outside service')}'s {info.get('tool', tool)} with {_short(args, 160)}?"


async def request(tool: str, args: dict) -> bool:
    """True if the tool may run. Risky tools ask first; in a turn that read web or mail text ("tainted"), any tool
    that can send something out, write files, run code or install also asks, even if the user said "Always" before."""
    from agent.tools import taint_gated
    tainted = taint_gated(tool) and tool not in ASKS_ITSELF and _tainted()
    mcp_asks = tool.startswith("mcp__") and _mcp_asks(tool)
    if not APPROVALS_ON or (tool not in RISKY and not mcp_asks and not tainted):
        return True
    s = session()
    if tool in s.always_allowed and not tainted:
        _audit(tool, args, "always", False)
        return True
    question = (RISKY[tool](args or {}) if tool in RISKY else _mcp_question(tool, args) if tool.startswith("mcp__")
                else f"Run {tool} with {_short(args, 160)}?")
    if tainted:
        question = TAINT_NOTE + question
    answer = await _confirm(question, tool)
    _audit(tool, args, answer, tainted, question)
    if answer == "always":
        s.always_allowed.add(tool)
        return True
    return answer == "yes"


async def ask(question: str, tool: str = "", allow_always: bool = True) -> bool:
    """Ask the user a custom yes/no question (for tools that first work out WHAT they'll do).
    Honors "always" for `tool` (not in a tainted turn; never when allow_always=False) and the APPROVALS=off switch."""
    if not APPROVALS_ON:
        return True
    s = session()
    tainted = _tainted()
    if allow_always and tool and tool in s.always_allowed and not tainted:
        _audit(tool, {}, "always", False, question)
        return True
    if tainted:
        question = TAINT_NOTE + question
    answer = await _confirm(question, tool or "custom")
    _audit(tool or "custom", {}, answer, tainted, question)
    if answer == "always" and tool and allow_always:
        s.always_allowed.add(tool)
    return answer in ("yes", "always")


DECLINED = ("[DECLINED]: The user chose NO when asked for permission, so the action was NOT done. "
            "Nothing failed and nothing is wrong with the request — it was simply declined. "
            "Do not retry it and do not invent a reason. Just confirm briefly that you didn't do it.")
