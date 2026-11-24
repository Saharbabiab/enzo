# Created by S.H.A.R.P. — self-written skill. Safe to edit or delete.
import httpx


def _aqi_label(aqi):
    if aqi is None:
        return "unknown"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for sensitive groups"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very unhealthy"
    return "Hazardous"


def get_air_quality(city: str) -> str:
    """Use when the user asks for the current air quality / AQI / pollution / dust level in a city.

    Args:
        city: City name, e.g. "Beer Sheva" or "Tel Aviv".
    """
    try:
        with httpx.Client(timeout=15) as client:
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            ).json()
            results = geo.get("results") or []
            if not results:
                return f"Could not find a location named '{city}'."
            loc = results[0]
            lat, lon = loc["latitude"], loc["longitude"]
            name = f"{loc.get('name')}, {loc.get('country', '')}".strip(", ")
            data = client.get(
                "https://air-quality-api.open-meteo.com/v1/air-quality",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "us_aqi,european_aqi,pm10,pm2_5,ozone,nitrogen_dioxide,dust",
                    "timezone": "auto",
                },
            ).json()
        cur = data.get("current") or {}
        if not cur:
            return f"No current air quality data available for {name}."
        us = cur.get("us_aqi")
        return (
            f"Air quality in {name} at {cur.get('time')}: US AQI {us} ({_aqi_label(us)}), "
            f"European AQI {cur.get('european_aqi')}. PM2.5 {cur.get('pm2_5')} ug/m3, "
            f"PM10 {cur.get('pm10')} ug/m3, ozone {cur.get('ozone')} ug/m3, "
            f"NO2 {cur.get('nitrogen_dioxide')} ug/m3, dust {cur.get('dust')} ug/m3."
        )
    except Exception as e:
        return f"Air quality lookup failed: {e}"
