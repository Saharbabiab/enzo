"""
agent/playbooks.py — skills as playbooks: plain-English recipes the model follows with its normal tools.

    skills/<name>/SKILL.md          shipped with Enzo
    data/skills/<name>/SKILL.md     the user's own (made by voice with save_playbook); same name wins, survives updates

A SKILL.md starts with a small header, then the steps:

    ---
    name: good_night
    description: Bedtime routine. Use when he says good night or he is going to sleep.
    tools: phone_settings, phone_alarm, calendar_events
    ---
    1. ...

catalog() (name + description) goes in the STABLE prompt; use_skill returns the steps as a tool result.
Files are re-read when they change (hot reload), so a new playbook needs no restart.
"""

from __future__ import annotations

import json
import os
import re
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DIR = os.path.join(_ROOT, "skills")
OWN_DIR = os.path.join(_ROOT, "data", "skills")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

_cache: dict[str, tuple[float, dict]] = {}


def parse(text: str) -> dict:
    """{'name', 'description', 'tools': [...], 'body'} from a SKILL.md text. Header lines are 'key: value'."""
    meta, body = {}, text.strip()
    if body.startswith("---"):
        head, _, body = body[3:].partition("\n---")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip()
    tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    return {"name": meta.get("name", ""), "description": " ".join(meta.get("description", "").split()),
            "tools": tools, "body": body.strip(), "source": meta.get("source", ""), "patch": meta.get("patch", "") == "yes"}


def _read(path: str) -> dict | None:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if not hit or hit[0] != mtime:
        with open(path, encoding="utf-8") as f:
            _cache[path] = (mtime, parse(f.read()))
    return _cache[path][1]


def all_playbooks() -> dict[str, dict]:
    """name -> playbook. Folders starting with '_' (like _proposed) are not live."""
    found: dict[str, dict] = {}
    for base in (SHIPPED_DIR, OWN_DIR):                      # own last, so it wins
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for folder in names:
            if folder.startswith("_"):
                continue
            pb = _read(os.path.join(base, folder, "SKILL.md"))
            if pb and pb["description"]:
                found[pb["name"] or folder] = {**pb, "name": pb["name"] or folder,
                                               "own": base == OWN_DIR}
    return found


def catalog() -> str:
    """The block for the stable prompt (empty when there are none). Changes only when a playbook file changes."""
    books = all_playbooks()
    if not books:
        return ""
    lines = [f"- {n}: {b['description']}" for n, b in sorted(books.items())]
    return ("\n=== PLAYBOOKS (call use_skill with the name to get the steps, then follow them with your tools) ===\n"
            + "\n".join(lines) + "\n")


def names_line() -> str:
    """' Playbooks: good_night (Bedtime routine. Use when ...), ...' for use_skill's tool description."""
    books = all_playbooks()
    if not books:
        return ""
    return " Playbooks: " + "; ".join(f"{n} ({b['description']})" for n, b in sorted(books.items())) + "."


def load(name: str) -> str:
    """The text use_skill returns: the steps plus the tools they use."""
    books = all_playbooks()
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    pb = books.get(key)
    if not pb:
        return f"[ERROR]: No playbook named '{name}'. Playbooks: {', '.join(sorted(books)) or 'none'}."
    tools = f"\nTools for this: {', '.join(pb['tools'])}." if pb["tools"] else ""
    return (f"PLAYBOOK {pb['name']}. Do these steps now: call each step's tool (skip a step that does not fit, e.g. a phone "
            f"step when he is not on the phone app). Say nothing you did not get from a tool in this turn. Then tell him briefly what you did.\n{pb['body']}{tools}")


def approve(name: str) -> bool:
    """Make a proposed playbook live (moves its folder out of _proposed). A proposed PATCH (header "patch: yes",
    agent/skill_review.py) replaces the live one; his old own copy is kept in data/skills/_replaced/ (a shipped one
    stays in skills/ and the new own copy wins by name)."""
    import shutil
    src = os.path.join(OWN_DIR, "_proposed", name)
    path = os.path.join(src, "SKILL.md")
    if not NAME_RE.match(name) or not os.path.isfile(path):
        return False
    if name in all_playbooks():
        if not (_read(path) or {}).get("patch"):
            return False
        live = os.path.join(OWN_DIR, name)
        if os.path.isdir(live):
            shutil.move(live, os.path.join(OWN_DIR, "_replaced", f"{name}-{int(time.time())}"))
    shutil.move(src, os.path.join(OWN_DIR, name))
    return True


def reject(name: str) -> bool:
    import shutil
    src = os.path.join(OWN_DIR, "_proposed", name)
    if not NAME_RE.match(name) or not os.path.isfile(os.path.join(src, "SKILL.md")):
        return False
    shutil.rmtree(src)
    return True


def _removed_file() -> str:
    return os.path.join(OWN_DIR, "_removed.json")


def _removed() -> dict:
    try:
        with open(_removed_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def note_removed(name: str) -> None:
    """Remember for a day that he deleted this routine (see recently_removed)."""
    d = {**_removed(), name: time.time()}
    os.makedirs(OWN_DIR, exist_ok=True)
    with open(_removed_file(), "w", encoding="utf-8") as f:
        json.dump(d, f)


def recently_removed(hours: float = 24) -> list[str]:
    """Routines he deleted in the last day that are still gone (saving one again clears it)."""
    live = all_playbooks()
    return sorted(n for n, ts in _removed().items() if time.time() - ts < hours * 3600 and n not in live)


def save(name: str, description: str, steps: str, tools: list[str] | None = None, proposed: bool = False,
         source: str = "", patch: bool = False) -> str:
    """Write data/skills/<name>/SKILL.md (proposed: data/skills/_proposed/<name>/, not live). Returns the path.
    source "learned" = written by agent/skill_review.py (scored and retired by it); patch = replaces a live one on approve."""
    folder = os.path.join(OWN_DIR, "_proposed", name) if proposed else os.path.join(OWN_DIR, name)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "SKILL.md")
    head = f"---\nname: {name}\ndescription: {' '.join(description.split())}\n"
    if tools:
        head += f"tools: {', '.join(tools)}\n"
    if source:
        head += f"source: {source}\n"
    if patch:
        head += "patch: yes\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "---\n" + steps.strip() + "\n")
    return path
