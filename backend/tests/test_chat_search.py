"""H3: search earlier conversations (agent/chat_search.py, tool search_chats)."""
import asyncio
import time

from agent import chat_search, traces, tools


def _doc(i, text, reply, days_ago=0, tools_=()):
    return {"id": f"t{i}", "ts": time.time() - days_ago * 86400, "text": text, "reply": reply,
            "tools": [{"name": n} for n in tools_]}


DOCS = [_doc(1, "plan a trip to Rome for me and Dana", "Here is a five day plan for Rome, sir.", 8),
        _doc(2, "what's the weather", "It's 24 degrees in Beer Sheva, sir.", 1, ["get_current_weather"]),
        _doc(3, "find a good pizza place in Rome", "Pizzeria Da Remo is well rated, sir.", 3, ["web_search"]),
        _doc(4, "what's the weather", "It's 24 degrees in Beer Sheva, sir.", 0),
        _doc(5, "", "a background job", 0)]


def test_search_finds_turns_by_his_words_newest_first_and_marks_outside_text(monkeypatch):
    monkeypatch.setattr(traces, "recent", lambda hours=24, limit=2000: [d for d in DOCS if time.time() - d["ts"] <= hours * 3600])
    hits = chat_search.search("Rome")
    assert [d["id"] for _, d in hits] == ["t1", "t3"] or {d["id"] for _, d in hits} == {"t1", "t3"}
    text = chat_search.describe(hits)
    assert text.index("pizza") < text.index("five day")                          # newest first
    assert "web / mail text" in text and "8 days ago" in text
    assert [d["id"] for _, d in chat_search.search("Rome", days=5)] == ["t3"]     # "last few days"
    assert len(chat_search.search("weather")) == 1                                # the same turn twice: shown once
    assert chat_search.search("Rome", exclude="t1")[0][1]["id"] == "t3"           # never the current turn
    assert not chat_search.search("dentist")


def test_the_tool_answers_with_dates_or_says_nothing_was_found(monkeypatch):
    monkeypatch.setattr(traces, "recent", lambda hours=24, limit=2000: DOCS)
    out = asyncio.run(tools.search_chats("Rome trip"))
    assert out.startswith("[CHATS]") and "Rome" in out
    assert "Nothing" in asyncio.run(tools.search_chats("dentist", days=7)) and "7 days" in asyncio.run(tools.search_chats("dentist", days=7))
    assert tools.risk("search_chats") == ("read", {"memory"})
