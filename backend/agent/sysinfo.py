"""
sysinfo.py — read the machine Enzo itself runs on (no PowerShell, no extra packages).

On Windows the old PowerShell code in instant.py / heartbeat.py still runs. On Linux
(the NAS container) this module reads /proc and the mounted storage, so "how much
disk space / RAM / CPU" describes the NAS. In Docker /proc/meminfo and /proc/stat
show the host's numbers.

Env:
  SYSINFO_DISK_PATHS  comma list of paths to report (default "/"), e.g. "/storage"
                      — mount the pool read-only into the container (see docker-compose.yml)
  HOST_LABEL          how Enzo calls this machine in answers (default "the server")
"""

from __future__ import annotations

import os
import shutil
import time


def host_label() -> str:
    return os.getenv("HOST_LABEL", "the server").strip() or "the server"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def disks() -> list[tuple[str, float, float]]:
    """[(name, free_gb, total_gb)] for SYSINFO_DISK_PATHS."""
    out = []
    for raw in os.getenv("SYSINFO_DISK_PATHS", "/").split(","):
        path = raw.strip()
        if not path:
            continue
        try:
            u = shutil.disk_usage(path)
        except OSError:
            continue
        name = os.path.basename(path.rstrip("/")) or "system"
        out.append((name, u.free / 1e9, u.total / 1e9))
    return out


def ram() -> tuple[float, float] | None:
    """(used_gb, total_gb) from /proc/meminfo, or None when not on Linux."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])  # kB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info["MemFree"])
    except (OSError, KeyError, ValueError, IndexError):
        return None
    return (total - avail) / 1048576, total / 1048576


def _cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return idle, sum(vals)


def cpu_percent(sample_seconds: float = 0.5) -> float | None:
    a = _cpu_times()
    if a is None:
        return None
    time.sleep(sample_seconds)
    b = _cpu_times()
    if b is None or b[1] == a[1]:
        return None
    return 100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1]))


# ── Sentences (same style as instant.py) ────────────────────

def disk_sentence() -> str | None:
    parts = [f"{n}: {free:g} GB free of {total:g}" for n, free, total in
             ((n, round(f, 1), round(t, 1)) for n, f, t in disks())]
    return f"On {host_label()}, " + "; ".join(parts) + ", sir." if parts else None


def ram_sentence() -> str | None:
    r = ram()
    if not r:
        return None
    used, total = round(r[0], 1), round(r[1], 1)
    return f"{_cap(host_label())} is using {used:g} GB of {total:g} GB of RAM ({used / total:.0%}), sir."


def cpu_sentence() -> str | None:
    pct = cpu_percent()
    return None if pct is None else f"The CPU on {host_label()} is at {pct:.0f}% right now, sir."


def health_alerts() -> list[str]:
    """Heartbeat checks: any watched disk > 90% full, or < 1 GB RAM free."""
    alerts = []
    for name, free, total in disks():
        if total and (1 - free / total) > 0.90:
            alerts.append(f"ALERT: {host_label()} drive {name}: {100 * (1 - free / total):.1f}% used")
    r = ram()
    if r and r[1] - r[0] < 1:
        alerts.append(f"LOW RAM: {r[1] - r[0]:.2f} GB free on {host_label()}")
    return alerts
