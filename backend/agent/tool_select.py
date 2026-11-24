"""
agent/tool_select.py - give the local model only the tools that matter for this request (task E-1.4).

All ~44 tool descriptions together are about 6000 tokens. The local model's window is 8192, so after the system
prompt there was almost no room left for the chat history and the answer, and replies were cut off after a few
words ("The weather in  today is."). A 9B model also picks better from ~12 tools than from 44.

MEASURED (benchmark, same cases): showing only ~10 relevant tools scored 60% on reminders, showing all tools 90-100%. The
model calls tools much more reliably when it sees the whole list, so the default mode is "all" and the real fix for the
small window is a bigger one: LLM_NUM_CTX=32768 uses only ~6.6 of the GPU's 8 GB (checked with /api/ps). Modes via
TOOLS_MODE: all (default) | compact (shorter descriptions) | select (only relevant tools, cheaper but less reliable).

This only decides what the model SEES. The model still chooses the tool. If nothing matches (another language,
unusual wording) it gets a broad default set, so it is never left without the everyday tools.
"""

from __future__ import annotations

import os
import re

MAX_TOOLS = int(os.getenv("TOOLS_MAX", "12"))

# Always on offer: the everyday tools.
CORE = ("web_search", "remember", "set_reminder", "get_current_weather", "morning_briefing", "calendar_events")

# Used when the request gives no English clue (e.g. Hebrew): the things the user asks for most.
BROAD = CORE + ("phone_call", "phone_message", "phone_music", "phone_settings", "list_reminders",
                "cancel_reminder", "email_inbox", "open_app", "get_user_location", "media_control")

_STOP = set("""a an the to of in on at for and or is are was be do does did me my you your i it this that with from by as
about can could would should please sharp hey hi hello what whats how when where why who which tell give show get make let
now today just also some any all one""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def _profile(schema: dict) -> tuple[set[str], set[str]]:
    f = schema["function"]
    name_words = _words(f["name"].replace("_", " "))
    desc = (f.get("description") or "")
    params = " ".join((f.get("parameters") or {}).get("properties", {}).keys())
    return name_words, _words(desc[:600] + " " + params.replace("_", " "))


def _short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: end + 1] if end > n * 0.5 else cut.rsplit(" ", 1)[0]).strip()


def compact(schemas: list[dict], desc_chars: int = None, param_chars: int = None) -> list[dict]:
    """The same tools with shorter descriptions: ~60 % fewer tokens, nothing removed."""
    desc_chars = desc_chars or int(os.getenv("TOOL_DESC_CHARS", "230"))
    param_chars = param_chars or int(os.getenv("TOOL_PARAM_CHARS", "70"))
    out = []
    for sc in schemas:
        f = sc["function"]
        props = {}
        for k, v in ((f.get("parameters") or {}).get("properties") or {}).items():
            v = dict(v)
            if "description" in v:
                v["description"] = _short(v["description"], param_chars)
            props[k] = v
        params = {**(f.get("parameters") or {}), "properties": props}
        out.append({**sc, "function": {**f, "description": _short(f.get("description", ""), desc_chars), "parameters": params}})
    return out


# Tools that can only work from one place. Showing the model a tool that cannot work here only invites the wrong
# call (on the phone it kept choosing the PC's media keys for "pause the music"). This is about what is POSSIBLE
# in this session, not about the wording of the request.
PHONE_APP_ONLY = {"phone_call", "phone_message", "phone_settings", "phone_timer", "phone_alarm", "phone_notifications", "pick_contact", "phone_screen", "end_conversation", "moovit_route"}
NOT_ON_PHONE_APP = {"media_control"}        # the PC's media keys; on the phone, music is phone_music and volume is phone_settings


def usable_here(schemas: list[dict], app: str = "web") -> list[dict]:
    if os.getenv("TOOLS_BY_SESSION", "on").strip().lower() in ("off", "false", "0"):
        return schemas
    hide = NOT_ON_PHONE_APP if app == "android" else PHONE_APP_ONLY
    return [s for s in schemas if s["function"]["name"] not in hide]


def select_tools(user_text: str, schemas: list[dict], app: str = "web", recent: list[str] | None = None,
                 limit: int | None = None, mode: str | None = None) -> list[dict]:
    """The schemas to show the model for this request (order kept). `mode` overrides TOOLS_MODE (the free brain)."""
    schemas = usable_here(schemas, app)
    mode = (mode or os.getenv("TOOLS_MODE", "all")).strip().lower()
    if mode == "all":
        return schemas
    if mode == "compact":                      # every tool, but with short descriptions (the model calls tools best when it sees all)
        return compact(schemas)
    limit = limit or MAX_TOOLS                 # mode "select": only the relevant ones (cheaper, but the model calls tools less reliably)
    by_name = {s["function"]["name"]: s for s in schemas}
    text = " ".join([*(recent or [])[-2:], user_text or ""])
    q = _words(text)
    scored = []
    for name, s in by_name.items():
        nw, dw = _profile(s)
        score = 3 * len(q & nw) + len(q & dw)
        if app == "android" and name.startswith("phone_"):
            score += 1 if score else 0
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))

    has_clue = bool(scored) and scored[0][0] >= 2
    chosen: list[str] = [n for n in CORE if n in by_name]
    if not has_clue:
        chosen += [n for n in BROAD if n in by_name and n not in chosen]
    else:
        for _, name in scored:
            if name not in chosen:
                chosen.append(name)
            if len(chosen) >= limit:
                break
    chosen = chosen[: max(limit, len(CORE))] if has_clue else chosen
    keep = set(chosen)
    return [s for s in schemas if s["function"]["name"] in keep]
