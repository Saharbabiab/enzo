"""
agent/facts.py - long-term memory as separate facts (upgrade plan, Phase 4).

Each fact is one record: text, embedding, source, trust, importance, core, timestamps, superseded_by.
  trust: "trusted"   = the user said it (remember tool, his profile)
         "auto"      = the background note-taker picked it up from his own words
         "untrusted" = saved in a turn that read a web page, mail or notifications (could be planted)
Recall: the small core profile always + the facts most relevant to the request (relevance x importance x recency).
Untrusted and quarantined facts never go into a prompt by themselves. Nothing is deleted: a replaced fact gets
superseded_by, a forgotten one forgotten_at, so every change can be undone.

Storage: Mongo collection "facts" (the old user_memory profile is left untouched); FACTS_STORE=file -> data/facts.json.
Per-turn results go in the volatile context, never the stable system prompt (Ollama prompt cache).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from loguru import logger

FILE = Path(os.getenv("FACTS_FILE") or Path(__file__).resolve().parent.parent / "data" / "facts.json")
DUPLICATE = 0.97        # same fact said again: refresh it, no new record
NEAR = 0.90             # probably about the same thing: the background check decides add / update / supersede / ignore
PROMPT_FLOOR = 0.78     # e5 scores are squeezed into ~0.65-0.9; below this a fact is rarely about the request
TAINT_TOOLS = {"web_search", "read_webpage", "fetch_webpage_text", "email_inbox", "email_read",
               "phone_notifications", "daily_briefing", "morning_briefing", "news_headlines",
               "browser_open", "browser_read", "browser_click", "browser_type", "phone_screen"}

_lock = threading.RLock()
_facts: list[dict] | None = None
_col = None


# ── storage ──────────────────────────────────────────────────────────────────

def _collection():
    if os.getenv("FACTS_STORE", "").lower() == "file":
        return None
    try:
        from shared import memory_store
        if getattr(memory_store, "_ok", False):
            return memory_store.db["facts"]
    except Exception as e:
        logger.warning(f"facts: Mongo not available, using {FILE.name}: {e}")
    return None


_checked = 0.0


def _changed_elsewhere() -> bool:
    """The Telegram bot is another process writing the same collection: reload when its newest change is not ours."""
    global _checked
    if _col is None or _facts is None or time.time() - _checked < 30:
        return False
    _checked = time.time()
    try:
        doc = _col.find_one({}, {"updated_at": 1}, sort=[("updated_at", -1)])
        newest = max((f.get("updated_at") or 0 for f in _facts), default=0)
        return bool(doc) and (doc.get("updated_at") or 0) > newest
    except Exception:
        return False


def _all() -> list[dict]:
    global _facts, _col
    with _lock:
        if _facts is None or _changed_elsewhere():
            _col = _collection()
            if _col is not None:
                _facts = list(_col.find({}))
            else:
                try:
                    _facts = json.loads(FILE.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    _facts = []
            for f in _facts:
                f["_vec"] = np.array(f["emb"], dtype=np.float32) if f.get("emb") else None
        return _facts


def _save(doc: dict) -> None:
    with _lock:
        clean = {k: v for k, v in doc.items() if k != "_vec"}
        if _col is not None:
            _col.replace_one({"_id": doc["_id"]}, clean, upsert=True)
        else:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            data = [{k: v for k, v in f.items() if k != "_vec"} for f in _all()]
            FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def reset_cache() -> None:
    """Tests: forget the loaded facts so the next call reads the store again."""
    global _facts
    with _lock:
        _facts = None


def active(include_untrusted: bool = True) -> list[dict]:
    return [f for f in _all() if not f.get("superseded_by") and not f.get("forgotten_at") and not f.get("quarantined")
            and (include_untrusted or f.get("trust") != "untrusted")]


def get(fid: str) -> dict | None:
    return next((f for f in _all() if f["_id"] == fid), None)


# ── injection scanner ────────────────────────────────────────────────────────

_INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instruction|prompt|rule|previous|above|everything)"
    r"|\b(system prompt|developer message|jailbreak|you are now|act as|pretend to be|new instructions?)\b"
    r"|\b(you|enzo|sharp|assistant|the ai)\s+(must|should|shall|will|have to|need to)\b"
    r"|\b(always|never)\s+(say|reply|answer|respond|send|call|run|open|forward|share|reveal|tell)\b"
    r"|\b(send|forward|email|upload|post|transfer)\b.{0,40}\b(to|at)\b.{0,40}(@|https?://|\bwww\.)"
    r"|\b(call|use|run|execute)\s+(the\s+)?(tool|function|command)\b"
    r"|\b(password|api[ _-]?key|token|secret|credentials?)\b"
    r"|https?://|<\s*/?\s*(system|script|tool)",
    re.IGNORECASE)


def looks_like_instruction(text: str) -> str:
    """The suspicious part of a fact that reads like an order to Enzo (or carries a link or a secret), else ""."""
    m = _INJECTION.search(text or "")
    return m.group(0)[:60] if m else ""


# ── writing ──────────────────────────────────────────────────────────────────

TAINT_SECONDS = 600     # outside text stays in the chat history, so the next turns ("yes, send it") count too


# His own phone screen is outside text for safety (asks before acting), but routines may still be learned from it:
# skill_review keeps only the app and the buttons used, never what the screen said.
SCREEN_TOOLS = {"phone_screen"}


def mark_tainted(name: str = "") -> None:
    """Called when a TAINT_TOOLS tool ran: this session counts as tainted for TAINT_SECONDS."""
    try:
        from agent.context import session
        session().extra["screen_tainted_until" if name in SCREEN_TOOLS else "tainted_until"] = time.time() + TAINT_SECONDS
    except Exception:
        pass


def turn_is_tainted() -> bool:
    """True when this turn, or a turn in the last TAINT_SECONDS, read text Enzo does not control (web, mail, notifications)."""
    try:
        from agent import traces
        from agent.context import session
        if time.time() < max(session().extra.get("tainted_until", 0), session().extra.get("screen_tainted_until", 0)):
            return True
        return any(t["name"] in TAINT_TOOLS for t in traces.current().tools)
    except Exception:
        return False


def _vec(text: str, query: bool = False):
    from agent.embed import embed
    v = embed([text], query=query)
    return None if v is None else v[0]


def _sim(a, b_vec) -> float:
    """Cosine; 0 without the model (then only the exact same text counts as a duplicate)."""
    return float(a @ b_vec) if a is not None and b_vec is not None else 0.0


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2}


def add(text: str, source: str = "user", trust: str = "trusted", importance: float = 0.5, core: bool = False) -> tuple[dict, list[dict]]:
    """Save a fact now (fast, on the voice path). Returns (the record, near-duplicates the background check should
    look at). A fact said again just gets refreshed. Facts not from the user himself that read like an instruction are
    quarantined: stored for review, never shown to a model."""
    text = re.sub(r"\s+", " ", text or "").strip()[:300]
    now = time.time()
    vec = _vec(text)
    with _lock:
        near = []
        for f in active():
            s = _sim(vec, f.get("_vec"))
            if s >= DUPLICATE or f["text"].lower() == text.lower():
                f["updated_at"] = now
                f["importance"] = max(f.get("importance", 0.5), importance)
                f["core"] = f.get("core") or core
                if _rank(trust) > _rank(f.get("trust")):       # he confirmed what was only extracted or read
                    f["trust"], f["source"] = trust, source
                _save(f)
                return f, []
            if s >= NEAR:
                near.append(f)
        doc = {"_id": uuid.uuid4().hex[:12], "text": text, "emb": vec.tolist() if vec is not None else None,
               "source": source, "trust": trust, "importance": float(max(0.0, min(1.0, importance))), "core": bool(core),
               "created_at": now, "updated_at": now, "used_at": None, "superseded_by": None, "forgotten_at": None,
               "quarantined": None}
        suspicious = looks_like_instruction(text) if trust != "trusted" else ""
        if suspicious:
            doc["quarantined"] = f"looks like an instruction: {suspicious!r}"
            logger.warning(f"🛡️ Quarantined a {trust} fact from {source}: {text[:80]!r}")
            near = []
        doc["_vec"] = vec
        _all().append(doc)
        _save(doc)
        return doc, near


def _rank(trust: str | None) -> int:
    return {"untrusted": 0, "auto": 1, "trusted": 2}.get(trust or "", 0)


def supersede(old_id: str, new_id: str) -> None:
    """The new fact replaces the old one. It keeps the old one's trust, core and importance when they are higher:
    the note-taker restating what he said himself must not turn a trusted core fact into an unconfirmed one."""
    with _lock:
        f, n = get(old_id), get(new_id)
        if f and n and f["_id"] != new_id:
            f["superseded_by"], f["updated_at"] = new_id, time.time()
            if _rank(f.get("trust")) > _rank(n.get("trust")):
                n["trust"], n["source"] = f["trust"], f.get("source", n.get("source"))
            n["core"] = bool(n.get("core") or f.get("core"))
            n["importance"] = max(n.get("importance", 0.5), f.get("importance", 0.5))
            _save(f)
            _save(n)


def update_text(fid: str, text: str) -> None:
    with _lock:
        f = get(fid)
        if f:
            vec = _vec(text)
            f["text"], f["emb"], f["_vec"], f["updated_at"] = text[:300], (vec.tolist() if vec is not None else None), vec, time.time()
            _save(f)


def listing() -> list[dict]:
    """For the app's memory page: what is in use plus quarantined ones (to review), newest first."""
    items = [f for f in _all() if not f.get("superseded_by") and not f.get("forgotten_at")]
    return sorted(items, key=lambda f: f.get("updated_at") or 0, reverse=True)


