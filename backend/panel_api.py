"""
panel_api.py — the app's panel pages (Phase 7): memory, lessons, playbook ideas, recent turns.

Every route needs a logged-in device (server.py adds the check). Nothing here deletes data for good:
a forgotten fact keeps forgotten_at, a removed lesson is switched off (src "owner", so the nightly job leaves it).
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/panel")


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=300)


# ── home screen and settings (real data only: a part the server cannot get is null) ──────────────────────

@router.get("/home")
async def home():
    from agent import briefing, google_services as g
    from agent.skills import weather

    def part(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    city = os.getenv("DEFAULT_CITY", "").strip()
    connected = part(g.connected)
    w, events, unread = await asyncio.gather(
        asyncio.to_thread(part, weather._now_in, city) if city else asyncio.sleep(0),
        asyncio.to_thread(part, g.today_events) if connected else asyncio.sleep(0),
        asyncio.to_thread(part, g.unread_count) if connected else asyncio.sleep(0))
    return {"weather": w, "events": events, "unread": unread,
            "briefing_ready": bool(part(briefing.ready_briefing))}


@router.get("/status")
async def status():
    from agent import big_brain, google_services as g
    age = await asyncio.to_thread(g.login_age_days)
    return {"brain": "claude" if os.getenv("MAIN_BRAIN", "claude").strip().lower() == "claude" else "local",
            "claude_spent": round(big_brain.spent_today(), 2), "claude_budget": big_brain.DAILY_USD,
            "google": {"connected": g.connected() and not g.auth_problem, "problem": g.auth_problem,
                       "days_left": None if age is None or g.LOGIN_VALID_DAYS <= 0 else round(max(0.0, g.LOGIN_VALID_DAYS - age), 1),
                       "can_renew": g.can_renew_from_app()}}


@router.post("/google/connect")
async def google_connect():
    """The Reconnect Google button: the app opens this Google page; Google sends him back to /api/google/callback."""
    from agent import google_services as g
    if not g.can_renew_from_app():
        raise HTTPException(409, "The Web OAuth client is not set up on the server yet (GOOGLE_WEB_CLIENT_JSON in .env).")
    return {"url": await asyncio.to_thread(g.web_login_url)}


class PushToken(BaseModel):
    token: str = Field(default="", max_length=400)
    error: str = Field(default="", max_length=200)


@router.post("/push")
async def push_token(body: PushToken):
    """The phone app registers for push as soon as it opens (agent/push.py); the live connection only opens when he
    talks to Enzo, so registering there alone left the server with no phone to push to."""
    from loguru import logger
    from agent import push
    if not body.token:
        logger.warning(f"📲 Push: the app has no push token ({body.error or 'no reason given'})")
        return {"ok": False}
    push.save_token(body.token)
    return {"ok": True}


# ── memory (agent/facts.py) ──────────────────────────────────────────────────

@router.get("/memory")
async def memory():
    from agent import facts
    items = await asyncio.to_thread(facts.listing)
    return {"facts": [{"id": f["_id"], "text": f["text"], "trust": f.get("trust", ""), "source": f.get("source", ""),
                       "core": bool(f.get("core")), "updated": f.get("updated_at") or f.get("created_at") or 0,
                       "quarantined": f.get("quarantined") or ""} for f in items]}


@router.put("/memory/{fid}")
async def memory_edit(fid: str, body: Text):
    from agent import facts
    if not await asyncio.to_thread(facts.edit, fid, body.text):
        raise HTTPException(404, "No such fact.")
    return {"ok": True}


@router.post("/memory/{fid}/trust")
async def memory_trust(fid: str):
    from agent import facts
    if not await asyncio.to_thread(facts.confirm, fid):
        raise HTTPException(404, "No such fact.")
    return {"ok": True}


@router.delete("/memory/{fid}")
async def memory_forget(fid: str):
    from agent import facts
    if not await asyncio.to_thread(facts.forget_id, fid):
        raise HTTPException(404, "No such fact.")
    return {"ok": True}


# ── lessons (agent/lessons.py) ───────────────────────────────────────────────

@router.get("/lessons")
async def lessons_list():
    from agent import lessons
    return {"lessons": [{k: n.get(k) for k in ("id", "text", "src", "helped", "hurt", "off")} for n in lessons.notes(include_off=True)]}


@router.post("/lessons")
async def lessons_add(body: Text):
    from agent import lessons
    return {"ok": True, "text": lessons.teach(body.text)}


@router.delete("/lessons/{lid}")
async def lessons_off(lid: str):
    from agent import lessons
    if not lessons.switch_off(lid):
        raise HTTPException(404, "No such lesson, or it is a built-in one.")
    return {"ok": True}


# ── playbook ideas from the nightly review (data/skills/_proposed) ───────────

@router.get("/playbooks")
async def playbook_ideas():
    from agent import lessons, playbooks
    out = []
    for name in lessons.proposals():
        pb = playbooks._read(os.path.join(playbooks.OWN_DIR, "_proposed", name, "SKILL.md")) or {}
        out.append({"name": name, "description": pb.get("description", ""), "steps": pb.get("body", ""), "tools": pb.get("tools", [])})
    return {"proposals": out}


@router.post("/playbooks/{name}/{answer}")
async def playbook_answer(name: str, answer: str):
    """His tap on Approve / Reject is the approval (same as Yes / No by voice); it goes to the audit log."""
    from agent import approvals, lessons, playbooks
    if answer not in ("approve", "reject"):
        raise HTTPException(400, "approve or reject.")
    if name not in lessons.proposals():
        raise HTTPException(404, "No such playbook idea.")
    approvals._audit("enzo_lessons", {"action": answer, "rule": name}, "yes" if answer == "approve" else "no", False,
                     f"App panel: {answer} the playbook idea '{name}'")
    if answer == "reject":
        playbooks.reject(name)
        return {"ok": True}
    if not playbooks.approve(name):
        raise HTTPException(409, "A playbook with that name exists already.")
    from agent.tools import registry
    registry().reload()
    return {"ok": True}


# ── recent turns (agent/traces.py) ───────────────────────────────────────────

@router.get("/traces")
async def recent_turns(hours: float = 48, limit: int = 60):
    from agent import traces
    docs = await asyncio.to_thread(traces.recent, min(hours, 24 * 14))
    docs = sorted(docs, key=lambda d: d.get("ts", 0), reverse=True)[:max(1, min(limit, 200))]
    return {"traces": [{"id": d.get("id", ""), "ts": d.get("ts", 0), "text": d.get("text", ""), "reply": d.get("reply", ""),
                        "route": d.get("route", ""), "ms": d.get("ms", 0), "cost": round(d.get("cost_usd", 0) or 0, 4),
                        "outcome": d.get("outcome") or traces.outcome(d),
                        "tools": [{"name": t.get("name"), "ok": t.get("ok", True), "ms": t.get("ms", 0)} for t in d.get("tools", [])],
                        "signals": [s.get("name") for s in d.get("signals", [])]} for d in docs]}
