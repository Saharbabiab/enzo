"""
rules.py — Post-processing for S.H.A.R.P. responses.
"""

import re
import random

try:
    from shared import SIR_FREQUENCY
except ImportError:
    SIR_FREQUENCY = 2

_MAX_SHORT   = 240
_MAX_FACTUAL = 440
_MAX_S_SHORT = 2
_MAX_S_LONG  = 4

_BANNED = [
    # AI disclaimers
    r"as an ai\b", r"i am an ai\b", r"i'?m an ai\b",
    r"i am a language model", r"i'?m a language model",
    r"as a language model", r"i am programmed",
    r"i cannot provide", r"i'?m not able to",
    r"i don'?t have the ability", r"i'?m unable to",
    r"please note that", r"it'?s important to note",
    r"i should mention", r"i would like to clarify",
    r"certainly[,!]?\s*", r"of course[,!]?\s*",
    r"absolutely[,!]?\s*", r"great question[,!]?\s*",
    r"i understand[,.]?\s*",
    # Rude / dismissive phrases — NEVER allowed
    r"you are interrupting",
    r"interrupting my",
    r"i am busy",
    r"i'm busy",
    r"do not disturb",
    r"stop bothering",
    r"leave me alone",
    r"processing cycle",
]
_BANNED_RE = re.compile("|".join(_BANNED), re.IGNORECASE)


def enforce_rules(text: str) -> str:
    if not text or not text.strip():
        return "Ready, sir."

    from agent.lang import strip_emoji
    text = strip_emoji(text).strip()     # no emojis — he asked for that
    text = re.sub(r"[ \t]+", " ", text)  # DO NOT squash newlines!

    # Strip markdown
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*",     r"\1", text)
    text = re.sub(r"`(.+?)`",       r"\1", text)
    text = re.sub(r"^#+\s+",        "",    text)

    # Remove banned phrases
    text = _BANNED_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"^[,.\s]+", "", text)

    if not text.strip():
        return "Ready, sir."

    # The system prompt controls verbosity. These bounds just stop runaway rambling.
    max_s = 25
    max_l = 2500

    sentences = re.split(r"(?<=[.!?]) +", text)
    text      = " ".join(sentences[:max_s])

    if len(text) > max_l:
        text = text[:max_l].rsplit(" ", 1)[0] + "..."

    if text and text[-1] not in ".!?,":
        text += "."


    # "sir/boss" — 1 in 2 chance (was 1 in 4, gemma4 needs more guidance)
    lower = text.lower()
    if "sir" not in lower and "boss" not in lower:
        if random.randint(1, SIR_FREQUENCY) == 1:
            honorific = "sir"
            text = text[:-1] + f", {honorific}." if text.endswith(".") else text + f" {honorific.capitalize()}."

    return text.strip()
