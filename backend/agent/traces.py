"""
agent/traces.py - one record per turn: what was asked, which path answered, every tool call, how long each part took,
and SIGNALS about whether it really went well. Everything Enzo later learns from comes from here.

Signals (name -> meaning):
    tool_error      a tool returned an error
    nudge           the model answered without acting and had to be pushed
    ungrounded      the grounding check caught a made-up or empty answer
    escalated       handed over to the big brain (detail = why)
    fallback        MAIN_BRAIN=claude, but Claude failed, so the local model answered (detail = why)
    loop_guard      the loop guard blocked a repeated call
    repeated        the user said (nearly) the same thing again within 2 minutes -> the first answer was not good
    interrupted     the user cut Enzo off while it was talking
    feedback_good / feedback_bad    the user said so (thumbs in the app, or by voice through the feedback tool)

Storage: MongoDB collection "traces" (kept 90 days); if Mongo is down, data/traces.jsonl. Writing happens in a
worker thread after the reply, so the voice path never waits for it. Secrets are stripped before anything is stored.
"""

from __future__ import annotations

import contextvars
import difflib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

FILE = Path(os.getenv("TRACE_FILE") or Path(__file__).resolve().parent.parent / "data" / "traces.jsonl")
KEEP_DAYS = int(os.getenv("TRACE_KEEP_DAYS", "90"))
REPEAT_WINDOW = 120            # seconds
BAD_SIGNALS = {"repeated", "feedback_bad", "interrupted"}
GOOD_SIGNALS = {"feedback_good"}

_SECRET_RES = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_\-]{20,}|xox[abp]-[A-Za-z0-9\-]{10,})\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b(password|passwd|token|api[_-]?key|secret)(\s*[=:]\s*)\S{6,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),                 # Telegram bot token
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"),                           # user:password@ in a URL (Mongo, git)
]


def redact(text) -> str:
    """Strip things that look like keys, tokens and passwords."""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    s = _SECRET_RES[0].sub("[secret]", s)
    s = _SECRET_RES[1].sub(r"\1 [secret]", s)
    s = _SECRET_RES[2].sub(r"\1\2[secret]", s)
    s = _SECRET_RES[3].sub("[secret]", s)
    return _SECRET_RES[4].sub(r"\1[secret]@", s)


def _redact_log(record) -> None:
    record["message"] = redact(record["message"])


logger.configure(patcher=_redact_log)      # every log line of every process that imports traces loses its secrets


def _clip(text, n: int) -> str:
    s = redact(text)
    return s if len(s) <= n else s[:n] + "..."


