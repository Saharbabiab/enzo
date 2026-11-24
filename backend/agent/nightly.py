"""
agent/nightly.py - the learning loop's nightly review (Phase 6).

Once a night (NIGHTLY_HOUR, default 03:00, when no turn is running):
1. Score the lesson notes from the day's trace outcomes (lessons.score) and count, per tool and per route, how
   often it worked (data/tool_stats.json, shown in system status).
2. Claude grades a sample of the day's turns plus the tool failures and answers with note changes (add / fix /
   retire) and playbook ideas from routines that went well. Notes change at once (they are only advice and are
   scored every night); playbooks go to data/skills/_proposed/ and wait for the user ("approve <name>").
Capped at NIGHTLY_USD (default $0.30) inside CLAUDE_DAILY_USD; skipped when Claude is not available.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime

from loguru import logger

from agent import lessons

HOUR = int(os.getenv("NIGHTLY_HOUR", "3"))
NIGHTLY_USD = float(os.getenv("NIGHTLY_USD", "0.30"))
SAMPLE = 40
STATE = lessons.DATA / "nightly.json"
STATS = lessons.DATA / "tool_stats.json"
MAX_TOKENS = 3000


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def due(now: datetime) -> bool:
    return HOUR <= now.hour < HOUR + 3 and _state().get("day") != now.strftime("%Y-%m-%d")


async def tick(now: datetime) -> None:
    if not due(now):
        return
    lessons.DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"day": now.strftime("%Y-%m-%d")}), encoding="utf-8")   # once a night, even if it fails
    from agent.agent_loop import wait_until_idle
    await asyncio.to_thread(wait_until_idle)
    status = await run()
    STATE.write_text(json.dumps({"day": now.strftime("%Y-%m-%d"), "status": status, "at": int(time.time())}), encoding="utf-8")
    logger.info(f"Nightly review: {status}")


def tool_stats(docs: list[dict]) -> dict:
    """Per tool: calls that worked / failed. Per route (local / claude / ...): turns, and how many went badly."""
    from agent.traces import outcome
    tools: dict[str, dict] = {}
    routes: dict[str, dict] = {}
    for d in docs:
        for t in d.get("tools") or []:
            s = tools.setdefault(t.get("name", "?"), {"ok": 0, "failed": 0})
            s["ok" if t.get("ok") else "failed"] += 1
        r = routes.setdefault(d.get("route") or "?", {"turns": 0, "bad": 0})
        r["turns"] += 1
        r["bad"] += outcome(d) == "bad"
    return {"at": int(time.time()), "turns": len(docs), "tools": tools, "routes": routes}


def stats_line() -> str:
    """For system status: the tools that failed most in the last nightly count."""
    try:
        d = json.loads(STATS.read_text(encoding="utf-8"))
    except Exception:
        return ""
    worst = sorted(((v["failed"], k, v["ok"]) for k, v in d.get("tools", {}).items() if v["failed"]), reverse=True)[:3]
    tail = ", ".join(f"{k} {f} of {f + ok} failed" for f, k, ok in worst) or "no tool failed"
    return f"last night's count: {d.get('turns', 0)} requests, {tail}"


def _sample(docs: list[dict]) -> list[dict]:
    """Bad turns first, then good ones, then turns that used tools (the material for notes and playbooks)."""
    from agent.traces import outcome
    rank = {"bad": 0, "good": 1}
    pool = [d for d in docs if outcome(d) in rank or d.get("tools")]
    pool.sort(key=lambda d: (rank.get(outcome(d), 2), -d.get("ts", 0)))
    return pool[:SAMPLE]


def _brief(d: dict) -> str:
    from agent.traces import outcome
    tools = "; ".join(f"{t.get('name')}({str(t.get('args'))[:80]}) -> {'ok' if t.get('ok') else 'FAILED: ' + str(t.get('result'))[:100]}"
                      for t in d.get("tools") or [])
    sig = ", ".join(s["name"] for s in d.get("signals") or [])
    return (f"[{outcome(d)}] route={d.get('route')} said: {str(d.get('text'))[:200]!r}\n  tools: {tools or 'none'}"
            f"\n  signals: {sig or 'none'}\n  reply: {str(d.get('reply'))[:200]!r}")


_PROMPT = """You review a day of work by Enzo, a voice assistant (a small local model that picks tools; you are its teacher).
Turns are marked [bad] (he repeated himself, interrupted, or said it was wrong), [good] (he praised it) or [unknown].

