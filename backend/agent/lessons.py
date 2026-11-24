"""
agent/lessons.py - Enzo learns from its own mistakes.

1. Every tool failure is written to data/failures.jsonl (what was asked, which tool, what went wrong).
2. Each lesson is a NOTE with helped / hurt counters. A request gets only the notes close to it (plus the user's own
   rules), in the per-turn context. The trace remembers which notes it showed; turns that went well / badly
   (traces.outcome) score them (score). A learned note that keeps hurting is switched off.
3. Every night agent/nightly.py has Claude review the day's traces and failures: add / fix / retire notes and
   propose playbooks (apply_review, proposals).
The user can also teach it directly ("learn: <rule>") and see the list ("what did you learn").
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from loguru import logger

DATA = Path(os.getenv("ENZO_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
FAILURES = DATA / "failures.jsonl"
LESSONS = DATA / "lessons.json"
MAX_FAILURES = 200
MAX_LESSONS = 25

_SECRET_KEYS = re.compile(r"message|text|content|body|password|token|key|code|number", re.I)


def _clip(v, n: int = 80) -> str:
    return str(v).replace("\n", " ")[:n]


def note_failure(tool: str, args: dict | None, result: str) -> None:
    """Remember that a tool call failed (never the contents of messages, numbers or secrets)."""
    try:
        safe = {k: ("..." if _SECRET_KEYS.search(k) else _clip(v, 60)) for k, v in (args or {}).items()}
        row = {"t": int(time.time()), "tool": tool, "args": safe, "error": _clip(result, 220)}
        DATA.mkdir(parents=True, exist_ok=True)
        lines = FAILURES.read_text(encoding="utf-8").splitlines() if FAILURES.exists() else []
        lines.append(json.dumps(row, ensure_ascii=False))
        FAILURES.write_text("\n".join(lines[-MAX_FAILURES:]) + "\n", encoding="utf-8")
    except Exception as e:                                   # learning must never break the real work
        logger.debug(f"lessons.note_failure: {e}")


def failures(since: float = 0) -> list[dict]:
    try:
        rows = [json.loads(x) for x in FAILURES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return []
    return [r for r in rows if r.get("t", 0) > since]


REPEAT_AT = 3                              # the same tool failing this many times in a day gets a post-mortem
_NOT_TOOLS = ("answer", "local_model")     # failure rows that are not a tool call
_postmortem_sent: dict[str, set] = {}      # day -> tools already reported (in memory: a restart may repeat it once)


def repeat_failures(day: str, since: float) -> list[str]:
    """Once per tool per day: a sentence for the user about each tool that failed REPEAT_AT+ times since `since`."""
    rows = [r for r in failures(since)
            if r.get("tool") not in _NOT_TOOLS and str(r.get("error", "")).lstrip().upper().startswith("[ERROR")]
    sent = _postmortem_sent.setdefault(day, set())
    for k in [k for k in _postmortem_sent if k != day]:
        del _postmortem_sent[k]
    out = []
    for tool in dict.fromkeys(r["tool"] for r in rows):
        mine = [r for r in rows if r["tool"] == tool]
        if len(mine) < REPEAT_AT or tool in sent:
            continue
        sent.add(tool)
        last = mine[-1]["error"].split(":", 1)[-1].strip()[:160]
        out.append(f"Sir, {tool} has failed {len(mine)} times today. The last error was: {last}. "
                   "I have logged it for tonight's review.")
    return out

def load() -> dict:
    try:
        d = json.loads(LESSONS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# Things already learned the hard way (kept in code so a fresh server starts with them).
SEED = [
    "A tool reporting OK is not proof: check the real result (what is playing, what was sent) before telling the user it is done.",
    "If an approach failed once, switch to a different method; never repeat the identical call.",
    "Spotify playback goes to the phone (Smartphone device), never to the PC that happens to be active.",
    "Spotify: to play one song, play its album starting at that track; a bare track command is ignored by the phone app.",
    "Pass people and song titles to tools whole and as he said them: 'dad' stays 'dad' (never a Hebrew word or a spelling of it), 'play hotel california' is query 'hotel california', not one word of it.",
    "Routines: 'when I say X, do Y' teaches a new one (save_playbook; do not do the steps now). A request that matches a PLAYBOOK opens it with use_skill first, even when he adds something to it.",
    "When he says your last answer was wrong, call feedback first, then redo what he asked before. Never delete or change anything to fix your answer unless he asks for that.",
]
PROMPT_K = 5              # notes per request (The user's own rules come on top: they are few and always apply)
PROMPT_FLOOR = 0.82       # measured on real notes: the matching note scores 0.82-0.90, unrelated ones 0.77-0.83
PROMPT_MARGIN = 0.03      # ...so keep only notes close to the best match, not every one above the floor
MAX_ADD = 5               # new notes per nightly review (each one is unproven until scored)
FIXED = ("owner", "seed")  # always shown, never switched off or rewritten by the nightly review
SWITCH_OFF_AT = 3         # a learned note that hurt this many times more than it helped is switched off


def _id(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:8]


def _new(text: str, src: str) -> dict:
    return {"id": _id(text), "text": text, "src": src, "helped": 0, "hurt": 0, "off": False, "t": int(time.time())}


def notes(include_off: bool = False) -> list[dict]:
    """Each lesson is a note: {id, text, src (seed / owner / learned / nightly), helped, hurt, off}."""
    d = load()
    items = d.get("notes")
    if items is None:                                         # old file: a plain list of strings
        items = [_new(str(x), "learned") for x in d.get("lessons", [])]
    seeds = {_id(x) for x in SEED}
    for n in items:
        if n["id"] in seeds:
            n["src"] = "seed"                                 # the old list held the built-in ones too: keep them always on
    have = {n["id"] for n in items}
    items = items + [_new(x, "seed") for x in SEED if _id(x) not in have]
    return [n for n in items if include_off or not n.get("off")]


def lessons() -> list[str]:
    return [n["text"] for n in notes()]


def _save_notes(items: list[dict], **extra) -> None:
    d = load()
    d.pop("lessons", None)
    d["notes"] = items[-MAX_LESSONS * 4:]                     # switched-off ones are kept for a while, then dropped
    d.update(extra)
    DATA.mkdir(parents=True, exist_ok=True)
    LESSONS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def teach(rule: str) -> str:
    rule = re.sub(r"\s+", " ", rule or "").strip(" .")[:240]
    if len(rule) < 8:
        return "Tell me the rule in a full sentence, sir."
    items = notes(include_off=True)
    old = next((n for n in items if n["text"].lower() == rule.lower()), None)
    if old:
        old.update(src="owner", off=False)
    else:
        items.append(_new(rule, "owner"))
    _save_notes(items)
    return "Noted, sir. I will remember that."


def forget(match: str) -> str:
    items = notes(include_off=True)
    gone = [n for n in items if not n.get("off") and match.lower() in n["text"].lower()]
    for n in gone:
        n["off"] = True
        n["src"] = "owner"                                    # he removed it: the nightly job must not bring it back
    _save_notes(items)
    return f"Forgot {len(gone)} lesson(s), sir."


def switch_off(lid: str) -> bool:
    """He removed one note in the app. Built-in (seed) notes stay."""
    items = notes(include_off=True)
    n = next((n for n in items if n["id"] == lid and n.get("src") != "seed"), None)
    if not n:
        return False
    n["off"], n["src"] = True, "owner"
    _save_notes(items)
    return True


_vecs: dict[str, object] = {}


def relevant(query: str) -> list[dict]:
    """The user's own rules and the built-in ones (general, they apply to every request), plus the learned notes closest
    to the request (best scored first without embeddings). Without the built-in ones the loop bench lost ~2.5 points."""
    live = notes()
    own = [n for n in live if n["src"] in FIXED]
    rest = [n for n in live if n["src"] not in FIXED]
    rest.sort(key=lambda n: n["helped"] - n["hurt"], reverse=True)
    if query.strip() and rest:
        try:
            from agent.embed import embed
            missing = [n for n in rest if n["id"] not in _vecs]
            vec = embed([n["text"] for n in missing]) if missing else None
            if vec is not None:
                _vecs.update({n["id"]: v for n, v in zip(missing, vec)})
            q = embed([query], query=True)
            if q is not None and all(n["id"] in _vecs for n in rest):
                scored = sorted(((float(q[0] @ _vecs[n["id"]]), n) for n in rest), key=lambda x: -x[0])
                best = scored[0][0] if scored else 0
                rest = [n for s, n in scored if s >= max(PROMPT_FLOOR, best - PROMPT_MARGIN)]
        except Exception as e:
            logger.debug(f"lessons.relevant: {e}")
    return own + rest[:PROMPT_K]


def prompt_block(query: str = "") -> str:
    items = relevant(query)
    try:
        from agent import traces
        if query.strip():                                     # calls without the request (stable prefix) are not the turn's
            traces.current().lessons = [n["id"] for n in items]   # so the turn's outcome can score these notes
    except Exception:
        pass
    if not items:
        return ""
    return ("=== LESSONS FROM PAST MISTAKES (follow these; do not repeat a failed approach) ===\n"
            + "\n".join(f"- {n['text']}" for n in items) + "\n")


def score(docs: list[dict]) -> int:
    """Turns that went well / badly (traces.outcome) add helped / hurt to the notes they were shown. Each turn counts
    once. A learned note that hurt SWITCH_OFF_AT more times than it helped is switched off (The user's never are)."""
    from agent.traces import outcome
    d = load()
    since = d.get("scored_until", 0)
    items = notes(include_off=True)
    by_id = {n["id"]: n for n in items}
    newest, counted = since, 0
    for doc in docs:
        ts = doc.get("ts", 0)
        newest = max(newest, ts)
        res = outcome(doc)
        if ts <= since or res not in ("good", "bad"):
            continue
        for i in doc.get("lessons") or []:
            if i in by_id:
                by_id[i]["helped" if res == "good" else "hurt"] += 1
                counted += 1
    for n in items:
        if n["src"] not in FIXED and n["hurt"] - n["helped"] >= SWITCH_OFF_AT:
            n["off"] = True
    _save_notes(items, scored_until=newest)
    return counted


def apply_review(add: list[str], retire: list[str], edit: list[dict]) -> str:
    """The nightly review's changes. The user's own rules are never retired or rewritten."""
    items = notes(include_off=True)
    by_id = {n["id"]: n for n in items}
    done = []
    for i in retire:
        n = by_id.get(str(i))
        if n and n["src"] not in FIXED and not n.get("off"):
            n["off"] = True
            done.append(f"retired {n['id']}")
    for e in edit:
        n = by_id.get(str(e.get("id")))
        text = re.sub(r"\s+", " ", str(e.get("text") or "")).strip()[:240]
        if n and n["src"] not in FIXED and len(text) > 8:
            n["text"] = text
            _vecs.pop(n["id"], None)
            done.append(f"edited {n['id']}")
    have = {n["text"].lower() for n in items}
    for text in add[:MAX_ADD]:
        text = re.sub(r"\s+", " ", str(text)).strip()[:240]
        if len(text) > 8 and text.lower() not in have and len([n for n in items if not n.get("off")]) < MAX_LESSONS:
            items.append(_new(text, "nightly"))
            have.add(text.lower())
            done.append("added 1")
    _save_notes(items)
    return ", ".join(done) or "no change"


def summary() -> str:
    items = notes()
    if not items:
        return "I have not learned any lessons yet, sir."
    out = "What I have learned so far, sir:\n" + "\n".join(
        f"- {n['text']}" + (f" (helped {n['helped']} times)" if n["helped"] else "") for n in items)
    waiting = proposals()
    if waiting:
        out += "\nPlaybook ideas waiting for your OK: " + ", ".join(waiting) + "."
    return out


def proposals() -> list[str]:
    """Playbooks the nightly review proposed (data/skills/_proposed/<name>/SKILL.md without code)."""
    from agent.playbooks import OWN_DIR
    base = Path(OWN_DIR) / "_proposed"
    try:
        return sorted(p.parent.name for p in base.glob("*/SKILL.md") if not any(p.parent.glob("*.py")))
    except Exception:
        return []
