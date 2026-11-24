"""
agent/spotify.py — Spotify's official Web API (needs Premium for playback).

Enzo on the NAS tells Spotify "play this on the user's phone": no screen taps, no switching apps.

Setup (once): a free Spotify developer app gives a CLIENT ID (no secret needed, PKCE login).
Put it in the stack .env as SPOTIFY_CLIENT_ID, then say "connect Spotify" to Enzo and press Allow.
The refresh token is stored in data/spotify.json (gitignored with the rest of data/).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from loguru import logger

TOKEN_FILE = Path(os.getenv("SPOTIFY_TOKEN_FILE", Path(__file__).resolve().parent.parent / "data" / "spotify.json"))
# user-library-modify + playlist-* added 2026-11-21 (T-11: like / add to playlist): an older login must connect Spotify once more.
SCOPES = ("user-modify-playback-state user-read-playback-state user-read-currently-playing user-library-read "
          "user-library-modify playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

_pending: dict[str, tuple[str, float]] = {}   # state -> (code_verifier, created)


class SpotifyError(Exception):
    """A problem that can be told to the user in plain words."""


class NotSwitched(SpotifyError):
    """Spotify said yes, but the phone kept playing the old song (the phone app can still start it itself)."""


def client_id() -> str:
    return os.getenv("SPOTIFY_CLIENT_ID", "").strip()


def redirect_uri() -> str:
    return os.getenv("SPOTIFY_REDIRECT_URI", "https://enzo.example.com:4444/api/spotify/callback").strip()


def _load() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")


def connected() -> bool:
    return bool(_load().get("refresh_token"))


# ── Login (PKCE) ─────────────────────────────────────────────

def login_url() -> str:
    if not client_id():
        raise SpotifyError("Spotify isn't set up yet: SPOTIFY_CLIENT_ID is missing in the server settings.")
    for k in [k for k, (_, t) in _pending.items() if time.time() - t > 900]:
        _pending.pop(k, None)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending[state] = (verifier, time.time())
    q = httpx.QueryParams({
        "client_id": client_id(), "response_type": "code", "redirect_uri": redirect_uri(),
        "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })
    return f"{AUTH_URL}?{q}"


async def finish_login(code: str, state: str) -> None:
    item = _pending.pop(state, None)
    if not item or time.time() - item[1] > 900:
        raise SpotifyError("That Spotify login link expired. Ask Enzo to connect Spotify again.")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": client_id(), "code_verifier": item[0]})
    if r.status_code != 200:
        raise SpotifyError(f"Spotify refused the login ({r.status_code}).")
    j = r.json()
    _save({"refresh_token": j["refresh_token"], "access_token": j["access_token"],
           "expires_at": time.time() + j.get("expires_in", 3600) - 60})
    logger.info("🎵 Spotify connected")


async def _access_token() -> str:
    d = _load()
    if not d.get("refresh_token"):
        raise SpotifyError("Spotify isn't connected yet. Say \"connect Spotify\" and press Allow.")
    if d.get("access_token") and d.get("expires_at", 0) > time.time():
        return d["access_token"]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                          "client_id": client_id()})
    if r.status_code != 200:
        raise SpotifyError("Spotify login expired. Say \"connect Spotify\" to sign in again.")
    j = r.json()
    d.update(access_token=j["access_token"], expires_at=time.time() + j.get("expires_in", 3600) - 60)
    if j.get("refresh_token"):
        d["refresh_token"] = j["refresh_token"]
    _save(d)
    return d["access_token"]


async def _api(method: str, path: str, **kw) -> httpx.Response:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20) as c:
        return await c.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)


# ── Search + decide what to play ─────────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", " ", s)).strip()


def pick(results: dict, query: str) -> tuple[str, dict, str]:
    """(kind, spotify object, how to say it). kind: track | artist | playlist | album."""
    nq = norm(query)
    tracks = [t for t in (results.get("tracks", {}).get("items") or []) if t]
    artists = [a for a in (results.get("artists", {}).get("items") or []) if a]
    playlists = [p for p in (results.get("playlists", {}).get("items") or []) if p]
    albums = [a for a in (results.get("albums", {}).get("items") or []) if a]

    def track_score(t: dict) -> int:
        name = norm(t["name"])
        by = " ".join(norm(a["name"]) for a in t.get("artists", []))
        if name == nq:
            return 3
        if name and name in nq and any(norm(a["name"]) in nq for a in t.get("artists", [])):
            return 3          # "beat it michael jackson"
        if name.startswith(nq) and len(nq) >= 4:
            return 2
        if nq in f"{name} {by}" and len(nq.split()) >= 2:
            return 1
        return 0

    if artists and norm(artists[0]["name"]) == nq:
        return "artist", artists[0], artists[0]["name"]
    best_track = max(tracks, key=track_score, default=None)
    if best_track and track_score(best_track) >= 2:
        return "track", best_track, f"{best_track['name']} by {best_track['artists'][0]['name']}"
    for p in playlists:
        if norm(p["name"]) == nq:
            return "playlist", p, p["name"]
    for a in albums:
        if norm(a["name"]) == nq:
            return "album", a, f"the album {a['name']}"
    if artists and nq in norm(artists[0]["name"]):
        return "artist", artists[0], artists[0]["name"]
    if best_track:
        return "track", best_track, f"{best_track['name']} by {best_track['artists'][0]['name']}"
    if artists:
        return "artist", artists[0], artists[0]["name"]
    raise SpotifyError(f"I couldn't find \"{query}\" on Spotify.")


async def _artist_playlist(artist_name: str) -> dict | None:
    """Spotify's own 'This Is <artist>' playlist, if there is one."""
    r = await _api("GET", "/search", params={"q": f"This Is {artist_name}", "type": "playlist", "limit": 10})
    if r.status_code != 200:
        return None
    want = norm(f"This Is {artist_name}")
    for p in (r.json().get("playlists", {}).get("items") or []):
        if p and norm(p["name"]) == want and (p.get("owner", {}).get("id") == "spotify"):
            return p
    return None


