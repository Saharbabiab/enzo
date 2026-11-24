"""
agent/backup.py - nightly backup of data/ (T-28).

Once a night (BACKUP_HOUR, default 02:00, before the nightly review changes notes) the proactive loop zips
data/ (lessons, contacts, Spotify/Google tokens, sessions, traces...) plus the memory facts from Mongo into
BACKUP_DIR/enzo-data-YYYY-MM-DD.zip and keeps the newest BACKUP_KEEP (default 14). The embedding model
(data/models) is skipped: it is re-downloaded on its own. On the NAS BACKUP_DIR is ./backups next to the stack
(docker-compose.yml), outside data/. BACKUP_DIR=off turns it off.
"""

from __future__ import annotations

import asyncio
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from loguru import logger

from agent import lessons

DATA = lessons.DATA
DIR = os.getenv("BACKUP_DIR", str(Path(__file__).resolve().parent.parent / "backups"))
HOUR = int(os.getenv("BACKUP_HOUR", "2"))
KEEP = int(os.getenv("BACKUP_KEEP", "14"))
SKIP = {"models"}


def _facts_json() -> str:
    from agent import facts
    docs = [{k: v for k, v in f.items() if k not in ("_vec", "emb")} for f in facts._all()]
    return json.dumps(docs, ensure_ascii=False, default=str, indent=1)


def make(now: datetime, dest: Path | None = None) -> Path | None:
    """Write today's zip (once a day), then drop the oldest beyond KEEP. Returns the new zip, or None if it existed."""
    dest = Path(dest or DIR)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"enzo-data-{now:%Y-%m-%d}.zip"
    if out.exists():
        return None
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(DATA.rglob("*")):
            rel = p.relative_to(DATA)
            if p.is_file() and rel.parts[0] not in SKIP and dest.resolve() not in p.resolve().parents:
                try:
                    z.write(p, Path("data") / rel)
                except OSError as e:          # a file being written right now: back up the rest
                    logger.warning(f"Backup skipped {rel}: {e}")
        try:
            z.writestr("mongo_facts.json", _facts_json())
        except Exception as e:
            logger.warning(f"Backup without memory facts: {e}")
    tmp.replace(out)
    for old in sorted(dest.glob("enzo-data-*.zip"))[:-KEEP]:
        old.unlink()
    return out


async def tick(now: datetime) -> None:
    if DIR.lower() == "off" or now.hour < HOUR:
        return
    out = await asyncio.to_thread(make, now)
    if out:
        logger.info(f"Backup written: {out} ({out.stat().st_size // 1024} KB)")
