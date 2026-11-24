"""Israeli public transport: route planning, bus departures and the Moovit link. No keys, no accounts:
- Transitous (api.transitous.org, community MOTIS router on the Ministry of Transport GTFS): plans and timetables.
- curlbus.app (Ministry SIRI feed, re-served without a key): live arrivals at a stop, by its 5-digit stop code.
- OpenStreetMap Nominatim (Israel only): place names to coordinates.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytz
from loguru import logger

from agent.context import session

TZ = pytz.timezone("Asia/Jerusalem")
TRANSITOUS = "https://api.transitous.org/api/v1"
UA = {"User-Agent": "Enzo-home-assistant/1.0"}
# where the user lives, when the phone shares no location: HOME_PLACE / HOME_LAT / HOME_LON in .env (default: Beer Sheva centre)
HOME = ((os.getenv("HOME_PLACE") or "home"), float(os.getenv("HOME_LAT") or "31.2518"), float(os.getenv("HOME_LON") or "34.7913"))


async def _get(url: str, params: dict | None = None, timeout: float = 15):
    async with httpx.AsyncClient(timeout=timeout, headers={**UA, "Accept": "application/json"}) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _km(a: tuple, b: tuple) -> float:
    return 111 * math.hypot(a[1] - b[1], (a[2] - b[2]) * math.cos(math.radians(a[1])))


async def places(text: str) -> list[tuple[str, float, float]]:
    """Places in Israel matching the text, as ("Name (Town)", lat, lon), one per spot (hits under 1 km apart are one place)."""
    try:
        hits = await _get("https://nominatim.openstreetmap.org/search",
                          {"q": text, "countrycodes": "il", "format": "jsonv2", "limit": 8, "addressdetails": 1,
                           "accept-language": "en"}, 8)
    except Exception as e:
        logger.warning(f"transit geocode {text!r}: {e}")
        return []
    out: list[tuple[str, float, float]] = []
    for h in hits:
        a = h.get("address") or {}
        name = h.get("name") or h["display_name"].split(",")[0]
        town = a.get("city") or a.get("town") or a.get("village") or ""
        p = (f"{name} ({town})" if town and town not in name else name, float(h["lat"]), float(h["lon"]))
        if all(_km(p, q) > 1 for q in out):
            out.append(p)
    return out


async def geocode(text: str) -> tuple[str, float, float] | None:
    """The best match for a place in Israel, or None."""
    found = await places(text)
    return found[0] if found else None


async def here() -> tuple[str, float, float]:
    s = session()
    if s.has_location:
        return (s.place or "your location"), s.latitude, s.longitude
    if s.place:                                  # he told us where he is
        found = await geocode(s.place)
        if found:
            return found
    return HOME


def start_guessed() -> bool:
    """True when we only assume he is at home (no phone location, he did not say where he is)."""
    s = session()
    return not s.has_location and not s.place


async def resolve(text: str, near: tuple[str, float, float] | None = None) -> tuple[str, float, float] | str:
    """Empty = where he is. One place, or an [ASK] / [ERROR] text for the model when it is not clear which place he means."""
    if not (text or "").strip() or text.strip().lower() in ("here", "my location", "current location", "me"):
        return await here()
    found = await places(text.strip())
    if not found:
        return (f"[ERROR]: \"{text}\" was not found on the map of Israel. Retry with the place's real name (a station: its "
                "official name, e.g. \"Be'er Sheva Merkaz\" or \"Beer Sheva North - University\"; or \"railway station in "
                "<city>\" to list a city's stations), or ask the user for an address.")
    near = near or await here()
    close = [p for p in found if _km(p, near) <= 40] or found          # the ones near him first
    if len(close) == 1:
        return close[0]
    names = "; ".join(f"{i}. {p[0]}" for i, p in enumerate(close[:5], 1))
    return (f"[ASK]: \"{text}\" matches several places: {names}. Ask the user which one he means (offer them as choices), "
            "then call again with that exact name.")


def _local(iso: str, day: bool = False) -> str:
    """ISO -> "16:25"; day=True adds the weekday when it is not today ("Tue 07:45")."""
    if not iso:
        return "?"
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    return d.strftime("%a %H:%M" if day and d.date() != datetime.now(TZ).date() else "%H:%M")


def _when(depart_at: str) -> str | None:
    """'17:30' or a full ISO time -> ISO for Transitous; '' = now."""
    t = (depart_at or "").strip()
    if not t:
        return None
    try:
        if len(t) <= 5:
            h, m = (int(x) for x in t.split(":"))
            now = datetime.now(TZ)
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return (at + timedelta(days=1) if at < now - timedelta(minutes=30) else at).isoformat()   # passed = tomorrow
        d = datetime.fromisoformat(t)
        return (d if d.tzinfo else TZ.localize(d)).isoformat()
    except ValueError:
        return None


NAMES_FILE = Path(os.getenv("STOP_NAMES_FILE") or Path(__file__).resolve().parent.parent / "data" / "stop_names_en.json")
_names: dict[str, str] | None = None      # stop code -> English name ("" = curlbus has none)


def _name(stop: dict) -> str:
    """A stop's English name when known (the planner only has Hebrew), else its own name."""
    return (_names or {}).get(str(stop.get("stopCode") or ""), "") or stop.get("name") or "?"


