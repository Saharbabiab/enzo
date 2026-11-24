"""
agent/bench.py - the tool-call benchmark (task E-1.1).

Enzo is meant to be model-driven: the model picks the tool and its arguments. This measures how well it does
that, with NO regex layer in between: each command in bench/commands.jsonl goes straight to the model with the real
system prompt and the real tool list, and we judge the model's FIRST tool call (or the absence of one).

    python -m agent.bench                      # local model (Ollama, the one Enzo runs)
    python -m agent.bench --backend claude     # Claude, for comparison
    python -m agent.bench --category reminders --limit 5
    python -m agent.bench --save               # also writes bench/results/<time>-<backend>.json
    python -m agent.bench --loop               # the REAL agent loop (nudge, grounding check, several steps) with fake tools

--loop scores the whole turn, not the first call: every expected tool must have been called (any order, with the
expected arguments) and Enzo must end with an answer. A turn the local model hands to the big brain counts as
"escalated": not a pass, not a wrong answer either (in real use Claude takes over), and is reported separately.
Case fields for --loop: "tools" = several tools that must ALL be called (multi-step), "result" = what the fake tool returns.

Case fields: id, category, session ("android"/"web"), text, tool (name, list of acceptable names, or null = must
NOT call a tool), args (name -> expected value: a number must match, a string must be contained, case-insensitive),
future (true = the tool does not exist yet; reported separately, not scored), note.
Run it before and after any change to prompts, tools or routing. The score must not go down.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = ROOT / "bench" / "commands.jsonl"
RESULTS_DIR = ROOT / "bench" / "results"

Calls = list  # [(tool name, args dict), ...]


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def _arg_ok(want, got) -> bool:
    if isinstance(want, list):                       # any of these is right ("mom" / "mother")
        return any(_arg_ok(w, got) for w in want)
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return abs(float(got) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(want).lower() in str(got).lower()


def judge(case: dict, calls: Calls, answer: str = "") -> tuple[bool, str]:
    """(passed, why not). Only the first tool call counts. A list may contain null = "answering without a tool is fine";
    "ask_ok": true accepts a clarifying question (or an ask_user call) instead of a call."""
    expected = case.get("tool")
    if expected is None:
        return (not calls, f"called {calls[0][0]} instead of just answering" if calls else "")
    names = expected if isinstance(expected, list) else [expected]
    if not calls:
        if None in names:
            return True, ""
        if case.get("ask_ok") and (answer or "").strip().endswith("?"):
            return True, ""
        return False, "no tool call"
    name, args = calls[0]
    if name == "ask_user" and case.get("ask_ok"):
        return True, ""
    if name not in names:
        return False, f"called {name}, wanted {'/'.join(str(n) for n in names)}"
    for key, want in (case.get("args") or {}).items():
        if key not in args or not _arg_ok(want, args[key]):
            return False, f"{name}: {key}={args.get(key)!r}, wanted {want!r}"
    return True, ""


def validate_cases(cases: list[dict], schemas: list[dict]) -> list[str]:
    """Problems in the case file itself (unknown tool, unknown argument): keeps the benchmark honest."""
    tools = {s["function"]["name"]: set(s["function"]["parameters"].get("properties", {})) for s in schemas}
    problems = []
    seen = set()
    for c in cases:
        if c["id"] in seen:
            problems.append(f"{c['id']}: duplicate id")
        seen.add(c["id"])
        if c.get("future"):
            continue
        exp = c.get("tool")
        for name in ([] if exp is None else exp if isinstance(exp, list) else [exp]):
            if name in ("NEED_SKILL", None):
                continue
            if name not in tools:
                problems.append(f"{c['id']}: unknown tool {name}")
            else:
                for arg in (c.get("args") or {}):
                    if arg not in tools[name] and len(([] if exp is None else exp if isinstance(exp, list) else [exp])) == 1:
                        problems.append(f"{c['id']}: {name} has no argument {arg}")
    return problems


# ── Backends: ask the model, return its tool calls ─────────────────────────────

def _system_for(case: dict) -> str:
    from agent.agent_loop import _AGENT_RULES
    from agent.context import new_session
    from agent.responder import build_system_parts
    s = new_session()
    s.app = case.get("session", "web")
    s.popup = bool(case.get("popup"))
    stable, volatile = build_system_parts(case["text"])
    return stable + _AGENT_RULES + "\n" + volatile + f"\n=== SESSION ===\n{s.describe()}\n"


async def ask_local(case: dict) -> tuple[Calls, str]:
    from agent import agent_loop
    from agent.tools import registry
    from shared import strip_thinking
    messages = [{"role": "system", "content": _system_for(case)}, {"role": "user", "content": case["text"]}]
    from agent.tool_select import select_tools        # the same selection the real agent uses
    tools = select_tools(case["text"], registry().schemas, app=case.get("session", "web"))
    resp = await asyncio.to_thread(agent_loop._chat, messages, tools)
    msg = resp.message
    calls = [(c.function.name, dict(c.function.arguments or {})) for c in (msg.tool_calls or [])]
    answer = strip_thinking(msg.content or "").strip()
    if not calls and "NEED_SKILL" in answer:
        calls = [("NEED_SKILL", {})]
    return calls, answer


async def ask_claude(case: dict) -> tuple[Calls, str]:
    from agent import big_brain
    resp = await asyncio.to_thread(
        big_brain._create, [{"type": "text", "text": _system_for(case)}],
        [{"role": "user", "content": case["text"]}], big_brain._anthropic_tools())
    calls = [(b.name, dict(b.input or {})) for b in resp.content if getattr(b, "type", "") == "tool_use"]
    answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if not calls and "NEED_SKILL" in answer:
        calls = [("NEED_SKILL", {})]
    return calls, answer


# ── The real loop with fake tools ──────────────────────────────────────────────

_FAKE_READS = {
    "get_current_weather": "Beer Sheva: 24 C, clear sky, light wind.",
    "calendar_events": "Today 14/11 16:00 - Dentist @ Clinic\nTomorrow 15/11 10:00 - Team meeting",
    "email_inbox": "1. From Dana: lunch tomorrow?\n2. From GitHub: new sign-in",
    "list_reminders": "1 reminder: call mom in 20 min [a1b2c3]",
    "web_search": "Top result: (benchmark) a short factual answer is available on the first page.",
    "system_status": "HOST: NAS, up 3 days\nCPU: 12% busy\nRAM: 9.1 of 32 GB used\nWARNINGS: none",
    "morning_briefing": "Good morning. 24 C and clear. One event: dentist at 16:00. No urgent mail.",
    "get_user_location": "Beer Sheva, Israel",
    "trace_report": "Last 24 hours: 31 turns. Went badly: 2.",
    "list_files": "notes.txt\nreport.pdf",
}


def _fake_result(case: dict, name: str) -> str:
    custom = case.get("result")
    if isinstance(custom, dict) and name in custom:
        return custom[name]
    if isinstance(custom, str):
        return custom
    return _FAKE_READS.get(name) or f"[OK]: {name} done."


def judge_loop(case: dict, calls: Calls, answer: str) -> tuple[str, str]:
    """("pass" | "escalated" | "fail", why). Scores the whole turn."""
    expected = case.get("tools") or case.get("tool")
    if not (answer or "").strip():
        names = expected if isinstance(expected, list) else [expected]
        if "ask_big_brain" in names and not case.get("tools"):
            return "pass", ""                                # handing this one over IS the right move
        return "escalated", "handed to the big brain"
    if "NEED_SKILL" in answer:
        names = expected if isinstance(expected, list) else [expected]
        return ("pass", "") if "NEED_SKILL" in names else ("escalated", "asked for a new skill")
    called = [c[0] for c in calls]
    wrong = [n for n in case.get("not_tools") or [] if n in called]
    if wrong:
        return "fail", f"called {wrong[0]}, which this case must not use"
    names = expected if isinstance(expected, list) else [expected]
    if "ask_big_brain" in names and os.getenv("MAIN_BRAIN", "").lower() == "claude":
        return "pass", ""                                    # Claude IS the big brain: doing the hard job itself is right
    if expected is None:
        acting = [n for n in called if n not in ("remember", "feedback")]      # noting a fact on the side is fine
        return ("fail", f"called {acting[0]} instead of just answering") if acting else ("pass", "")
    if case.get("tools"):                                   # multi-step: all of them
        missing = [n for n in case["tools"] if n not in called]
        return ("fail", f"never called {', '.join(missing)} (called {called or 'nothing'})") if missing else ("pass", "")
    names = expected if isinstance(expected, list) else [expected]
    hits = [c for c in calls if c[0] in names]
    if not hits:
        if None in names:
            return "pass", ""
        if case.get("ask_ok") and (answer.strip().endswith("?") or "ask_user" in called):
            return "pass", ""
        return "fail", f"never called {'/'.join(str(n) for n in names if n)} (called {called or 'nothing'})"
    for key, want in (case.get("args") or {}).items():
        if not any(key in a and _arg_ok(want, a[key]) for _, a in hits):
            return "fail", f"{hits[0][0]}: {key}={hits[0][1].get(key)!r}, wanted {want!r}"
    return "pass", ""


async def ask_loop(case: dict, brain: str = "local") -> tuple[Calls, str]:
    """Run the real agent for this case: run_agent (local) or run_big_brain (claude, the MAIN_BRAIN=claude path).
    Tools are replaced by fakes that record the call."""
    from agent import agent_loop, big_brain, traces
    from agent.context import new_session
    from agent.tools import registry
    s = new_session()
    s.app = case.get("session", "web")
    s.popup = bool(case.get("popup"))
    reg = registry()
    calls: Calls = []

    async def fake(name, args):
        from agent import playbooks
        if name in playbooks.all_playbooks():    # a playbook called by its name: the registry treats it as use_skill
            name, args = "use_skill", {"name": name}
        calls.append((name, dict(args or {})))
        if name == "use_skill":                  # the real playbook steps (reading them changes nothing)
            return playbooks.load(str((args or {}).get("name", "")))
        return _fake_result(case, name) if name in reg.funcs else f"[ERROR]: No tool named '{name}'."

    real = reg.call
    reg.call = fake
    try:
        traces.start(case["text"])
        history = list(case.get("history") or []) + [{"role": "user", "content": case["text"]}]
        agent_loop.escalation.set("")
        run = big_brain.run_big_brain if brain == "claude" else agent_loop.run_agent
        answer, _ = await run(case["text"], history)
    finally:
        reg.call = real
        traces._current.set(None)                # a benchmark turn is not a real conversation: do not store it
    return calls, answer


BACKENDS = {"local": ask_local, "claude": ask_claude}


# ── Run + report ───────────────────────────────────────────────────────────────

async def run(cases: list[dict], ask, loop: bool = False) -> list[dict]:
    results = []
    for c in cases:
        t0 = time.time()
        verdict = ""
        try:
            calls, answer = await ask(c)
            if loop:
                verdict, why = judge_loop(c, calls, answer)
                ok = verdict == "pass"
            else:
                ok, why = judge(c, calls, answer)
        except Exception as e:                           # a crash is a failure, not the end of the run
            calls, answer, ok, why = [], "", False, f"error: {e.__class__.__name__}: {e}"
        results.append({"id": c["id"], "category": c["category"], "text": c["text"], "future": bool(c.get("future")),
                        "verdict": verdict or ("pass" if ok else "fail"), "all_called": [x[0] for x in calls],
                        "ok": ok, "why": why, "called": calls[0][0] if calls else None,
                        "args": calls[0][1] if calls else None, "answer": (answer or "")[:160], "seconds": round(time.time() - t0, 1)})
    return results


def summarize(results: list[dict]) -> dict:
    scored = [r for r in results if not r["future"]]
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in scored:
        by_cat[r["category"]][1] += 1
        by_cat[r["category"]][0] += int(r["ok"])
    passed = sum(1 for r in scored if r["ok"])
    secs = sorted(r["seconds"] for r in scored) or [0]
    return {"passed": passed, "total": len(scored), "percent": round(100 * passed / len(scored), 1) if scored else 0.0,
            "escalated": sum(1 for r in scored if r.get("verdict") == "escalated"),
            "wrong": sum(1 for r in scored if r.get("verdict") == "fail"),
            "median_seconds": secs[len(secs) // 2], "p90_seconds": secs[min(len(secs) - 1, int(len(secs) * 0.9))],
            "categories": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_cat.items())},
            "future_cases": sum(1 for r in results if r["future"])}


def render(summary: dict, results: list[dict]) -> str:
    lines = [f"Tool-call score: {summary['passed']}/{summary['total']} = {summary['percent']}%",
             f"  handed to the big brain: {summary.get('escalated', 0)}, wrong: {summary.get('wrong', 0)}, "
             f"typical time {summary.get('median_seconds', 0)} s, slow tenth {summary.get('p90_seconds', 0)} s", ""]
    for cat, v in summary["categories"].items():
        lines.append(f"  {cat:<15} {v['passed']}/{v['total']}")
    fails = [r for r in results if not r["ok"] and not r["future"]]
    if fails:
        lines += ["", "Failures:"]
        lines += [f"  {r['id']:<8} {r['text'][:50]!r} -> {r['why']}" for r in fails if r.get("verdict") != "escalated"]
        esc = [r for r in fails if r.get("verdict") == "escalated"]
        if esc:
            lines += ["", "Handed to the big brain:"] + [f"  {r['id']:<8} {r['text'][:60]!r}" for r in esc]
    if summary["future_cases"]:
        lines += ["", f"({summary['future_cases']} future case(s) not scored: tools that do not exist yet)"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Enzo tool-call benchmark")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="local")
    ap.add_argument("--category")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--repeat", type=int, default=1, help="run N times and average (the local model varies from run to run)")
    ap.add_argument("--loop", action="store_true", help="run the real agent loop (fake tools) and score the whole turn")
    ap.add_argument("--brain", choices=["local", "claude"], default="local",
                    help="with --loop: which agent answers (claude = the MAIN_BRAIN=claude path, model from BIG_BRAIN_MODEL)")
    a = ap.parse_args()

    # Memory facts go to a throwaway file (the real profile is copied into it): the benchmark never writes his memory.
    import os
    import tempfile
    os.environ.setdefault("FACTS_STORE", "file")
    os.environ["MORNING_BRIEFING_WINDOW"] = "off"
    if a.brain == "claude":
        os.environ["MAIN_BRAIN"] = "claude"          # the same checks as the real Claude-first path (grounding on Claude)   # the cases expect no pending briefing: the score must not depend on the clock
    from agent import facts
    facts.FILE = Path(tempfile.mkdtemp(prefix="enzo_bench_")) / "facts.json"
    from shared import OLLAMA_OPTIONS
    if a.backend == "local" and a.brain == "local" and OLLAMA_OPTIONS.get("num_ctx", 0) < 16384:
        sys.exit(f"LLM_NUM_CTX is {OLLAMA_OPTIONS.get('num_ctx')}: the tool list does not fit and gets cut off, so the score would be "
                 "meaningless. Set LLM_NUM_CTX=32768 in .env (the NAS runs with 32768).")
    cases = [c for c in load_cases() if not a.category or c["category"] == a.category]
    if not a.loop:
        cases = [c for c in cases if not c.get("tools")]          # multi-step cases need the loop
    if a.limit:
        cases = cases[: a.limit]
    # Phone and web have different tool lists, so different cached prompts: Ollama keeps only a few, and alternating
    # made every case re-read ~10k tokens (~30 s), which the phone never does. Run each kind together.
    cases.sort(key=lambda c: c.get("session", "web"))
    from agent import big_brain
    ask = BACKENDS[a.backend]
    if a.loop:
        a.backend = "loop" if a.brain == "local" else f"loop-claude-{big_brain.BIG_BRAIN_MODEL}"
        ask = lambda c: ask_loop(c, a.brain)
    spent0 = big_brain.spent_today()
    runs = [asyncio.run(run(cases, ask, loop=a.loop)) for _ in range(max(1, a.repeat))]
    cost = big_brain.spent_today() - spent0
    if cost:
        print(f"Claude cost: ${cost:.3f} for {len(cases) * len(runs)} turns (${cost / (len(cases) * len(runs)):.4f} a turn)\n")
    if len(runs) > 1:
        pcts = [summarize(r)["percent"] for r in runs]
        print(f"{len(runs)} runs: {pcts} -> mean {round(sum(pcts) / len(pcts), 1)}%\n")
    results = runs[-1]
    summary = summarize(results)
    if a.save:                       # save first: printing Hebrew on a Windows console can raise
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.backend}.json"
        out.write_text(json.dumps({"backend": a.backend, "summary": summary, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {out}\n")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(render(summary, results))


if __name__ == "__main__":
    main()
