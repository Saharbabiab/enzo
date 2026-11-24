"""
scheduler.py — S.H.A.R.P. persistent reminder and scheduling system.

Fix: scheduler now generates TTS audio and sends it directly to connected
WebSocket clients when a reminder fires. No longer relies on the bot loop.
"""

import os
import uuid
import asyncio
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import pytz
from loguru import logger
from pymongo import MongoClient, ASCENDING
from dotenv import load_dotenv

load_dotenv()

MONGO_URI      = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
ISRAEL_TZ      = pytz.timezone("Asia/Jerusalem")
CHECK_INTERVAL = 10  # seconds between scheduler ticks
# Create a shared queue for tasks
TASK_QUEUE = asyncio.Queue()

_active_websockets: set = set()

def register_websocket(ws):
    _active_websockets.add(ws)

def unregister_websocket(ws):
    _active_websockets.discard(ws)


# ────────────────────────────────────────────────────────────────
# REMINDER STORE
# ────────────────────────────────────────────────────────────────
class ReminderStore:

    def __init__(self, uri: str):
        self._ok = False
        try:
            self.client    = MongoClient(uri, serverSelectionTimeoutMS=5000)
            self.db        = self.client["sharp_db"]
            self.reminders = self.db["reminders"]
            self.reminders.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                background=True,
                sparse=True,
            )
            self._ok = True
            logger.info("🟢 ReminderStore connected.")
        except Exception as e:
            logger.error(f"🔴 ReminderStore: {e}")

    def add(
        self,
        message:          str,
        reminder_type:    str             = "once",
        fire_at:          datetime | None = None,
        interval_seconds: int             = 0,
        label:            str             = "",
        metadata:         dict | None     = None,
    ) -> str | None:
        if not self._ok:
            return None

        rid = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc)

        # RESTORED LOGIC: Calculate when the reminder should fire
        if reminder_type in ("alarm", "recurring") and fire_at:
            next_fire = fire_at          # recurring + fire_at = "every day at 20:00" style
        elif interval_seconds > 0:
            next_fire = now + timedelta(seconds=interval_seconds)
        elif fire_at:
            next_fire = fire_at
        else:
            logger.warning(f"Reminder [{rid}] has no valid time/interval.")
            return None

        doc = {
            "_id":              rid,
            "message":          message,
            "label":            label or message[:30],
            "type":             reminder_type,
            "fire_at":          fire_at,
            "interval_seconds": interval_seconds,
            "next_fire":        next_fire,
            "active":           True,
            "created_at":       now,
            "last_fired":       None,
            "metadata":         metadata,
        }
        try:
            self.reminders.insert_one(doc)
            logger.info(f"⏰ Reminder [{rid}]: '{message}' type={reminder_type}")
            return rid
        except Exception as e:
            logger.error(f"Reminder add error: {e}")
            return None

    def get_due(self) -> list[dict]:
        if not self._ok:
            return []
        now = datetime.now(timezone.utc)
        try:
            return list(self.reminders.find({"active": True, "next_fire": {"$lte": now}}))
        except Exception as e:
            logger.error(f"get_due: {e}")
            return []

    def mark_fired(self, rid: str):
        if not self._ok:
            return
        now = datetime.now(timezone.utc)
        try:
            doc = self.reminders.find_one({"_id": rid})
            if not doc:
                return
            if doc["type"] == "recurring":
                interval = max(doc.get("interval_seconds", 60), 1)
                # Step from the previous scheduled time (not "now") so "every day at
                # 20:00" stays at 20:00; skip any slots missed while Enzo was off.
                nxt = doc.get("next_fire") or now
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                while nxt <= now:
                    nxt += timedelta(seconds=interval)
                self.reminders.update_one(
                    {"_id": rid},
                    {"$set": {"last_fired": now, "next_fire": nxt}}
                )
            else:
                self.reminders.update_one({"_id": rid}, {"$set": {"active": False, "last_fired": now}})
        except Exception as e:
            logger.error(f"mark_fired: {e}")

    def cancel(self, rid: str) -> bool:
        if not self._ok:
            return False
        try:
            r = self.reminders.update_one({"_id": rid}, {"$set": {"active": False}})
            return r.modified_count > 0
        except Exception:
            return False

    def cancel_by_label(self, label: str) -> int:
        if not self._ok:
            return 0
        try:
            r = self.reminders.update_many(
                {"label": {"$regex": label, "$options": "i"}, "active": True},
                {"$set": {"active": False}}
            )
            return r.modified_count
        except Exception:
            return 0

    def list_active(self) -> list[dict]:
        if not self._ok:
            return []
        try:
            return list(self.reminders.find({"active": True}).sort("next_fire", 1))
        except Exception:
            return []

    def recent_and_active(self) -> list[dict]:
        """Active reminders plus the ones that rang in the last day (for snooze / done)."""
        if not self._ok:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=1)
        try:
            return list(self.reminders.find({"$or": [{"active": True}, {"last_fired": {"$gte": since}}]}))
        except Exception:
            return []

    def set_fields(self, rid: str, fields: dict) -> bool:
        if not self._ok:
            return False
        try:
            return self.reminders.update_one({"_id": rid}, {"$set": fields}).modified_count > 0
        except Exception:
            return False


