"""
agent/profile_setup.py - Enzo gets to know the user (and the people in his life).

The model does the talking (one question at a time, in its own words). This module gives it:
  * a list of topics with what is already known and what is missing  (setup_status tool),
  * a way to save a PERSON from the phone's contact picker             (pick_contact tool + app),
  * the saved people, so "call mom" / "message my girlfriend" resolve to real numbers.
Nothing here decides what the user meant; it only stores and reports.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from loguru import logger

DATA = Path(os.getenv("ENZO_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
PEOPLE_FILE = DATA / "people.json"
STATE_FILE = DATA / "setup_state.json"

# role -> words people use for it (English + Hebrew). These become aliases for the saved number.
ROLES: dict[str, list[str]] = {
    "mother": ["mom", "mum", "mother", "mommy", "אמא", "אימא", "אמי"],
    "father": ["dad", "father", "daddy", "אבא", "אבי"],
    "partner": ["girlfriend", "boyfriend", "partner", "wife", "husband", "בת זוג", "בן זוג", "אשתי", "בעלי"],
    "brother": ["brother", "אח", "אחי"],
    "sister": ["sister", "אחות", "אחותי"],
    "best friend": ["best friend", "bestie", "חבר הכי טוב", "חברה הכי טובה"],
    "boss": ["boss", "manager", "בוס", "מנהל"],
}

# Relations Enzo may already have in memory for a role (so it can say "Please choose Dana" instead of asking who).
KNOWN_AS: dict[str, set[str]] = {
    "mother": {"mother", "mom", "mum"}, "father": {"father", "dad"},
    "partner": {"girlfriend", "boyfriend", "wife", "husband", "partner", "fiancee", "fiance"},
    "best friend": {"best friend"}, "brother": {"brother"}, "sister": {"sister"},
}


def known_name(role: str) -> str:
    """A person already in memory with this role, else ''."""
    try:
        for name, data in (_mem_read().get("people") or {}).items():
            rel = (data.get("relation_to_user") if isinstance(data, dict) else str(data) or "").lower().strip()
            if rel in KNOWN_AS.get(role, set()):
                return str(name)
    except Exception:
        pass
    return ""


# What Enzo would like to know (the people first: that is what makes "call mom" work). kind "person" = pick from contacts; "fact" = the user says it, Enzo calls remember().
TOPICS = [
    {"id": "mother", "label": "his mother", "kind": "person", "role": "mother"},
    {"id": "father", "label": "his father", "kind": "person", "role": "father"},
    {"id": "partner", "label": "his partner (girlfriend / wife)", "kind": "person", "role": "partner"},
    {"id": "best friend", "label": "his best friend", "kind": "person", "role": "best friend"},
    {"id": "name", "label": "what to call him (name or nickname)", "question": "What should I call you, sir? A name or a nickname.", "kind": "fact", "category": "owner", "keys": ["name", "nickname", "call_me"]},
    {"id": "home", "label": "where he lives (city and area)", "question": "Which city and area do you live in?", "kind": "fact", "category": "owner", "keys": ["home", "city", "location", "lives_in"]},
    {"id": "work", "label": "what he does: work or study, and where", "question": "What do you do, work or study, and where?", "kind": "fact", "category": "owner", "keys": ["job", "work", "study", "occupation"]},
    {"id": "wake", "label": "his usual wake-up and bedtime", "question": "What time do you usually wake up, and when do you go to bed?", "kind": "fact", "category": "routines", "keys": ["wake_time", "wake", "wake_up", "sleep_time", "bedtime"]},
    {"id": "music", "label": "the music he likes", "question": "What music do you like? Artists or styles.", "kind": "fact", "category": "preferences", "keys": ["music", "favorite_music", "favorite_artist", "favorite_artists", "genres"]},
    {"id": "news", "label": "the news and topics he cares about", "question": "Which news and topics do you care about?", "kind": "fact", "category": "preferences", "keys": ["news", "news_topics", "interests", "hobbies"]},
]


# ── storage ────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _mem_read() -> dict:
    from shared import memory_store
    return memory_store.read() or {}


def _mem_write(update: dict) -> None:
    from shared import memory_store
    memory_store.write(update)


def people() -> dict:
    return _load(PEOPLE_FILE)


def role_of(word: str) -> str:
    """The role a spoken word stands for ("mom" -> "mother"), or ""."""
    w = re.sub(r"[^\w ]", "", (word or "").lower()).strip()
    for role, words in ROLES.items():
        if w == role or w in [x.lower() for x in words]:
            return role
    return ""


def normalize_role(text: str) -> str:
    return role_of(text) or re.sub(r"\s+", " ", (text or "").strip().lower())[:30]


def set_person(role: str, name: str, number: str) -> str:
    """Remember who this person is: their number answers to the role words AND their name from now on."""
    from agent import tools
    role = normalize_role(role)
    digits = re.sub(r"[^\d+]", "", number or "")
    if not role or len(digits) < 6:
        return "[ERROR]: I need the person's role (like mother) and a phone number."
    data = people()
    data[role] = {"name": (name or "").strip(), "number": digits, "saved": int(time.time())}
    _save(PEOPLE_FILE, data)
    for word in ROLES.get(role, [role]) + ([name.strip()] if name and name.strip() else []):
        tools._save_alias(word, digits)
    try:                                              # long-term memory: "Rina is his mother"
        if name.strip():
            _mem_write({"people": {re.sub(r"[^\w \-]", "", name).strip()[:40]: {"relation_to_user": role}}})
            from agent import facts
            facts.add(f"{name.strip()} is the user's {role}", "contacts", "trusted", 0.8, core=True)
    except Exception as e:
        logger.debug(f"profile_setup memory write: {e}")
    return f"[OK]: Saved {name or 'the contact'} as {role}."


def handle_pick(data: dict) -> str:
    """The app's answer after the contact picker: {"role", "name", "number", "cancelled"}. Returns what Enzo says."""
    role = normalize_role(str(data.get("role") or ""))
    if data.get("cancelled") or not data.get("number"):
        return "No problem, sir. We can do that another time."
    out = set_person(role, str(data.get("name") or ""), str(data.get("number") or ""))
    if out.startswith("[ERROR]"):
        return "I could not save that contact, sir."
    return f"Done, sir. {data.get('name') or 'That contact'} is now your {role}. Just say \"call {ROLES.get(role, [role])[0]}\" any time."