async def english(stops: list[dict]) -> None:
    """Learn the English names of these stops from curlbus (Ministry data), once per stop; kept in NAMES_FILE."""
    global _names
    if _names is None:
        try:
            _names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _names = {}
    todo = {str(s["stopCode"]): s for s in stops if s.get("stopCode") and "lat" in s and str(s["stopCode"]) not in _names}
    for code, s in list(todo.items())[:8]:                       # one at a time: curlbus rate-limits bursts
        try:
            near = await _get("https://curlbus.app/nearby", {"lat": s["lat"], "lon": s["lon"], "radius": 50}, 8)
        except Exception as e:                                    # 429 / down: the rest waits for a later plan
            logger.debug(f"stop name {code}: {e}")
            break
        _names[code] = next((x["name"].get("EN", "") for x in near if str(x.get("code")) == code), "")
    if todo:
        try:
            NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.write_text(json.dumps(_names, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning(f"stop names not saved: {e}")


def _leg(l: dict) -> str:
    mins = round(l.get("duration", 0) / 60)
    if l["mode"] == "WALK":
        far = f" ({int(l['distance'])} m)" if l.get("distance") else ""
        return f"walk {mins} min{far} to {_name(l['to'])}"
    code = l["from"].get("stopCode")
    kind = "train" if "RAIL" in l["mode"] else l["mode"].lower()
    line = l.get("routeShortName") if kind != "train" else ""          # rail "short names" are whole route names
    agency = f" ({l['agencyName']})" if l.get("agencyName") else ""
    live = " live" if l.get("realTime") else ""
    toward = _name(l["tripTo"]) if l.get("tripTo") else (l.get("headsign") or "?")
    return (f"{kind}{f' {line}' if line else ''}{agency} toward {toward}: board at {_name(l['from'])}"
            f"{f' (stop {code})' if code else ''} at {_local(l['startTime'])}{live}, get off at {_name(l['to'])} "
            f"at {_local(l['endTime'])}")


def format_plan(data: dict, frm: str, to: str, limit: int = 3) -> str:
    its = sorted((i for i in data.get("itineraries", []) if any(l["mode"] != "WALK" for l in i["legs"])),
                 key=lambda i: (i["endTime"], i.get("transfers", 0)))[:limit]          # earliest arrival first
    if not its:
        return f"[ERROR]: No public transport found from {frm} to {to} at that time."
    out = [f"Public transport from {frm} to {to} (names still in Hebrew: translate them):"]
    for n, it in enumerate(its, 1):
        it["legs"][0]["from"]["name"], it["legs"][-1]["to"]["name"] = frm, to          # not "START" / "END"
        ch = it.get("transfers", 0)
        out.append(f"Option {n}: leave {_local(it['startTime'], day=True)}, arrive {_local(it['endTime'])} "
                   f"({round(it['duration'] / 60)} min, {ch} change{'s' if ch != 1 else ''}).")
        out += [f"  - {_leg(l)}" for l in it["legs"] if not (l["mode"] == "WALK" and l.get("duration", 0) < 60)]
    return "\n".join(out)


async def plan(to: str, frm: str = "", depart_at: str = "") -> str:
    orig = await resolve(frm)
    if isinstance(orig, str):
        return orig
    dest = await resolve(to, orig)
    if isinstance(dest, str):
        return dest
    params = {"fromPlace": f"{orig[1]},{orig[2]}", "toPlace": f"{dest[1]},{dest[2]}", "numItineraries": 6,
              "maxPreTransitTime": 1200}          # up to a 20 min walk to the first stop (the home junction is 16 min)
    when = _when(depart_at)
    if when:
        params["time"] = when
    try:
        data = await _get(f"{TRANSITOUS}/plan", params, 30)
    except Exception as e:
        logger.warning(f"transit plan: {e}")
        return "[ERROR]: The route planner (Transitous) did not answer. Try again in a minute."
    note = ("\n[NOTE]: He did not say where he starts and his phone shares no location, so this is planned from his home "
            "(home). If he may not be at home, ask where he leaves from.") if not frm.strip() and start_guessed() else ""
    shown = sorted(data.get("itineraries", []), key=lambda i: (i["endTime"], i.get("transfers", 0)))   # as format_plan
    await english([s for it in shown for l in it["legs"] if l["mode"] != "WALK" for s in (l["from"], l["to"], l.get("tripTo") or {})])
    return format_plan(data, orig[0], dest[0]) + note


def _dist(a: dict, lat: float, lon: float) -> float:
    return math.hypot(a["lat"] - lat, (a["lon"] - lon) * math.cos(math.radians(lat)))


def _same_line(name: str, line: str) -> bool:
    return not line or str(name).strip().lower() == line.strip().lower()


def format_live(visits: dict, line: str, stop_names: dict) -> list[str]:
    """curlbus visits -> 'line 60 to X: 15:31 (in 8 min, live) at stop Y' lines, soonest first."""
    now = datetime.now(TZ)
    rows = []
    for code, items in (visits or {}).items():
        for v in items:
            if not _same_line(v.get("line_name", ""), line) or not v.get("eta"):
                continue
            eta = datetime.fromisoformat(v["eta"]).astimezone(TZ)
            dest = ((v.get("static_info") or {}).get("route") or {}).get("destination") or {}
            names = dest.get("name") or {}
            rows.append((eta, f"line {v['line_name']} to {names.get('EN') or names.get('HE') or '?'}: "
                              f"{eta.strftime('%H:%M')} (in {max(0, round((eta - now).total_seconds() / 60))} min, live) "
                              f"at {stop_names.get(code, '')} (stop {code})"))
    return [r for _, r in sorted(set(rows))]


async def bus_times(line: str = "", stop: str = "") -> str:
    place = await resolve(stop)
    if isinstance(place, str):
        return place
    name, lat, lon = place
    if not stop.strip() and start_guessed():
        name += " (his home: his phone shares no location; if he is elsewhere, ask where)"
    try:
        d = 0.004                                                  # ~400 m box around the place
        stops = await _get(f"{TRANSITOUS}/map/stops", {"min": f"{lat - d},{lon - d}", "max": f"{lat + d},{lon + d}"})
    except Exception as e:
        logger.warning(f"transit stops: {e}")
        return "[ERROR]: The bus stop lookup (Transitous) did not answer. Try again in a minute."
    nearest = {}                                                  # one entry per stop code (platforms repeat it)
    for s in sorted((s for s in stops if s.get("stopCode")), key=lambda s: _dist(s, lat, lon)):
        nearest.setdefault(s["stopCode"], s)
    stops = list(nearest.values())[:4]
    if not stops:
        return f"[ERROR]: No bus stop within about 400 m of {name}. Ask the user for the stop name or its number (on the sign)."
    names = {s["stopCode"]: s["name"] for s in stops}
    head = f"Buses near {name}{f', line {line}' if line else ''} (stop names are Hebrew: translate them):"

    rows: list[str] = []
    for s in stops:                                               # live first (Ministry SIRI via curlbus)
        try:
            rows += format_live((await _get(f"https://curlbus.app/{s['stopCode']}", timeout=10)).get("visits"), line, names)
        except Exception as e:
            logger.debug(f"curlbus {s['stopCode']}: {e}")
    if rows:
        return "\n".join([head, *sorted(rows, key=lambda r: r.split(": ", 1)[1])[:8]])

    try:                                                           # no live data: the timetable
        data = await _get(f"{TRANSITOUS}/stoptimes", {"stopId": stops[0]["stopId"], "n": 40, "radius": 400})
    except Exception as e:
        logger.warning(f"transit stoptimes: {e}")
        return "[ERROR]: No live bus data and the timetable did not answer. Try again in a minute."
    rows = [f"line {t.get('routeShortName', '?')} to {t.get('headsign') or '?'}: {_local(t['place'].get('departure'), day=True)} "
            f"(timetable) at {t['place']['name']} (stop {t['place'].get('stopCode', '?')})"
            for t in data.get("stopTimes", []) if _same_line(t.get("routeShortName", ""), line)][:8]
    if not rows:
        return f"[OK]: No {'line ' + line + ' ' if line else ''}buses due soon near {name}."
    return "\n".join([head, *rows])


def moovit_link(dest: tuple[str, float, float], orig: tuple[str, float, float] | None = None) -> str:
    """Moovit's documented deep link (moovit.com/developers/deeplinking). No origin = Moovit uses the phone's location."""
    q = {"dest_lat": dest[1], "dest_lon": dest[2], "dest_name": dest[0]}
    if orig:
        q |= {"orig_lat": orig[1], "orig_lon": orig[2], "orig_name": orig[0]}
    return "moovit://directions?" + urlencode(q | {"auto_run": "true", "partner_id": "Enzo"})
