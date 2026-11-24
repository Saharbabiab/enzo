"""T-07: snooze a reminder that rang, and "done" (skips the next ring of a repeating one when done early)."""
import asyncio
from datetime import datetime, timedelta, timezone

import scheduler
from scheduler import done_plan, pick_reminder

NOW = datetime.now(timezone.utc)
DAY = 86400


def _docs():
    return [
        {"_id": "w1", "label": "drink water", "message": "Drink water, sir.", "type": "countdown",
         "active": False, "last_fired": (NOW - timedelta(minutes=2)).replace(tzinfo=None)},
        {"_id": "p1", "label": "take the pills", "message": "Pills, sir.", "type": "recurring", "interval_seconds": DAY,
         "active": True, "next_fire": NOW + timedelta(hours=3), "last_fired": NOW - timedelta(hours=21)},
        {"_id": "t1", "label": "btc", "message": "btc", "type": "countdown", "active": False,
         "last_fired": NOW - timedelta(minutes=1), "metadata": {"task": "tell me the bitcoin price"}},
    ]


def test_pick_last_rung_skips_tasks_and_matches_words():
    assert pick_reminder(_docs())["_id"] == "w1"              # the task rang later but is not a reminder
    assert pick_reminder(_docs(), "pill")["_id"] == "p1"
    assert pick_reminder(_docs(), "nothing like it") is None


def test_done_plan():
    w1, p1 = _docs()[0], _docs()[1]
    assert done_plan(w1, NOW) == ("ok", None)                  # just rang
    what, nxt = done_plan(p1, NOW)                             # done before today's ring -> skip to tomorrow
    assert what == "skip" and nxt == p1["next_fire"] + timedelta(seconds=DAY)
    p1["last_fired"] = NOW - timedelta(minutes=10)             # done right after it rang -> keep the schedule
    assert done_plan(p1, NOW) == ("ok", None)
    once = {"_id": "o", "type": "alarm", "active": True, "next_fire": NOW + timedelta(hours=1)}
    assert done_plan(once, NOW) == ("cancel", None)


def _fake_store(monkeypatch):
    added, updated = [], []
    store = scheduler.reminder_store
    monkeypatch.setattr(store, "recent_and_active", _docs)
    monkeypatch.setattr(store, "add", lambda *a, **k: added.append((a, k)) or "new1")
    monkeypatch.setattr(store, "set_fields", lambda rid, f: updated.append((rid, f)) or True)

    async def no_sync(*a, **k):
        return False
    from agent import phone_reminders
    monkeypatch.setattr(phone_reminders, "sync", no_sync)
    return added, updated


def test_snooze_tool_re_adds_the_same_message(monkeypatch):
    from agent.tools import snooze_reminder
    added, _ = _fake_store(monkeypatch)
    out = asyncio.run(snooze_reminder(5))
    assert out.startswith("[OK]") and "5 minutes" in out
    (args, kw), = added
    assert args[:2] == ("Drink water, sir.", "countdown") and kw["interval_seconds"] == 300
    assert kw["metadata"] == {"snoozed_from": "w1"}


def test_done_tool_skips_next_ring_of_recurring(monkeypatch):
    from agent.tools import reminder_done
    _, updated = _fake_store(monkeypatch)
    out = asyncio.run(reminder_done("pills"))
    assert out.startswith("[OK]") and "skipped" in out
    assert updated[0][0] == "p1" and "next_fire" in updated[0][1]
    assert asyncio.run(reminder_done()).startswith("[OK]: 'drink water' marked done")