def _utc(dt):
    if dt is not None and dt.tzinfo is None:   # Mongo returns naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pick_reminder(docs: list[dict], which: str = "") -> dict | None:
    """The reminder he means: by id or words of its text, else the one that rang last. Tasks are not reminders."""
    docs = [d for d in docs if not ((d.get("metadata") or {}).get("task") or (d.get("metadata") or {}).get("command"))]
    w = which.strip().lower()
    if w:
        docs = [d for d in docs if str(d.get("_id")) == w
                or w in str(d.get("label", "")).lower() or w in str(d.get("message", "")).lower()]
    fired = sorted((d for d in docs if d.get("last_fired")), key=lambda d: _utc(d["last_fired"]), reverse=True)
    if fired and (not w or not any(d.get("active") for d in docs)
                  or datetime.now(timezone.utc) - _utc(fired[0]["last_fired"]) < RECENT_FIRE):
        return fired[0]
    active = sorted((d for d in docs if d.get("active") and d.get("next_fire")), key=lambda d: _utc(d["next_fire"]))
    return active[0] if active else (fired[0] if fired else None)


RECENT_FIRE = timedelta(hours=1)   # "done" within this after it rang = he did it; it does not skip the next ring


def done_plan(doc: dict, now: datetime | None = None) -> tuple[str, datetime | None]:
    """What "done" means for this reminder: ("ok", None) it already rang, ("skip", next_fire) a recurring
    one done before it rings skips that ring, ("cancel", None) a one-time one done early is cancelled."""
    now = now or datetime.now(timezone.utc)
    last = _utc(doc.get("last_fired"))
    if last and now - last < RECENT_FIRE or not doc.get("active"):
        return "ok", None
    if doc.get("type") == "recurring":
        interval = timedelta(seconds=max(int(doc.get("interval_seconds") or 60), 1))
        nxt = _utc(doc.get("next_fire")) or now
        while nxt <= now:
            nxt += interval
        return "skip", nxt + interval
    return "cancel", None


reminder_store = ReminderStore(MONGO_URI)


# ────────────────────────────────────────────────────────────────
# TTS — generates audio and sends to all connected clients
# ────────────────────────────────────────────────────────────────
async def _speak_reminder(text: str, is_alarm: bool = False, rid: str = "", stale_after: int = 0):
    """
    Generate TTS for a reminder and send audio bytes directly to all
    connected WebSocket clients. Also sends a transcript message so
    the frontend can display it.
    """
    logger.info(f"Reminder fired: {len(_active_websockets)} connected client(s)")
    prefix = "Attention sir. " if is_alarm else ""
    full_text = f"{prefix}{text}"
    audio_bytes = None
    if _active_websockets:  # only synthesize when someone can hear it
        try:
            from agent.voice import synthesize   # same voice as normal replies (ElevenLabs → edge-tts)
            audio_bytes = await synthesize(full_text)
        except Exception as e:
            logger.warning(f"TTS for reminder failed: {e}")

    # A real push first (agent/push.py): it shows on time even while the app sleeps; then the app connection
    # need not post the notification too.
    from agent import push
    pushed = await push.send(text, rid=rid or text[:24], alarm=is_alarm, stale_after=stale_after)

    # Send to all connected clients
    dead = set()
    icon = "🚨" if is_alarm else "🔔"
    for ws in _active_websockets:
        try:
            # Send transcript so UI shows the reminder
            await ws.send_json({
                "type":    "transcript",
                "role":    "sharp",
                "text":    f"{icon} {text}",
            })
            # The phone app turns this into a real Android notification (other clients ignore it).
            if not pushed:
                await ws.send_json({"type": "device_action", "action": "notify", "text": text,
                                    "id": rid or text[:24], "alarm": is_alarm, **({"stale_after": stale_after} if stale_after else {})})
                logger.info(f"Reminder pushed to an app client as a notification: {text[:40]!r}")
            # Send audio bytes so voice plays
            if audio_bytes:
                await ws.send_bytes(audio_bytes)
        except Exception:
            dead.add(ws)
    _active_websockets.difference_update(dead)


async def _send_telegram_text(text: str) -> None:
    token, chat_id = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.warning(f"Telegram notify: {e}")