@dataclass
class Trace:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)
    channel: str = "web"
    app: str = "web"
    text: str = ""
    route: str = ""                 # instant | phone_intent | intent | recall | briefing | local | claude | gemini
    models: list = field(default_factory=list)      # [{"model", "ms", "in", "out", "cached"}]
    tools: list = field(default_factory=list)       # [{"name", "args", "result", "ms", "ok"}]
    signals: list = field(default_factory=list)     # [{"name", "detail", "ts"}]
    lessons: list = field(default_factory=list)     # ids of the lesson notes this turn was shown (lessons.score)
    stages: dict = field(default_factory=dict)      # stage name -> ms
    reply: str = ""
    ms: int = 0
    cost_usd: float = 0.0
    _open: dict = field(default_factory=dict, repr=False)

    # ── recording ──
    def signal(self, name: str, detail: str = "") -> None:
        self.signals.append({"name": name, "detail": _clip(detail, 200), "ts": time.time()})

    def tool(self, name: str, args, result: str, ms: float) -> None:
        ok = not str(result).lstrip().upper().startswith(("[ERROR", "[BLOCKED", "[SHELL ERROR", "[SKILL ERROR"))
        self.tools.append({"name": name, "args": _clip(args, 300), "result": _clip(result, 400), "ms": int(ms), "ok": ok})
        if not ok:
            self.signal("tool_error", f"{name}: {str(result)[:120]}")

    def model(self, model: str, ms: float, tokens_in: int = 0, tokens_out: int = 0, cached: int = 0, cost: float = 0.0) -> None:
        self.models.append({"model": model, "ms": int(ms), "in": int(tokens_in or 0), "out": int(tokens_out or 0), "cached": int(cached or 0)})
        self.cost_usd += cost

    def stage(self, name: str):
        """with trace.stage("grounding"): ...  -> adds the time to stages[name]."""
        return _Stage(self, name)

    def has(self, name: str) -> bool:
        return any(s["name"] == name for s in self.signals)

    def to_doc(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["stages"] = {k: v for k, v in self.stages.items() if not k.startswith("_")}
        d["text"], d["reply"] = _clip(self.text, 600), _clip(self.reply, 800)
        d["at"] = datetime.fromtimestamp(self.ts, tz=timezone.utc)
        d["outcome"] = outcome(d)
        return d


class _Stage:
    def __init__(self, trace: Trace, name: str):
        self.trace, self.name = trace, name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.trace.stages[self.name] = self.trace.stages.get(self.name, 0) + int((time.perf_counter() - self.t0) * 1000)
        return False


def outcome(doc: dict) -> str:
    """bad / good / unknown, from real signals only. "No exception" is NOT success."""
    names = {s["name"] for s in doc.get("signals", [])}
    if names & BAD_SIGNALS:
        return "bad"
    if names & GOOD_SIGNALS:
        return "good"
    return "unknown"


# ── the current turn ─────────────────────────────────────────────────────────

_current: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("enzo_trace", default=None)
_NULL = Trace(id="null")           # a sink, so callers never need "if trace"


def current() -> Trace:
    t = _current.get()
    if t is None:
        _NULL.models.clear(); _NULL.tools.clear(); _NULL.signals.clear(); _NULL.stages.clear()
        return _NULL
    return t


def start(text: str) -> Trace:
    """Begin the trace for this turn. Also notices "he is repeating himself" and marks the previous turn."""
    from agent.context import session
    s = session()
    t = Trace(text=text, channel=s.device if s.device == "telegram" else "web", app=s.app)
    prev = s.extra.get("last_trace")
    if prev and time.time() - prev["ts"] <= REPEAT_WINDOW and _similar(prev["text"], text):
        add_signal(prev["id"], "repeated", text)
    s.extra["prev_trace_id"] = prev["id"] if prev else ""
    s.extra["last_trace"] = {"id": t.id, "ts": t.ts, "text": text}
    _current.set(t)
    return t


def finish(reply: str) -> Trace:
    t = current()
    if t is _NULL:
        return t
    t.reply = reply or ""
    t.ms = int((time.time() - t.ts) * 1000)
    _current.set(None)
    doc = t.to_doc()
    threading.Thread(target=_store, args=(doc,), daemon=True).start()
    try:
        from agent import events
        events.emit("turn_finished", trace=t)
    except Exception:
        pass
    return t


def _similar(a: str, b: str) -> bool:
    a, b = re.sub(r"\W+", " ", a.lower()).strip(), re.sub(r"\W+", " ", b.lower()).strip()
    if len(a) < 6 or len(b) < 6:           # "yes", "ok", "next": repeating these means nothing
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


# ── storage ──────────────────────────────────────────────────────────────────

_col = None
_col_tried = False
_lock = threading.Lock()


def _collection():
    global _col, _col_tried
    if _col_tried:
        return _col
    _col_tried = True
    if os.getenv("TRACE_STORE", "").lower() == "file":
        return None
    try:
        from shared import memory_store
        if getattr(memory_store, "_ok", False):
            _col = memory_store.db["traces"]
            _col.create_index("at", expireAfterSeconds=KEEP_DAYS * 86400, background=True)
            _col.create_index("id", background=True)
    except Exception as e:
        logger.warning(f"traces: Mongo not available, using {FILE.name}: {e}")
        _col = None
    return _col


def _store(doc: dict) -> None:
    try:
        col = _collection()
        if col is not None:
            col.insert_one(dict(doc))
            return
    except Exception as e:
        logger.warning(f"traces: Mongo write failed, using the file: {e}")
    try:
        d = dict(doc)
        d["at"] = d["at"].isoformat()
        with _lock:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            with FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"traces: could not store a trace: {e}")


def add_signal(trace_id: str, name: str, detail: str = "") -> None:
    """Add a signal to a turn that is already stored (feedback, "he repeated himself", barge-in)."""
    sig = {"name": name, "detail": _clip(detail, 200), "ts": time.time()}
    cur = _current.get()
    if cur is not None and cur.id == trace_id:
        cur.signals.append(sig)
        return
    threading.Thread(target=_add_signal_stored, args=(trace_id, sig), daemon=True).start()


