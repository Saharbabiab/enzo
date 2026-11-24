"""
agent/proactive.py — Enzo speaks up on his own during the day.

Runs every minute (started in server.py) and, without being asked:
  • Calendar heads-up: "In 15 minutes: Dinner with Dana @ Café X" (once per event)
  • Google login: Google expires logins of apps in Testing mode after 7 days —
    from day 6 Enzo reminds you (once a day, 09:00-21:00) to run google_setup.py,
    and immediately if a Google call fails because the login expired.
  • Self-check: every 10 minutes (08:00-22:00) the local model and the database are checked; each problem is
    told once a day (SELF_CHECK=off turns it off).

Messages go through scheduler.notify(): web app voice + Telegram + PC notification.
Turn off with PROACTIVE=off. Heads-up time: CALENDAR_HEADSUP_MINUTES (default 15).
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime

import pytz
from loguru import logger

TZ = pytz.timezone("Asia/Jerusalem")
TICK_SECONDS = 60
HEADSUP_MIN = int(os.getenv("CALENDAR_HEADSUP_MINUTES", "15"))

_announced_events: set[str] = set()
_last_login_nag: str = ""           # date of the last "re-login" reminder
_told_problems: set[str] = set()    # "date|problem" already announced by the self-check


async def check_google_login(now: datetime, notify) -> None:
    """Remind to re-run google_setup.py before (or right after) the 7-day expiry."""
    global _last_login_nag
    from agent import google_services as g
    if not os.path.exists(g.CREDENTIALS_FILE):
        return
    today = now.date().isoformat()
    if _last_login_nag == today or not (9 <= now.hour < 21):
        return
    age = await asyncio.to_thread(g.login_age_days)
    if g.auth_problem:
        msg = ("My Google login expired, sir, so I can't see your calendar or email. "
               "Tap Google in the app's Settings to sign in again.")
    elif g.LOGIN_VALID_DAYS > 0 and age is not None and age >= g.LOGIN_VALID_DAYS - 1:
        left = max(0.0, g.LOGIN_VALID_DAYS - age)
        when = "today" if left < 1 else "tomorrow"
        msg = (f"Heads up, sir: my Google login expires {when}. Tap Google in the app's Settings "
               "to keep your calendar and email working.")
    else:
        return
    _last_login_nag = today
    logger.info("🔑 Reminding about Google re-login")
    await notify(msg, title="Enzo — Google login")


def headsup(e: dict, mins: int = HEADSUP_MIN) -> tuple[str, str]:
    """(id, text) of an event's heads-up. The id is the phone notification's: a moved event gets a new one."""
    where = f" at {e['location']}" if e.get("location") else ""
    when = "now" if mins <= 1 else f"in {mins} minutes"
    return f"cal-{e['id']}-{int(e['start'].timestamp())}", f"{e['summary']} starts {when}{where}, sir."


async def check_calendar(now: datetime, notify) -> None:
    from agent import google_services as g
    if not g.connected() or g.auth_problem:
        return
    try:
        events = await asyncio.to_thread(g.upcoming_events, HEADSUP_MIN + 1)
    except Exception as e:
        logger.debug(f"Calendar heads-up: {e}")
        return
    for e in events:
        if e["id"] in _announced_events:
            continue
        mins = int((e["start"] - now).total_seconds() // 60)
        if 0 <= mins <= HEADSUP_MIN:
            _announced_events.add(e["id"])
            rid, text = headsup(e, mins)
            # Pushed to the phone (agent/push.py); stale_after: past the start it would be wrong, so a late one is dropped
            await notify(text, title="Enzo — Calendar", rid=rid, stale_after=int(e["start"].timestamp() * 1000))


async def check_briefing(now: datetime, notify) -> None:
    """Prepare the morning briefing at 07:00 (kept until 12:00), delete it when it expires."""
    from agent import briefing
    await briefing.tick()


async def check_lessons(now: datetime, notify) -> None:
    """The nightly review: score lesson notes, Claude improves them and proposes playbooks (agent/nightly.py)."""
    from agent import nightly
    await nightly.tick(now)


async def check_learned_skills(now: datetime, notify) -> None:
    """A task that went well may be a routine worth keeping: review it once he is quiet (agent/skill_review.py)."""
    from agent import skill_review
    await skill_review.tick(notify)


async def check_code_jobs(now: datetime, notify) -> None:
    """Coding jobs wait for the PC: send queued ones once it is connected (agent/code_jobs.py)."""
    from agent import code_jobs
    await code_jobs.send_queued()


async def check_budget(now: datetime, notify) -> None:
    """Tell the user once a day when Claude spending passes 80% of the daily budget."""
    from agent import big_brain
    msg = big_brain.budget_warning()
    if msg:
        await notify(msg)


async def check_backup(now: datetime, notify) -> None:
    """Zip data/ + memory facts once a night, keep 14 (agent/backup.py)."""
    from agent import backup
    await backup.tick(now)


async def check_health(now: datetime, notify) -> None:
    """Self-check: tell the user once a day about each broken part (local model, database)."""
    if os.getenv("SELF_CHECK", "on").strip().lower() in ("off", "false", "0") or now.minute % 10 or not (8 <= now.hour < 22):
        return
    from agent import health
    today = now.date().isoformat()
    new = [p for p in await asyncio.to_thread(health.problems) if f"{today}|{p}" not in _told_problems]
    if not new:
        return
    _told_problems.update(f"{today}|{p}" for p in new)
    logger.warning(f"🩺 Self-check: {'; '.join(new)}")
    await notify(f"Self-check, sir: {'; and '.join(new)}. Say system status for details.", title="Enzo — Self-check")


async def check_repeat_failures(now: datetime, notify) -> None:
    """Post-mortem: tell the user once when the same tool has failed 3 times today."""
    from agent import lessons
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for msg in lessons.repeat_failures(now.date().isoformat(), midnight.timestamp()):
        await notify(msg, title="Enzo - Repeated failure")


async def proactive_loop():
    if os.getenv("PROACTIVE", "on").lower() == "off":
        logger.info("🫀 Proactive mode off")
        return
    from scheduler import notify
    logger.info("🫀 Proactive loop started (calendar heads-up, Google login reminders)")
    while True:
        now = datetime.now(TZ)
        for check in (check_calendar, check_google_login, check_lessons, check_learned_skills, check_briefing, check_budget,
                      check_code_jobs, check_backup, check_health, check_repeat_failures):
            try:
                await check(now, notify)
            except Exception as e:
                logger.debug(f"Proactive {check.__name__}: {e}")
        await asyncio.sleep(TICK_SECONDS)
