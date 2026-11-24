"""
agent/lang.py — Hebrew / English helpers.

Enzo answers in the language of the user's message, and can speak mixed text
("You have ארוחת ערב עם מאיה at eight") by voicing Hebrew parts with a Hebrew
voice and English parts with an English one.
"""

from __future__ import annotations

import re

_HEB = r"֐-׿יִ-ﭏ"
_HEB_CHAR = re.compile(f"[{_HEB}]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")
# A run of Hebrew words, allowing spaces / digits / Hebrew punctuation between them
_HEB_RUN = re.compile(f"[{_HEB}](?:[{_HEB}0-9\\s\"'׳״\\-–:,.!?]*[{_HEB}])?")
_HEB_WORD = re.compile(f"[{_HEB}]+")
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]")


def detect_lang(text: str) -> str:
    """'he' if the message is mainly Hebrew (by words; ties go to the first word's language)."""
    t = text or ""
    heb, lat = len(_HEB_WORD.findall(t)), len(_LATIN_WORD.findall(t))
    if heb != lat:
        return "he" if heb > lat else "en"
    first = re.search(f"[{_HEB}A-Za-z]", t)
    return "he" if first and _HEB_CHAR.match(first.group(0)) else "en"


def has_hebrew(text: str) -> bool:
    return bool(_HEB_CHAR.search(text or ""))


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text or "")


def split_lang_segments(text: str) -> list[tuple[str, str]]:
    """
    'You have ארוחת ערב at 8.' → [('en', 'You have'), ('he', 'ארוחת ערב'), ('en', 'at 8.')]
    Punctuation/whitespace-only pieces are dropped.
    """
    segs: list[tuple[str, str]] = []
    pos = 0
    for m in _HEB_RUN.finditer(text or ""):
        before = text[pos:m.start()]
        if re.search(r"[A-Za-z0-9]", before):
            segs.append(("en", before.strip()))
        segs.append(("he", m.group(0).strip()))
        pos = m.end()
    rest = (text or "")[pos:]
    if re.search(r"[A-Za-z0-9]", rest):
        segs.append(("en", rest.strip()))
    merged: list[tuple[str, str]] = []
    for lang, s in segs:
        if not s:
            continue
        if merged and merged[-1][0] == lang:
            merged[-1] = (lang, f"{merged[-1][1]} {s}")
        else:
            merged.append((lang, s))
    return merged


def language_instruction(lang: str) -> str:
    """Line added to the system prompt for the current message."""
    base = ("LANGUAGE: Think and reply in ENGLISH only, and call him \"sir\" (at most once). "
            "When data is in Hebrew (calendar events, emails, places), translate it into natural English "
            "by meaning and context — who the people are to him, what the event is — and write names in "
            "English letters (e.g. דנה → Dana, הברזל → HaBarzel). Don't output Hebrew script unless he "
            "asks for the exact original. No emojis.")
    if lang == "he":
        return base + " The user wrote in Hebrew: understand it fully, but still answer in English."
    return base
