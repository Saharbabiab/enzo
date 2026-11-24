"""
agent/responder.py - S.H.A.R.P. response generation.

respond()           - generates the final LLM answer
build_system_prompt()  - composes the full system prompt with memory + live rules
"""

import os
import asyncio
import json
from loguru import logger

from shared import ollama_client, OLLAMA_MODEL, OLLAMA_OPTIONS, memory_store, get_time_context, strip_thinking
from agent.memory import get_profile_summary
from rules import enforce_rules


# ------------------------------------------------
# SYSTEM PROMPT BUILDER
# ------------------------------------------------
def build_system_prompt() -> str:
    stable, volatile = build_system_parts()
    return f"{stable}\n\n{volatile}"


def build_system_parts(query: str = "") -> tuple[str, str]:
    """
    (stable, volatile). Stable = personality + rules: identical on every call, so
    LLM prompt caches (Ollama KV cache, Claude prompt caching) can reuse it.
    Volatile = memory (core profile + facts relevant to `query`) + current time: changes, so it goes LAST.
    """
    stable = _stable_prompt()
    volatile = (
        "=== WHAT YOU KNOW ABOUT THE USER ===\n"
        f"{get_profile_summary(query)}\n\n"
        f"=== TIME ===\n{get_time_context()}\n"
    )
    try:
        from agent import lessons
        volatile += "\n" + lessons.prompt_block(query)
    except Exception:
        pass
    try:
        from agent import profile_setup
        volatile += profile_setup.hint()
    except Exception:
        pass
    try:
        from agent import playbooks
        gone = playbooks.recently_removed()
        if gone:   # the chat still shows their old runs, and the model copied them after he deleted one
            volatile += (f"\n=== REMOVED ROUTINES ===\nHe deleted these routines: {', '.join(gone)}. If he says one, do NOT "
                         "repeat its old steps from earlier in the chat: tell him it was removed and ask what he wants.\n")
    except Exception:
        pass
    try:
        from agent import briefing
        if briefing.morning_briefing_due():          # morning, and he has not heard today's briefing yet
            volatile += ("\n=== MORNING BRIEFING ===\nThe user has not heard today's morning briefing yet. If he is opening the conversation "
                         "with a greeting (\"good morning\", \"hey enzo\") or asks how his day looks, call morning_briefing and read it "
                         "out as it is. A thank-you, a goodbye (\"thanks, that's all\") or a reaction to your last answer is not an "
                         "opening: just answer it. If he asks for something specific, just do that; the briefing can wait.\n")
    except Exception:
        pass
    return stable, volatile


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_persona_cache: dict[str, tuple[float, str]] = {}


def _persona(name: str) -> str:
    """persona/<name> from the repo, or the user's own copy in data/<name> (wins, survives updates). Re-read when the file changes."""
    for path in (os.path.join(_ROOT, "data", name), os.path.join(_ROOT, "persona", name)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        cached = _persona_cache.get(path)
        if not cached or cached[0] != mtime:
            with open(path, encoding="utf-8") as f:
                _persona_cache[path] = (mtime, f.read().strip())
        return _persona_cache[path][1]
    return ""


def _stable_prompt() -> str:
    """Who Enzo is + lasting facts about the user: hand-editable files (see persona/README.md). Identical on every
    call until a file changes, so the model's prompt cache can reuse it."""
    enzo = _persona("ENZO.md") or "You are Enzo, the user's personal assistant. Reply in English, call him sir, never invent facts."
    user = _persona("USER.md")
    extra = os.getenv("BOT_SYSTEM_PROMPT", "").strip()
    parts = [enzo]
    if extra:
        parts.append(extra)
    if user:
        parts.append("\n=== ABOUT THE USER (written by him) ===\n" + user)
    from agent import playbooks
    return "\n".join(parts) + "\n" + playbooks.catalog()


# ------------------------------------------------
async def reflect_and_improve(chat_history: list):
    """Called every N messages - logs one improvement note to MongoDB."""
    if len(chat_history) < 6:
        return
    conv = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in chat_history[-10:])
    try:
        from agent.agent_loop import background_chat
        resp = await asyncio.to_thread(background_chat,
            f"Review this Enzo conversation:\n{conv}\n\n"
            "One specific improvement for next time, 1 sentence, or output NONE:\n", num_predict=60)
        note = strip_thinking(resp["message"]["content"]).strip()
        if note and note.upper() != "NONE" and len(note) > 10:
            memory_store.log_improvement(note)
            logger.debug(f"Improvement logged: {note[:60]}")
    except Exception:
        pass


async def reflect_and_learn(user_text: str, context: str):
    """Caches a fact from search results for future quick retrieval."""
    from shared import TTL_WEEKLY, TTL_DAILY, TTL_STABLE, TTL_LIVE
    if not context or "[SEARCH RESULTS" not in context:
        return
    try:
        from agent.agent_loop import background_chat
        resp = await asyncio.to_thread(background_chat,
            f"Extract ONE cacheable fact.\nASKED: {user_text}\n"
            f"RESULT: {context[:400]}\n"
            f"ttl: {TTL_LIVE}=live {TTL_DAILY}=daily {TTL_WEEKLY}=stable {TTL_STABLE}=historical\n"
            f"Nothing? Return {{}}\n"
            f'JSON only: {{"key":"...","value":"...","ttl_hours":{TTL_WEEKLY}}}', num_predict=80)
        from shared import _repair_json
        data = _repair_json(resp["message"]["content"])
        if data.get("key") and data.get("value"):
            memory_store.cache_fact(str(data["key"]), str(data["value"]), int(data.get("ttl_hours", TTL_WEEKLY)))
    except Exception:
        pass
