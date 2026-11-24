"""agent/transit.py with the network faked (shapes copied from real Transitous / curlbus / Nominatim answers)."""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from agent import transit
from agent.context import new_session

PLAN = {"itineraries": [{
    "startTime": "2026-11-23T14:22:00Z", "endTime": "2026-11-23T15:22:00Z", "duration": 3600, "transfers": 1,
    "legs": [
        {"mode": "WALK", "duration": 180, "distance": 179.0, "from": {"name": "START"}, "to": {"name": "הבית"},
         "startTime": "2026-11-23T14:22:00Z", "endTime": "2026-11-23T14:25:00Z"},
        {"mode": "BUS", "routeShortName": "60", "agencyName": "מטרופולין", "headsign": "מצפה רמון", "duration": 120,
         "from": {"name": "הבית", "stopCode": "12079"}, "to": {"name": "צומת הבית"},
         "startTime": "2026-11-23T14:25:00Z", "endTime": "2026-11-23T14:27:00Z", "realTime": False},
        {"mode": "WALK", "duration": 30, "distance": 20, "from": {"name": "a"}, "to": {"name": "b"},
         "startTime": "2026-11-23T14:27:00Z", "endTime": "2026-11-23T14:28:00Z"},
    ]}, {"startTime": "x", "endTime": "y", "duration": 600, "legs": [{"mode": "WALK", "duration": 600}]}]}


@pytest.fixture
def net(monkeypatch, tmp_path):
    calls = []
    eta = (datetime.now(transit.TZ) + timedelta(minutes=8)).isoformat()
    answers = {
        "nominatim": [{"display_name": "Soroka hospital, Be'er Sheva", "lat": "31.258", "lon": "34.800"}],
        "/plan": PLAN,
        "/map/stops": [{"name": "far", "stopId": "il-2", "stopCode": "13866", "lat": 30.985, "lon": 34.769},
                       {"name": "הבית", "stopId": "il-1", "stopCode": "12079", "lat": 31.25, "lon": 34.79}],
        "curlbus.app/12079": {"visits": {"12079": [
            {"line_name": "60", "eta": eta, "static_info": {"route": {"destination": {"name": {"EN": "Mitspe Ramon"}}}}},
            {"line_name": "65", "eta": eta, "static_info": {"route": {"destination": {"name": {"EN": "Eilat"}}}}}]}},
        "curlbus.app/13866": {"visits": {}},
    }

    async def fake_get(url, params=None, timeout=15):
        calls.append((url, params))
        return next(v for k, v in answers.items() if k in url)
    monkeypatch.setattr(transit, "_get", fake_get)
    monkeypatch.setattr(transit, "NAMES_FILE", tmp_path / "names.json")
    monkeypatch.setattr(transit, "_names", {"12079": "home"})
    s = new_session()
    s.app, s.latitude, s.longitude, s.place = "web", None, None, ""
    return calls


def test_plan_from_home_lists_legs_in_local_time(net, monkeypatch):
    class Clock(datetime):                     # "today" = the day of PLAN, so no weekday is shown
        @classmethod
        def now(cls, tz=None):
            return transit.TZ.localize(datetime(2026, 11, 23, 15, 0)).astimezone(tz)
    monkeypatch.setattr(transit, "datetime", Clock)
    out = asyncio.run(transit.plan("Soroka"))
    assert net[1][1]["fromPlace"] == f"{transit.HOME[1]},{transit.HOME[2]}"      # no location -> home
    assert "Option 1: leave 16:22, arrive 17:22 (60 min, 1 change)" in out        # UTC+2 in November
    assert "bus 60" in out and "board at home (stop 12079)" in out and "walk 3 min" in out   # English name when known
    assert "צומת הבית" in out and "START" not in out                                     # else Hebrew
    assert "Option 2" not in out                                                 # walk-only option dropped
    assert "a to b" not in out and "walk 0 min" not in out                       # sub-minute walks dropped


def test_bus_times_live_filtered_by_line(net):
    out = asyncio.run(transit.bus_times(line="60"))
    assert "line 60 to Mitspe Ramon" in out and "live" in out and "(stop 12079)" in out
    assert "line 65" not in out


def test_moovit_link_is_moovits_documented_format():
    q = parse_qs(urlparse(transit.moovit_link(("Soroka", 31.25, 34.8), ("home", 31.25, 34.79))).query)
    assert transit.moovit_link(("x", 1, 2)).startswith("moovit://directions?")
    assert q["dest_lat"] == ["31.25"] and q["orig_name"] == ["home"] and q["auto_run"] == ["true"]
    assert "orig_lat" not in transit.moovit_link(("x", 1, 2))          # no origin -> Moovit uses the phone's GPS


def test_moovit_route_opens_the_link_on_the_phone(net):
    from agent import tools as tl
    from agent.context import session
    s = session()
    assert asyncio.run(tl.moovit_route("Soroka")) == tl._NO_APP
    sent = []

    async def dev(p):
        sent.append(p)
    s.app, s.send_device = "android", dev
    assert "[OK]" in asyncio.run(tl.moovit_route("Soroka"))
    assert sent[0]["action"] == "open_url" and sent[0]["url"].startswith("moovit://directions?dest_lat=31.258")
    assert sent[0]["moovit_start"] is True and sent[0]["moovit_line"] == ""
    asyncio.run(tl.moovit_route("Soroka", line=" 64 ", start=False))
    assert sent[1]["moovit_start"] is False and sent[1]["moovit_line"] == "64"


def test_several_matches_near_him_ask_which_one(monkeypatch):
    async def fake_get(url, params=None, timeout=15):
        return [{"name": "Be'er Sheva Merkaz", "lat": "31.243", "lon": "34.798", "address": {"city": "Be'er Sheva"}, "display_name": "x"},
                {"name": "Beer Sheva North - University", "lat": "31.262", "lon": "34.809", "address": {"city": "Be'er Sheva"}, "display_name": "y"},
                {"name": "Platform B", "lat": "31.2621", "lon": "34.8091", "display_name": "z"},          # same spot as North
                {"name": "Far station", "lat": "32.8", "lon": "35.0", "display_name": "w"}]           # Haifa: not near him
    monkeypatch.setattr(transit, "_get", fake_get)
    s = new_session()
    s.latitude, s.longitude, s.place = None, None, ""
    out = asyncio.run(transit.resolve("railway station in Beer Sheva"))
    assert out.startswith("[ASK]") and "1. Be'er Sheva Merkaz;" in out and "2. Beer Sheva North" in out
    assert "Platform B" not in out and "Far station" not in out


def test_plan_says_when_the_start_is_only_assumed(net):
    assert "[NOTE]: He did not say where he starts" in asyncio.run(transit.plan("Soroka"))
    assert "[NOTE]" not in asyncio.run(transit.plan("Soroka", "home"))
