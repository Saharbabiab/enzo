"""
heartbeat.py — S.H.A.R.P. background intelligence loop.

Runs every HEARTBEAT_INTERVAL seconds:
  1. Health checks (disk, RAM)
  2. Daily morning briefing at 8:00 AM Israel time
  3. Reasoning cycle — reads past improvement logs and derives a live rule
     that gets injected into future responses, making the bot smarter over time.

WebSocket registration is handled by scheduler.py.
"""

import os
import asyncio
from datetime import datetime

import pytz
from loguru import logger
from dotenv import load_dotenv

from shared import ollama_client, OLLAMA_MODEL, OLLAMA_OPTIONS, memory_store, get_time_context
from shell_executor import execute_shell, IS_WINDOWS
from scheduler import _send_voice_alert, _active_websockets, reminder_store

# Re-export for server.py backward compat
from scheduler import register_websocket, unregister_websocket

load_dotenv()

HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "1800"))  # 30 min
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ISRAEL_TZ          = pytz.timezone("Asia/Jerusalem")


# ────────────────────────────────────────────────────────────────
# TELEGRAM
# ────────────────────────────────────────────────────────────────
async def _send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import httpx
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       f"🤖 Enzo:\n\n{text}",
                "parse_mode": "Markdown",
            })
    except Exception as e:
        logger.warning(f"Telegram: {e}")


# ────────────────────────────────────────────────────────────────
# HEALTH CHECKS
# ────────────────────────────────────────────────────────────────
async def _run_health_checks() -> list[str]:
    alerts = []

    if not IS_WINDOWS:  # NAS / Linux: read this machine directly (agent/sysinfo.py)
        from agent import sysinfo
        return [f"⚠️ {a}" for a in await asyncio.to_thread(sysinfo.health_alerts)]

    # Disk > 90%
    result = await asyncio.to_thread(
        execute_shell,
        'powershell -command "Get-PSDrive -PSProvider FileSystem | '
        "ForEach-Object { $used = ($_.Used / ($_.Used + $_.Free)) * 100; "
        "if ($used -gt 90) { Write-Host ('ALERT: Drive ' + $_.Name + ': ' + [math]::Round($used,1) + '% used') } }\"",
    )
    if result.get("success") and result.get("output", "").strip():
        for line in result["output"].splitlines():
            if "ALERT" in line:
                alerts.append(f"⚠️ {line.strip()}")

    # RAM < 1GB free
    result = await asyncio.to_thread(
        execute_shell,
        'powershell -command "$os = Get-CimInstance Win32_OperatingSystem; '
        "$free = [math]::Round($os.FreePhysicalMemory/1MB,2); "
        "if ($free -lt 1) { Write-Host ('LOW RAM: ' + $free + ' GB free') }\"",
    )
    if result.get("success") and result.get("output", "").strip():
        alerts.append(f"⚠️ {result['output'].strip()}")

    return alerts


# ────────────────────────────────────────────────────────────────
# DAILY BRIEFING
# ────────────────────────────────────────────────────────────────
async def _send_daily_briefing():
    mem         = memory_store.read()
    owner       = mem.get("owner", {})
    user_name   = owner.get("name") or "sir"
    time_ctx    = get_time_context()

    if IS_WINDOWS:
        disk_result = await asyncio.to_thread(
            execute_shell,
            'powershell -command "Get-PSDrive -PSProvider FileSystem | '
            "ForEach-Object { 'Drive ' + $_.Name + ': ' + [math]::Round($_.Free/1GB,1) + ' GB free' }\"",
        )
        disk_info = disk_result.get("output", "N/A")[:300]
    else:
        from agent import sysinfo
        disk_info = sysinfo.disk_sentence() or "N/A"

    active_count = len(reminder_store.list_active())
    reminder_note = f"{active_count} active reminder(s)." if active_count else "No active reminders."

    try:
        from agent.agent_loop import background_chat      # same start as the agent: keeps its prompt cache
        resp = await asyncio.to_thread(background_chat,
            f"{time_ctx}\n\nGenerate a short morning briefing for {user_name}. "
            "Include disk status and reminder count. "
            f"Disk: {disk_info}\n{reminder_note}\n"
            "Keep it under 3 sentences. J.A.R.V.I.S. style.", num_predict=120)
        briefing = resp["message"]["content"].strip()
        await _send_telegram(briefing)
        await _send_voice_alert(briefing)
        logger.info(f"📋 Daily briefing: {briefing[:80]}")
    except Exception as e:
        logger.error(f"Briefing: {e}")


# ────────────────────────────────────────────────────────────────
# REASONING HEARTBEAT — makes the bot smarter over time
# ────────────────────────────────────────────────────────────────
async def _reasoning_heartbeat():
    """
    Reads the last 5 improvement notes logged by reflect_and_improve().
    Asks the model to derive ONE actionable rule from them.
    Saves it as memory['live_rules']['current'] — injected into every future system prompt.
    """
    improvements = memory_store.get_improvements(limit=5)
    if not improvements:
        logger.debug("💭 No improvement logs yet — skipping reasoning heartbeat.")
        return

    notes_text = "\n".join(f"- {n}" for n in improvements if n)
    if not notes_text.strip():
        return

    prompt = (
        "You are reviewing past performance notes for an AI assistant called Enzo\n\n"
        "PAST NOTES:\n"
        f"{notes_text}\n\n"
        "Based on these notes, write ONE specific improvement rule that Enzo should apply "
        "in all future responses. Make it concrete and actionable — one sentence.\n"
        "If there's nothing useful, output: NONE\n\n"
        "Rule:"
    )

    try:
        from agent.agent_loop import background_chat      # same start as the agent: keeps its prompt cache
        resp = await asyncio.to_thread(background_chat, prompt, num_predict=60)
        rule = resp["message"]["content"].strip()
        if rule and rule.upper() != "NONE" and len(rule) > 10:
            memory_store.write({"live_rules": {"current": rule}})
            logger.info(f"💭 Live rule updated: {rule[:80]}")
    except Exception as e:
        logger.warning(f"Reasoning heartbeat: {e}")


# ────────────────────────────────────────────────────────────────
# MAIN HEARTBEAT LOOP
# ────────────────────────────────────────────────────────────────
_last_briefing_date = None


async def heartbeat_loop():
    logger.info(f"💓 Heartbeat started (interval: {HEARTBEAT_INTERVAL}s)")
    await asyncio.sleep(30)  # startup delay

    global _last_briefing_date

    while True:
        try:
            logger.info("💓 Health check...")
            now    = datetime.now(ISRAEL_TZ)
            alerts = await _run_health_checks()

            if alerts:
                text = "\n".join(alerts)
                logger.warning(f"💓 Issues: {text}")
                await _send_telegram(text)
                await _send_voice_alert(f"Heads up — {alerts[0]}")
            else:
                logger.info("💓 All systems nominal.")

            # Daily briefing at 8:00 AM Israel time
            today = now.date()
            if now.hour == 8 and _last_briefing_date != today:
                _last_briefing_date = today
                await _send_daily_briefing()

            # Reasoning — makes bot smarter over time
            await _reasoning_heartbeat()

        except Exception as e:
            logger.error(f"💓 Heartbeat error: {e}")

        await asyncio.sleep(HEARTBEAT_INTERVAL)