def edit(fid: str, text: str) -> bool:
    """He corrected a fact in the app: his own words now, so trusted."""
    text = re.sub(r"\s+", " ", text or "").strip()
    with _lock:
        f = get(fid)
        if not f or not text:
            return False
        f["trust"], f["source"], f["quarantined"] = "trusted", "user", None
    update_text(fid, text)
    return True


def confirm(fid: str) -> bool:
    """He confirmed a fact Enzo picked up or read (or let a quarantined one in): trusted from now on."""
    with _lock:
        f = get(fid)
        if not f:
            return False
        f["trust"], f["source"], f["quarantined"], f["updated_at"] = "trusted", "user", None, time.time()
        _save(f)
    return True


def forget_id(fid: str) -> bool:
    with _lock:
        f = get(fid)
        if not f:
            return False
        f["forgotten_at"] = f["updated_at"] = time.time()
        _save(f)
    return True


def drop(fid: str) -> None:
    """The background check found a new fact adds nothing: hide it (kept, marked)."""
    with _lock:
        f = get(fid)
        if f:
            f["superseded_by"], f["updated_at"] = "duplicate", time.time()
            _save(f)


def forget(what: str) -> list[str]:
    """Hide the facts that best match `what` (kept with forgotten_at, so it can be undone). Returns their texts."""
    hits = search(what, k=3, include_untrusted=True, floor=0.85)
    if not hits:
        w = _words(what)
        hits = [f for f in active() if w and w <= _words(f["text"])][:3]
    with _lock:
        for f in hits:
            f["forgotten_at"] = f["updated_at"] = time.time()
            _save(f)
    return [f["text"] for f in hits]


