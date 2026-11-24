"""
agent/gemini_brain.py — Google Gemini as Enzo's backup big brain.

Used when Claude is unavailable (no key, API error, outage). Same tools and
loop guard as the local agent and Claude.

Config (.env):
  GEMINI_API_KEY      required (already used for search fallback)
  GEMINI_MODEL        default gemini-flash-latest (always Google's newest Flash)

Privacy: when Gemini answers, that conversation, memory profile, session
location and tool results are sent to Google.
"""

from __future__ import annotations

import os
import json
import asyncio

from loguru import logger

from agent.context import session
from agent.responder import build_system_parts
from agent.tools import registry

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# Tried in order when a model is overloaded (503), rate-limited (429) or retired (404).
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-3.8-flash,gemini-flash-lite-latest").split(",") if m.strip()]
MAX_TURNS = int(os.getenv("BIG_BRAIN_MAX_TURNS", "10"))

_RULES = """
=== HOW YOU WORK ===
You are Enzo's backup brain for harder requests. Use the tools whenever you need
real data or need to DO something; never invent facts, numbers, file contents or
system info. If something important is missing, ask the user one short question.
For anything location-based, call get_user_location first.
Your answer is spoken aloud: natural sentences, no markdown, no raw tool output.
"""


_client = None


def available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _get_client():
    """Own client for the brain: 20 s timeout and ONE attempt per model, so an
    overloaded model fails fast and we move on to the next fallback model instead
    of the SDK silently retrying for minutes."""
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(
                timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "20000")),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def _declarations():
    from google.genai import types
    return [
        types.FunctionDeclaration(
            name=s["function"]["name"],
            description=s["function"]["description"],
            parameters_json_schema=s["function"]["parameters"],
        )
        for s in registry().schemas
    ]


async def run_gemini(user_text: str, chat_history: list) -> tuple[str, str]:
    """Returns (final_answer, tool_log). Raises on API errors."""
    from google.genai import types
    from agent.agent_loop import LoopGuard

    if not available():
        raise RuntimeError("GEMINI_API_KEY not configured")
    gemini_client = _get_client()

    reg = registry()
    stable, volatile = build_system_parts(user_text)
    config = types.GenerateContentConfig(
        system_instruction=stable + _RULES + "\n" + volatile + f"\n=== SESSION ===\n{session().describe()}\n",
        tools=[types.Tool(function_declarations=_declarations())],
        # We run the tools ourselves (loop guard, logging) — no automatic calling.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents: list = []
    for m in chat_history[:-1][-12:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            role = "model" if m["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))

    guard = LoopGuard(max_per_tool=6)
    tool_log: list[str] = []

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]

    async def _generate():
        nonlocal models
        last = None
        for i, model in enumerate(models):
            try:
                r = await asyncio.to_thread(
                    gemini_client.models.generate_content, model=model, contents=contents, config=config,
                )
                if i:  # stick with the model that worked for the rest of this request
                    models = models[i:]
                return r
            except Exception as e:
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code in (404, 429, 500, 503, 504):
                    logger.warning(f"♊ {model} unavailable ({code}) — trying next model")
                    last = e
                    continue
                raise
        raise last or RuntimeError("No Gemini model available")

    for turn in range(MAX_TURNS):
        resp = await _generate()
        if not resp.candidates or resp.candidates[0].content is None:
            reason = getattr(resp, "prompt_feedback", None)
            raise RuntimeError(f"Gemini returned no answer ({reason})")

        # Keep the model's full turn (incl. thought signatures) — required for tool loops.
        contents.append(resp.candidates[0].content)

        calls = resp.function_calls or []
        if not calls:
            logger.info(f"♊ Gemini done after {turn} tool round(s)")
            return (resp.text or "").strip(), "\n".join(tool_log)

        parts = []
        for call in calls:
            args = dict(call.args or {})
            blocked = guard.check(call.name, args)
            if blocked:
                logger.warning(f"🛑 LoopGuard (Gemini): {call.name} — {blocked}")
                out = f"[BLOCKED]: {blocked}"
            else:
                logger.info(f"♊🧰 {call.name}({json.dumps(args, ensure_ascii=False)[:150]})")
                out = await reg.call(call.name, args)
                guard.record(out)
                tool_log.append(f"[{call.name}] {out}")
            parts.append(types.Part.from_function_response(name=call.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=parts))

    logger.warning("Gemini hit MAX_TURNS")
    return "I ran out of steps on that one, sir.", "\n".join(tool_log)
