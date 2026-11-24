import urllib.request
import json
from loguru import logger

def get_current_weather(city_name: str = "") -> str:
    """
    Fetches the real-time weather and temperature. Use this whenever the user asks about the weather.
    Leave city_name empty to use the user's current location.

    Args:
        city_name (str): The name of the city, e.g., "London" or "Tokyo". Empty = where the user is now.
    """
    if not city_name.strip():
        from agent.context import session
        s = session()
        if s.has_location:
            if not s.place:      # name the place: "your location" made Ramat Gan sound like home (Beer Sheva)
                s.place = _place_name(s.latitude, s.longitude)
            return _weather_at(s.latitude, s.longitude, s.place or "your location")
        if s.place:
            city_name = s.place
        else:   # no GPS and no place he named: tell the model what it needs to decide (his home city), not a silent default
            import os
            home = os.getenv("DEFAULT_CITY", "").strip()
            if home:
                return (f"[UNKNOWN]: The phone did not share a location and he has not said where he is. His home city is "
                        f"{home}. Decide: if he is probably home, call get_current_weather again with city_name='{home}' "
                        f"(and say it is for {home}); otherwise ask him which city.")
            return "Location unknown — ask the user which city."
    try:
        found = _geocode(city_name)
        if not found:
            return f"Could not find coordinates for city: {city_name}"
        return _weather_at(*found)
    except Exception as e:
        logger.error(f"Weather skill error: {e}")
        return f"Error fetching weather data: {str(e)}"


def _place_name(lat: float, lon: float) -> str:
    """GPS -> "City, Country" (OpenStreetMap Nominatim, no key), or "" if it cannot tell."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10&accept-language=en"
        req = urllib.request.Request(url, headers={"User-Agent": "Enzo-home-assistant/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            a = json.loads(r.read().decode()).get("address", {})
        city = a.get("city") or a.get("town") or a.get("village") or a.get("county") or ""
        return ", ".join(x for x in (city, a.get("country", "")) if x)
    except Exception as e:
        logger.debug(f"place name: {e}")
        return ""


def _geocode(city_name: str):
    """City name -> (lat, lon, "Name, Country"), or None."""
    if not (city_name or "").strip():
        return None
    # Hebrew city names ("באר שבע") are only found when searching in Hebrew
    lang = "he" if any("֐" <= ch <= "׿" for ch in city_name) else "en"
    geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city_name)}&count=1&language={lang}&format=json"
    req = urllib.request.Request(geocode_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
    if not data.get("results"):
        return None
    loc = data["results"][0]
    return loc["latitude"], loc["longitude"], f'{loc.get("name")}, {loc.get("country")}'


# Open-Meteo weather codes -> a word for the app's card
_SKY = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle",
        55: "Drizzle", 61: "Rain", 63: "Rain", 65: "Heavy rain", 71: "Snow", 73: "Snow", 75: "Snow", 80: "Showers",
        81: "Showers", 82: "Heavy showers", 95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm"}


def _forecast(lat: float, lon: float) -> dict:
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
           "&hourly=temperature_2m&daily=temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=2")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())


def _now_in(city_name: str) -> dict | None:
    """{place, temp, condition} for the app's home screen, or None if the city or the service is not found."""
    found = _geocode(city_name)
    if not found:
        return None
    cur = _forecast(found[0], found[1]).get("current_weather") or {}
    if cur.get("temperature") is None:
        return None
    return {"place": found[2].split(",")[0], "temp": cur["temperature"], "condition": _SKY.get(cur.get("weathercode"), "")}


def _weather_at(lat: float, lon: float, resolved_name: str) -> str:
    try:
        weather_data = _forecast(lat, lon)
        current = weather_data.get("current_weather", {})
        temp = current.get("temperature")
        wind = current.get("windspeed")
        _card(weather_data, resolved_name)

        return f"Real-time weather for {resolved_name}: Temperature is {temp}°C, Wind speed is {wind} km/h."
        
    except Exception as e:
        logger.error(f"Weather skill error: {e}")
        return f"Error fetching weather data: {str(e)}"


def _card(w: dict, place: str) -> None:
    """The weather card in the app: now, today's low/high and every 3 hours after now (all from this same reply)."""
    try:
        from agent.context import show_card
        cur = w.get("current_weather") or {}
        daily, hourly = w.get("daily") or {}, w.get("hourly") or {}
        now = (cur.get("time") or "")[:13]                      # local "YYYY-MM-DDTHH" (timezone=auto)
        times, temps = hourly.get("time") or [], hourly.get("temperature_2m") or []
        start = next((i for i, t in enumerate(times) if t[:13] >= now), 0) if now else 0
        hours = [{"time": times[i][11:16], "temp": temps[i]} for i in range(start + 3, min(start + 15, len(times)), 3)
                 if temps[i] is not None][:4]
        show_card("weather", {
            "place": place, "temp": cur.get("temperature"), "wind": cur.get("windspeed"),
            "condition": _SKY.get(cur.get("weathercode"), ""),
            "low": (daily.get("temperature_2m_min") or [None])[0], "high": (daily.get("temperature_2m_max") or [None])[0],
            "hours": hours,
        })
    except Exception as e:                                       # a card must never break the answer
        logger.debug(f"weather card: {e}")

# We need urllib.parse explicitly
import urllib.parse
