"""
agent/health.py - how the server (the NAS) and Enzo's own parts are doing. Feeds the system_status tool.
Everything is read live; a part that cannot be checked says so instead of guessing.
"""

from __future__ import annotations

import os
import time

import httpx
from loguru import logger


def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except Exception:
        return ""
    d, rem = divmod(secs, 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d} days {h} h" if d else f"{h} h {m} min"


def _ollama() -> str:
    host = os.getenv("OLLAMA_HOST", "").rstrip("/")
    if not host:
        return "local model: not configured"
    try:
        ps = httpx.get(host + "/api/ps", timeout=4).json().get("models", [])
        if not ps:
            return "local model server (Ollama): reachable, no model loaded right now (it loads on the next question)"
        m = ps[0]
        vram = m.get("size_vram", 0) / 1e9
        return (f"local model server (Ollama): reachable, {m['name']} loaded, {vram:.1f} GB on the GPU, "
                f"window {m.get('context_length', '?')} tokens")
    except Exception as e:
        return f"local model server (Ollama): NOT reachable ({e.__class__.__name__})"


def snapshot() -> str:
    from agent import sysinfo
    lines = []
    label = sysinfo.host_label()
    up = _uptime()
    lines.append(f"HOST: {label}" + (f", container up {up}" if up else ""))
    cpu = sysinfo.cpu_percent(0.4)
    if cpu is not None:
        lines.append(f"CPU: {cpu:.0f}% busy")
    r = sysinfo.ram()
    if r:
        lines.append(f"RAM: {r[0]:.1f} of {r[1]:.1f} GB used ({r[0] / r[1]:.0%})")
    for name, free, total in sysinfo.disks():
        lines.append(f"DISK {name}: {free:.0f} GB free of {total:.0f} GB ({100 * (1 - free / total):.0f}% used)")
    lines.append(_ollama())
    try:
        from scheduler import reminder_store
        lines.append("database (reminders): " + ("connected" if reminder_store._ok else "NOT connected"))
    except Exception:
        lines.append("database: unknown")
    try:
        from agent import google_services, spotify, stt, briefing
        lines.append("Google (calendar + mail): " + ("connected" if google_services.connected() else "not connected"))
        lines.append("Spotify: " + ("connected" if spotify.connected() else "not connected"))
        lines.append("speech recognition: Whisper " + os.getenv("STT_WHISPER_MODEL", "small") + (" loaded" if stt._whisper is not None else " (loads on first use)"))
        lines.append("morning briefing for today: " + ("ready" if briefing.ready_briefing() else "not prepared (made at 07:00, deleted at 12:00)"))
    except Exception as e:
        logger.debug(f"health: {e}")
    try:
        from agent import lessons
        recent = [f for f in lessons.failures(time.time() - 86400)]
        lines.append(f"tool failures in the last 24 h: {len(recent)}")
        from agent import nightly
        if nightly.stats_line():
            lines.append(nightly.stats_line())
    except Exception:
        pass
    try:
        from agent import big_brain
        lines.append(big_brain.budget_line())
    except Exception:
        pass
    alerts = sysinfo.health_alerts()
    lines.append("WARNINGS: " + ("; ".join(alerts) if alerts else "none"))
    return "\n".join(lines)


def problems() -> list[str]:
    """What is broken right now, in plain words, for the self-check (agent/proactive.py). Disk and RAM are the heartbeat's."""
    out = []
    if "NOT reachable" in _ollama():
        backup = os.getenv("MAIN_BRAIN", "claude").strip().lower() == "claude"   # then Ollama is only the fallback
        out.append("the local model server (Ollama) is not reachable" + (", so there is no backup if Claude fails" if backup else ""))
    try:
        from scheduler import reminder_store
        if not reminder_store._ok:
            out.append("the database is not connected, so reminders and memory are not saved")
    except Exception:
        pass
    return out