SETTLE = 1.2   # seconds to let Spotify's phone app react (tests set it to 0)


async def wait_for_device(seconds: float) -> bool:
    """True as soon as Spotify lists any playback device (the phone woke up)."""
    for _ in range(int(seconds)):
        r = await _api("GET", "/me/player/devices")
        if r.status_code == 200 and r.json().get("devices"):
            return True
        await asyncio.sleep(1 if SETTLE else 0)
    return False


async def _pick_device(wake: Callable[[], Awaitable[None]] | None) -> dict | None:
    async def devices() -> list[dict]:
        r = await _api("GET", "/me/player/devices")
        return r.json().get("devices", []) if r.status_code == 200 else []

    def choose(ds: list[dict]) -> dict | None:
        want = os.getenv("SPOTIFY_DEVICE_NAME", "").strip().lower()
        if want:
            for d in ds:
                if want in (d.get("name") or "").lower():
                    return d
        for d in ds:                                  # Enzo is for the phone: never the PC that happens to be active
            if (d.get("type") or "").lower() == "smartphone":
                return d
        for d in ds:
            if d.get("is_active"):
                return d
        return ds[0] if ds else None

    dev = choose(await devices())
    if dev or wake is None:
        return dev
    await wake()                              # opens Spotify on the phone so it shows up as a device
    for _ in range(8):
        await asyncio.sleep(1 if SETTLE else 0)
        dev = choose(await devices())
        if dev:
            return dev
    return None


async def _now_playing() -> dict:
    r = await _api("GET", "/me/player")
    return r.json() if r.status_code == 200 and r.content else {}


def _matches(state: dict, body: dict, before: str = "") -> bool:
    if "uris" in body:
        # Any song from the list counts: with shuffle on, or a song swapped for its version in this country
        # (linked_from), Spotify does not start on the first one, and a "wrong song" retry restarted it 3 times.
        item = state.get("item") or {}
        playing = {item.get("uri"), (item.get("linked_from") or {}).get("uri")} - {None}
        return bool(playing & set(body["uris"]))
    if "offset" in body:                       # one specific track, started from inside its album
        return (state.get("item") or {}).get("uri") == body["offset"]["uri"]
    if (state.get("context") or {}).get("uri") == body.get("context_uri"):
        return True
    ctx = body.get("context_uri") or ""
    # "Play <artist>" while one of his songs already plays restarts that same song: by the artist and playing is a match.
    if ctx.startswith("spotify:artist:") and state.get("is_playing") and any(a.get("uri") == ctx for a in (state.get("item") or {}).get("artists") or []):
        return True
    # A playlist/artist/album starts on its own first track; Spotify's phone app does not always report the
    # context right away, so a new track that is really playing counts too.
    item = (state.get("item") or {}).get("uri", "")
    return bool(item) and item != before and bool(state.get("is_playing"))


