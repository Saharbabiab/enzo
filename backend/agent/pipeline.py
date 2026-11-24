"""
agent/pipeline.py - one message in, one answer out. Shared by every channel (web socket in bot.py, Telegram).

    1. The clock    time and date are answered by code (no risk, no waiting).
    2. The agent    everything else: the model decides which tools and skills to use.
                    MAIN_BRAIN=claude: Claude answers first with every tool; if it is unavailable, over budget or
                    fails, the local model answers (and Gemini / the free model if that fails too).
                    MAIN_BRAIN=local (default): the local model answers first; when it cannot finish (see
                    ESCALATION below) the big brain (Claude, then Gemini, then free) takes over.

There is no phrase matching here: what a request means is the model's decision (CLAUDE.md, "Design principle").

ESCALATION - the local model hands over to the big brain when:
    - it calls ask_big_brain itself (research, comparisons, code, long planning, a missing ability)
    - it answered without acting and, asked to choose a tool, still chose none
    - the loop guard had to stop it, or it ran out of steps
    - it crashed or returned nothing
Every hand-over is logged with its reason on the turn's trace (agent/traces.py), so frequent reasons can be fixed.

Channels supply an optional `status(text)` coroutine for progress messages ("Thinking harder...") and set
session().confirm for approvals.
"""

from __future__ import annotations

import os
import re
from typing import Awaitable, Callable

from loguru import logger

from agent.agent_loop import run_agent
from rules import enforce_rules

Status = Callable[[str], Awaitable[None]] | None


def main_brain() -> str:
    """"claude" or "local" (MAIN_BRAIN in .env, read on every turn so a change needs no code)."""
    return "claude" if os.getenv("MAIN_BRAIN", "claude").strip().lower() == "claude" else "local"


def looks_failed(result: str) -> bool:
    return bool(re.search(r"\[(\w+ )?(error|fatal)\]", result or "", re.I))


async def hybrid_agent(user_text: str, chat_history: list, status: Status = None) -> tuple[str, str]:
    """Local model first; the big brain when the local model hands over or fails."""
    from agent import agent_loop, big_brain, free_brain, gemini_brain, traces

    use_gemini = os.getenv("GEMINI_BACKUP", "true").lower() == "true" and gemini_brain.available()
    cloud_ok = big_brain.available() or use_gemini or free_brain.available()
    tr = traces.current()

    async def _status(text: str):
        if status is not None:
            try:
                await status(text)
            except Exception:
                pass

    claude_tried = False

    async def _cloud(reason: str):
        """Claude first (unless it already failed this turn), then Gemini, then a free model. None if all fail."""
        tr.signal("escalated", reason)
        await _status("Thinking harder...")
        if big_brain.available() and not claude_tried:
            try:
                logger.info(f"🧠 Routing to Claude ({big_brain.BIG_BRAIN_MODEL}) because: {reason}")
                with tr.stage("claude"):
                    answer, ctx = await big_brain.run_big_brain(user_text, chat_history)
                if answer:
                    tr.route = "claude"
                    return answer, ctx
            except Exception as e:
                logger.error(f"Claude failed: {e.__class__.__name__}: {str(e)[:200]}")
        if use_gemini:
            try:
                logger.info(f"♊ Routing to Gemini backup ({gemini_brain.GEMINI_MODEL})")
                with tr.stage("gemini"):
                    answer, ctx = await gemini_brain.run_gemini(user_text, chat_history)
                if answer:
                    tr.route = "gemini"
                    return answer, ctx
            except Exception as e:
                logger.error(f"Gemini failed: {e.__class__.__name__}: {str(e)[:200]}")
        if free_brain.available():                  # a free model (Groq, the FreeLLMAPI router...): last cloud backup
            try:
                logger.info(f"🆓 Routing to the free backup ({free_brain.models()[0]})")
                with tr.stage("free"):
                    answer, ctx = await free_brain.run_free(user_text, chat_history)
                if answer:
                    tr.route = "free"
                    return answer, ctx
            except Exception as e:
                logger.error(f"Free model failed: {e.__class__.__name__}: {str(e)[:200]}")
        return None

    if main_brain() == "claude" and big_brain.available():
        claude_tried = True
        tr.route = "claude"
        try:
            with tr.stage("claude"):
                answer, ctx = await big_brain.run_big_brain(user_text, chat_history)
            if answer:
                return answer, ctx
            reason = "Claude gave no answer"
        except Exception as e:
            reason = f"Claude failed: {e.__class__.__name__}: {str(e)[:160]}"
        logger.warning(f"Main brain: {reason} - the local model answers instead")
        tr.signal("fallback", reason)

    tr.route = "local"
    agent_loop.escalation.set("")
    try:
        with tr.stage("local"):
            answer, ctx = await run_agent(user_text, chat_history)
        why = agent_loop.escalation.get()
    except Exception as e:
        logger.error(f"Local agent failed: {e.__class__.__name__}: {e}")
        answer, ctx, why = "", "", f"local model error: {e.__class__.__name__}"

    if "NEED_SKILL" in (answer or ""):                 # older wording of "no tool can do this"
        answer = answer.replace("NEED_SKILL", "").strip()
        why = why or "no tool can do this"
    if not answer and not why:
        why = "local model gave no answer"

    if why and cloud_ok:
        result = await _cloud(why)
        if result:
            return result
        logger.warning("Cloud brains failed - falling back to the local answer")
    if why and not answer:
        answer = "I couldn't work that one out, sir, and the big brain is not reachable right now."
    return answer, ctx


