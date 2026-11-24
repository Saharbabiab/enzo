"""
agent/notifications.py - turn the phone's recent notifications into a short spoken summary.

The app sends {"type": "device_result", "action": "notifications", "items": [...]} only when the user asked
("what did I miss"). No LLM: a plain, predictable summary, newest first, messengers read out in full.
"""

from __future__ import annotations

import re
import time

_CHAT_APPS = ("whatsapp", "telegram", "messages", "signal", "messenger", "sms", "gmail", "mail", "outlook")
_NOISE = re.compile(r"(checking for new messages|new messages?$|\d+ new messages?$)", re.I)


def _clip(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


def summarize(data: dict, now: float | None = None, hours: float = 12) -> str:
    items = data.get("items") or []
    now = now or time.time()
    cutoff = (now - hours * 3600) * 1000
    items = [i for i in items if isinstance(i, dict) and float(i.get("time", 0)) >= cutoff and (i.get("title") or i.get("text"))]
    if not items:
        return "Nothing new on your phone, sir."
    items.sort(key=lambda i: i.get("time", 0), reverse=True)

    chats = [i for i in items if any(k in str(i.get("app", "")).lower() for k in _CHAT_APPS) and not _NOISE.search(str(i.get("text", "")))]
    others = [i for i in items if i not in chats]

    lines: list[str] = []
    for i in chats[:6]:
        who, text = _clip(i.get("title", ""), 40), _clip(i.get("text", ""))
        lines.append(f"{i.get('app', 'App')}, {who}: {text}" if who else f"{i.get('app', 'App')}: {text}")
    if len(chats) > 6:
        lines.append(f"and {len(chats) - 6} more messages")
    if others:
        apps: dict[str, int] = {}
        for i in others:
            apps[str(i.get("app", "App"))] = apps.get(str(i.get("app", "App")), 0) + 1
        top = sorted(apps.items(), key=lambda kv: -kv[1])[:4]
        lines.append("Also " + ", ".join(f"{n} from {a}" for a, n in top))
    head = f"You have {len(items)} notification{'s' if len(items) != 1 else ''}, sir."
    return head + " " + ". ".join(lines) + "."