async def notify(text: str, is_alarm: bool = False, title: str = "Enzo", telegram: bool = True, rid: str = "",
                 stale_after: int = 0) -> None:
    """
    Tell the user something proactively, wherever he is:
      • every open web app (text + Enzo's voice)
      • Telegram (if TELEGRAM_TOKEN + TELEGRAM_CHAT_ID are set)
      • a Windows notification on the PC
    """
    await _speak_reminder(text, is_alarm=is_alarm, rid=rid, stale_after=stale_after)
    # Telegram copies are off by default (the phone app shows a real notification); NOTIFY_TELEGRAM=on brings them back.
    if telegram and os.getenv("NOTIFY_TELEGRAM", "off").strip().lower() in ("on", "true", "1", "yes"):
        await _send_telegram_text(f"{'🚨' if is_alarm else '🔔'} {text}")
    try:
        await asyncio.get_running_loop().run_in_executor(None, _windows_toast, title, text[:200])
    except Exception as e:
        logger.debug(f"Toast: {e}")


async def run_scheduled_task(task: str, label: str = "") -> None:
    """Run a task the user scheduled earlier with the full agent, then report the result."""
    try:
        from agent.context import new_session
        from agent import pipeline
        from rules import enforce_rules
        s = new_session()          # no approval channel → risky actions are declined automatically
        s.device = "scheduler"
        prompt = (f"[Scheduled task the user set earlier — do it now and report the result in 1-3 short "
                  f"spoken sentences. Don't ask follow-up questions.] {task}")
        answer, _ = await pipeline.hybrid_agent(prompt, [{"role": "user", "content": prompt}])
        reply = enforce_rules(answer) if answer else f"I tried to {task}, but it didn't work, sir."
    except Exception as e:
        logger.error(f"Scheduled task failed ({task!r}): {e}")
        reply = f"I couldn't complete the scheduled task '{task}', sir."
    logger.info(f"🤖 Scheduled task result: {reply[:150]}")
    await notify(reply, title=f"Enzo — {label[:40] or 'Task'}")


async def _send_voice_alert(text: str, is_alarm: bool = False):
    """Alias for backward compatibility with heartbeat.py."""
    await _speak_reminder(text, is_alarm=is_alarm)


# ────────────────────────────────────────────────────────────────
# WINDOWS TOAST NOTIFICATION
# ────────────────────────────────────────────────────────────────
def _windows_toast(title: str, message: str):
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(8000, '{title}', '{message}', "
            "[System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Seconds 9; $n.Dispose()"
        )
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", ps], creationflags=flags)
    except Exception as e:
        logger.debug(f"Toast: {e}")


# ────────────────────────────────────────────────────────────────
# SCHEDULER LOOP
# ────────────────────────────────────────────────────────────────
async def scheduler_loop():
    logger.info("⏰ Scheduler started.")
    while True:
        try:
            for reminder in reminder_store.get_due():
                msg      = reminder.get("message", "Reminder")
                is_alarm = reminder.get("type") == "alarm"
                rid      = reminder["_id"]
                
                logger.info(f"🔔 Firing [{rid}]: {msg}")

                meta = reminder.get("metadata") or {}
                task = meta.get("task") or meta.get("command")
                if task:
                    # Scheduled TASK: Enzo does the work now and reports the result.
                    logger.info(f"🚀 Running scheduled task: {task}")
                    asyncio.create_task(run_scheduled_task(task, reminder.get("label") or msg))
                else:
                    # Plain reminder: say it everywhere (web voice, Telegram, toast).
                    await notify(msg, is_alarm=is_alarm, rid=str(rid),
                                 title="Enzo Alarm" if is_alarm else "Enzo Reminder")

                # 4. Update Database Status
                reminder_store.mark_fired(rid)

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ────────────────────────────────────────────────────────────────
# PARSER — converts intent detector output → ReminderStore.add() kwargs
# ────────────────────────────────────────────────────────────────
def parse_reminder_from_plan(inp: dict) -> dict | None:
    """
    Maps intent detector / planner dict to ReminderStore.add() keyword args.
    Key mapping: "type" → "reminder_type"
    """
    if not isinstance(inp, dict):
        return None

    message          = inp.get("message", "")
    reminder_type    = inp.get("type", "once")
    label            = inp.get("label", message[:30])
    interval_seconds = int(inp.get("interval_seconds", 0))
    fire_at          = None

    fire_at_str = inp.get("fire_at", "")
    if fire_at_str:
        try:
            naive = datetime.fromisoformat(str(fire_at_str))
            if naive.tzinfo is None:
                aware = ISRAEL_TZ.localize(naive)
            else:
                aware = naive
            fire_at = aware.astimezone(timezone.utc)
        except Exception as e:
            logger.warning(f"fire_at parse: {e}")

    if not message:
        return None

    return {
        "message":          message,
        "reminder_type":    reminder_type,
        "fire_at":          fire_at,
        "interval_seconds": interval_seconds,
        "label":            label,
        "metadata":         {"command": inp.get("command")} if inp.get("command") else None, # Added this
    }