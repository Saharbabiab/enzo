"""
agent/briefing.py — Opt-in scheduled morning briefing.

Set in .env (both optional):
  BRIEFING_TIME=07:30          daily time (Israel time). Empty/unset = OFF.
  BRIEFING_DAYS=0,1,2,3,4      weekdays to run (0=Mon … 6=Sun). Default: every day.

At that time Enzo builds the briefing (weather, reminders, headlines via the
daily_briefing tool), speaks it on any open web app and sends it to Telegram.
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timedelta

import pytz
from loguru import logger

ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")


def _parse_time(value: str) -> tuple[int, int] | None:
    try:
        h, m = (int(x) for x in value.strip().split(":"))
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, AttributeError):
        return None


def _days() -> set[int]:
    raw = os.getenv("BRIEFING_DAYS", "").strip()
    if not raw:
        return set(range(7))
    return {int(d) for d in raw.split(",") if d.strip().isdigit() and 0 <= int(d) <= 6}


def next_fire(now: datetime, hm: tuple[int, int], days: set[int]) -> datetime:
    """Next datetime (tz-aware, Israel) at HH:MM on an allowed weekday, strictly after `now`."""
    candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in days:
            return candidate
        candidate += timedelta(days=1)
    return candidate


# The briefing has two parts: the KEPT part (calendar, news, fun fact) is written at 07:00 and kept until noon;
# the LIVE opening (greeting, weather where he is, reminders, email) is written when he hears it, so it is current.
_STYLE = """Spoken English, no markdown, no lists. Call him "sir" (never by his name).
Calm, dry J.A.R.V.I.S. tone: no exclamation marks. Translate Hebrew by meaning, names in English letters, never Hebrew script."""

_KEPT_PROMPT = """You are Enzo, a witty J.A.R.V.I.S.-style home assistant, writing the second half of
The user's spoken morning briefing (another part before it already greets him and covers weather, reminders and email,
so do NOT greet him and do NOT mention weather). Use ONLY the data below for the calendar and news.

{data}

WHAT YOU KNOW ABOUT THE USER (use for the facts):
{profile}

