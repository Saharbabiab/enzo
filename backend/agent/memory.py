"""
agent/memory.py — S.H.A.R.P. deep memory system.

Proactively extracts and saves facts about the user from every message.
Provides rich profile summaries injected into every system prompt.
"""

import re
import asyncio
import json
from loguru import logger

from shared import memory_store, ollama_client, OLLAMA_MODEL, OLLAMA_OPTIONS, _repair_json


# ────────────────────────────────────────────────────────────────
# PROFILE SUMMARY — injected into every system prompt
# ────────────────────────────────────────────────────────────────
_migrated = False


def get_profile_summary(query: str = "") -> str:
    """What Enzo knows about the user for this request: the core profile + the facts relevant to `query`
    (agent/facts.py). Goes in the per-turn context, never the stable prompt. The old whole-profile dump is
    only used when there are no facts at all (Mongo down)."""
    global _migrated
    try:
        from agent import facts
        if not _migrated:
            _migrated = True
            if not any(f.get("source") == "profile" for f in facts._all()):
                facts.migrate_from_profile(memory_store.read() or {})
        if facts.active():
            return facts.prompt_block(query)
    except Exception as e:
        logger.warning(f"facts recall failed, using the old profile: {e}")
    return old_profile_summary()


def old_profile_summary() -> str:
    """The old key-value profile (user_memory), as one block."""
    mem = memory_store.read()
    if not mem:
        return "No personal data yet."

    lines = []
    owner = mem.get("owner", {})
    if owner:
        lines.append("About the user: " + ", ".join(f"{k}={v}" for k, v in owner.items()))

    prefs = mem.get("preferences", {})
    if prefs:
        lines.append("Preferences: " + ", ".join(f"{k}={v}" for k, v in prefs.items()))

    people = mem.get("people", {})
    if people:
        person_parts = []
        for name, data in people.items():
            if isinstance(data, dict):
                rel = data.get("relation_to_user", "")
                heb = data.get("hebrew_name", "")
                label = f"{name} / {heb}" if heb else name   # "Dana / דנה" → calendar titles match
                person_parts.append(f"{label} ({rel})" if rel else label)
            else:
                person_parts.append(name)
        lines.append("People: " + ", ".join(person_parts))

    routines = mem.get("routines", {})
    if routines:
        lines.append("Routines: " + ", ".join(f"{k}={v}" for k, v in routines.items()))

    misc = mem.get("misc", {})
    if misc:
        lines.append("Misc: " + ", ".join(f"{k}={v}" for k, v in misc.items()))

    # Live rule from heartbeat reasoning (if any)
    live_rules = mem.get("live_rules", {})
    if live_rules:
        rule = live_rules.get("current", "")
        if rule:
            lines.append(f"[ACTIVE RULE]: {rule}")

    return "\n".join(lines) if lines else "No personal data yet."


# ────────────────────────────────────────────────────────────────
# LEARN FROM EVERY CONVERSATION — runs in the background after each reply
# ────────────────────────────────────────────────────────────────
_LEARN_SKIP_RE = re.compile(
    r"^(what time|what'?s the (time|date|weather)|open |launch |close |pause|play|next|skip|"
    r"volume|mute|remind me|cancel|yes|no|ok|thanks|thank you|hi|hey|hello|good morning)\b",
    re.IGNORECASE,
)

_LEARN_PROMPT = """You maintain a profile of the user. Read his message and extract ONLY durable,
personal information he states about HIMSELF (not questions, not one-off tasks).
Return JSON with exactly these keys:
{{"interests": [], "facts": {{}}, "people": {{}}}}
- interests: hobbies, topics, games, music, sports, shows he says he likes/does/is into (short lowercase nouns)
- facts: stable facts about him, key → value (e.g. "job": "student", "city": "Beer Sheva", "shoe_size": "45 EU")
- people: name → relation (e.g. "Maya": "sister")
If there is nothing durable, return {{"interests": [], "facts": {{}}, "people": {{}}}}.
Asking ABOUT something is not an interest ("what's the weather" → nothing).

Already known: {known}

Message: {text}"""


