"""
agent/instant.py - the only answers Enzo gives without the model: the time and the date.

These are zero-risk and worth answering instantly by voice. Everything else, greetings included, is the model's job.
If a sentence is anything more than a plain "what time is it" / "what's the date" (another city, a reminder, a
second request), this returns None and the model handles it.
"""

import re
from datetime import datetime

import pytz

ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")

_LEAD = r"^(?:hey |hi |ok |okay )?(?:(?:enzo|sharp)[, ]+)?(?:please |can you |could you )?(?:tell me )?"
_TAIL = r"(?: now| right now| please| enzo| sharp)*[\s?.!]*$"
_TIME_RE = re.compile(_LEAD + r"(?:what(?:'s| is)? the time|what time is it|the time)" + _TAIL, re.I)
_DATE_RE = re.compile(_LEAD + r"(?:what(?:'s| is)? (?:the |today'?s )date(?: today)?|what day is (?:it|today)|today'?s date)" + _TAIL, re.I)
_HE_TIME_RE = re.compile(r"^(?:שארפ[, ]+)?(?:מה השעה(?: עכשיו)?|כמה השעה|תגיד לי את השעה)[\s?.!]*$")
_HE_DATE_RE = re.compile(r"^(?:שארפ[, ]+)?(?:מה התאריך(?: היום)?|איזה יום היום|מה היום בשבוע|איזה תאריך היום)[\s?.!]*$")


def _now():
    return datetime.now(ISRAEL_TZ)


def clock_answer(text: str) -> str | None:
    t = (text or "").strip()
    if _TIME_RE.match(t) or _HE_TIME_RE.match(t):
        return f"It's {_now().strftime('%H:%M')}, sir."
    if _DATE_RE.match(t) or _HE_DATE_RE.match(t):
        return f"Today is {_now().strftime('%A, %B %d, %Y').replace(' 0', ' ')}, sir."
    return None
