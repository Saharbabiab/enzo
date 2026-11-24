"""Container health: the web server answers /api/health; the Telegram bot container just needs its process alive."""
import os
import sys
import urllib.request


def running(fragment: str) -> bool:
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if fragment in f.read().decode(errors="ignore"):
                    return True
        except OSError:
            continue
    return False


if running("server.py"):
    try:
        urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=4)
    except Exception:
        sys.exit(1)
elif not running("telegram_bot.py"):
    sys.exit(1)
