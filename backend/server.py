"""
server.py — FastAPI entrypoint for S.H.A.R.P.

Routes:
  GET  /api/health          Health check
  POST /api/login           Password → per-device session token (auth.py)
  POST /api/logout          Sign this device out
  GET/DELETE /api/sessions  See / revoke signed-in devices
  WS   /ws                  Voice pipeline (first message = session token)
  GET  /                    The built web app (frontend/dist), if present
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from loguru import logger
from agent import traces as _traces  # noqa: F401  (strips secrets from every log line)
from scheduler import scheduler_loop

import auth
from agent import pc_bridge
from bot import run_bot
from heartbeat import heartbeat_loop, register_websocket, unregister_websocket

load_dotenv(override=True)


async def _warm_agent_prompt():
    """Read Enzo's prompt into Ollama's cache now (~35 s), so his first request after a restart or deploy does not
    wait for it. The phone app's tool list, the one he uses most. (A plain "Hi" here used to WIPE that cache.)"""
    from agent.context import new_session
    from agent.agent_loop import keep_prefix_cached
    new_session().app = "android"
    await asyncio.to_thread(keep_prefix_cached)
    logger.success("✅ Agent prompt cached.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Enzo starting up...")
    asyncio.create_task(_warm_agent_prompt())
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(scheduler_loop())
    from agent.briefing import briefing_loop
    asyncio.create_task(briefing_loop())  # no-op unless BRIEFING_TIME is set
    from agent import stt
    asyncio.create_task(asyncio.to_thread(stt.preload))   # load Whisper now so the first sentence is not slow
    from agent import embed
    asyncio.create_task(asyncio.to_thread(embed.preload))  # memory recall model (CPU, ~118 MB, data/models)
    from agent.proactive import proactive_loop
    asyncio.create_task(proactive_loop())  # calendar heads-up, Google login reminders
    logger.info("💓 Heartbeat running.")
    yield
    logger.info("🛑 Enzo shutting down.")


app = FastAPI(
    title="Enzo",
    description="The user's Home Artificial Response Program — voice + AI backend.",
    version="3.0.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health():
    return {"status": "online", "system": "Enzo v3.0"}


# ── Login (see auth.py) ─────────────────────────────────────

class LoginBody(BaseModel):
    password: str = Field(max_length=256)
    device: str = Field(default="", max_length=120)
    role: str = Field(default="user", max_length=10)  # "user" (phone/PC browser) or "helper" (pc_helper.py)


def _bearer(request: Request) -> dict | None:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return auth.validate_token(token)


@app.get("/api/auth/status")
async def auth_status():
    return {"password_set": auth.password_configured()}


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    peer = request.client.host if request.client else ""
    ip = auth.client_ip(peer, request.headers)
    if auth.limiter.blocked(ip):
        logger.warning(f"⛔ Login blocked (too many tries) from {ip}")
        raise HTTPException(429, "Too many wrong tries — wait 15 minutes.")
    if not auth.password_configured():
        raise HTTPException(503, "No password set yet — run: python set_password.py")
    ok = await asyncio.to_thread(auth.check_password, body.password)
    if not ok:
        auth.limiter.fail(ip)
        logger.warning(f"⛔ Wrong password from {ip}")
        raise HTTPException(401, "Wrong password.")
    auth.limiter.success(ip)
    session = auth.create_session(body.device, ip, body.role)
    logger.info(f"🔑 Login: new session {session['id']} for '{body.device}' from {ip}")
    return session


@app.get("/api/spotify/callback")
async def spotify_callback(code: str = "", state: str = "", error: str = ""):
    """Spotify sends the browser back here after the user pressed Allow (state = one-time secret from Enzo)."""
    from fastapi.responses import HTMLResponse
    from agent import spotify
    page = ("<html><body style='font-family:sans-serif;background:#07090d;color:#e9eef5;text-align:center;padding-top:20vh'>"
            "<h2>{}</h2><p>{}</p></body></html>")
    if error or not code:
        return HTMLResponse(page.format("Spotify was not connected", "You can try again by telling Enzo: connect Spotify."), 400)
    try:
        await spotify.finish_login(code, state)
    except spotify.SpotifyError as e:
        return HTMLResponse(page.format("Spotify was not connected", str(e)), 400)
    return HTMLResponse(page.format("Spotify connected", "You can go back to Enzo now."))


@app.post("/api/logout")
async def logout(request: Request):
    me = _bearer(request)
    if not me:
        raise HTTPException(401, "Not logged in.")
    auth.revoke_session(me["id"])
    return {"ok": True}


@app.post("/api/stt")
async def speech_to_text(request: Request, lang: str = ""):
    """One utterance of audio (webm/opus, mp4, ogg or wav) -> {"text", "engine"}. Logged-in devices only."""
    if not _bearer(request):
        raise HTTPException(401, "Not logged in.")
    audio = await request.body()
    if len(audio) > 6_000_000:
        raise HTTPException(413, "Audio too long.")
    from agent import stt
    text, engine = await stt.transcribe(audio, request.headers.get("content-type", "audio/webm"), lang[:2])
    logger.info(f"STT [{engine}] {len(audio) // 1024} KB -> {text[:80]!r}")
    return {"text": text, "engine": engine}


@app.get("/api/sessions")
async def sessions(request: Request):
    me = _bearer(request)
    if not me:
        raise HTTPException(401, "Not logged in.")
    return {"current": me["id"], "sessions": auth.list_sessions()}


@app.delete("/api/sessions/{session_id}")
async def revoke(session_id: str, request: Request):
    if not _bearer(request):
        raise HTTPException(401, "Not logged in.")
    if not auth.revoke_session(session_id):
        raise HTTPException(404, "No such session.")
    return {"ok": True}


def _signed_in_user(request: Request) -> None:
    """The panel pages: a logged-in phone/PC browser only (not the PC helper's session)."""
    me = _bearer(request)
    if not me or me.get("role", "user") != "user":
        raise HTTPException(401, "Not logged in.")


import panel_api  # noqa: E402
app.include_router(panel_api.router, dependencies=[Depends(_signed_in_user)])


@app.get("/api/google/callback")
async def google_callback(state: str = "", code: str = "", error: str = ""):
    """Google sends the browser here after the Reconnect Google login. No app token on this request:
    the one-time state from /api/panel/google/connect (a signed-in device) is the check."""
    from html import escape
    from fastapi.responses import HTMLResponse
    from agent import google_services as g
    if error or not code:
        msg = f"Google did not connect ({error or 'no code'})."
    else:
        try:
            msg = await asyncio.to_thread(g.finish_web_login, state, code)
        except ValueError as e:
            msg = str(e)
        except Exception as e:
            logger.warning(f"Google reconnect failed: {e.__class__.__name__}")
            msg = "Google did not accept the login. Tap Reconnect Google again."
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'><title>Enzo</title>"
                        f"<body style='font:18px system-ui;background:#000;color:#eee;padding:32px'>"
                        f"<p>{escape(msg)}</p><p>You can close this page and go back to Enzo.</p>")