Write 4-6 short sentences:
1. Today's calendar events (or that the calendar is clear). Say what each event MEANS for him in natural
   English (e.g. a Hebrew title with his girlfriend's name and a heart = "your date with Dana").
2. Two or three of the most important headlines in one or two sentences.
3. "Fun fact of the day": ONE or TWO genuinely interesting, accurate facts tied to his
   interests (pick different interests than you'd expect, rotate between them). Only
   facts you are confident are true; no made-up numbers. Today's rotation hint: {rotation}.
End with a short upbeat line.
He may hear this hours after it is written: give events their times, but never "now", "soon" or "in an hour".
""" + _STYLE

_LIVE_PROMPT = """You are Enzo, a witty J.A.R.V.I.S.-style home assistant, writing the opening of
The user's spoken morning briefing, right now as he hears it. Use ONLY the data below.

{data}

Write 2-4 short sentences:
1. A warm good-morning with the day and date.
2. The weather (temperature, and whether to grab a jacket).
3. Reminders still ahead today (or none).
4. Email (only if EMAIL data is present): ONE short sentence, how many need attention and who they are from /
   what about. Ignore newsletters, promotions, notifications and anything routine. If nothing important, say so
   in a few words.
Do not cover the calendar, news or facts: they follow right after this.
""" + _STYLE


def _interests_rotation(profile: str, today: datetime) -> str:
    """Pick which interest to focus the facts on today, so they vary day to day."""
    import re
    m = re.search(r"hobbies=\[([^\]]*)\]", profile)
    items = [x.strip(" '\"") for x in m.group(1).split(",")] if m else []
    if "anime" in profile.lower() and "anime" not in items:
        items.append("anime")
    if not items:
        return "any topic he'd enjoy"
    return items[today.toordinal() % len(items)]


async def _write(prompt: str) -> str:
    """Best available writer: Claude → Gemini → local model. Facts need a strong model."""
    from agent import big_brain, gemini_brain
    if big_brain.available():
        try:
            def _claude():
                client = big_brain._get_client()
                r = client.messages.create(
                    model=big_brain.BIG_BRAIN_MODEL, max_tokens=4000,
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": prompt}],
                )
                return "".join(b.text for b in r.content if b.type == "text").strip()
            text = await asyncio.to_thread(_claude)
            if text:
                return text
        except Exception as e:
            logger.warning(f"Briefing via Claude failed: {e}")
    if gemini_brain.available():
        try:
            client = gemini_brain._get_client()
            for model in [gemini_brain.GEMINI_MODEL] + gemini_brain.GEMINI_FALLBACK_MODELS:
                try:
                    r = await asyncio.to_thread(client.models.generate_content, model=model, contents=prompt)
                    if r.text:
                        return r.text.strip()
                except Exception as e:
                    logger.warning(f"Briefing via {model} failed: {e}")
        except Exception as e:
            logger.warning(f"Briefing via Gemini failed: {e}")
    from agent.agent_loop import background_chat      # same start and window as the agent: keeps its prompt cache
    r = await asyncio.to_thread(background_chat, prompt, num_predict=700)
    return (r.message.content or "").strip()


async def compose_briefing() -> str:
    """The whole briefing, written now: the live opening + the kept part."""
    return await finish(await compose_kept())


async def finish(kept: str) -> str:
    """Put the live opening (greeting, weather where he is now, reminders, email) in front of the kept part."""
    from agent.tools import briefing_data
    from rules import enforce_rules
    try:
        opening = await _write(_LIVE_PROMPT.format(data=await briefing_data(live=True, kept=False)))
    except Exception as e:
        logger.warning(f"Briefing opening failed: {e}")
        opening = ""
    opening = enforce_rules(opening) if opening else ""
    return " ".join(p for p in (opening, kept) if p)


async def compose_kept() -> str:
    """The part written at 07:00 and kept until noon: calendar, news, fun fact (and Enzo's own playbook ideas)."""
    from agent.tools import briefing_data
    from agent.memory import get_profile_summary
    from rules import enforce_rules
    data = await briefing_data(live=False, kept=True)
    from agent import lessons
    ideas = lessons.proposals()
    if ideas:   # the nightly review's playbook ideas wait for his OK
        data += (f"\nEnzo's own news: {len(ideas)} new playbook idea(s) waiting for his OK: {', '.join(ideas)} "
                 "(he can say 'approve <name>' or 'reject <name>').")
    profile = await asyncio.to_thread(get_profile_summary)
    prompt = _KEPT_PROMPT.format(
        data=data, profile=profile, rotation=_interests_rotation(profile, datetime.now(ISRAEL_TZ)))
    text = await _write(prompt)
    return enforce_rules(text) if text else ""


# ════════════════════════════════════════════════════════════════
# "First chat of the morning" briefing
# ════════════════════════════════════════════════════════════════

_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "briefing_state.json")


def _window() -> tuple[tuple[int, int], tuple[int, int]] | None:
    """MORNING_BRIEFING_WINDOW=07:00-11:00 (default). 'off' disables."""
    raw = os.getenv("MORNING_BRIEFING_WINDOW", "07:00-11:00").strip().lower()
    if raw in ("", "off", "false", "0"):
        return None
    try:
        a, b = raw.split("-")
        start, end = _parse_time(a), _parse_time(b)
        return (start, end) if start and end else None
    except ValueError:
        return None


def _last_date() -> str:
    import json
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("last_date", "")
    except Exception:
        return ""


def _mark_done(day: str) -> None:
    import json
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_date": day}, f)
    except Exception as e:
        logger.warning(f"Briefing state: {e}")


def morning_briefing_due(now: datetime | None = None) -> bool:
    """True for the first conversation of the day inside the morning window."""
    w = _window()
    if not w:
        return False
    now = now or datetime.now(ISRAEL_TZ)
    minutes = now.hour * 60 + now.minute
    start, end = w[0][0] * 60 + w[0][1], w[1][0] * 60 + w[1][1]
    return start <= minutes < end and _last_date() != now.date().isoformat()


async def maybe_morning_briefing() -> str | None:
    """If due, compose today's briefing (once per day) and return it."""
    if not morning_briefing_due():
        return None
    today = datetime.now(ISRAEL_TZ).date().isoformat()
    _mark_done(today)  # mark first, so two clients don't both get it
    try:
        text = await get_briefing()
        return text or None
    except Exception as e:
        logger.error(f"Morning briefing failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# Prepared briefing: written in the background at 07:00, ready when the user wakes, deleted at 12:00
# ════════════════════════════════════════════════════════════════

READY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "briefing_ready.json")
_preparing = False


def _hm_env(name: str, default: str) -> tuple[int, int]:
    return _parse_time(os.getenv(name, "")) or _parse_time(default)  # type: ignore[return-value]


def _at(now: datetime, hm: tuple[int, int]) -> datetime:
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def prepare_at(now: datetime) -> datetime:
    return _at(now, _hm_env("BRIEFING_PREPARE_AT", "07:00"))


def expires_at(now: datetime) -> datetime:
    return _at(now, _hm_env("BRIEFING_EXPIRES", "12:00"))


def _read_ready() -> dict:
    import json
    try:
        with open(READY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def clear_ready() -> None:
    try:
        os.remove(READY_FILE)
    except OSError:
        pass


def ready_briefing(now: datetime | None = None) -> str | None:
    """Today's prepared briefing, or None. A stale or expired one is deleted on the spot."""
    now = now or datetime.now(ISRAEL_TZ)
    d = _read_ready()
    if not d:
        return None
    if d.get("date") != now.date().isoformat() or now >= expires_at(now) or d.get("part") != "kept":   # "part": an old whole briefing would double the greeting
        clear_ready()
        return None
    return d.get("text") or None


def prepare_due(now: datetime) -> bool:
    """Between the prepare time and the expiry, and nothing prepared yet today."""
    if os.getenv("BRIEFING_PREPARE", "on").strip().lower() in ("off", "false", "0"):
        return False
    return prepare_at(now) <= now < expires_at(now) and ready_briefing(now) is None


async def prepare_briefing(now: datetime | None = None) -> str | None:
    """Write today's kept part (calendar, news, fun fact) and keep it until the expiry time."""
    now = now or datetime.now(ISRAEL_TZ)
    text = await compose_kept()
    if not text:
        return None
    _save_ready({"date": now.date().isoformat(), "created": now.isoformat(), "part": "kept", "text": text})
    logger.info(f"Morning briefing prepared ({len(text)} chars), kept until {expires_at(now).strftime('%H:%M')}")
    return text


def _save_ready(d: dict) -> None:
    import json
    os.makedirs(os.path.dirname(READY_FILE), exist_ok=True)
    with open(READY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


async def tick(now: datetime | None = None) -> None:
    """Called every minute by the background loop: prepare when it is time, delete when it has expired."""
    global _preparing
    now = now or datetime.now(ISRAEL_TZ)
    ready_briefing(now)                       # deletes an expired/old one
    if _preparing or not prepare_due(now):
        return
    _preparing = True
    try:
        await prepare_briefing(now)
    except Exception as e:
        logger.warning(f"Briefing prepare failed: {e}")
    finally:
        _preparing = False


async def get_briefing() -> str:
    """The briefing for the morning_briefing tool: a live opening + the part kept from 07:00 (made now if missing)."""
    kept = ready_briefing()
    if not kept:
        now = datetime.now(ISRAEL_TZ)
        if prepare_at(now) <= now < expires_at(now):
            kept = await prepare_briefing(now)     # morning and nothing prepared yet: prepare + keep
        else:
            kept = await compose_kept()            # outside the window: just a fresh one, not kept
    return await finish(kept or "")


async def deliver(text: str) -> None:
    from scheduler import _send_voice_alert
    from heartbeat import _send_telegram
    try:
        await _send_voice_alert(text)
    except Exception as e:
        logger.warning(f"Briefing voice: {e}")
    await _send_telegram(f"☀️ Morning briefing\n\n{text}")


async def briefing_loop():
    hm = _parse_time(os.getenv("BRIEFING_TIME", ""))
    if not hm:
        logger.info("☀️ Morning briefing off (set BRIEFING_TIME=HH:MM to enable)")
        return
    days = _days()
    logger.info(f"☀️ Morning briefing at {hm[0]:02d}:{hm[1]:02d} on days {sorted(days)}")
    while True:
        now = datetime.now(ISRAEL_TZ)
        fire = next_fire(now, hm, days)
        await asyncio.sleep(max(1.0, (fire - now).total_seconds()))
        try:
            text = await compose_briefing()
            if text:
                logger.info(f"☀️ Briefing: {text[:120]}")
                await deliver(text)
                _mark_done(datetime.now(ISRAEL_TZ).date().isoformat())  # no second one on first chat
        except Exception as e:
            logger.error(f"Briefing failed: {e}")
        await asyncio.sleep(61)  # don't fire twice in the same minute
