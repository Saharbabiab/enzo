"""
agent/phone_reminders.py - keep the phone's own alarm clock in step with Enzo's reminders.

The server fires reminders too (web voice + Telegram), but a reminder that lives ON the phone still
rings when the app is closed, the screen is off or the network is down. After every change (and when
the app connects) the active reminder list is pushed to the app, which schedules it with Android.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger


def _ms(dt) -> int | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_items(docs: list[dict]) -> list[dict]:
    """Reminder documents -> what the phone needs. Scheduled TASKS stay on the server (it does the work)."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    items = []
    for d in docs:
        meta = d.get("metadata") or {}
        if meta.get("task") or meta.get("command"):
            continue
        at = _ms(d.get("next_fire"))
        if not at or at < now - 60_000:
            continue
        recurring = d.get("type") == "recurring"
        items.append({
            "id": str(d.get("_id")),
            "text": str(d.get("message") or "Reminder")[:200],
            "at": at,
            "repeat": int(d.get("interval_seconds", 0)) * 1000 if recurring else 0,
            "alarm": d.get("type") == "alarm",
        })
    return items[:60]


async def sync(sess=None) -> bool:
    """Push the active reminders to the phone in this session (does nothing for the web/PC)."""
    try:
        if sess is None:
            from agent.context import session
            sess = session()
        if getattr(sess, "app", "") != "android" or sess.send_device is None:
            logger.info(f"Phone reminders: not pushed (app={getattr(sess, 'app', '?')!r}, channel={'yes' if sess.send_device else 'no'})")
            return False
        from scheduler import reminder_store
        items = build_items(reminder_store.list_active())
        await sess.send_device({"action": "sync_reminders", "items": items})
        logger.info(f"Phone reminders: pushed {len(items)} to the phone")
        return True
    except Exception as e:
        logger.debug(f"phone_reminders.sync: {e}")
        return False


def delivery_note(data: dict, sess=None) -> str | None:
    """The app's report after a reminder rang -> a line for the chat. A blocked one is said once per session."""
    if data.get("posted"):
        from agent.instant import _now
        return f"Reminder delivered at {_now():%H:%M}, sir."
    if sess is not None:
        if sess.__dict__.get("reminder_blocked_said"):
            return None
        sess.__dict__["reminder_blocked_said"] = True
    return "Android blocked that reminder notification, sir. Please allow notifications for Enzo in the settings screen I opened."
