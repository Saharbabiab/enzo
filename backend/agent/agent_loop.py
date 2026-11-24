"""
agent/agent_loop.py — Native tool-calling agent (replaces the JSON planner).

Flow per user message:
    system prompt (personality + memory + session) + history + user
      → model answers directly, OR returns tool_calls
      → we run the tools, append results as role="tool" messages, ask again
      → repeat until the model answers in plain text (max MAX_TURNS rounds)

The model's final text IS the reply — no separate planner + responder calls.
A LoopGuard stops the model from calling the same tool over and over.
"""

from __future__ import annotations

import os
import json
import time
import asyncio
import threading
from dataclasses import dataclass, field

from loguru import logger

from shared import ollama_client, OLLAMA_OPTIONS, strip_thinking
from agent.context import session
from agent.responder import build_system_parts
from agent.tools import registry

import contextvars

# Why the local model is handing this turn to the big brain ("" = it is not). Read by agent/pipeline.py.
escalation: contextvars.ContextVar[str] = contextvars.ContextVar("enzo_escalation", default="")

AGENT_MODEL = os.getenv("AGENT_MODEL") or os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "6"))
TRANSCRIPT_SHARE = 0.4   # the reused conversation may fill this much of the room the fixed prompt leaves, then it is rebuilt

# How Enzo uses its memory, feedback and routines: the local agent and the cloud brain both get this text.
HABITS = """- "Remember ...", "keep in mind ...", "note that ..." or a lasting fact about him -> call remember. Saying "noted"
  without calling it saves nothing.
- "Who is my ...", "what did I tell you about ...", "do you remember ..." -> call recall: your context holds only a few facts.
  "Forget that ..." about a fact -> forget.
- When he reacts to your last answer ("that's wrong", "not what I asked", "perfect", "exactly") -> call feedback.
- "From now on ..." -> a lasting rule about how you behave: save it with enzo_lessons (action teach).
- "When I say X, do A, B and C" (a set of actions under a name) -> save_playbook. When a request matches a
  PLAYBOOK, call use_skill first and then do its steps.
- A request can have several parts ("do X and Y"): do every part, each with its tool, before you answer.
"""

_AGENT_RULES = """
=== HOW YOU WORK ===
You can use tools. Call a tool whenever you need real data or need to DO something —
never invent facts, numbers, file contents or system info.
- Pick the most specific tool. Weather → get_current_weather. Anything "near me" or
  location-based → get_user_location first.
- If something important is missing (which city, which file, what time) or a name fits several people,
  call ask_user with one short question (and 2-3 choices) instead of guessing. If location is unknown, ask where they are.
- You may call several tools, one after another, until you have what you need.
- If a tool returns an error, try a different approach once, then explain the problem.
- NEVER fake an action by misusing another tool (e.g. adding an event called "DELETED" to
  "remove" one, or writing a file to "send" something). Use the tool made for it.
- If a tool says [AMBIGUOUS], call ask_user with the options it gave — don't guess.
- When your answer offers choices, drafts a message, lists several things or gives steps, also call show_ui
  so he can read it on screen and tap an answer (choices always go in its options); then say only one short
  sentence, never read the screen out.
- Some requests are too big for you: research with several sources, a detailed comparison or buying advice,
  writing code, planning a trip or a project, or anything none of your tools can do (it needs a new ability).
  For those call ask_big_brain and a stronger model takes over. Never just say you can't, and never answer
  a different, easier question instead.
""" + HABITS + """- Your final answer is spoken aloud: short, natural, flowing sentences like a person talking —
  no bullet lists, no markdown tables, no raw tool output, no tags. Never write out your
  reasoning — only the answer.
/no_think
"""


# ════════════════════════════════════════════════════════════════
# LOOP GUARD — stops degenerate tool-call loops
# ════════════════════════════════════════════════════════════════