# ── recall ───────────────────────────────────────────────────────────────────

def _score(f: dict, rel: float, now: float) -> float:
    # e5 similarities sit in a narrow band (~0.7-0.9), so importance and recency only break near-ties:
    # multiplying them in let an important but unrelated fact beat the one he asked about.
    age_days = (now - (f.get("updated_at") or now)) / 86400
    return rel + 0.03 * f.get("importance", 0.5) + 0.02 * math.exp(-age_days / 120)


def search(query: str, k: int = 6, include_untrusted: bool = False, floor: float = 0.0) -> list[dict]:
    """The facts most relevant to `query`, best first."""
    pool = active(include_untrusted)
    if not pool or not (query or "").strip():
        return []
    q = _vec(query, query=True)
    now = time.time()
    scored = []
    qw = _words(query)
    for f in pool:
        if q is not None and f.get("_vec") is not None:
            rel = float(q @ f["_vec"])
        else:      # no model: word overlap, scaled into the same range
            fw = _words(f["text"])
            rel = 0.7 + 0.3 * (len(qw & fw) / max(1, len(qw))) if qw & fw else 0.0
        if rel >= floor:
            scored.append((_score(f, rel, now), f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:k]]


def core(limit: int = 12) -> list[dict]:
    """The small profile that is always in the context: core facts the user gave himself."""
    cs = [f for f in active(False) if f.get("core") and f.get("trust") == "trusted"]
    cs.sort(key=lambda f: -f.get("importance", 0.5))
    return cs[:limit]


def prompt_block(query: str, k: int = 6) -> str:
    """For the per-turn context: the core profile + the facts relevant to this request."""
    base = core()
    ids = {f["_id"] for f in base}
    rel = [f for f in search(query, k=k + len(base), floor=PROMPT_FLOOR) if f["_id"] not in ids][:k]
    now = time.time()
    with _lock:
        for f in rel:
            f["used_at"] = now                 # in memory only: a write per turn is not worth it
    lines = [f"- {f['text']}" for f in base]
    lines += [f"- {f['text']}" + (" (picked up, not confirmed)" if f.get("trust") == "auto" else "") for f in rel]
    return "\n".join(lines) if lines else "Nothing saved yet."


def describe(f: dict) -> str:
    when = time.strftime("%Y-%m-%d", time.localtime(f.get("updated_at") or time.time()))
    tag = {"trusted": "he said", "auto": "picked up", "untrusted": "from a web page or message, unverified"}.get(f.get("trust"), "")
    return f"{f['text']} ({tag}, {when})"


# ── migration from the old profile document (user_memory, kept untouched) ──

def migrate_from_profile(profile: dict) -> int:
    """Copy the old key-value profile into facts once (source "profile"). Safe to run again: duplicates refresh."""
    if any(f.get("source") == "profile" for f in _all()):
        return 0
    n = 0
    for key, val in (profile.get("owner") or {}).items():
        if str(val).strip():
            add(f"The user's {key.replace('_', ' ')}: {val}", "profile", "trusted", 0.8, core=True); n += 1
    for name, data in (profile.get("people") or {}).items():
        rel = data.get("relation_to_user", "") if isinstance(data, dict) else str(data)
        heb = data.get("hebrew_name", "") if isinstance(data, dict) else ""
        label = f"{name} / {heb}" if heb else name
        add(f"{label} is the user's {rel}" if rel else f"The user knows {label}", "profile", "trusted", 0.8, core=bool(rel)); n += 1
    for section in ("preferences", "routines", "misc"):
        for key, val in (profile.get(section) or {}).items():
            if isinstance(val, list):
                val = ", ".join(map(str, val))
            if str(val).strip():
                add(f"The user's {key.replace('_', ' ')}: {val}", "profile", "trusted", 0.6); n += 1
    logger.info(f"🧠 Migrated {n} facts from the old profile")
    return n