# ── WebSocket ───────────────────────────────────────────────

def _origin_allowed(websocket: WebSocket) -> bool:
    """Browsers always send Origin — accept only our own page (same host) or ALLOWED_ORIGINS."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # not a browser (tests, scripts) — the token still applies
    allowed = {o.strip().rstrip("/") for o in os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()}
    if origin.rstrip("/") in allowed:
        return True
    return urlparse(origin).netloc.lower() == websocket.headers.get("host", "").lower()


async def _authenticate(websocket: WebSocket, role: str = "user") -> bool:
    """
    The first message on /ws must be {"type": "auth", "token": "<session token>"}
    (from /api/login).  Before a password is set, only a browser on this PC
    (not proxied) is allowed in, so nothing is open by accident.
    """
    peer = websocket.client.host if websocket.client else ""
    ip = auth.client_ip(peer, websocket.headers)

    if not _origin_allowed(websocket):
        logger.warning(f"⛔ Rejected {ip}: origin {websocket.headers.get('origin')} not allowed")
        return False
    if auth.limiter.blocked(ip):
        logger.warning(f"⛔ Rejected {ip}: too many bad tries")
        return False

    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except Exception:
        logger.warning(f"⛔ Rejected {ip}: no auth message")
        return False
    token = msg.get("token", "") if isinstance(msg, dict) and msg.get("type") == "auth" else ""

    if not auth.password_configured():
        if role == "user" and auth.is_direct_local(peer, websocket.headers):
            return True
        logger.warning(f"⛔ Rejected {ip}: no password set, remote clients blocked")
        return False

    session = auth.validate_token(str(token))
    if session and session.get("role", "user") != role:
        logger.warning(f"⛔ Rejected {ip}: a '{session.get('role')}' session can't open the '{role}' socket")
        session = None
    if not session:
        auth.limiter.fail(ip)
        logger.warning(f"⛔ Rejected {ip}: bad or expired session token")
        return False
    auth.limiter.success(ip)
    logger.info(f"🔑 Session {session['id']} ({session['device']}) from {ip}")
    return True


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    if not await _authenticate(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.send_json({"type": "auth_ok"})
    logger.info(f"🔌 WebSocket connected: {websocket.client}")
    register_websocket(websocket)
    try:
        await run_bot(websocket)
    except Exception as e:
        logger.error(f"Pipeline error: {e.__class__.__name__}: {e}")
    finally:
        unregister_websocket(websocket)
        logger.info("WebSocket session ended.")


@app.websocket("/pc")
async def pc_endpoint(websocket: WebSocket):
    """The PC helper (pc_helper.py) connects here so Enzo can use PC tools while it runs on the NAS."""
    await websocket.accept()
    if not await _authenticate(websocket, role="helper"):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.send_json({"type": "auth_ok", "tools": sorted(pc_bridge.PC_TOOLS)})
    await pc_bridge.serve(websocket)


# ── The web app itself (built frontend), so PC and phone use one HTTPS address ──
# Build it with `npm run build` in ../frontend; override with FRONTEND_DIST.
_DIST = Path(os.getenv("FRONTEND_DIST", Path(__file__).parent.parent / "frontend" / "dist"))
class _Frontend(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        if not scope["path"].startswith("/assets/"):  # hashed assets may cache; the page must not, or the phone runs an old build
            r.headers["Cache-Control"] = "no-cache"
        return r


if (_DIST / "index.html").is_file():
    app.mount("/", _Frontend(directory=_DIST, html=True), name="frontend")
    logger.info(f"🌐 Serving web app from {_DIST}")
else:
    @app.get("/")
    async def root():
        return JSONResponse({"status": "online", "system": "Enzo v3.0",
                             "note": f"web app not built ({_DIST})"})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    logger.info(f"Listening on {host}:{port}")
    import logging

    class _NoHealthSpam(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "/api/health" not in record.getMessage()

    logging.getLogger("uvicorn.access").addFilter(_NoHealthSpam())
    uvicorn.run("server:app", host=host, port=port, log_level="info")