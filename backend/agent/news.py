"""
agent/news.py - real headlines from RSS feeds (the morning briefing used a web search that often returned only list pages).

Default feeds: Times of Israel, BBC World, BBC Technology, The Verge. Change them with
BRIEFING_NEWS_FEEDS="Name|https://feed.url,Name2|https://feed2.url".
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import xml.etree.ElementTree as ET

import httpx
from loguru import logger

DEFAULT_FEEDS = [
    ("Times of Israel", "https://www.timesofisrael.com/feed/"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
]


def feeds() -> list[tuple[str, str]]:
    raw = os.getenv("BRIEFING_NEWS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    out = []
    for part in raw.split(","):
        if "|" in part:
            name, url = part.split("|", 1)
            if url.strip().startswith("http"):
                out.append((name.strip(), url.strip()))
    return out or DEFAULT_FEEDS


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _clean(s: str, n: int = 170) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


def parse_feed(xml_text: str, limit: int = 4) -> list[tuple[str, str]]:
    """[(title, short summary)] from an RSS 2.0 or Atom document, newest first as the feed lists them."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = summary = ""
        for ch in it:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "title":
                title = _clean(_text(ch), 200)
            elif t in ("description", "summary") and not summary:
                summary = _clean(_text(ch))
        if title:
            items.append((title, "" if summary.lower().startswith(title.lower()[:30]) else summary))
        if len(items) >= limit:
            break
    return items


async def _one(client: httpx.AsyncClient, name: str, url: str, limit: int) -> tuple[str, list[tuple[str, str]]]:
    try:
        r = await client.get(url)
        r.raise_for_status()
        return name, parse_feed(r.text, limit)
    except Exception as e:
        logger.debug(f"News feed {name}: {e.__class__.__name__}")
        return name, []


async def headlines(per_feed: int = 4) -> str:
    """Headlines from all feeds, grouped by source. Empty string if every feed failed."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Enzo news)"}) as client:
        got = await asyncio.gather(*[_one(client, n, u, per_feed) for n, u in feeds()])
    blocks = []
    for name, items in got:
        if items:
            blocks.append(f"[{name}]\n" + "\n".join(f"- {t}" + (f": {s}" if s else "") for t, s in items))
    return "\n\n".join(blocks)
