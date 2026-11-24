"""
agent/skill_review.py - Enzo learns routines from tasks that went well (Hermes plan H2, docs/HERMES_PLAN.md).

Hermes reviews every 10 tool calls / 10 turns, pushes the model to save "at least one skill update", never checks that
the task worked, and saves straight into live skills (its scan and approval are off by default). Enzo instead:

1. note(trace)  - on "turn_finished": a turn that ran 3+ real actions with no failure signal and read nothing from
                  outside (web, mail, notifications: facts.TAINT_TOOLS) becomes a candidate. His phone screen
                  (phone_screen) does not block it: the review sees only the app and the buttons used.
2. tick()       - every proactive tick: once he has been quiet for REVIEW_AFTER seconds, the candidate's stored trace is
                  read again (did he repeat himself, interrupt or say it was wrong afterwards? then it is dropped),
                  and a cheap Claude model (SKILL_REVIEW_MODEL, default Haiku) decides: a NEW routine, a PATCH to the
                  playbook it used or one about the same thing, or (most turns) nothing.
3. A proposal is staged in data/skills/_proposed/<name>/SKILL.md (never live), checked first (known tools only,
   nothing that reads like an instruction, size caps), and Enzo tells him once: "say approve <name>". At most
   MAX_PER_DAY a day. Approve / reject = enzo_lessons (asks Yes / No before anything goes live).
4. score(docs)  - nightly: every learned playbook counts the turns that used it and how they went
                  (data/skills/_stats.json); one that went badly 3+ times and more often than well is retired
                  (moved to data/skills/_retired/, nothing is deleted) and he is told in the nightly status.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time

from loguru import logger

MIN_ACTIONS = 3
REVIEW_AFTER = float(os.getenv("SKILL_REVIEW_AFTER", "180"))     # seconds of quiet before a turn is judged a success
MAX_PER_DAY = int(os.getenv("SKILL_REVIEW_PER_DAY", "2"))
MODEL = os.getenv("SKILL_REVIEW_MODEL", "claude-haiku-5-5")
MAX_STEPS_CHARS = 2500
# Tools that are not part of doing a task: memory, feedback, routines themselves, status lookups.
NOT_ACTIONS = {"remember", "recall", "forget", "feedback", "enzo_lessons", "save_playbook", "use_skill", "trace_report",
               "setup_status", "setup_skip", "list_skills", "system_status", "ask_big_brain", "create_skill", "remove_skill"}
BAD = {"repeated", "feedback_bad", "interrupted", "tool_error", "loop_guard", "ungrounded", "crashed", "nudge"}

_queue: list[dict] = []
_STATE = {"day": "", "proposed": 0}


def _own_dir() -> str:
    from agent.playbooks import OWN_DIR
    return OWN_DIR


# ── 1. candidates ─────────────────────────────────────────────────────────────

def candidate(trace, tainted: bool) -> dict | None:
    """The parts of a finished turn the review needs, or None when it is not a candidate."""
    tools = list(getattr(trace, "tools", None) or [])
    acts = [t for t in tools if t.get("ok") and t.get("name") not in NOT_ACTIONS]
    if len(acts) < MIN_ACTIONS or tainted:
        return None
    if any(s.get("name") in BAD for s in getattr(trace, "signals", None) or []):
        return None
    if getattr(trace, "route", "") in ("instant", ""):
        return None
    used = [str(t.get("args", "")) for t in tools if t.get("name") == "use_skill"]
    return {"id": trace.id, "ts": trace.ts, "text": trace.text, "reply": trace.reply, "app": getattr(trace, "app", "web"),
            "tools": [{"name": t["name"], "args": t.get("args"), "result": _result(t)} for t in tools],
            "used": used}


def _result(t: dict) -> str:
    """What the review sees of a tool's result. A screen read: only the app (the screen's text is not his and is never
    learned from); a tap: the button's label."""
    r = str(t.get("result", ""))
    if t.get("name") in ("phone_screen",) and r.startswith("[SCREEN]"):
        return r.split("\n", 1)[0]
    return r[:200]


def _tainted(trace) -> bool:
    from agent import facts
    from agent.context import session
    try:
        if time.time() < session().extra.get("tainted_until", 0):
            return True
    except Exception:
        pass
    return any(t.get("name") in facts.TAINT_TOOLS - facts.SCREEN_TOOLS for t in getattr(trace, "tools", None) or [])


def note(trace=None, **_) -> None:
    """events "turn_finished" handler (runs inside the turn's context, so the session's taint is visible)."""
    if trace is None or os.getenv("SKILL_REVIEW", "on").strip().lower() in ("off", "false", "0"):
        return
    c = candidate(trace, _tainted(trace))
    if c:
        _queue.append(c)
        del _queue[:-10]
        logger.debug(f"skill review: candidate {c['text'][:60]!r}")