async def extract_profile_updates(text: str, known: str = "") -> tuple[list, dict, dict]:
    """LLM extraction only (no saving): (new_interests, facts, people)."""
    text = (text or "").strip()
    if len(text) < 12 or _LEARN_SKIP_RE.match(text):
        return [], {}, {}
    # Same start as the agent's own prompt (system + tools): a different prompt would push the agent's out of
    # Ollama's cache and the next request would pay ~30 s to read it again.
    from agent.agent_loop import shared_prefix, keep_prefix_cached
    prefix, tools = shared_prefix()
    await asyncio.to_thread(keep_prefix_cached)        # else this call wipes the agent's cache (see that function)
    prompt = _LEARN_PROMPT.format(known=known or "nothing yet", text=text[:600])
    try:
        resp = await asyncio.to_thread(
            ollama_client.chat, model=OLLAMA_MODEL, format="json", tools=tools,
            messages=prefix + [{"role": "user", "content": "(Not from the user: background note-taking. Reply with the JSON only.)\n" + prompt}],
            options={**OLLAMA_OPTIONS, "num_predict": 200, "temperature": 0.1},
        )
        answer = resp["message"]["content"]
    except Exception as e:                                # Ollama stopped: Claude Haiku takes the notes
        from agent import big_brain
        logger.debug(f"learn: local model off ({e.__class__.__name__}): Claude Haiku")
        answer = await asyncio.to_thread(big_brain.side_job, prompt + "\nReply with the JSON only.", 400)
    data = _repair_json(answer) or {}
    interests = [str(i).strip().lower()[:40] for i in data.get("interests", []) if str(i).strip()]
    facts = {re.sub(r"[^\w]", "_", str(k)).lower()[:30]: str(v)[:120]
             for k, v in (data.get("facts") or {}).items() if str(v).strip()}
    people = {str(n).strip()[:40]: {"relation_to_user": str(r)[:60]}
              for n, r in (data.get("people") or {}).items() if str(n).strip() and str(r).strip()}
    known_low = (known or "").lower()
    return [i for i in interests if i not in known_low][:5], facts, people


# (new fact id, [near-duplicate ids]) saved during a reply; checked after it, when the GPU is free.
pending_checks: list[tuple[str, list[str]]] = []


async def learn_from_turn(user_text: str) -> None:
    """After the reply: settle the near-duplicates remember() found, then pick up new interests / facts / people
    from what the user said. They go to the facts store as "auto" (and, as before, to the old profile)."""
    from agent import facts as store
    try:
        new_interests, facts, people = await extract_profile_updates(user_text, get_profile_summary(user_text)[:800])
        if new_interests:
            memory_store.add_to_list("preferences.hobbies", new_interests)
        if facts:
            memory_store.write({"owner": facts})
        if people:
            memory_store.write({"people": people})
        found = ([f"The user is into {i}" for i in new_interests] + [f"The user's {k.replace('_', ' ')}: {v}" for k, v in facts.items()]
                 + [f"{n} is the user's {d['relation_to_user']}" for n, d in people.items()])
        for text in found:
            doc, near = await asyncio.to_thread(store.add, text, "extracted", "auto", 0.5)
            if near:
                pending_checks.append((doc["_id"], [f["_id"] for f in near]))
        if found:
            logger.info(f"🧠 Learned: {found}")
    except Exception as e:
        logger.debug(f"learn_from_turn: {e}")
    while pending_checks:
        new_id, near_ids = pending_checks.pop(0)
        try:
            await resolve_near(new_id, near_ids)
        except Exception as e:
            logger.debug(f"resolve_near: {e}")


_RESOLVE_PROMPT = """A new fact about the user was just saved, and some saved facts may be about the same thing.
NEW: {new}
SAVED:
{old}
Decide ONE:
- "add": the new fact is different information, keep both
- "update": the new fact adds to or corrects one saved fact; give that id and one merged sentence
- "supersede": one saved fact is no longer true because of the new one; give that id
- "ignore": the new fact says nothing the saved ones do not already say
JSON only: {{"action": "add|update|supersede|ignore", "id": "<saved id or empty>", "text": "<merged sentence for update>"}}"""


async def resolve_near(new_id: str, near_ids: list[str]) -> str:
    """The local model decides add / update / supersede / ignore for a new fact and its near-duplicates.
    Nothing is deleted: the loser is marked superseded_by, so it can be restored."""
    from agent import facts as store
    from agent.agent_loop import side_text
    new = store.get(new_id)
    old = [f for f in (store.get(i) for i in near_ids) if f and not f.get("superseded_by")]
    if not new or new.get("superseded_by") or not old:
        return "add"
    listing = "\n".join(f'{f["_id"]}: {f["text"]}' for f in old)
    data = _repair_json(await asyncio.to_thread(side_text, _RESOLVE_PROMPT.format(new=new["text"], old=listing),
                                                num_predict=120, temperature=0.1)) or {}
    action, oid = str(data.get("action", "add")).lower(), str(data.get("id", ""))
    if action in ("update", "supersede") and oid in {f["_id"] for f in old}:
        if action == "update" and str(data.get("text", "")).strip():
            store.update_text(new_id, str(data["text"]).strip())
        store.supersede(oid, new_id)
    elif action == "ignore":
        store.drop(new_id)
    else:
        action = "add"
    logger.info(f"🧠 Near-duplicate check: {action} ({new['text'][:60]!r})")
    return action


# ────────────────────────────────────────────────────────────────
# EXPLICIT SAVE — called by executor when action='save_memory'
# ────────────────────────────────────────────────────────────────
def save_explicit(updates: dict) -> str:
    if not updates or not isinstance(updates, dict):
        return "[MEMORY]: Nothing to save."
    memory_store.write(updates)
    return "[MEMORY]: Saved."