# ── what is known / missing ────────────────────────────────────────────

def _fact_known(topic: dict, mem: dict) -> bool:
    cat = mem.get(topic["category"], {}) or {}
    return any(k in cat and str(cat[k]).strip() for k in topic["keys"])


def status() -> dict:
    """{"known": [labels], "missing": [topic dicts], "skipped": [...]}"""
    mem, ppl, st = _mem_read(), people(), _load(STATE_FILE)
    skipped = set(st.get("skipped", []))
    known, missing = [], []
    for t in TOPICS:
        ok = (t["role"] in ppl) if t["kind"] == "person" else _fact_known(t, mem)
        (known if ok else missing).append(t)
    return {"known": known, "missing": [t for t in missing if t["id"] not in skipped],
            "skipped": [t for t in missing if t["id"] in skipped]}


def skip(topic_id: str) -> str:
    st = _load(STATE_FILE)
    ids = set(st.get("skipped", []))
    ids.add(normalize_role(topic_id))
    st["skipped"] = sorted(ids)
    _save(STATE_FILE, st)
    return f"[OK]: Skipping {topic_id}; I will not ask about it again."


def report() -> str:
    """Text for the setup_status tool: what the model should do next."""
    s = status()
    lines = ["KNOWN: " + (", ".join(t["label"] for t in s["known"]) or "nothing yet")]
    if not s["missing"]:
        lines.append("MISSING: nothing. The profile is complete; just say so.")
        return "\n".join(lines)
    lines.append("MISSING (go through ONE at a time, in your own words, short and friendly; do not read this list out):")
    for t in s["missing"]:
        how = (f'call pick_contact(role="{t["role"]}") RIGHT AWAY: the phone opens his contacts and he taps the person. Say only "Please choose {t["label"]} from your contacts." Do NOT ask for a name or number in words'
               if t["kind"] == "person" else f'ask one short question; when he answers, call remember(fact="<his answer as one sentence about him>", category="{t["category"]}", key="{t["keys"][0]}")')
        lines.append(f'- {t["label"]}: {how}')
    lines.append('If he does not want to answer one, call setup_skip with its topic and move on. After each answer, thank him briefly and go to the next topic. If he names a person in words instead of choosing, still call pick_contact (or save_person only if he also dictates the number).')
    return "\n".join(lines)


