"""
agent/big_brain.py — Claude as Enzo's "big brain" for hard requests.

Hybrid setup:
  • Local qwen3 (Ollama, on your GPU) handles everyday commands — fast, free, private.
  • Claude handles hard requests: research, comparisons, planning, code, long or
    multi-step asks — or when the local agent fails.

Same tools, same loop guard as the local agent (agent/agent_loop.py).

Config (.env):
  ANTHROPIC_API_KEY   required — without it Enzo stays 100% local
  BIG_BRAIN           "claude" (default) | "off"
  BIG_BRAIN_MODEL     default claude-opus-5-5 (cheaper: claude-sonnet-5-5, claude-haiku-5-5)
  BIG_BRAIN_EFFORT    low | medium (default) | high — how hard it thinks (latency/cost)
  CLAUDE_DAILY_USD    daily Claude budget in dollars (default 2). Over it, Claude is not called until tomorrow;
                      at 80% the user is told once. Every Claude call (big brain, lessons, briefing, Spotify) counts.

Privacy: when Claude is used, the conversation, your memory profile, session
location and tool results for that request are sent to Anthropic.
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

from agent.context import session
from agent.responder import build_system_parts
from agent.tools import registry

BIG_BRAIN = os.getenv("BIG_BRAIN", "claude").lower()
BIG_BRAIN_MODEL = os.getenv("BIG_BRAIN_MODEL", "claude-opus-5-5")
BIG_BRAIN_EFFORT = os.getenv("BIG_BRAIN_EFFORT", "medium")
MAX_TURNS = int(os.getenv("BIG_BRAIN_MAX_TURNS", "10"))      # every this many rounds Claude is asked: answer now or keep going?
HARD_MAX_TURNS = int(os.getenv("BIG_BRAIN_HARD_MAX_TURNS", "30"))   # then it must answer (the daily budget still applies)

_client = None

# ── daily budget (TASKS T-32) ──
DAILY_USD = float(os.getenv("CLAUDE_DAILY_USD", "2"))
SPEND_FILE = Path(os.getenv("CLAUDE_SPEND_FILE") or Path(__file__).resolve().parent.parent / "data" / "claude_spend.json")
# $ per million tokens: input, output, cache read, cache write. Unknown models are priced as Opus (the safe side).
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
_spend_lock = threading.Lock()


def _today() -> str:
    from shared import ISRAEL_TZ
    return datetime.now(ISRAEL_TZ).strftime("%Y-%m-%d")


def _spend_state() -> dict:
    try:
        d = json.loads(SPEND_FILE.read_text(encoding="utf-8"))
        if d.get("day") == _today():
            return d
    except Exception:
        pass
    return {"day": _today(), "usd": 0.0, "calls": 0, "warned": False}


def cost_of(model: str, usage) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
    g = lambda k: getattr(usage, k, 0) or 0
    return (g("input_tokens") * p_in + g("output_tokens") * p_out + g("cache_read_input_tokens") * p_read
            + g("cache_creation_input_tokens") * p_write) / 1e6


def spent_today() -> float:
    with _spend_lock:
        return _spend_state()["usd"]


def over_budget() -> bool:
    return DAILY_USD > 0 and spent_today() >= DAILY_USD


def _record(model: str, usage) -> None:
    """Add one call's cost to today's total and to the current trace."""
    cost = cost_of(model, usage)
    with _spend_lock:
        d = _spend_state()
        d["usd"] = round(d["usd"] + cost, 6)
        d["calls"] = d.get("calls", 0) + 1
        try:
            SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
            SPEND_FILE.write_text(json.dumps(d), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Claude spend not saved: {e}")
    try:
        from agent import traces
        traces.current().cost_usd += cost
    except Exception:
        pass


def budget_warning() -> str:
    """Once a day, a sentence for the user when Claude spending passes 80% of the budget, else ""."""
    if DAILY_USD <= 0:
        return ""
    with _spend_lock:
        d = _spend_state()
        if d.get("warned") or d["usd"] < 0.8 * DAILY_USD:
            return ""
        d["warned"] = True
        try:
            SPEND_FILE.write_text(json.dumps(d), encoding="utf-8")
        except Exception:
            pass
    return (f"Sir, Claude has used ${d['usd']:.2f} of today's ${DAILY_USD:.2f} budget. "
            "When it runs out I will answer on my own until midnight.")


def budget_line() -> str:
    return f"Claude spending today: ${spent_today():.2f} of ${DAILY_USD:.2f}" + (" (budget reached)" if over_budget() else "")


def _counted(create):
    def call(*a, **k):
        if over_budget():
            raise RuntimeError(f"Claude daily budget (${DAILY_USD:.2f}) reached")
        resp = create(*a, **k)
        _record(getattr(resp, "model", None) or k.get("model") or BIG_BRAIN_MODEL, getattr(resp, "usage", None))
        return resp
    return call


def available() -> bool:
    return BIG_BRAIN == "claude" and bool(os.getenv("ANTHROPIC_API_KEY")) and not over_budget()


SIDE_MODEL = os.getenv("SIDE_JOB_MODEL", "claude-haiku-5-5")


def side_job(prompt: str, max_tokens: int = 300) -> str:
    """A background job (learning facts, folding old chat) on Claude Haiku, for when the local model is off
    (The user stopped Ollama on 2026-11-24 to give Kokoro the GPU). Blocking; costed like every Claude call."""
    if not available():
        raise RuntimeError("Claude is not available")
    r = _get_client().messages.create(model=SIDE_MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in r.content)


def _get_client():
    """The Anthropic client. Every messages.create through it is costed and refused once today's budget is used."""
    global _client
    if _client is None:
        import anthropic
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        for res in (client.messages, client.beta.messages):
            res.create = _counted(res.create)
        _client = client
    return _client