@dataclass
class LoopGuard:
    max_same_call: int = 1        # identical (tool, args) allowed this many times
    max_per_tool: int = 4         # any single tool, total calls per message
    max_consecutive_errors: int = 3
    _seen: dict = field(default_factory=dict)
    _per_tool: dict = field(default_factory=dict)
    _errors: int = 0
    _names: list = field(default_factory=list)
    blocks: int = 0               # how many calls this guard had to stop

    def check(self, name: str, args: dict) -> str | None:
        """Return a reason to block this call, or None to allow it."""
        if name == "phone_screen":      # The user: never limit the screen (read, tap, read...); MAX_TURNS still ends a turn
            return None
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if self._seen.get(key, 0) >= self.max_same_call:
            return (f"You already called {name} with these exact arguments. "
                    "Use that earlier result, or answer the user now.")
        if self._per_tool.get(name, 0) >= self.max_per_tool:
            return f"{name} was called {self.max_per_tool} times already. Answer with what you have."
        if self._errors >= self.max_consecutive_errors:
            return "Too many failed tool calls. Stop calling tools and explain the problem to the user."
        last = self._names[-4:] + [name]
        if len(last) == 5 and len(set(last)) == 2 and all(last[i] != last[i + 1] for i in range(4)):
            # A, B, A, B, A: going back and forth between two tools without getting anywhere.
            return (f"You keep switching between {last[0]} and {last[1]} without progress. "
                    "Stop calling tools and answer with what you have, or say what is missing.")
        if key not in self._seen and name not in PARALLEL_SAFE and args.get("action") != "read":
            self._seen.clear()      # a new action may change what reads return (open WhatsApp, then read the screen again)
        self._names.append(name)
        self._seen[key] = self._seen.get(key, 0) + 1
        self._per_tool[name] = self._per_tool.get(name, 0) + 1
        return None

    def record(self, result: str) -> None:
        self._errors = self._errors + 1 if result.lstrip().upper().startswith(("[ERROR", "[SHELL ERROR", "[SKILL ERROR")) else 0


# Tools that only READ. Several of them asked for in one step run side by side (a briefing-style question
# gets weather, calendar and mail at once). Anything that acts or may ask for approval runs one at a time, in order.
PARALLEL_SAFE = {
    "web_search", "read_webpage", "list_files", "read_file", "system_status", "list_reminders", "get_user_location",
    "morning_briefing", "list_skills", "calendar_events", "email_inbox", "email_read", "trace_report",
    "get_current_weather", "get_air_quality", "get_crypto_price", "use_skill",
}


# Tools whose result IS the reply: already written for speaking, so it is read out as it is (the model reworded or
# dropped the morning briefing). Also saves a model call.
SPEAK_AS_IS = {"morning_briefing", "ask_user"}   # ask_user: the question ends the turn, so it cannot guess after asking


def _as_is(results: list[tuple[str, str]]) -> str:
    ok = [(n, r) for n, r in results if not str(r).lstrip().startswith("[")]
    return ok[0][1] if len(results) == 1 and ok and ok[0][0] in SPEAK_AS_IS else ""


async def run_tool_calls(reg, guard: "LoopGuard", calls: list[tuple[str, dict]], tool_log: list[str]) -> list[tuple[str, str]]:
    """Run one step's tool calls through the loop guard. Returns [(name, result)] in the order asked."""
    from agent import traces
    results: list = [None] * len(calls)
    allowed = []
    for i, (name, args) in enumerate(calls):
        blocked = guard.check(name, args)
        if blocked:
            guard.blocks += 1
            logger.warning(f"🛑 LoopGuard: {name} {args} — {blocked}")
            traces.current().signal("loop_guard", f"{name}: {blocked[:80]}")
            results[i] = f"[BLOCKED]: {blocked}"
        else:
            logger.info(f"🧰 Tool {name}({json.dumps(args, ensure_ascii=False)[:150]})")
            allowed.append(i)
    show = getattr(session(), "activity", None)
    if show and allowed:
        try:
            await show(", ".join(dict.fromkeys(calls[i][0] for i in allowed)))
        except Exception as e:                        # the app went away: the turn goes on
            logger.debug(f"activity: {e}")
    if len(allowed) > 1 and all(calls[i][0] in PARALLEL_SAFE for i in allowed):
        outs = await asyncio.gather(*(reg.call(*calls[i]) for i in allowed))
    else:
        outs = [await reg.call(*calls[i]) for i in allowed]
    from agent import compaction
    for i, out in zip(allowed, outs):
        out = compaction.cut(calls[i][0], out)        # a long result: its start + a pointer to the full text
        guard.record(out)
        tool_log.append(f"[{calls[i][0]}] {out}")
        results[i] = out
    return [(calls[i][0], results[i]) for i in range(len(calls))]


