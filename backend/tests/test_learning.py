"""Phase 6: lesson notes with counters, the nightly review, playbook proposals."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest


@pytest.fixture
def L(tmp_path, monkeypatch):
    import agent.lessons as L
    import agent.playbooks as pb
    monkeypatch.setattr(L, "DATA", tmp_path)
    monkeypatch.setattr(L, "FAILURES", tmp_path / "failures.jsonl")
    monkeypatch.setattr(L, "LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(pb, "OWN_DIR", str(tmp_path / "skills"))
    return L


def test_old_lesson_list_becomes_notes_and_scores_from_outcomes(L):
    L.LESSONS.write_text(json.dumps({"lessons": ["Check the calendar before you answer about tomorrow", L.SEED[1]]}), encoding="utf-8")
    notes = L.notes()
    assert [n["src"] for n in notes if n["text"] == L.SEED[1]] == ["seed"]   # old copies of built-in rules stay fixed
    assert notes[0]["text"].startswith("Check the calendar") and notes[0]["src"] == "learned"
    assert len(notes) == 1 + len(L.SEED)
    nid = notes[0]["id"]
    bad = {"ts": 10, "lessons": [nid], "signals": [{"name": "repeated"}]}
    good = {"ts": 11, "lessons": [nid], "signals": [{"name": "feedback_good"}]}
    plain = {"ts": 12, "lessons": [nid], "signals": []}
    assert L.score([bad, good, plain]) == 2
    n = next(x for x in L.notes() if x["id"] == nid)
    assert (n["helped"], n["hurt"]) == (1, 1)
    assert L.score([bad, good]) == 0                       # each turn counts once
    # keeps hurting -> switched off; the user's own rule never is
    L.teach("Answer shorter from now on")
    own = next(x for x in L.notes() if x["src"] == "owner")["id"]
    many = [{"ts": 100 + i, "lessons": [nid, own], "signals": [{"name": "feedback_bad"}]} for i in range(4)]
    L.score(many)
    live = {x["id"] for x in L.notes()}
    assert nid not in live and own in live


def test_prompt_block_records_shown_notes_on_the_turn(L, monkeypatch):
    from agent import traces
    L.teach("Tell the user the weather in Celsius")
    t = traces.Trace(text="weather")
    monkeypatch.setattr(traces, "current", lambda: t)
    block = L.prompt_block("what's the weather")
    assert "Celsius" in block and len(t.lessons) <= len(L.SEED) + 1 + L.PROMPT_K
    assert any(n["id"] in t.lessons for n in L.notes() if n["src"] == "owner")
    L.prompt_block("")                                     # the stable prefix call must not overwrite it
    assert t.lessons
    assert "Forgot 1" in L.forget("Celsius") and "Celsius" not in L.prompt_block("weather")


def test_nightly_review_updates_notes_and_stages_playbooks(L, monkeypatch):
    from agent import nightly, big_brain, traces, playbooks
    monkeypatch.setattr(nightly, "STATE", L.DATA / "nightly.json")
    monkeypatch.setattr(nightly, "STATS", L.DATA / "tool_stats.json")
    L.LESSONS.write_text(json.dumps({"lessons": ["Old learned rule about timers"]}), encoding="utf-8")
    seed = L.notes()[0]["id"]                              # a learned note (built-in ones can't be retired)
    fixed = next(n["id"] for n in L.notes() if n["src"] == "seed")
    docs = [{"ts": 1, "route": "local", "text": "plan my evening", "lessons": [seed], "signals": [{"name": "feedback_good"}],
             "tools": [{"name": "calendar_events", "args": "{}", "result": "ok", "ok": True},
                       {"name": "phone_music", "args": "{}", "result": "[ERROR]: x", "ok": False}], "reply": "done"}]
    monkeypatch.setattr(traces, "recent", lambda hours=24, limit=2000: docs)
    answer = {"add": ["Before playing music, check what is already playing"], "retire": [seed, fixed], "edit": [],
              "playbooks": [{"name": "plan_my_evening", "description": "Evening plan. Use when he says plan my evening.",
                             "steps": "1. calendar_events for tonight", "tools": ["calendar_events"]},
                            {"name": "Bad Name!", "description": "x", "steps": "y"}]}
    sent = []

    class Client:
        class messages:
            @staticmethod
            def create(**k):
                sent.append(k)
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer))])

    monkeypatch.setattr(big_brain, "available", lambda: True)
    monkeypatch.setattr(big_brain, "_get_client", lambda: Client)
    monkeypatch.setattr(big_brain, "spent_today", lambda: 0.0)
    status = asyncio.run(nightly.run())
    assert "plan_my_evening" in status and sent and sent[0]["max_tokens"] == nightly.MAX_TOKENS
    assert any("already playing" in t for t in L.lessons()) and seed not in {n["id"] for n in L.notes()}
    assert fixed in {n["id"] for n in L.notes()}
    assert L.proposals() == ["plan_my_evening"] and "plan_my_evening" not in playbooks.all_playbooks()
    stats = json.loads(nightly.STATS.read_text())
    assert stats["tools"]["phone_music"] == {"ok": 0, "failed": 1} and "phone_music 1 of 1 failed" in nightly.stats_line()
    # over the cap: no Claude call at all
    sent.clear()
    monkeypatch.setattr(big_brain, "spent_today", lambda: big_brain.DAILY_USD)
    assert "skipped" in asyncio.run(nightly.run()) and not sent
    # once a night, at the hour
    assert nightly.due(datetime(2026, 11, 17, nightly.HOUR, 5)) and not nightly.due(datetime(2026, 11, 17, 12, 0))


def test_approve_and_reject_proposed_playbooks(L, monkeypatch):
    from agent import tools, playbooks, approvals
    from agent.tools import registry
    playbooks.save("plan_my_evening", "Evening plan. Use when he says plan my evening.", "1. calendar_events", proposed=True)
    playbooks.save("tidy_up", "Tidy. Use when he says tidy up.", "1. x", proposed=True)
    monkeypatch.setattr(registry(), "reload", lambda: None)
    out = asyncio.run(tools.enzo_lessons("approve", "nope"))
    assert out.startswith("[ERROR]") and "plan_my_evening" in out
    asked = []

    async def ask(q, tool=""):
        asked.append(q)
        return True
    monkeypatch.setattr(approvals, "ask", ask)
    assert asyncio.run(tools.enzo_lessons("approve", "plan my evening")).startswith("[OK]")
    assert "plan_my_evening" in playbooks.all_playbooks() and "Evening plan" in asked[0]
    assert asyncio.run(tools.enzo_lessons("reject", "tidy_up")).startswith("[OK]")
    assert L.proposals() == []


def test_repeat_failures_postmortem_once_per_tool_per_day(tmp_path, monkeypatch):
    import json
    from agent import lessons
    monkeypatch.setattr(lessons, "FAILURES", tmp_path / "failures.jsonl")
    monkeypatch.setattr(lessons, "_postmortem_sent", {})
    rows = [{"t": 50, "tool": "spotify_play", "error": "[ERROR]: old, yesterday"}]
    rows += [{"t": 100 + i, "tool": "spotify_play", "error": f"[ERROR]: no active device {i}"} for i in range(3)]
    rows += [{"t": 100 + i, "tool": "read_webpage", "error": "[BLOCKED]: internal address"} for i in range(3)]
    rows += [{"t": 100 + i, "tool": "answer", "error": "[ERROR]: the user said wrong"} for i in range(3)]
    rows += [{"t": 100 + i, "tool": "phone_call", "error": "[ERROR]: x"} for i in range(2)]
    (tmp_path / "failures.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    msgs = lessons.repeat_failures("2026-11-19", since=99)
    assert len(msgs) == 1 and "spotify_play has failed 3 times" in msgs[0] and "no active device 2" in msgs[0]
    assert msgs[0].startswith("Sir,")
    assert lessons.repeat_failures("2026-11-19", since=99) == []       # once per day
    assert len(lessons.repeat_failures("2026-11-20", since=99)) == 1   # a new day may report it again