# ── a guided setup: the code opens the contact picker (the model only talks) ─────

def next_step() -> tuple[str, dict | None]:
    """(what Enzo says, device action or None) for the next missing topic."""
    missing = status()["missing"]
    if not missing:
        return "That is everything I wanted to know for now, sir. Thank you, it will make me much more useful.", None
    t = missing[0]
    if t["kind"] == "person":
        who = re.sub(r"\s*\(.*?\)", "", t["label"]).replace("his ", "your ")
        name = known_name(t["role"])
        if name:                                              # "Please choose Dana, your partner, from your contacts."
            return (f"Please choose {name}, {who}, from your contacts.",
                    {"action": "pick_contact", "role": t["role"], "title": f"Choose {name} ({who.replace('your ', '')})"})
        return f"Please choose {who} from your contacts.", {"action": "pick_contact", "role": t["role"], "title": f"Choose {who}"}
    return t.get("question") or f"Tell me about {t['label']}.", None


async def send_later(sess, action: dict, delay: float = 3.5) -> None:
    """Open the contact picker AFTER Enzo has had time to say what to pick (the picker covers the screen)."""
    import asyncio
    await asyncio.sleep(delay)
    try:
        await sess.send_device(action)
    except Exception as e:
        logger.debug(f"profile_setup.send_later: {e}")


def restart() -> str:
    """Forget the skips so every topic that is still unknown is asked again."""
    st = _load(STATE_FILE)
    st["skipped"] = []
    _save(STATE_FILE, st)
    return "[OK]: Starting over: every topic I do not know yet will be asked again."


def start_setup() -> tuple[str, dict | None]:
    """The setup_status tool calls this: report for the model, plus the first contact picker when the first topic is a person."""
    text, action = next_step()
    return text, action


# ── a gentle once-a-day offer ──────────────────────────────────────────

def hint() -> str:
    """Added to the system prompt (once per day) while the profile is incomplete. Marks itself as shown."""
    if os.getenv("SETUP_OFFERS", "on").strip().lower() in ("off", "false", "0"):
        return ""
    try:
        s = status()
        if not s["missing"]:
            return ""
        # Always (while something is missing): how to run a setup. Never from memory: the tool says what to ask.
        standing = ("\n=== GETTING TO KNOW THE USER ===\n"
                    "If he says \"let's do a setup\", \"setup\", \"get to know me\", or accepts your offer, you MUST call the setup_status tool in this very reply (do not just promise it). "
                    "Never ask the setup questions from memory, and never ask for a person's name or number in words: "
                    "setup_status tells you what to ask and which tool to use (people are chosen from his phone contacts with pick_contact).\n")
        st = _load(STATE_FILE)
        if time.time() - float(st.get("last_offer", 0)) < 20 * 3600:
            return standing
        st["last_offer"] = time.time()
        _save(STATE_FILE, st)
        return standing + ("Once today, at ONE calm moment (never in the middle of a task), offer: "
                           "\"Sir, may I ask a few quick questions so I can serve you better?\" If he declines, drop it.\n")
    except Exception as e:
        logger.debug(f"profile_setup.hint: {e}")
        return ""