1. NOTES: short imperative lessons the local model sees when a request is close to them. Using the turns and the
   failures, add notes that would have avoided a bad turn, fix vague ones, retire ones that are wrong or useless.
   At most 5 new notes, the most useful first; each note is about ONE kind of request and names it, so it
   can be matched to similar requests. Never secrets, names or phone numbers. Notes marked src=owner or src=seed are fixed: leave them.
2. PLAYBOOKS: if the same multi-step routine went well more than once and is not an existing playbook, propose it:
   name (lower_snake_case), description ("<what it does>. Use when he says ..."), numbered steps that name the tools,
   tools. Propose nothing if there is no clear repeated routine. At most 2.

Answer with ONLY JSON:
{{"add": ["..."], "retire": ["<note id>"], "edit": [{{"id": "...", "text": "..."}}],
 "playbooks": [{{"name": "...", "description": "...", "steps": "1. ...", "tools": ["..."]}}]}}

CURRENT NOTES (id, src, helped/hurt):
{notes}

EXISTING PLAYBOOKS: {books}

TOOL FAILURES SINCE THE LAST REVIEW:
{fails}

TURNS:
{turns}
"""


def _prompt(docs: list[dict], since: float) -> str:
    from agent import playbooks
    notes = "\n".join(f"{n['id']} [{n['src']}] {n['helped']}/{n['hurt']}: {n['text']}" for n in lessons.notes()) or "(none)"
    fails = "\n".join(f"{r['tool']} {json.dumps(r['args'], ensure_ascii=False)} -> {r['error']}"
                      for r in lessons.failures(since)[-40:]) or "(none)"
    return _PROMPT.format(notes=notes, books=", ".join(sorted(playbooks.all_playbooks())) or "none",
                          fails=fails, turns="\n".join(_brief(d) for d in docs) or "(none)")


def _affordable(prompt: str) -> bool:
    from agent import big_brain
    p_in, p_out, *_ = big_brain.PRICES.get(big_brain.BIG_BRAIN_MODEL, big_brain.PRICES["claude-opus-5-5"])
    est = (len(prompt) / 3 * p_in + MAX_TOKENS * p_out) / 1e6       # ~3 characters per token: errs high
    left = big_brain.DAILY_USD - big_brain.spent_today() if big_brain.DAILY_USD > 0 else NIGHTLY_USD
    return est <= min(NIGHTLY_USD, left)


def propose(pb: dict) -> str:
    """Stage one playbook idea in data/skills/_proposed/<name>/SKILL.md (not live until approve)."""
    from agent import playbooks
    name = str(pb.get("name") or "").strip().lower()
    desc, steps = str(pb.get("description") or "").strip(), str(pb.get("steps") or "").strip()
    if not playbooks.NAME_RE.match(name) or not desc or not steps or name in playbooks.all_playbooks():
        return ""
    tools = [str(t) for t in pb.get("tools") or [] if re.match(r"^[a-z_]+$", str(t))]
    playbooks.save(name, desc, steps, tools, proposed=True)
    return name


async def run() -> str:
    from agent import big_brain, traces
    since = time.time() - 86400
    docs = traces.recent(24)
    counted = lessons.score(docs)
    from agent import skill_review
    retired = skill_review.score(docs)                 # learned routines that keep going badly are retired
    if retired:
        logger.info(f"Nightly: {retired}")
    STATS.write_text(json.dumps(tool_stats(docs)), encoding="utf-8")
    sample = _sample(docs)
    if not sample and not lessons.failures(since):
        return f"scored {counted}; nothing to review"
    if not big_brain.available():
        return f"scored {counted}; Claude not available"
    prompt = _prompt(sample, since)
    while not _affordable(prompt) and len(sample) > 5:
        sample = sample[: len(sample) * 2 // 3]
        prompt = _prompt(sample, since)
    if not _affordable(prompt):
        return f"scored {counted}; skipped review (over the ${NIGHTLY_USD:.2f} cap or the daily budget)"

    def call() -> str:
        r = big_brain._get_client().messages.create(model=big_brain.BIG_BRAIN_MODEL, max_tokens=MAX_TOKENS,
                                                    messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    try:
        text = await asyncio.to_thread(call)
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0)) if m else {}
    except Exception as e:
        logger.warning(f"nightly review failed: {e.__class__.__name__}: {e}")
        return f"scored {counted}; review failed"
    notes = lessons.apply_review(out.get("add") or [], out.get("retire") or [], out.get("edit") or [])
    staged = [n for n in (propose(p) for p in (out.get("playbooks") or [])[:2] if isinstance(p, dict)) if n]
    return f"scored {counted}; notes: {notes}; proposed playbooks: {', '.join(staged) or 'none'}"