async def _start(body: dict, wake, alt: dict | None = None, accept: set | None = None) -> str:
    """Tell the phone to play, then CHECK it really did (Spotify answers 204 even when the phone ignores it)."""
    last = ""
    before = (await _now_playing()).get("item", {}) or {}
    before = before.get("uri", "")
    woke = False

    async def wake_once():                     # open Spotify on the phone at most ONCE (it jumps to the front)
        nonlocal woke
        woke = True
        if wake:
            await wake()

    for attempt in range(2):                   # each try restarts the song, so only one retry
        dev = await _pick_device(None if woke or wake is None else wake_once)
        if not dev:
            raise SpotifyError("I can't see Spotify running on your phone. Open the Spotify app once and ask again.")
        if attempt or not dev.get("is_active"):
            # make the phone the active player first (a sleeping device ignores plain play commands)
            await _api("PUT", "/me/player", json={"device_ids": [dev["id"]], "play": False})
            await asyncio.sleep(SETTLE)
        send = alt if (alt and attempt >= 1) else body      # second try: the other way of asking for the same song
        r = await _api("PUT", "/me/player/play", params={"device_id": dev["id"]}, json=send)
        if r.status_code == 403:
            raise SpotifyError("Spotify says playback control needs Premium.")
        if r.status_code == 404:
            last = "device vanished"
            continue
        if r.status_code == 400 and alt and send is body:
            last = "that song is not in the first choice"      # e.g. the song is not in the artist playlist
            continue
        if r.status_code not in (200, 202, 204):
            raise SpotifyError(f"Spotify wouldn't start it ({r.status_code}).")
        for _ in range(5):                       # give the phone a few seconds to switch before judging
            await asyncio.sleep(SETTLE)
            state = await _now_playing()
            if _matches(state, send, before) or (accept and state.get("is_playing")
                                                  and (state.get("item") or {}).get("uri") in accept):
                return dev["id"]
        last = (state.get("item") or {}).get("name") or "nothing"
        seen = (f"try {attempt + 1}: device {dev.get('name')!r} active={dev.get('is_active')}, now playing={state.get('is_playing')} "
                f"{last!r} on {(state.get('device') or {}).get('name')!r}, context {(state.get('context') or {}).get('uri')}")
        logger.warning(f"Spotify accepted the play command but: {seen}")
        try:
            from agent import traces
            traces.current().signal("spotify_check", seen)     # so a failure can be read from the trace
        except Exception:
            pass
    raise NotSwitched(f"Spotify accepted it, but the phone kept playing {last}. Open the Spotify app once and try again.")


_tasks: set = set()


def _spawn(coro) -> None:
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


_RADIO_PROMPT = (
    "List 10 songs that sound most like \"{title}\" by {artist}: same era, genre and mood, the kind of songs a good "
    "song radio would play next. Mix different artists (the same artist at most twice), no duplicates, not the song itself. "
    'Answer with ONLY a JSON array like [{{"title": "...", "artist": "..."}}].'
)