# ════════════════════════════════════════════════════════════════
# AGENT
# ════════════════════════════════════════════════════════════════

import re as _re



# "Let me check…" / "אני בודק…" with no tool call = an empty promise


# Tool results that mean an action really happened (vs. read-only lookups)




_CAL_LINE_RE = _re.compile(r"^\S+ \d{2}/\d{2}(?: \d{2}:\d{2})?(?: \(all day\))? — (.+?)(?: @ .*)?$")


def _hebrew_names_from_tools(tool_log: list[str]) -> list[str]:
    """Hebrew event titles from calendar_events results (to keep them untranslated)."""
    from agent.lang import has_hebrew
    names = []
    for entry in tool_log:
        if not entry.startswith("[calendar_events]"):
            continue
        for line in entry.splitlines():
            m = _CAL_LINE_RE.match(line.replace("[calendar_events] ", "").strip())
            if m and has_hebrew(m.group(1)):
                names.append(m.group(1).strip())
    return names[:8]






def _chat(messages: list, tools: list | None):
    kwargs = dict(model=AGENT_MODEL, messages=messages, options=OLLAMA_OPTIONS, think=False)
    if tools:
        kwargs["tools"] = tools
    try:
        return ollama_client.chat(**kwargs)
    except TypeError:
        kwargs.pop("think", None)  # older ollama-python without `think`
        return ollama_client.chat(**kwargs)


def _msg_to_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [
            {"function": {"name": c.function.name, "arguments": dict(c.function.arguments or {})}}
            for c in msg.tool_calls
        ]
    return d


def shared_prefix() -> tuple[list, list]:
    """The fixed start of every local-model call: the system message and the tool list (Ollama renders the tools
    right after it). Ollama keeps only a few prompts cached and re-reading this one costs ~30 s on the NAS, so EVERY
    call (the agent, its checks, background learning) starts with exactly this and adds its own part at the end."""
    from agent.tool_select import select_tools
    from agent import mcp_client
    stable, _ = build_system_parts()
    hidden = mcp_client.hidden_from_local()          # MCP tools go to the cloud brain unless a server is marked "local"
    schemas = [t for t in registry().schemas if t["function"]["name"] not in hidden]
    return [{"role": "system", "content": stable + _AGENT_RULES}], select_tools("", schemas, app=session().app)


# Background jobs (note-taking, summaries, heartbeat) run only when no turn has run for IDLE_SECONDS. A job that
# runs during or right between his turns queues his request behind it and replaces the conversation's cache
# checkpoints, so the next model call re-reads everything (~11 s instead of ~1 s, seen in the traces).
IDLE_SECONDS = float(os.getenv("AGENT_IDLE_SECONDS", "20"))
_busy = 0
_last_turn = 0.0
_busy_lock = threading.Lock()


def turn_begins() -> None:
    global _busy
    with _busy_lock:
        _busy += 1


def turn_ends() -> None:
    global _busy, _last_turn
    with _busy_lock:
        _busy = max(0, _busy - 1)
        _last_turn = time.time()


def wait_until_idle(longest: float = 900) -> None:
    """Blocking. Returns at once inside a turn (its own checks must not wait for it to end)."""
    from agent import traces
    if traces._current.get() is not None:
        return
    t0 = time.time()
    while (_busy or time.time() - _last_turn < IDLE_SECONDS) and time.time() - t0 < longest:
        time.sleep(1)