async def answer(user_text: str, chat_history: list, status: Status = None) -> tuple[str, str]:
    """Answer one message, then learn from it in the background (interests, facts, people)."""
    import asyncio
    from agent import traces
    from agent.context import session
    from agent.lang import detect_lang
    session().lang = detect_lang(user_text)   # reply in the language he wrote in
    from agent import mcp_client, skill_review
    await mcp_client.start()                   # MCP connections (data/mcp_servers.json), once per event loop
    skill_review.register()                    # a turn that went well may be a routine worth keeping (once, idempotent)
    from agent.agent_loop import turn_begins, turn_ends
    traces.start(user_text)
    turn_begins()                                 # background jobs wait until he has been quiet for a while
    try:
        result = await _answer(user_text, chat_history, status)
    except BaseException as e:
        traces.current().signal("crashed", f"{e.__class__.__name__}: {e}")
        traces.finish("")
        raise
    finally:
        turn_ends()
    traces.finish(result[0])
    from agent.memory import learn_from_turn
    from agent import compaction

    async def after_reply():                   # one after the other: both use the local model
        await learn_from_turn(user_text)
        await compaction.fold()
    asyncio.create_task(after_reply())
    return result


async def _answer(user_text: str, chat_history: list, status: Status = None) -> tuple[str, str]:
    """`chat_history` must already contain the user message as its last entry. Returns (final_reply, tool_context)."""
    from agent import traces
    from agent.instant import clock_answer

    final = clock_answer(user_text)
    if final:
        logger.info(f"⚡ Clock answer: {final}")
        traces.current().route = "instant"
        return final, ""

    reply, context = await hybrid_agent(user_text, chat_history, status)
    # Keep what the tools really did with this message: the history is text only, and Claude later took
    # "Calling your sister now, sir." for a claim it never acted on, said it never called, and called again.
    from agent.tools import RISK
    # Lookups too (name and request only): without them Claude saw train times in its own earlier reply, decided it had
    # never run the planner, and kept apologising for "unchecked" times in every later answer.
    done = [f"{t['name']}({t['args']}) -> {str(t['result'])[:160]}" if RISK.get(t["name"], ("read",))[0] != "read"
            else f"looked up with {t['name']}({str(t['args'])[:120]})" for t in traces.current().tools]
    if done and chat_history and chat_history[-1].get("role") == "user":
        chat_history[-1]["done"] = done
    return (enforce_rules(reply) if reply else "Something went wrong, sir. Try again."), context