async def similar_songs(title: str, artist: str) -> list[dict]:
    """Spotify no longer gives recommendations to new apps, so Claude picks the similar songs."""
    from agent import big_brain
    if not big_brain.available():
        return []

    def call() -> str:
        r = big_brain._get_client().messages.create(
            model=big_brain.BIG_BRAIN_MODEL, max_tokens=800,
            messages=[{"role": "user", "content": _RADIO_PROMPT.format(title=title, artist=artist)}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    try:
        text = await asyncio.to_thread(call)
        m = re.search(r"\[.*\]", text, re.S)
        return [x for x in json.loads(m.group(0)) if isinstance(x, dict) and x.get("title")] if m else []
    except Exception as e:
        logger.warning(f"Spotify radio: Claude gave no list ({e.__class__.__name__}: {e})")
        return []


async def _radio_queue(track: dict, device_id: str) -> None:
    """Put songs similar to `track` right behind it (runs in the background after the song started).

    T-09: Spotify's queue cannot be cleared, so songs added with /me/player/queue stayed behind when he switched
    songs and the radios stacked up. Instead the song is restarted as a list [song, radio...] at the same second:
    a new list replaces the old one, so nothing is left over. Only the queue is used if that is refused."""
    try:
        artist = (track.get("artists") or [{}])[0].get("name", "")
        songs = await similar_songs(track.get("name", ""), artist)
        uris: list[str] = []
        for sg in songs[:10]:
            r = await _api("GET", "/search", params={"q": f"track:{sg['title']} artist:{sg.get('artist', '')}", "type": "track", "limit": 1})
            items = (r.json().get("tracks", {}).get("items") or []) if r.status_code == 200 else []
            if items and items[0]["uri"] != track.get("uri") and items[0]["uri"] not in uris:
                uris.append(items[0]["uri"])
        if not uris:
            return
        state = await _now_playing()
        if not _matches(state, {"uris": [track["uri"]]}):
            logger.info("Spotify radio: he switched songs meanwhile, no radio")      # never undo his newer choice
            return
        r = await _api("PUT", "/me/player/play", params={"device_id": device_id},
                       json={"uris": [track["uri"], *uris], "position_ms": int(state.get("progress_ms") or 0)})
        if r.status_code in (200, 202, 204):
            logger.info(f"Spotify radio: {len(uris)} songs like {track.get('name')!r} follow it (old radio replaced)")
            return
        queued = 0
        for uri in uris:
            q = await _api("POST", "/me/player/queue", params={"uri": uri, "device_id": device_id})
            queued += q.status_code in (200, 202, 204)
        logger.info(f"Spotify radio: list refused ({r.status_code}), queued {queued} songs like {track.get('name')!r}")
    except Exception as e:
        logger.warning(f"Spotify radio failed: {e.__class__.__name__}: {e}")


async def play_query(query: str, wake: Callable[[], Awaitable[None]] | None = None) -> str:
    """Search and play. Returns what is playing, in words."""
    r = await _api("GET", "/search", params={"q": query, "type": "track,artist,playlist,album", "limit": 5})
    if r.status_code == 401:
        raise SpotifyError("Spotify login expired. Say \"connect Spotify\" to sign in again.")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify search failed ({r.status_code}).")
    kind, obj, said = pick(r.json(), query)
    alt = None
    if kind == "track":
        # The phone app ignores a bare "play this track" when something else is playing, but obeys
        # "play this album starting at this track" (found by testing on the real phone).
        alb = (obj.get("album") or {}).get("uri")
        plain = {"uris": [obj["uri"]]}
        body, alt = ({"context_uri": alb, "offset": {"uri": obj["uri"]}}, plain) if alb else (plain, None)
    elif kind == "artist":
        pl = await _artist_playlist(obj["name"])
        if pl:
            body, said = {"context_uri": pl["uri"]}, f"{pl['name']}"
        else:
            body, said = {"context_uri": obj["uri"]}, f"{obj['name']}'s top tracks"
    else:
        body = {"context_uri": obj["uri"]}
    dev = await _start(body, wake, alt)
    if kind == "track" and dev and os.getenv("SPOTIFY_RADIO", "on").lower() != "off":
        _spawn(_radio_queue(obj, dev))
        said += " (with a radio of similar songs)"
    return said


async def play_liked(wake: Callable[[], Awaitable[None]] | None = None) -> str:
    """His own Liked Songs (saved tracks), shuffled. Needs the user-library-read permission (added 2026-11-15:
    a login from before that must connect Spotify once more)."""
    import random
    uris: list[str] = []
    for offset in (0, 50, 100, 150):
        r = await _api("GET", "/me/tracks", params={"limit": 50, "offset": offset})
        if r.status_code in (401, 403):
            raise SpotifyError("I'm not allowed to read your Liked Songs yet. Say \"connect Spotify\" and sign in once more to allow it.")
        if r.status_code != 200:
            raise SpotifyError(f"Spotify would not give me your Liked Songs ({r.status_code}).")
        items = r.json().get("items") or []
        uris += [(i.get("track") or {}).get("uri") for i in items if (i.get("track") or {}).get("uri")]
        if len(items) < 50:
            break
    if not uris:
        raise SpotifyError("Your Liked Songs list is empty.")
    random.shuffle(uris)
    # The phone sometimes keeps its old queue: a liked song playing is what he asked for, so don't restart it 3 times.
    await _start({"uris": uris[:100]}, wake, accept=set(uris))
    return "your Liked Songs"


async def control(action: str) -> None:
    """pause | resume | next | previous"""
    table = {"pause": ("PUT", "/me/player/pause"), "resume": ("PUT", "/me/player/play"),
             "next": ("POST", "/me/player/next"), "previous": ("POST", "/me/player/previous")}
    method, path = table[action]
    r = await _api(method, path)
    if r.status_code == 403:
        try:
            reason = (r.json().get("error") or {}).get("reason", "")
        except Exception:
            reason = ""
        if reason == "PREMIUM_REQUIRED":
            raise SpotifyError("Spotify says playback control needs Premium.")
        # 403 "restriction violated" = already in that state (pause while paused, play while playing)
        playing = bool((await _now_playing()).get("is_playing"))
        if (action == "pause" and not playing) or (action == "resume" and playing):
            return
        raise SpotifyError("Spotify refused that right now. Try again in a moment.")
    if r.status_code == 404:
        raise SpotifyError("Nothing is playing on Spotify right now.")
    if r.status_code not in (200, 202, 204):
        raise SpotifyError(f"Spotify said {r.status_code}.")


# ── What is playing, like it, add it to a playlist (T-11) ─────────────────

def _say(item: dict) -> str:
    artists = ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))
    return f"{item.get('name', '?')} by {artists}" if artists else item.get("name", "?")


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code in (401, 403):
        raise SpotifyError(f"Spotify won't let me {what}. Say \"connect Spotify\" and sign in once more to allow it.")
    if r.status_code not in (200, 201, 204):
        raise SpotifyError(f"Spotify said {r.status_code}.")