# ════════════════════════════════════════════════════════════════
# ROUTER — when is a request "hard"?
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# AGENT LOOP (Claude Messages API + our tools)
# ════════════════════════════════════════════════════════════════

_RULES = """
=== HOW YOU WORK ===
You can use tools — call them whenever you need real data or need to DO something; never invent facts,
numbers, file contents or system info. Run several searches and read pages when
researching. If something important is missing (which city, budget, which file),
ask the user one short question instead of guessing; for anything location-based,
call get_user_location first.
Your final answer is spoken aloud: clear, natural sentences, no markdown tables,
no raw tool output. Keep it as short as the question allows.
In the Enzo app, when your answer offers choices, drafts a message, lists several things or gives steps,
also call show_ui so he can read it and tap an answer (choices always go in its options); then say only one
short sentence, never read the screen out. Look up only what he asked about: drafting a reply he asked for needs
no searching of mail, calendar or notifications unless he asks you to.

Never fake an action by misusing another tool (e.g. adding an event called "DELETED" to remove one).
If a tool returns [AMBIGUOUS], ask the user which one they mean.

LEARNING NEW ABILITIES: you can extend yourself. If no existing tool can complete the task,
but a small Python function using a free, keyless public web API (or pure computation) could,
write it with create_skill — don't ask the user whether to learn, just do it (the user gets an
approval prompt automatically) — then call the new tool and finish the task. Prefer reliable,
well-known free APIs; keep skills small, general and reusable (e.g. "currency rates", not
"convert 50 dollars"). If create_skill reports problems, fix the code and try again. Don't
build a skill when an existing tool or a web search already does the job.

Skills can also use the user's Google account (Calendar + Gmail) through ONE helper:
    from agent.skill_api import google_call
    google_call("gmail", "users.messages.batchModify", userId="me", body={...}, description="Archive 12 emails from Notion")
    google_call("calendar", "events.list", calendarId="primary", timeMin=..., singleEvents=True)
It uses his login (never touch tokens/credentials yourself); read calls run freely, calls that
change something ask him for approval automatically — always pass a clear description. Use it
for Google abilities the built-in tools lack (archive/label/mark-read email, accept invites,
free/busy, other calendars…). If it says the login lacks permission, tell him to run
`python google_setup.py` once.
"""


def _anthropic_tools() -> list[dict]:
    """Our Ollama-style schemas → Anthropic tool definitions."""
    from agent.tool_select import usable_here
    tools = []
    for s in usable_here(registry().schemas, session().app):   # the same per-app list as the local model (no phone tools on the web)
        f = s["function"]
        if f["name"] == "ask_big_brain":          # that tool is how the local model reaches Claude
            continue
        tools.append({"name": f["name"], "description": f["description"], "input_schema": f["parameters"]})
    return tools


