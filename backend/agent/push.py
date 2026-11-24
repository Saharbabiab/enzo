"""
agent/push.py - Enzo's notifications as real push messages (Firebase Cloud Messaging), like other apps.

Android pauses the app's page in the background, so a notification sent over the app's connection showed only when
he woke the phone (a 09:45 heads-up at 10:07). A push goes through Google: Android wakes the app's EnzoPush service
and the notification shows on time, even with the app closed.

- The app sends its push token with its session context (bot.py) -> data/push_tokens.json.
- The server signs in with the Firebase service-account key: FCM_KEY_FILE (default data/fcm-key.json, never in git).
- Data-only messages (id, text, alarm, stale_after): the app shows them like reminders; the same id replaces.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from loguru import logger

KEY_FILE = os.getenv("FCM_KEY_FILE", os.path.join("data", "fcm-key.json"))
TOKENS_FILE = os.path.join("data", "push_tokens.json")
MAX_TOKENS = 3                       # his phone (a reinstall or a new phone gets a new token)
_lock = threading.Lock()
_creds = None


def tokens() -> list[str]:
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return [t for t in json.load(f) if isinstance(t, str)]
    except Exception:
        return []


def _write(toks: list[str]) -> None:
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(toks, f)


def save_token(token) -> None:
    """The app's current token, newest first."""
    if not isinstance(token, str) or not (20 <= len(token) <= 400):
        return
    with _lock:
        old = tokens()
        if old[:1] == [token]:
            return
        _write(([token] + [t for t in old if t != token])[:MAX_TOKENS])
    logger.info("📲 Push: phone token saved")


def _drop(token: str) -> None:
    with _lock:
        _write([t for t in tokens() if t != token])


def ready() -> bool:
    return os.path.exists(KEY_FILE) and bool(tokens())


def _access() -> tuple[str, str]:
    """(OAuth access token, project id) from the service-account key. Blocking; the token is reused until it expires."""
    global _creds
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    with _lock:
        if _creds is None:
            _creds = service_account.Credentials.from_service_account_file(
                KEY_FILE, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token, _creds.project_id


def message(token: str, text: str, rid: str, alarm: bool = False, stale_after: int = 0) -> dict:
    data = {"id": rid or text[:24], "text": text[:500], "alarm": "true" if alarm else "false"}
    android: dict = {"priority": "high"}
    if stale_after:
        data["stale_after"] = str(int(stale_after))
        android["ttl"] = f"{max(0, int(stale_after / 1000 - time.time()))}s"   # Google drops it once it is wrong
    return {"message": {"token": token, "data": data, "android": android}}


async def send(text: str, rid: str = "", alarm: bool = False, stale_after: int = 0) -> bool:
    """Push to his phone. True if at least one phone took it (then the app connection need not post it)."""
    if not ready():
        return False
    import httpx
    try:
        access, project = await asyncio.to_thread(_access)
    except Exception as e:
        logger.warning(f"Push: no access token ({e.__class__.__name__}: {e})")
        return False
    ok = False
    async with httpx.AsyncClient(timeout=15) as c:
        for tok in tokens():
            try:
                r = await c.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                                 headers={"Authorization": f"Bearer {access}"}, json=message(tok, text, rid, alarm, stale_after))
            except Exception as e:
                logger.warning(f"Push failed: {e.__class__.__name__}")
                continue
            if r.status_code == 200:
                ok = True
            elif r.status_code == 404 and "UNREGISTERED" in r.text:
                _drop(tok)                                   # app reinstalled / token replaced
                logger.info("Push: dropped a token the phone no longer has")
            else:
                logger.warning(f"Push: HTTP {r.status_code} {r.text[:200]}")
    if ok:
        logger.info(f"📲 Pushed to the phone: {text[:40]!r}")
    return ok