def keep_prefix_cached() -> None:
    """With qwen3.5 (hybrid memory) Ollama can only resume from a saved checkpoint, and it saves ONE per request, about
    512 tokens before its end. A request that differs right after shared_prefix() (a background job) finds no
    checkpoint that early: it re-reads ~12k tokens (~35 s) and erases the later ones, so his next turn pays it again.
    This tiny request ends right after the prefix, so it leaves a checkpoint there that every later call can resume from.
    Blocking; costs ~1-2 s once that checkpoint exists. Outside a turn it first waits until he is idle."""
    wait_until_idle()
    prefix, tools = shared_prefix()
    try:
        ollama_client.chat(model=AGENT_MODEL, messages=prefix + [{"role": "user", "content": "ok"}], tools=tools,
                           think=False, options={**OLLAMA_OPTIONS, "num_predict": 1})
    except Exception as e:
        logger.debug(f"keep_prefix_cached: {e}")


def background_chat(prompt: str, **options):
    """A side job on the local model (judge, reflection, heartbeat, briefing fallback). Blocking.
    It MUST start with shared_prefix(): with qwen3.5 (hybrid memory) a request with any other start makes Ollama ERASE
    the agent's cache checkpoints, and his next request re-reads ~12k tokens (~35 s on the NAS)."""
    keep_prefix_cached()
    prefix, tools = shared_prefix()
    messages = prefix + [{"role": "user", "content": "(Not from the user: a background job. Do exactly this, no tools.)\n" + prompt}]
    kwargs = dict(model=AGENT_MODEL, messages=messages, tools=tools, think=False, options={**OLLAMA_OPTIONS, **options})
    try:
        return ollama_client.chat(**kwargs)
    except TypeError:
        kwargs.pop("think", None)
        return ollama_client.chat(**kwargs)


def side_text(prompt: str, num_predict: int = 200, temperature: float = 0.2) -> str:
    """background_chat's answer as text, or Claude Haiku's when the local model fails (Ollama stopped). Blocking."""
    try:
        return strip_thinking(background_chat(prompt, num_predict=num_predict, temperature=temperature)["message"]["content"] or "")
    except Exception as e:
        logger.debug(f"local model off for a side job ({e.__class__.__name__}): Claude Haiku")
        from agent import big_brain
        return big_brain.side_job(prompt, max_tokens=num_predict * 2)


def ask_json(messages: list, fmt: dict, schemas: list, max_tokens: int = 40):
    """One constrained-JSON answer on top of a conversation that starts with shared_prefix(). Blocking."""
    kwargs = dict(model=AGENT_MODEL, format=fmt, think=False, messages=messages, tools=schemas,
                  options={**OLLAMA_OPTIONS, "temperature": 0, "num_predict": max_tokens})
    try:
        resp = ollama_client.chat(**kwargs)
    except TypeError:
        kwargs.pop("think", None)
        resp = ollama_client.chat(**kwargs)
    return json.loads(strip_thinking(resp.message.content or "{}"))


async def _pick(messages: list, answer: str, question: str, names: list[str], schemas: list, what: str) -> str:
    fmt = {"type": "object", "properties": {"tool": {"type": "string", "enum": names + ["none"]}}, "required": ["tool"]}
    ask = messages + [{"role": "assistant", "content": answer}, {"role": "user", "content": question}]
    try:
        tool = (await asyncio.to_thread(ask_json, ask, fmt, schemas, 30) or {}).get("tool", "none")
    except Exception as e:
        logger.warning(f"{what} failed: {e.__class__.__name__}: {e}")
        return ""
    return tool if tool in names else ""


