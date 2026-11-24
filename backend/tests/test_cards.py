"""Answer cards: the tools that have structured data send {"kind", "data"} to the app (Session.card), from their
REAL result, and a card never breaks a tool. No network: Google, Spotify and Open-Meteo are faked."""
import asyncio

import agent.google_services as g
from agent import spotify
from agent.context import new_session
import sys

from agent.tools import registry

KEYS = {
    "weather": {"place", "temp", "wind", "condition", "low", "high", "hours"},
    "calendar": {"day", "events"},
    "mail": {"query", "emails"},
    "music": {"title", "artist", "image", "playing", "progress_ms", "duration_ms"},
    **{k: {"title", "items", "options"} for k in ("options", "draft", "list", "steps", "links")},
}


def run_tool(name: str, args: dict) -> tuple[str, list]:
    """Call the tool through the registry (as the agent does) and collect the cards it sent."""
    cards = []

    async def go():
        s = new_session()

        async def card(kind, data):
            cards.append((kind, data))
        s.card = card
        out = await registry().call(name, args)
        await asyncio.sleep(0.05)                    # let fire-and-forget sends finish
        return out
    out = asyncio.run(go())
    for kind, data in cards:
        assert set(data) == KEYS[kind], (kind, data)
    return out, cards


class _Req:
    def __init__(self, out): self.out = out
    def execute(self, num_retries=0): return self.out


class _Fake:
    """Enough of googleapiclient's chained calls: events().list(), users().messages().list()/get()."""
    def events(self): return self
    def users(self): return self
    def messages(self): return self

    def list(self, **kw):
        if "calendarId" in kw:
            return _Req({"items": [
                {"summary": "Class", "start": {"dateTime": "2026-11-24T09:00:00+02:00"}, "end": {"dateTime": "2026-11-24T10:30:00+02:00"}},
                {"summary": "Holiday", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}]})
        return _Req({"messages": [{"id": "m1"}], "resultSizeEstimate": 1})

    def get(self, **kw):
        return _Req({"snippet": "hi", "payload": {"headers": [
            {"name": "From", "value": "Noa Levi <noa@example.com>"}, {"name": "Subject", "value": "Thursday"}]}})


def test_weather_card(monkeypatch):
    registry()
    weather = sys.modules["weather"]                   # the copy skills_manager loaded (what the tool runs)
    hours = [f"2026-11-22T{h:02d}:00" for h in range(24)]
    monkeypatch.setattr(weather, "_geocode", lambda c: (31.25, 34.79, "Beer Sheva, Israel"))
    monkeypatch.setattr(weather, "_forecast", lambda lat, lon: {
        "current_weather": {"temperature": 24.0, "windspeed": 9.0, "weathercode": 0, "time": "2026-11-22T12:00"},
        "hourly": {"time": hours, "temperature_2m": [float(h) for h in range(24)]},
        "daily": {"temperature_2m_min": [13.0], "temperature_2m_max": [25.0]}})
    out, cards = run_tool("get_current_weather", {"city_name": "Beer Sheva"})
    assert "24.0°C" in out and len(cards) == 1
    kind, d = cards[0]
    assert kind == "weather" and d["temp"] == 24.0 and d["condition"] == "Clear" and d["low"] == 13.0
    assert d["hours"] == [{"time": "15:00", "temp": 15.0}, {"time": "18:00", "temp": 18.0}, {"time": "21:00", "temp": 21.0}]


def test_calendar_and_mail_cards(monkeypatch):
    monkeypatch.setattr(g, "connected", lambda: True)
    monkeypatch.setattr(g, "_service", lambda name, ver: _Fake())
    _, cards = run_tool("calendar_events", {"date": "2026-11-24"})
    assert cards == [("calendar", {"day": "Tuesday 24/11", "events": [
        {"start": "09:00", "end": "10:30", "title": "Class", "location": ""},
        {"start": "", "end": "", "title": "Holiday", "location": ""}]})]
    _, cards = run_tool("calendar_events", {"days": 7})            # a week is not a day view: no card
    assert cards == []
    _, cards = run_tool("email_inbox", {})
    assert cards[0][0] == "mail" and cards[0][1]["emails"] == [{"from": "Noa Levi", "subject": "Thursday"}]


def test_music_card(monkeypatch):
    async def state():
        return {"is_playing": True, "progress_ms": 84000, "item": {
            "uri": "spotify:track:1", "name": "Beat It", "artists": [{"name": "Michael Jackson"}], "duration_ms": 258000,
            "album": {"images": [{"url": "https://i.scdn.co/image/x"}]}}}
    monkeypatch.setattr(spotify, "connected", lambda: True)
    monkeypatch.setattr(spotify, "_now_playing", state)
    out, cards = run_tool("phone_music", {"action": "now"})
    assert "Beat It" in out
    assert cards == [("music", {"title": "Beat It", "artist": "Michael Jackson", "image": "https://i.scdn.co/image/x",
                                "playing": True, "progress_ms": 84000, "duration_ms": 258000})]


def test_no_app_no_card_and_a_failing_card_does_not_break_the_tool(monkeypatch):
    monkeypatch.setattr(g, "connected", lambda: True)
    monkeypatch.setattr(g, "_service", lambda name, ver: _Fake())

    async def go(card):
        s = new_session()
        s.card = card
        return await registry().call("email_inbox", {})

    async def broken(kind, data):
        raise ConnectionError("app gone")
    assert "Thursday" in asyncio.run(go(None))
    assert "Thursday" in asyncio.run(go(broken))


def test_show_ui_draws_what_the_model_offers_and_checks_it():
    out, cards = run_tool("show_ui", {"kind": "draft", "title": "Reply to Dean", "items": ["Hey Dean, lunch today?", "Hey Dean, tomorrow?"],
                                      "options": ["Send the first draft", {"label": "Shorter", "say": "Make it shorter"}, "", "a", "b", "c"]})
    assert out.startswith("[OK]") and cards == [("draft", {"title": "Reply to Dean", "items": ["Hey Dean, lunch today?", "Hey Dean, tomorrow?"],
        "options": [{"label": "Send the first draft", "say": "Send the first draft"}, {"label": "Shorter", "say": "Make it shorter"},
                    {"label": "a", "say": "a"}]})]                     # at most 4 given, empty ones dropped
    _, cards = run_tool("show_ui", {"kind": "links", "items": ["Docs | https://example.com", "Bad | javascript:alert(1)", "no link"]})
    assert cards[0][1]["items"] == ["Docs | https://example.com"]      # only real web links
    assert run_tool("show_ui", {"kind": "poster", "items": ["x"]})[0].startswith("[ERROR]")
    assert run_tool("show_ui", {"kind": "list"})[0].startswith("[ERROR]") and run_tool("show_ui", {"kind": "options"})[1] == []


def test_ask_user_choices_become_buttons_and_no_app_means_no_screen():
    _, cards = run_tool("ask_user", {"question": "Which Ron?", "choices": ["Ron Cohen", "Ron Levi"]})
    assert cards == [("options", {"title": "", "items": [], "options": [{"label": "Ron Cohen", "say": "Ron Cohen"}, {"label": "Ron Levi", "say": "Ron Levi"}]})]
    async def go():
        new_session()                                  # no app: Telegram / background
        return await registry().call("show_ui", {"kind": "options", "options": ["Yes", "No"]})
    assert "no screen" in asyncio.run(go())