def _create(system: list, messages: list, tools: list, **extra):
    client = _get_client()
    return client.beta.messages.create(
        **extra,
        model=BIG_BRAIN_MODEL,
        max_tokens=16000,
        system=system,
        tools=tools,
        messages=messages,
        output_config={"effort": BIG_BRAIN_EFFORT},
        cache_control={"type": "ephemeral"},  # cache the growing conversation between tool rounds
        # If a safety classifier declines, retry server-side on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def _done_note(history: list) -> str:
    """What the tools really did earlier in this chat (pipeline stores it on each user message as "done")."""
    lines = [f'- after he said "{str(m.get("content"))[:80]}": ' + "; ".join(m["done"]) for m in history if m.get("done")]
    if not lines:
        return ""
    return ("=== WHAT YOUR TOOLS REALLY DID EARLIER IN THIS CHAT ===\nThese happened (or were declined) exactly as listed. "
            "Never say they did not happen and never redo one unless he asks again now. Facts in your reply to a message with a "
            "lookup came from that tool: do not call them unchecked or apologise for them.\n" + "\n".join(lines) + "\n")


async def run_big_brain(user_text: str, chat_history: list) -> tuple[str, str]:
    """Returns (final_answer, tool_log). Raises on API errors — caller falls back to local."""
    from agent.agent_loop import LoopGuard

    from agent import compaction
    from agent.agent_loop import HABITS
    reg = registry()
    tools = _anthropic_tools()
    stable, volatile = build_system_parts(user_text)
    earlier = compaction.summary()
    # The history below is text only: without this, Claude took "Ten minute timer started, sir." for a made-up claim and redid it.
    old_note = ("=== EARLIER ANSWERS ===\nYour earlier answers in this chat came from tool calls that are not shown here: an action "
                "they report really happened. Their data is old: for weather, calendar, news or anything live, call the tool again.\n"
                # Without this, "Give me my morning briefing" also re-ran yesterday's train plan and weather question.
                "Earlier requests are finished: answer ONLY his newest message. Do not redo, retry or finish an earlier request "
                "unless his newest message asks for it.\n"
                if any(m.get("role") == "assistant" for m in chat_history[:-1]) else "")
    old_note += _done_note(chat_history[:-1][-12:])
    system = [
        # Stable part first + cache breakpoint: tools + this block are cached across requests.
        {"type": "text", "text": stable + _RULES + HABITS, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"{volatile}\n=== SESSION ===\n{session().describe()}\n"
                                 + (f"=== EARLIER IN THIS CONVERSATION (summary) ===\n{earlier}\n" if earlier else "") + old_note},
    ]

    messages: list = []
    for m in chat_history[:-1][-12:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            if messages and messages[-1]["role"] == m["role"]:
                messages[-1]["content"] += "\n" + m["content"]
            else:
                messages.append({"role": m["role"], "content": m["content"]})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)  # must start with a user turn
    messages.append({"role": "user", "content": user_text})

    guard = LoopGuard(max_per_tool=6)
    tool_log: list[str] = []
    nudged = False

    from agent import traces
    import time as _time
    tr = traces.current()

    for turn in range(HARD_MAX_TURNS):
        t0 = _time.perf_counter()
        resp = await asyncio.to_thread(_create, system, messages, tools)
        u = resp.usage
        tr.model(BIG_BRAIN_MODEL, (_time.perf_counter() - t0) * 1000, getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0),
                 getattr(u, "cache_read_input_tokens", 0) or 0)

        if resp.stop_reason == "refusal":
            logger.warning(f"🧠 Claude refused: {getattr(resp, 'stop_details', None)}")
            return "I'm not able to help with that one, sir.", "\n".join(tool_log)

        # Keep the full content (thinking + tool_use blocks) — required for tool loops.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if not tool_log:                      # no tool used: is this a made-up fact or an empty promise ("let me look...")?
                from agent import grounding
                from agent.memory import get_profile_summary
                ctx = " / ".join(f"{m['role']}: {str(m['content'])[:160]}" for m in messages[:-1][-4:] if isinstance(m.get("content"), str))
                profile = await asyncio.to_thread(get_profile_summary, user_text)
                if await grounding.fabricated(user_text, text, ctx, profile):
                    tr.signal("ungrounded", f"claude: {text[:100]}")
                    if not nudged:
                        nudged = True
                        tr.signal("nudge", "claude answered without a tool")
                        logger.warning(f"Claude answered without a tool and promised/claimed something: {text[:80]!r} - nudging")
                        messages.append({"role": "user", "content":
                            "You did not call any tool, so nothing was done and nothing you said is checked. Every fact must come from a tool, "
                            "and a promise (\"let me look...\") means nothing unless you act. Call the right tool NOW, or tell me plainly that you have "
                            "no way to do it."})
                        continue
                    return "I don't have a way to do that yet, sir.", "\n".join(tool_log)
            u = resp.usage
            logger.info(f"🧠 Claude done after {turn} tool round(s) — in {u.input_tokens} "
                        f"(cache read {getattr(u, 'cache_read_input_tokens', 0)}), out {u.output_tokens}")
            return text, "\n".join(tool_log)

        from agent.agent_loop import run_tool_calls
        outs = await run_tool_calls(reg, guard, [(tu.name, dict(tu.input or {})) for tu in tool_uses], tool_log)
        results = []
        from agent.context import session as _session
        shot = _session().extra.pop("screen_image", None)       # phone_screen read: let Claude see the screen too
        for tu, (_, out) in zip(tool_uses, outs):
            is_err = out.lstrip().upper().startswith(("[ERROR", "[BLOCKED", "[SHELL ERROR", "[SKILL ERROR"))
            content = out
            if shot and tu.name == "phone_screen" and not is_err:
                content = [{"type": "text", "text": out},
                           {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": shot}}]
                shot = None
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_err})
        done = turn + 1
        if done % MAX_TURNS == 0 and done < HARD_MAX_TURNS:
            results.append({"type": "text", "text": f"(Not from him: {done} tool rounds so far. If the job is done or you are "
                            f"stuck, answer him now. If it really needs more steps, keep going: up to {HARD_MAX_TURNS - done} more.)"})
        # All results for this turn in ONE user message.
        messages.append({"role": "user", "content": results})

    logger.warning("Big brain hit HARD_MAX_TURNS: asking for a final answer")
    last = {"type": "text", "text": "(Not from him: no more tool rounds. Answer him now: what you did, what you found, and what is left.)"}
    if messages[-1]["role"] == "user":
        messages[-1]["content"].append(last)
    else:                                     # ended on a pause_turn
        messages.append({"role": "user", "content": [last]})
    resp = await asyncio.to_thread(_create, system, messages, tools, tool_choice={"type": "none"})
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or "I ran out of steps on that one, sir.", "\n".join(tool_log)
