"""
agent/skill_api.py — The ONLY door from a self-written skill to the user's accounts.

Skills Enzo writes for itself can't touch credentials, tokens or files. To work
with Google Calendar / Gmail they call google_call(), which:
  • uses Enzo's existing Google login (the skill never sees the token)
  • runs read-only calls (list / get / getProfile / query …) freely
  • asks the user for approval before ANY call that changes something
    (insert, patch, delete, send, modify, trash, …) — web Yes/No or Telegram buttons

Example (inside a skill):

    from agent.skill_api import google_call

    def archive_emails_from(sender: str) -> str:
        '''Archive all inbox emails from a sender.

        Args:
            sender: Email address or name, e.g. "notion".
        '''
        res = google_call("gmail", "users.messages.list", userId="me", q=f"in:inbox from:{sender}")
        ids = [m["id"] for m in res.get("messages", [])]
        if not ids:
            return "Nothing to archive."
        google_call("gmail", "users.messages.batchModify", userId="me",
                    body={"ids": ids, "removeLabelIds": ["INBOX"]},
                    description=f"Archive {len(ids)} emails from {sender}")
        return f"Archived {len(ids)} emails."
"""

from __future__ import annotations

import asyncio
import re

from loguru import logger

_VERSIONS = {"calendar": "v3", "gmail": "v1"}
_READ_METHODS = {"list", "get", "getProfile", "query", "instances", "listHistory", "watch_list"}
_MAX_RESULT_ITEMS = 200


class SkillApiError(Exception):
    pass


def _ask_user(question: str, tool: str) -> bool:
    """Ask for approval from a worker thread, via the request's event loop."""
    from agent.context import session
    from agent import approvals
    s = session()
    if s.loop is None:
        logger.warning(f"skill_api: no event loop to ask on — denied ({question})")
        return False
    fut = asyncio.run_coroutine_threadsafe(approvals.ask(question, tool), s.loop)
    try:
        return bool(fut.result(timeout=approvals.APPROVAL_TIMEOUT + 15))
    except Exception as e:
        logger.warning(f"skill_api approval failed: {e}")
        return False


def _summary(params: dict) -> str:
    parts = []
    for k, v in params.items():
        if k in ("userId", "calendarId"):
            continue
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:60]}{'…' if len(s) > 60 else ''}")
    return ", ".join(parts)[:220]


def user_location() -> dict:
    """Where the user is right now (if shared): {"latitude", "longitude", "place", "device"} — values may be None/""."""
    from agent.context import session
    s = session()
    return {"latitude": s.latitude, "longitude": s.longitude, "place": s.place, "device": s.device}


def google_call(service: str, method: str, description: str = "", **params) -> dict:
    """
    Call a Google API method with Enzo's login.
      service: "calendar" or "gmail"
      method:  dotted path under the service, e.g. "events.list", "users.messages.modify"
      description: plain-English summary shown when asking for approval (recommended for writes)
      params:  the method's parameters (userId="me", calendarId="primary", body={...}, …)
    Returns the API response (a dict). Changing calls ask the user first; if they say no,
    raises SkillApiError("declined").
    """
    from agent import google_services as g
    if service not in _VERSIONS:
        raise SkillApiError(f"Unknown service '{service}'. Use 'calendar' or 'gmail'.")
    if not re.fullmatch(r"[A-Za-z]+(\.[A-Za-z]+){0,4}", method or ""):
        raise SkillApiError(f"Bad method path '{method}'.")
    if not g.connected():
        raise SkillApiError("Google isn't connected — run `python google_setup.py`.")

    parts = method.split(".")
    is_write = parts[-1] not in _READ_METHODS
    if is_write:
        question = (f"{description.strip()} — allow?" if description.strip()
                    else f"Allow a skill to {service} {method} ({_summary(params)})?")
        if not _ask_user(question, f"skill:{service}.{method}"):
            raise SkillApiError("declined: the user said no, nothing was changed")

    obj = g._service(service, _VERSIONS[service])
    try:
        for p in parts[:-1]:
            obj = getattr(obj, p)()
        result = getattr(obj, parts[-1])(**params).execute()
    except AttributeError:
        raise SkillApiError(f"'{method}' doesn't exist in the {service} API.")
    except Exception as e:
        msg = str(e)
        if "insufficient" in msg.lower() or "scope" in msg.lower():
            raise SkillApiError("Enzo's Google login doesn't allow this yet — run "
                                "`python google_setup.py` again to grant the new permission.")
        raise SkillApiError(f"Google API error: {msg[:300]}")
    logger.info(f"🔑 skill_api {service}.{method} ({'write' if is_write else 'read'})")
    if isinstance(result, dict):
        for key in ("items", "messages", "threads", "labels"):
            if isinstance(result.get(key), list) and len(result[key]) > _MAX_RESULT_ITEMS:
                result[key] = result[key][:_MAX_RESULT_ITEMS]
    return result if isinstance(result, dict) else {"result": result}