async def _current_track() -> dict:
    item = (await _now_playing()).get("item") or {}
    if not item.get("uri", "").startswith("spotify:track:"):
        raise SpotifyError("Nothing is playing on Spotify right now.")
    return item


def _card(state: dict) -> None:
    """The app's now-playing card, from the same player state the answer uses."""
    item = state.get("item") or {}
    if not item.get("uri"):
        return
    from agent.context import show_card
    images = (item.get("album") or {}).get("images") or []
    show_card("music", {
        "title": item.get("name", ""), "artist": ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name")),
        "image": images[0].get("url", "") if images else "", "playing": bool(state.get("is_playing")),
        "progress_ms": state.get("progress_ms") or 0, "duration_ms": item.get("duration_ms") or 0,
    })


async def show_playing() -> None:
    """After Enzo started something: show what Spotify is really playing now (no card if it can't tell)."""
    try:
        _card(await _now_playing())
    except Exception as e:
        logger.debug(f"music card: {e}")


async def now_playing() -> str:
    state = await _now_playing()
    _card(state)
    item = state.get("item") or {}
    if not item.get("uri"):
        return "Nothing is playing on Spotify right now."
    paused = "" if state.get("is_playing") else " (paused)"
    dev = (state.get("device") or {}).get("name")
    return f"Now playing: {_say(item)}{paused}" + (f" on {dev}." if dev else ".")


async def like_current() -> str:
    item = await _current_track()
    _check(await _api("PUT", "/me/tracks", params={"ids": item["id"]}), "change your Liked Songs")
    return _say(item)


async def add_current_to_playlist(name: str) -> tuple[str, str]:
    """(song, playlist name). Matches his playlists by name: exact first, then one that contains it."""
    item = await _current_track()
    lists: list[dict] = []
    for offset in (0, 50, 100, 150):
        r = await _api("GET", "/me/playlists", params={"limit": 50, "offset": offset})
        _check(r, "read your playlists")
        items = [p for p in r.json().get("items") or [] if p]
        lists += items
        if len(items) < 50:
            break
    n = norm(name)
    found = [p for p in lists if norm(p.get("name", "")) == n] or [p for p in lists if n and n in norm(p.get("name", ""))]
    if not found:
        raise SpotifyError(f"I couldn't find a playlist called {name} in your Spotify.")
    if len(found) > 1:
        raise SpotifyError("Several playlists match: " + ", ".join(p["name"] for p in found[:4]) + ". Ask which one.")
    pl = found[0]
    _check(await _api("POST", f"/playlists/{pl['id']}/tracks", json={"uris": [item["uri"]]}), f"change the playlist {pl['name']}")
    return _say(item), pl["name"]