# ── 2. the review ────────────────────────────────────────────────────────────

_PROMPT = """Enzo, a voice assistant, just handled one request from the user, and it went well (he did not repeat himself,
complain or interrupt). Decide if this turn shows a ROUTINE worth saving as a playbook: a multi-step way of doing
something he is likely to ask for again (e.g. "plan my evening": calendar, then weather, then a suggestion).

Most turns are NOT routines: a one-off lookup, a single action, chat, something tied to one date or person.
For those answer {{"action": "none"}}. Saving nothing is the normal answer.

If it is a routine:
- Prefer improving an existing playbook below that this turn used or that is about the same thing: "patch" with that
  playbook's name and the FULL improved steps.
- Otherwise "new": a short lower_snake_case name (3-40 characters).
- description: "<what it does>. Use when he says ..." (one or two sentences).
- steps: numbered, one tool per step, written for ANY day: no dates, times, names, numbers or results from this turn
  unless they are always the same; never contact details or secrets.
- tools: only names from the TOOLS list.

THIS TURN (on the {app} app):
He said: {text}
Tools, in order:
{tools}
Enzo answered: {reply}

PLAYBOOKS THAT MAY BE RELATED:
{related}

TOOLS: {names}

Answer with ONLY JSON:
{{"action": "none|new|patch", "name": "...", "description": "...", "steps": "1. ...", "tools": ["..."], "why": "one sentence"}}"""


def _related(c: dict, books: dict, k: int = 3) -> list[str]:
    """The playbook this turn used, plus the ones closest to his request (embeddings; word overlap without them)."""
    used = {n for n in books if any(n in u for u in c["used"])}
    try:
        from agent.embed import embed
        names = sorted(books)
        vecs = embed([c["text"]] + [books[n]["description"] for n in names], query=False)
        if vecs is not None:
            sims = sorted(((float(vecs[0] @ v), n) for n, v in zip(names, vecs[1:])), reverse=True)
            used |= {n for s, n in sims[:k] if s > 0.75}
    except Exception:
        pass
    if not used:
        words = set(re.findall(r"\w{4,}", c["text"].lower()))
        used = {n for n, b in books.items() if words & set(re.findall(r"\w{4,}", (b["description"] + " " + n).lower()))}
    return sorted(used)[:k]


def build_prompt(c: dict) -> str:
    from agent import playbooks
    from agent.tools import registry
    books = playbooks.all_playbooks()
    rel = _related(c, books)
    related = "\n\n".join(f"{n}: {books[n]['description']}\n{books[n]['body'][:800]}" for n in rel) or "(none)"
    tools = "\n".join(f"- {t['name']}({str(t.get('args'))[:150]}) -> {t['result'][:120]}" for t in c["tools"])
    return _PROMPT.format(app=c.get("app", "web"), text=c["text"][:400], tools=tools, reply=str(c["reply"])[:300],
                          related=related, names=", ".join(sorted(registry().funcs)))


def check(out: dict, related_ok: set[str] | None = None) -> tuple[dict | None, str]:
    """(proposal, "") when the review's answer is safe to stage, else (None, why not)."""
    from agent import facts, playbooks
    from agent.tools import registry
    action = str(out.get("action") or "none").strip().lower()
    if action not in ("new", "patch"):
        return None, "no routine"
    name = str(out.get("name") or "").strip().lower()
    desc = " ".join(str(out.get("description") or "").split())
    steps = str(out.get("steps") or "").strip()
    if not playbooks.NAME_RE.match(name) or not desc or not steps:
        return None, "incomplete"
    if len(desc) > 300 or len(steps) > MAX_STEPS_CHARS:
        return None, "too long"
    known = set(registry().funcs)
    tools = [str(t) for t in out.get("tools") or []]
    if not tools or any(t not in known for t in tools):
        return None, f"unknown tools: {[t for t in tools if t not in known]}"
    bad = facts.looks_like_instruction(desc + "\n" + steps)
    if bad:
        return None, f"reads like an instruction: {bad!r}"
    live = playbooks.all_playbooks()
    if action == "new" and name in live:
        action = "patch"
    if action == "patch" and name not in live:
        return None, "patch of a playbook that does not exist"
    if action == "patch" and related_ok is not None and name not in related_ok:
        return None, "patch of an unrelated playbook"
    if os.path.isdir(os.path.join(_own_dir(), "_proposed", name)):
        return None, "already waiting for him"
    return {"action": action, "name": name, "description": desc, "steps": steps, "tools": tools,
            "why": " ".join(str(out.get("why") or "").split())[:200]}, ""


