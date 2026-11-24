"""
agent/compaction.py - keep the local model's window small without losing things (upgrade plan, Phase 4).

1. Long tool results: the model sees the start, the full text is kept (Mongo "tool_outputs", 7 days; file
   fallback) and recall(output_id=...) reads it back.
2. Conversation: the newest turns go in as they are, up to ~70% of the room left after the fixed prompt.
   Older turns are folded into a short structured summary AFTER the reply (background_chat, GPU free), and that
   summary goes in the per-turn context (never the stable prompt: Ollama's prompt cache).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from loguru import logger

CUT_AT = int(os.getenv("TOOL_OUTPUT_CUT", "3000"))         # characters the model sees of one tool result
NO_CUT = {"use_skill", "recall"}                             # playbook steps / a read-back must arrive whole
DATA = Path(__file__).resolve().parent.parent / "data"
SUMMARY_FILE = DATA / "conversation_summary.json"
SUMMARY_MAX_AGE = 12 * 3600                                  # the web chat itself is kept 12 h
CHARS_PER_TOKEN = 3.2


# ── 1. long tool outputs ─────────────────────────────────────────────────────

_outputs: dict[str, str] = {}          # fallback when Mongo is down (and in tests)


def _out_col():
    if os.getenv("FACTS_STORE", "").lower() == "file":
        return None
    try:
        from shared import memory_store
        if getattr(memory_store, "_ok", False):
            col = memory_store.db["tool_outputs"]
            col.create_index("at", expireAfterSeconds=7 * 86400, background=True)
            return col
    except Exception:
        pass
    return None


def cut(name: str, text: str) -> str:
    """The start of a long tool result + a pointer to the full text."""
    if name in NO_CUT or not isinstance(text, str) or len(text) <= CUT_AT:
        return text
    oid = uuid.uuid4().hex[:10]
    try:
        col = _out_col()
        if col is not None:
            from datetime import datetime, timezone
            col.insert_one({"_id": oid, "tool": name, "text": text, "at": datetime.now(timezone.utc)})
        else:
            _outputs[oid] = text
    except Exception as e:
        logger.warning(f"tool output not stored ({e}); cutting without a pointer")
        return text[:CUT_AT] + f"\n[... {len(text) - CUT_AT} more characters cut]"
    return text[:CUT_AT] + f"\n[... {len(text) - CUT_AT} more characters cut. Full text: recall(output_id=\"{oid}\")]"


def read_output(oid: str, query: str = "") -> str:
    """The full text of a cut tool result (up to 8000 characters; `query` = a number to start from that character)."""
    text = _outputs.get(oid)
    if text is None:
        try:
            col = _out_col()
            doc = col.find_one({"_id": oid}) if col is not None else None
            text = doc and doc.get("text")
        except Exception:
            text = None
    if not text:
        return f"[ERROR]: No saved tool output {oid!r} (they are kept 7 days)."
    start = int(query) if str(query).strip().isdigit() else 0
    part = text[start:start + 8000]
    more = len(text) - start - len(part)
    return part + (f"\n[... {more} more characters: recall(output_id=\"{oid}\", query=\"{start + 8000}\")]" if more > 0 else "")


# ── 2. conversation summary ──────────────────────────────────────────────────

def tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def _key() -> str:
    from agent.context import session
    return "telegram" if session().device == "telegram" else "web"


def _load() -> dict:
    try:
        return json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _h(m: dict) -> str:
    return hashlib.sha1(f"{m.get('role')}:{m.get('content')}".encode("utf-8", "ignore")).hexdigest()[:12]


def summary() -> str:
    s = _load().get(_key()) or {}
    return s.get("text", "") if time.time() - s.get("ts", 0) < SUMMARY_MAX_AGE else ""


def clear() -> None:
    """He said "forget our conversation": the summary goes too."""
    data = _load()
    data.pop(_key(), None)
    _write(data)


def fit(past: list[dict], budget_tokens: int) -> tuple[list[dict], list[dict]]:
    """(kept, older). Newest first into the budget, each cut to 500 characters. Always the same cut, so a message
    looks the same on every turn and Ollama can reuse the cached history."""
    kept: list[dict] = []
    used = 0
    for i, m in enumerate(reversed(past)):
        c = m.get("content") or ""
        c = c[:500] + ("..." if len(c) > 500 else "")
        t = tokens(c)
        if kept and used + t > budget_tokens:
            return list(reversed(kept)), past[:len(past) - i]
        kept.append({**m, "content": c})
        used += t
    return list(reversed(kept)), []


# The messages run_agent left out on its last turn, folded in after the reply (see fold()).
_older: dict[str, list[dict]] = {}


def note_older(older: list[dict]) -> None:
    _older[_key()] = older


_FOLD_PROMPT = """Update the running summary of this conversation between the user and you (Enzo) with the older turns below.
Keep it short (at most 120 words), in English, with exactly these lines:
Topics: ...
He told me: ... (facts, names, numbers he gave)
Done: ... (what I did for him)
Open: ... (questions not answered, things I promised or he still wants)
Write "-" for an empty line. Only the summary.

SUMMARY SO FAR:
{old}

OLDER TURNS:
{turns}"""


async def fold() -> None:
    """After the reply: add the turns that no longer fit the window to the summary. Uses background_chat, so the
    agent's cached prompt survives."""
    import asyncio
    key = _key()
    older = _older.pop(key, [])
    data = _load()
    cur = data.get(key) or {}
    if time.time() - cur.get("ts", 0) >= SUMMARY_MAX_AGE:
        cur = {}
    done = set(cur.get("seen", []))
    new = [m for m in older if _h(m) not in done]
    if not new:
        return
    from agent.agent_loop import side_text
    turns = "\n".join(f"{'The user' if m.get('role') == 'user' else 'Enzo'}: {(m.get('content') or '')[:400]}" for m in new)
    try:
        text = (await asyncio.to_thread(side_text, _FOLD_PROMPT.format(old=cur.get("text") or "-", turns=turns),
                                        num_predict=220, temperature=0.2)).strip()
    except Exception as e:
        logger.debug(f"compaction fold: {e}")
        return
    if not text:
        return
    data = _load()
    data[key] = {"text": text[:1200], "ts": time.time(), "seen": (list(done) + [_h(m) for m in new])[-80:]}
    _write(data)
    logger.info(f"🗜️ Folded {len(new)} older messages into the conversation summary")