async def choose_tool(messages: list, draft: str, schemas: list) -> str:
    """The model answered without calling any tool: make it choose one from a closed list (constrained output, it
    cannot dodge). Asked inside the same conversation, so the cached prompt is reused. Returns a tool name or ""."""
    names = [t["function"]["name"] for t in schemas]
    return await _pick(messages, draft,
        "(Check, not from the user: you answered without calling any tool, so nothing was done or looked up. If my request "
        "only asks about me and your answer is taken from WHAT YOU KNOW ABOUT THE USER in my message (where I live, who someone "
        "is, what I like), that is real data already: answer none, or recall to look it up. A question about YOU (who you are, what you can do) is answered from your own instructions: none. Never pick a tool that saves or "
        "changes something I did not ask to change (remember, set_user_location). But when I TELL you something new about "
        "me ('X is my favorite artist', 'I like Y'), that is remember, never a tool that acts on it. Otherwise, does my request need a tool to really do it or "
        "to get real data? If your answer is just a greeting, chat, an opinion, general knowledge or a question back to me, answer none. Otherwise name the ONE tool. A reaction to your previous answer ('perfect', "
        "'that is wrong') needs feedback. A rule for the future ('from now on ...') needs enzo_lessons. Too big for you needs "
        "ask_big_brain. JSON only.)", names, schemas, "choose_tool")


FAILED = ("[ERROR", "[BLOCKED", "[SHELL ERROR", "[SKILL ERROR")


def failed_tools(messages: list) -> list[tuple[str, str]]:
    """(tool, result) for tools whose LAST call this turn failed (a later success of the same tool fixes it)."""
    last: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool":
            last[m.get("tool_name", "")] = str(m.get("content", ""))
    return [(n, r) for n, r in last.items() if r.lstrip().upper().startswith(FAILED)]


async def admits_failure(messages: list, answer: str, failed: list[tuple[str, str]], schemas: list) -> bool:
    """A tool failed: does the answer say so, or does it claim it worked? The model judges, constrained to a yes/no,
    inside the same conversation (cached prompt). True when honest or when the check itself fails."""
    what = "; ".join(f"{n}: {r[:160]}" for n, r in failed)
    fmt = {"type": "object", "properties": {"says_it_failed": {"type": "boolean"}}, "required": ["says_it_failed"]}
    ask = messages + [{"role": "assistant", "content": answer}, {"role": "user", "content":
        f"(Check, not from the user: this did NOT work: {what}. Does your answer tell me plainly that it did not work "
        "(or what you did instead)? An answer that says or suggests it was done is false. JSON only.)"}]
    try:
        return bool((await asyncio.to_thread(ask_json, ask, fmt, schemas, 20) or {}).get("says_it_failed", True))
    except Exception as e:
        logger.warning(f"admits_failure failed: {e.__class__.__name__}: {e}")
        return True


async def tool_for_rest(messages: list, answer: str, done: list[str], schemas: list) -> str:
    """After the model acted and answered: is a part of the request still undone? Returns a tool name, or ""."""
    names = [t["function"]["name"] for t in schemas if t["function"]["name"] not in done and t["function"]["name"] != "ask_big_brain"]
    if not names:
        return ""
    return await _pick(messages, answer,
        f"(Check, not from the user: you called {', '.join(done)}. Is a part of my LAST request still not done? If so, name the "
        "tool for that part. If everything I asked for was done, or the rest needs no tool, answer none. Never pick a tool just "
        "to check or to do more than I asked. JSON only.)", names, schemas, "tool_for_rest")


async def fill_args(messages: list, schema: dict, schemas: list) -> dict | None:
    """The arguments for ONE tool, as JSON constrained to that tool's own parameter schema (the model cannot dodge
    it). Asked inside the same conversation, so the cached prompt is reused. None if it fails."""
    params = (schema.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}
    name = schema["function"]["name"]
    ask = messages + [{"role": "user", "content": f"(Not from the user: give the arguments for {name} for my last request, as JSON only.)"}]
    try:
        args = await asyncio.to_thread(ask_json, ask, params, schemas, 200)
    except Exception as e:
        logger.warning(f"fill_args failed: {e.__class__.__name__}: {e}")
        return None
    if not isinstance(args, dict):
        return None
    missing = [k for k in params.get("required") or [] if args.get(k) in (None, "")]
    return None if missing else args