def stage(p: dict) -> str:
    from agent import playbooks
    playbooks.save(p["name"], p["description"], p["steps"], p["tools"], proposed=True, source="learned",
                   patch=p["action"] == "patch")
    return p["name"]


def message(p: dict) -> str:
    if p["action"] == "patch":
        return (f"Sir, I found a better way to run your {p['name'].replace('_', ' ')} routine. {p['why']} "
                f"Say \"approve {p['name']}\" to use it, or \"reject {p['name']}\".").replace("  ", " ")
    return (f"Sir, that looked like a routine worth keeping: {p['description']} "
            f"Say \"approve {p['name']}\" to save it as {p['name'].replace('_', ' ')}, or \"reject {p['name']}\".")


async def review(c: dict) -> dict | None:
    """Ask the review model about one candidate. The staged proposal, or None."""
    from agent import big_brain, playbooks
    if not big_brain.available():
        return None
    prompt = build_prompt(c)

    def call() -> str:
        r = big_brain._get_client().messages.create(model=MODEL, max_tokens=900,
                                                    messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    try:
        text = await asyncio.to_thread(call)
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0)) if m else {}
    except Exception as e:
        logger.warning(f"skill review failed: {e.__class__.__name__}: {e}")
        return None
    p, why = check(out, set(_related(c, playbooks.all_playbooks())))
    if not p:
        logger.info(f"skill review: nothing staged for {c['text'][:50]!r} ({why})")
        return None
    stage(p)
    logger.info(f"skill review: proposed {p['action']} {p['name']}")
    return p


def _still_good(c: dict) -> bool:
    """Read the stored trace again: signals added after the reply (he repeated it, said it was wrong) end it."""
    from agent import traces
    try:
        for d in traces.recent(hours=max(1.0, (time.time() - c["ts"]) / 3600 + 0.5)):
            if d.get("id") == c["id"]:
                return not any(s.get("name") in BAD for s in d.get("signals") or [])
    except Exception:
        pass
    return True


def _quiet() -> bool:
    from agent import agent_loop
    return not agent_loop._busy and time.time() - agent_loop._last_turn >= REVIEW_AFTER


async def tick(notify=None) -> dict | None:
    """One candidate per call, once he is quiet. Returns the staged proposal (and tells him), or None."""
    if not _queue or not _quiet():
        return None
    today = time.strftime("%Y-%m-%d")
    if _STATE["day"] != today:
        _STATE.update(day=today, proposed=0)
    if _STATE["proposed"] >= MAX_PER_DAY:
        _queue.clear()
        return None
    c = _queue.pop(0)
    if not _still_good(c):
        return None
    p = await review(c)
    if p:
        _STATE["proposed"] += 1
        if notify is not None:
            try:
                await notify(message(p))
            except Exception as e:
                logger.debug(f"skill review notify: {e}")
    return p


# ── 4. scoring learned playbooks ─────────────────────────────────────────────

def _stats_file() -> str:
    return os.path.join(_own_dir(), "_stats.json")


def stats() -> dict:
    try:
        with open(_stats_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def score(docs: list[dict]) -> str:
    """Count each learned playbook's uses and outcomes in these traces, retire the ones that go badly. Returns a status."""
    from agent import playbooks
    from agent.traces import outcome
    books = playbooks.all_playbooks()
    learned = {n for n, b in books.items() if b.get("own") and b.get("source") == "learned"}
    if not learned:
        return ""
    st = stats()
    seen = set(st.get("_seen", []))
    for d in docs:
        if d.get("id") in seen:
            continue
        for t in d.get("tools") or []:
            if t.get("name") != "use_skill":
                continue
            name = next((n for n in learned if n in str(t.get("args", ""))), None)
            if name:
                s = st.setdefault(name, {"used": 0, "good": 0, "bad": 0})
                s["used"] += 1
                o = outcome(d)
                if o in ("good", "bad"):
                    s[o] += 1
                seen.add(d.get("id"))
    st["_seen"] = sorted(seen)[-2000:]
    retired = []
    for n in sorted(learned):
        s = st.get(n) or {}
        if s.get("bad", 0) >= 3 and s.get("bad", 0) > s.get("good", 0):
            dst = os.path.join(_own_dir(), "_retired", f"{n}-{int(time.time())}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(os.path.join(_own_dir(), n), dst)
            retired.append(n)
    os.makedirs(_own_dir(), exist_ok=True)
    with open(_stats_file(), "w", encoding="utf-8") as f:
        json.dump(st, f)
    return f"learned routines retired: {', '.join(retired)}" if retired else ""


def register() -> None:
    from agent import events
    events.on("turn_finished", note)