def _add_signal_stored(trace_id: str, sig: dict) -> None:
    time.sleep(0.3)                               # the turn's own write may still be on its way
    try:
        col = _collection()
        if col is not None:
            doc = col.find_one({"id": trace_id}, {"signals": 1})
            if doc is not None:
                signals = doc.get("signals", []) + [sig]
                col.update_one({"id": trace_id}, {"$set": {"signals": signals, "outcome": outcome({"signals": signals})}})
            return
        with _lock:
            if not FILE.exists():
                return
            lines = FILE.read_text(encoding="utf-8").splitlines()
            for i in range(len(lines) - 1, max(-1, len(lines) - 200), -1):
                d = json.loads(lines[i])
                if d.get("id") == trace_id:
                    d.setdefault("signals", []).append(sig)
                    d["outcome"] = outcome(d)
                    lines[i] = json.dumps(d, ensure_ascii=False, default=str)
                    FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    break
    except Exception as e:
        logger.warning(f"traces: could not add signal {sig['name']}: {e}")


def last_trace_id() -> str:
    """The turn before the current one in this session (what "that was wrong" refers to)."""
    from agent.context import session
    s = session()
    cur = _current.get()
    prev = s.extra.get("prev_trace_id", "")
    last = (s.extra.get("last_trace") or {}).get("id", "")
    return prev if (cur is not None and last == cur.id) else last


def recent(hours: float = 24, limit: int = 2000) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        col = _collection()
        if col is not None:
            return list(col.find({"at": {"$gte": since}}, {"_id": 0}).sort("at", -1).limit(limit))
    except Exception as e:
        logger.warning(f"traces: read failed: {e}")
    out = []
    if FILE.exists():
        for line in FILE.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                d = json.loads(line)
                if d.get("ts", 0) >= since.timestamp():
                    out.append(d)
            except Exception:
                continue
    return out[::-1]


# ── report ───────────────────────────────────────────────────────────────────

def report(hours: float = 24) -> str:
    """A plain summary of how the last hours went: for the user ("how did you do today") and for the nightly review."""
    docs = recent(hours)
    if not docs:
        return f"No conversations recorded in the last {hours:g} hours."
    n = len(docs)
    routes: dict[str, int] = {}
    sig: dict[str, int] = {}
    tool_fail: dict[str, int] = {}
    esc: dict[str, int] = {}
    for d in docs:
        routes[d.get("route") or "?"] = routes.get(d.get("route") or "?", 0) + 1
        for s in d.get("signals", []):
            sig[s["name"]] = sig.get(s["name"], 0) + 1
            if s["name"] == "escalated":
                esc[s.get("detail") or "?"] = esc.get(s.get("detail") or "?", 0) + 1
        for t in d.get("tools", []):
            if not t.get("ok", True):
                tool_fail[t["name"]] = tool_fail.get(t["name"], 0) + 1
    times = sorted(d.get("ms", 0) for d in docs)
    bad = [d for d in docs if d.get("outcome") == "bad" or outcome(d) == "bad"]
    good = sum(1 for d in docs if outcome(d) == "good")
    cloud = [m for d in docs for m in d.get("models", []) if m.get("model", "").startswith(("claude", "gemini"))]
    c_in, c_out = sum(m.get("in", 0) for m in cloud), sum(m.get("out", 0) for m in cloud)
    try:        # prices are set in .env (dollars per million tokens); without them only tokens are reported
        cost = (c_in * float(os.environ["CLOUD_USD_PER_MTOK_IN"]) + c_out * float(os.environ["CLOUD_USD_PER_MTOK_OUT"])) / 1e6
        cost_line = f"Big brain use: {len(cloud)} calls, {c_in} tokens in, {c_out} out, about ${cost:.2f}."
    except (KeyError, ValueError):
        cost_line = f"Big brain use: {len(cloud)} calls, {c_in} tokens in, {c_out} out."
    top = lambda m: ", ".join(f"{k} {v}" for k, v in sorted(m.items(), key=lambda kv: -kv[1])[:6]) or "none"
    lines = [
        f"Last {hours:g} hours: {n} turns. Answered by: {top(routes)}.",
        f"Reply time: typical {times[n // 2] / 1000:.1f} s, slowest tenth above {times[min(n - 1, int(n * 0.9))] / 1000:.1f} s.",
        f"Went badly (repeated, interrupted or thumbs down): {len(bad)}. Marked good: {good}. No signal either way: {n - len(bad) - good}.",
        f"Signals: {top(sig)}.",
        f"Tools that failed: {top(tool_fail)}.",
        f"Handed to the big brain because: {top(esc)}.",
        cost_line,
    ]
    if bad:
        lines.append("Turns that went badly: " + " | ".join(f"\"{_clip(d.get('text', ''), 60)}\"" for d in bad[:5]))
    return "\n".join(lines)
