"""
agent/context.py — What Enzo knows about the *current* session.

The frontend sends {"type": "context", ...} right after connecting (device type,
timezone and — if the user allowed it — GPS location). Tools read it from here,
so e.g. "what's the weather?" works without naming a city.

Uses a ContextVar so each WebSocket connection has its own session.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field


@dataclass
class Session:
    device: str = "unknown"            # "mobile" | "desktop" | "telegram" | "unknown"
    timezone: str = ""
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    place: str = ""                    # human-readable place, if known (told by user or geocoded)
    app: str = "web"                   # "android" when the Enzo Android app is connected, else "web"
    send_device: object = None         # async (action: dict) -> None: ask the connected app to do something
    ask_device: object = None          # async (action: dict, timeout) -> dict | None: ask the app and wait for its answer
    popup: bool = False                # he talks from the Enzo popup over another app (phone assistant shortcut)
    lang: str = "en"                   # language of the user's latest message ("he" / "en")
    extra: dict = field(default_factory=dict)
    # Approvals (agent/approvals.py): how to ask the user, and tools they said "always" to.
    confirm: object = None             # async (question) -> "yes" | "always" | "no"
    always_allowed: set = field(default_factory=set)
    loop: object = None                # event loop of the current request (for approvals from threads)
    activity: object = None            # async (tool_name) -> None: show in the app which tool is running
    card: object = None                # async (kind, data) -> None: show a tool's real result as a card in the app
    transcript: object = None          # (his last request, the messages after the prefix exactly as sent + the answer)

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def update(self, data: dict) -> None:
        if data.get("device") in ("mobile", "desktop", "telegram"):
            self.device = data["device"]
        if data.get("app") in ("android", "web"):
            self.app = data["app"]
        if "popup" in data:
            self.popup = bool(data["popup"])
        if isinstance(data.get("timezone"), str):
            self.timezone = data["timezone"][:64]
        loc = data.get("location")
        if isinstance(loc, dict):
            try:
                lat, lon = float(loc["latitude"]), float(loc["longitude"])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    self.latitude, self.longitude = lat, lon
                    self.accuracy_m = float(loc.get("accuracy") or 0) or None
                    self.place = ""  # re-resolve lazily
            except (KeyError, TypeError, ValueError):
                pass

    def describe(self) -> str:
        """One line for the system prompt."""
        if self.has_location:
            loc = f"shared by device ({self.place})" if self.place else "shared by device — call get_user_location for the place name"
        else:
            loc = self.place or "unknown — ask the user if you need it"
        from agent.lang import language_instruction
        phone = ("He is talking to you through the Enzo app on his PHONE: music, volume, brightness, calls, messages, timers, "
                 "alarms and notifications mean the phone (phone_music, phone_settings, phone_call, phone_message, phone_timer, "
                 "phone_alarm, phone_notifications) unless he says PC or computer. phone_screen sees and uses his phone screen.\n"
                 + ("He opened you in the small popup over the app he is using, so a question about what he is looking at "
                    "(\"this\", \"here\", \"my screen\", \"what does she want\", \"what does it say\", \"reply to him\") is about "
                    "that phone screen, not the PC: phone_screen read first.When he is done (\"that's all\", \"thanks, bye\") call end_conversation.\n"
                    if self.popup else "") if self.app == "android" else
                 "He is NOT on the phone app right now, so the phone_* tools cannot work.\n")
        return (phone + f"Device: {self.device}. Location: {loc}. Timezone: {self.timezone or 'unknown'}.\n"
                f"{language_instruction(self.lang)}")


_current: contextvars.ContextVar[Session] = contextvars.ContextVar("enzo_session")


def new_session() -> Session:
    s = Session()
    _current.set(s)
    return s


def use_session(s: Session) -> Session:
    """Make an existing session current (e.g. a Telegram chat's stored session)."""
    _current.set(s)
    return s


def session() -> Session:
    try:
        return _current.get()
    except LookupError:
        return new_session()


_card_tasks: set = set()   # keeps fire-and-forget card sends alive until done


def show_card(kind: str, data: dict) -> None:
    """Send an answer card (a tool's real data) to the app. Works from async tools and from worker threads;
    does nothing where no app can draw it (Telegram, background jobs). Never fails the tool."""
    s = session()
    if s.card is None:
        return

    async def send():
        try:
            await s.card(kind, data)
        except Exception:
            pass                                  # the app went away: the turn goes on

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            t = loop.create_task(send())
            _card_tasks.add(t)
            t.add_done_callback(_card_tasks.discard)
        elif s.loop is not None:
            asyncio.run_coroutine_threadsafe(send(), s.loop)
    except Exception:
        pass                                      # e.g. the request's loop already closed
