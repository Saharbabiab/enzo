"""
agent/free_brain.py — a free OpenAI-compatible model as Enzo's last cloud backup (after Claude and Gemini).

Works with any OpenAI-compatible endpoint: one free provider (e.g. Groq) or the FreeLLMAPI router
(https://github.com/tashfeenahmed/freellmapi, the `freellmapi` service in docker-compose.yml), which puts many free
providers behind one address and switches on 429s.

Config (.env), off unless both are set:
  FREE_LLM_BASE_URL   e.g. https://api.groq.com/openai/v1  or  http://freellmapi:3001/v1 (the router on the NAS)
  FREE_LLM_MODELS     comma list, tried in order on 404/429/5xx, e.g. openai/gpt-oss-120b,qwen/qwen3-32b  (the router: auto)
  FREE_LLM_API_KEY    the provider's key, or the router's unified key

Privacy: like Gemini, the request, the memory profile, the session and tool results go to that provider,
and free tiers may keep or train on them.
"""

from __future__ import annotations

import json
import os

from loguru import logger

from agent.context import session
from agent.responder import build_system_parts
from agent.tools import registry

MAX_TURNS = int(os.getenv("BIG_BRAIN_MAX_TURNS", "10"))

_RULES = """
=== HOW YOU WORK ===
You are Enzo's backup brain for harder requests. Use the tools whenever you need
real data or need to DO something; never invent facts, numbers, file contents or
system info. If something important is missing, ask the user one short question.
Your answer is spoken aloud: natural sentences, no markdown, no raw tool output.
"""


def models() -> list[str]:
    return [m.strip() for m in os.getenv("FREE_LLM_MODELS", "").split(",") if m.strip()]


def available() -> bool:
    return bool(os.getenv("FREE_LLM_BASE_URL", "").strip() and models())


def _client():
    from openai import AsyncOpenAI
    return AsyncOpenAI(base_url=os.environ["FREE_LLM_BASE_URL"].strip(), api_key=os.getenv("FREE_LLM_API_KEY") or "none",
                       timeout=float(os.getenv("FREE_LLM_TIMEOUT", "25")), max_retries=0)


async def run_free(user_text: str, chat_history: list) -> tuple[str, str]:
    """Returns (final_answer, tool_log). Raises when no model answers."""
    from agent.agent_loop import LoopGuard, run_tool_calls
    if not available():
        raise RuntimeError("FREE_LLM_BASE_URL / FREE_LLM_MODELS not configured")
    client, reg = _client(), registry()
    stable, volatile = build_system_parts(user_text)
    messages: list = [{"role": "system", "content": stable + _RULES + "\n" + volatile + f"\n=== SESSION ===\n{session().describe()}\n"}]
    messages += [{"role": m["role"], "content": m["content"]} for m in chat_history[:-1][-12:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": user_text})
    guard, tool_log, todo = LoopGuard(max_per_tool=6), [], models()
    from agent.tool_select import compact, select_tools
    # Free tiers allow ~6-8K tokens a minute and a tool turn takes 2+ requests: only the tools that fit this request,
    # with short texts (the full list of 53 is ~8K tokens alone). shortcut: smaller choice than Claude/Gemini get.
    schemas = compact(select_tools(user_text, reg.schemas, session().app, recent=[m.get("content", "") for m in chat_history[-3:-1]], mode="select"))

    async def ask():
        nonlocal todo
        last = None
        for i, model in enumerate(todo):
            try:
                r = await client.chat.completions.create(model=model, messages=messages, tools=schemas)
                todo = todo[i:]                    # stay on the model that worked for the rest of this request
                return r
            except Exception as e:
                code = getattr(e, "status_code", None)
                if code in (404, 408, 413, 429, 500, 502, 503, 504) or code is None:   # 413: over the free tokens-per-minute
                    logger.warning(f"🆓 {model} unavailable ({code or e.__class__.__name__}) — trying next model")
                    last = e
                    continue
                raise
        raise last or RuntimeError("No free model available")

    for turn in range(MAX_TURNS):
        msg = (await ask()).choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            logger.info(f"🆓 Free model done after {turn} tool round(s)")
            return (msg.content or "").strip(), "\n".join(tool_log)
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]})
        parsed = []
        for c in calls:
            try:
                parsed.append((c.function.name, json.loads(c.function.arguments or "{}") or {}))
            except json.JSONDecodeError:
                parsed.append((c.function.name, {}))
        results = await run_tool_calls(reg, guard, parsed, tool_log)
        for c, (_, out) in zip(calls, results):
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out})

    logger.warning("Free model hit MAX_TURNS")
    return "I ran out of steps on that one, sir.", "\n".join(tool_log)
