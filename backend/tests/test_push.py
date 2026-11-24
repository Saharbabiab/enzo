"""Enzo's notifications as real pushes (agent/push.py): a heads-up sent over the sleeping app's connection showed late."""
import asyncio
import json
import time
from datetime import datetime, timedelta

import pytz

from agent import proactive, push

TZ = pytz.timezone("Asia/Jerusalem")
TOK = "f" * 40


def _setup(tmp_path, monkeypatch, key=True):
    monkeypatch.setattr(push, "TOKENS_FILE", str(tmp_path / "push_tokens.json"))
    monkeypatch.setattr(push, "KEY_FILE", str(tmp_path / "fcm-key.json"))
    if key:
        (tmp_path / "fcm-key.json").write_text("{}")
    monkeypatch.setattr(push, "_access", lambda: ("access", "enzo-133fb"))


def test_tokens_are_kept_newest_first_and_junk_is_ignored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    push.save_token("short")
    push.save_token(TOK)
    push.save_token("a" * 40)
    push.save_token(TOK)
    assert push.tokens() == [TOK, "a" * 40]


def test_a_heads_up_is_a_data_push_that_google_drops_once_it_is_stale():
    stale = int((time.time() + 600) * 1000)
    m = push.message(TOK, "talk starts in 15 minutes, sir.", "cal-x-1", stale_after=stale)["message"]
    assert m["token"] == TOK and m["android"]["priority"] == "high" and "notification" not in m
    assert m["data"] == {"id": "cal-x-1", "text": "talk starts in 15 minutes, sir.", "alarm": "false", "stale_after": str(stale)}
    assert 595 <= int(m["android"]["ttl"][:-1]) <= 600


def test_send_posts_to_fcm_and_drops_a_token_the_phone_no_longer_has(tmp_path, monkeypatch):
    import httpx
    _setup(tmp_path, monkeypatch)
    push.save_token("b" * 40)
    push.save_token(TOK)
    seen = []
    real = httpx.AsyncClient

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.url.path, req.headers["authorization"], body["message"]["token"]))
        if body["message"]["token"] == "b" * 40:
            return httpx.Response(404, text='{"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}')
        return httpx.Response(200, json={"name": "ok"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    assert asyncio.run(push.send("hello", rid="r1"))
    assert seen[0] == ("/v1/projects/enzo-133fb/messages:send", "Bearer access", TOK)
    assert push.tokens() == [TOK]


def test_without_the_key_or_a_phone_nothing_is_pushed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, key=False)
    push.save_token(TOK)
    assert not push.ready() and not asyncio.run(push.send("hello"))


def test_a_pushed_notification_is_not_posted_again_over_the_app_connection(monkeypatch):
    import scheduler

    class WS:
        def __init__(self): self.sent = []
        async def send_json(self, o): self.sent.append(o)
        async def send_bytes(self, b): pass

    ws = WS()
    monkeypatch.setattr(scheduler, "_active_websockets", {ws})
    async def no_voice(text, mixed=None): return None
    monkeypatch.setattr("agent.voice.synthesize", no_voice)
    async def pushed(*a, **k): return True
    monkeypatch.setattr(push, "send", pushed)
    asyncio.run(scheduler._speak_reminder("Call mom", rid="r1"))
    assert [m["type"] for m in ws.sent] == ["transcript"]
    async def not_pushed(*a, **k): return False
    monkeypatch.setattr(push, "send", not_pushed)
    ws.sent.clear()
    asyncio.run(scheduler._speak_reminder("Call mom", rid="r1"))
    assert ws.sent[1]["action"] == "notify" and ws.sent[1]["id"] == "r1"


def test_the_calendar_heads_up_carries_its_id_and_goes_stale_at_the_start(monkeypatch):
    from agent import google_services as g
    ev = {"id": "e1", "summary": "presentation to alona", "location": "",
          "start": (datetime.now(TZ) + timedelta(minutes=10)).replace(microsecond=0)}
    monkeypatch.setattr(g, "connected", lambda: True)
    monkeypatch.setattr(g, "auth_problem", None, raising=False)
    monkeypatch.setattr(g, "upcoming_events", lambda m: [ev])
    monkeypatch.setattr(proactive, "_announced_events", set())
    sent = []
    async def notify(text, **k): sent.append((text, k))
    asyncio.run(proactive.check_calendar(datetime.now(TZ), notify))
    text, k = sent[0]
    assert text.startswith("presentation to alona starts in")
    assert k["rid"] == proactive.headsup(ev)[0] and k["stale_after"] == int(ev["start"].timestamp() * 1000)


def test_the_app_registers_its_push_token_when_it_opens(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import panel_api
    _setup(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(panel_api.router)
    c = TestClient(app)
    assert c.post("/api/panel/push", json={"token": "", "error": "pushToken failed"}).json() == {"ok": False}
    assert c.post("/api/panel/push", json={"token": TOK}).json() == {"ok": True}
    assert push.tokens() == [TOK]