def _keep_transcript(user_text: str, messages: list, answer: str) -> None:
    """This turn's messages after the system prompt, exactly as sent, plus the answer: the next turn continues them."""
    session().transcript = (user_text, [dict(m) for m in messages[1:]] + [{"role": "assistant", "content": answer}])


async def run_agent(user_text: str, chat_history: list) -> tuple[str, str]:
    """
    Returns (final_answer, tool_log). tool_log is the concatenated tool results,
    used by background learning (reflect_and_learn) like the old planner context.
    """
    reg = registry()
    stable, volatile = build_system_parts(user_text)
    # The system message (and the tool list, which Ollama renders right after it) must be IDENTICAL on every turn.
    # Measured on the NAS: re-reading it costs ~28 s, reusing it ~0.1 s, and qwen3.5 cannot reuse anything after a
    # change, so even the clock ticking one minute meant the full 28 s on every phone request. Everything that
    # changes (time, memory, lessons, briefing, where he is) goes with his message instead.
    messages, tools = shared_prefix()
    from agent import compaction
    earlier = compaction.summary()
    past = [m for m in chat_history[:-1] if m.get("role") in ("user", "assistant")]
    room = OLLAMA_OPTIONS["num_ctx"] - compaction.tokens(messages[0]["content"] + json.dumps(tools))
    # qwen3.5 can only resume from a cache checkpoint near the END of an earlier request, so a turn that rebuilds the
    # history re-reads all of it (~6 s on the NAS). Continuing the last turn's messages exactly re-reads ~700 tokens.
    prev, session().transcript = session().transcript, None   # kept again only if this turn ends with a local answer
    last_user = next((m.get("content") for m in reversed(past) if m.get("role") == "user"), None)
    reuse = bool(prev and prev[0] == last_user and past and past[-1].get("role") == "assistant"
                 and compaction.tokens(json.dumps(prev[1], ensure_ascii=False)) < room * TRANSCRIPT_SHARE)
    # A fact a rebuilt history hides: the phone traces showed it copying an earlier weather answer instead of calling the tool.
    old_note = ("(Your earlier answers in this chat came from tool calls that are not shown here, and their data is old: "
                "for weather, calendar, news or anything live, call the tool again; never repeat an earlier answer. Earlier requests are finished: answer only "
                "my newest message.)\n"
                if not reuse and any(m.get("role") == "assistant" for m in past) else "")
    now_context = (f"=== CONTEXT (now) ===\n{volatile}\n=== SESSION ===\n{session().describe()}\n"
                   + (f"=== EARLIER IN THIS CONVERSATION (summary) ===\n{earlier}\n" if earlier else "") + old_note
                   + "=== MY REQUEST ===\n")
    # The newest turns go in as they are, up to ~70% of the room the fixed prompt leaves (the reply needs the rest).
    # Older ones are folded into the summary above after the reply (compaction.fold) instead of being lost.
    if reuse:
        messages += prev[1]
    else:
        kept, older = compaction.fit(past[-12:], max(300, int(room * 0.7) - compaction.tokens(now_context + user_text)))
        compaction.note_older(past[:-12] + older)
        messages += kept
    # Hebrew message → the model tends to answer in (clumsy, slow) Hebrew. Repeat the
    # rule right next to the request (only in what the model sees, not in saved history).
    lang_hint = ("\n\n(Reply in fluent English — translate any Hebrew content naturally. No emojis.)"
                 if session().lang == "he" else "")
    messages.append({"role": "user", "content": now_context + user_text + lang_hint})

    guard = LoopGuard()
    tool_log: list[str] = []
    nudged = False
    names_fixed = False
    rest_off = os.getenv("AGENT_REST_CHECK", "on").strip().lower() in ("off", "false", "0")
    rest_checks = 0
    failure_checked = False
    playbook_pushed = False
    called: list[str] = []                    # tools that really ran this turn, in order
    playbook = ""                             # "loaded" / "missing": what use_skill returned this turn
    from agent import traces
    import time as _time
    tr = traces.current()
    as_is = ""                                # a SPEAK_AS_IS tool's text: the reply

    async def do(name: str) -> bool:
        """Run the tool the model chose but did not call: it fills the arguments, the tool runs. False if it cannot."""
        schema = next((t for t in tools if t["function"]["name"] == name), None)
        if schema is None:
            return False
        with tr.stage("fill_args"):
            args = await fill_args(messages, schema, tools)
        if args is None:
            return False
        tr.signal("filled_args", f"{name} {json.dumps(args, ensure_ascii=False)[:100]}")
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]})
        nonlocal playbook, as_is
        ran = await run_tool_calls(reg, guard, [(name, args)], tool_log)
        as_is = _as_is(ran)
        for n, result in ran:
            if not result.startswith("[BLOCKED"):
                called.append(n)
            if n == "use_skill":
                playbook = "loaded" if result.startswith("PLAYBOOK") else (playbook or "missing")
            messages.append({"role": "tool", "content": result, "tool_name": n})
        return True

    for turn in range(MAX_TURNS):
        if as_is:
            logger.info("🤖 Agent done: reading the tool's text as it is")
            _keep_transcript(user_text, messages, as_is)
            return as_is, "\n".join(tool_log)
        t0 = _time.perf_counter()
        resp = await asyncio.to_thread(_chat, messages, tools)
        msg = resp.message
        used = (getattr(resp, "prompt_eval_count", 0) or 0) + (getattr(resp, "eval_count", 0) or 0)
        tr.model(AGENT_MODEL, (_time.perf_counter() - t0) * 1000, getattr(resp, "prompt_eval_count", 0), getattr(resp, "eval_count", 0))
        # How long Ollama spent reading the prompt: small when the stable prefix was reused from its cache.
        tr.stages["prompt_read"] = tr.stages.get("prompt_read", 0) + int((getattr(resp, "prompt_eval_duration", 0) or 0) / 1e6)
        if used > OLLAMA_OPTIONS["num_ctx"] * 0.9:
            logger.warning(f"Context almost full: {used} of {OLLAMA_OPTIONS['num_ctx']} tokens (answers may be cut)")

        if not msg.tool_calls:
            answer = strip_thinking(msg.content or "").strip()
            # Honesty check. The model answered without calling any tool. Was a tool needed? It decides itself, from a
            # closed list it cannot dodge (choose_tool): a tool name means "this needed a tool", so the draft was an empty
            # promise or made up, and that one tool is called now. "none" means it really was conversation.
            if not tool_log and not nudged:
                nudged = True
                with tr.stage("choose_tool"):
                    choice = await choose_tool(messages, answer, tools)
                if choice:
                    tr.signal("ungrounded", f"answered without a tool: {answer[:100]}")
                    tr.signal("nudge", f"chose {choice}")
                    logger.warning(f"🤥 Answered without a tool ({answer[:70]!r}) but then chose {choice}")
                    if choice == "ask_big_brain":
                        escalation.set("answered without acting, then asked for the big brain")
                        return "", ""
                    if await do(choice):
                        continue
                    logger.warning(f"🤥 Chose {choice} but could not fill its arguments — escalating")
                    try:
                        from agent import lessons
                        lessons.note_failure("local_model", {"asked": user_text[:60]}, f"[ERROR]: chose {choice} but could not call it; handed to the big brain")
                    except Exception:
                        pass
                    escalation.set(f"chose {choice} but could not call it")
                    return "", ""
            # Translation check: Hebrew calendar titles were copied as-is instead of
            # being explained in English → one rewrite, naturally and with context.
            if not names_fixed and tool_log:
                from agent.lang import has_hebrew
                names = _hebrew_names_from_tools(tool_log)
                if names and has_hebrew(answer):
                    names_fixed = True
                    logger.warning(f"🔤 Answer kept Hebrew titles {names} — asking for a natural English rewrite")
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content":
                        "Rewrite that in fluent, natural English with NO Hebrew letters: say what each event "
                        "means for me using what you know about me (e.g. a title with my girlfriend's name and a "
                        "heart = our date), translate titles by meaning, write names and places in English "
                        "letters, and use flowing sentences instead of a list."})
                    continue
            # A playbook was opened but none of its tools ran: whatever the answer says (weather, events) is made up.
            if playbook == "loaded" and set(called) <= {"use_skill"} and not playbook_pushed and turn < MAX_TURNS - 1:
                playbook_pushed = True
                tr.signal("unfinished", "opened a playbook but called none of its tools")
                logger.warning(f"🧩 Playbook opened but no step done; answer was {answer[:70]!r}")
                messages.append({"role": "assistant", "content": answer})
                messages.append({"role": "user", "content": "(Not from the user: you opened the playbook but called none "
                                 "of its tools, so nothing in that answer is checked. Do its steps now: call the tools.)"})
                continue
            # Completeness check: it acted, but maybe only on part of "do X and Y". It decides itself, from a closed list.
            # A playbook has several steps and the model tends to stop after one: then ask again, up to 3 times.
            # A playbook that does not exist (he removed it) has no steps to finish: no check, so nothing is pushed.
            # After a failure it must not start a different action he did not ask for (a failed WhatsApp became a call).
            allowed = {"loaded": 3, "missing": 0}.get(playbook, 1)
            if tool_log and not rest_off and rest_checks < allowed and turn < MAX_TURNS - 1 and not failed_tools(messages):
                rest_checks += 1
                with tr.stage("rest_check"):
                    rest = await tool_for_rest(messages, answer, called, tools)
                if rest:
                    tr.signal("unfinished", f"after {', '.join(called)} still needed {rest}")
                    logger.warning(f"🧩 Only part of the request was done; still needs {rest}")
                    if await do(rest):
                        continue
            # Honesty after a failure: a tool said it could not (nothing matched, an error) but the answer may still say
            # "done". The model checks its own answer; if it pretended, it tells him what really happened. Once per turn.
            failed = failed_tools(messages) if not failure_checked else []
            if failed:
                failure_checked = True
                with tr.stage("failure_check"):
                    honest = await admits_failure(messages, answer, failed, tools)
                if not honest:
                    tr.signal("claimed_success", f"{failed[0][0]} failed but the answer said: {answer[:100]}")
                    logger.warning(f"🤥 A tool failed ({failed[0][0]}) but the answer claims success: {answer[:70]!r}")
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content": "(Not from the user: that answer was wrong, it did not "
                                     "work. Tell me plainly in one or two short sentences what did not work and why, "
                                     "and what I can do, without calling a tool.)"})
                    continue
            logger.info(f"🤖 Agent done after {turn} tool round(s)")
            if guard.blocks >= 2:
                escalation.set("the loop guard had to stop it twice")
            _keep_transcript(user_text, messages, answer)
            return answer, "\n".join(tool_log)

        messages.append(_msg_to_dict(msg))
        calls = [(c.function.name, dict(c.function.arguments or {})) for c in msg.tool_calls]
        for name, args in calls:
            if name == "ask_big_brain":           # the model itself says this one is beyond it
                escalation.set("asked for the big brain: " + str(args.get("reason", ""))[:120])
                return "", "\n".join(tool_log)
        ran = await run_tool_calls(reg, guard, calls, tool_log)
        as_is = _as_is(ran)
        for name, result in ran:
            if not result.startswith("[BLOCKED"):
                called.append(name)
            if name == "use_skill":
                playbook = "loaded" if result.startswith("PLAYBOOK") else (playbook or "missing")
            messages.append({"role": "tool", "content": result, "tool_name": name})

    # Out of rounds — force a final answer without tools.
    logger.warning("Agent hit MAX_TURNS — forcing final answer")
    escalation.set("ran out of steps")
    messages.append({"role": "user", "content": "Stop using tools. Answer me now with what you found."})
    resp = await asyncio.to_thread(_chat, messages, None)
    return strip_thinking(resp.message.content or "").strip(), "\n".join(tool_log)
