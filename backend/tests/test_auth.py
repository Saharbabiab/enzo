"""Login, per-device sessions, rate limiting and /ws auth (auth.py + server.py)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def env(tmp_path, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(auth, "limiter", auth.RateLimiter(per_ip=3, global_max=10, window=60))
    monkeypatch.setenv("ENZO_PASSWORD_HASH", auth.hash_password("correct horse", iterations=1000))
    return auth


@pytest.fixture
def client(env, monkeypatch):
    import server  # runs load_dotenv(override=True): re-apply the test password hash afterwards
    monkeypatch.setenv("ENZO_PASSWORD_HASH", env.hash_password("correct horse", iterations=1000))
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)

    async def fake_bot(ws):  # don't start the real pipeline
        await ws.send_json({"type": "hello"})

    monkeypatch.setattr(server, "run_bot", fake_bot)
    monkeypatch.setattr(server, "register_websocket", lambda ws: None)
    monkeypatch.setattr(server, "unregister_websocket", lambda ws: None)
    app = FastAPI()  # same routes, without the startup tasks (Ollama warm-up etc.)
    app.router.routes.extend(r for r in server.app.router.routes if getattr(r, "path", "") != "")
    return TestClient(app)


def test_password_hash_roundtrip():
    import auth
    h = auth.hash_password("pw-QZ#99", iterations=1000)
    assert "pw-QZ#99" not in h
    assert auth.verify_password("pw-QZ#99", h)
    assert not auth.verify_password("pw-QZ#98", h)
    assert not auth.verify_password("pw-QZ#99", "garbage")


def test_session_create_validate_revoke(env):
    s = env.create_session("Pixel phone")
    assert env.validate_token(s["token"])["device"] == "Pixel phone"
    assert s["token"] not in env.SESSIONS_FILE.read_text()  # only the hash is stored
    assert env.validate_token("nope") is None
    assert env.revoke_session(s["id"])
    assert env.validate_token(s["token"]) is None


def test_session_expires(env, monkeypatch):
    monkeypatch.setenv("SESSION_DAYS", "0.01")
    s = env.create_session("x")
    import time
    monkeypatch.setattr(time, "time", lambda: s["expires"] + 1)
    assert env.validate_token(s["token"]) is None


def test_rate_limiter_per_ip_and_success_resets():
    import auth
    rl = auth.RateLimiter(per_ip=2, global_max=100, window=60)
    rl.fail("1.1.1.1")
    assert not rl.blocked("1.1.1.1")
    rl.fail("1.1.1.1")
    assert rl.blocked("1.1.1.1") and not rl.blocked("2.2.2.2")
    rl.success("1.1.1.1")
    assert not rl.blocked("1.1.1.1")


def test_rate_limiter_global():
    import auth
    rl = auth.RateLimiter(per_ip=100, global_max=3, window=60)
    for i in range(3):
        rl.fail(f"10.0.0.{i}")
    assert rl.blocked("9.9.9.9")


def test_client_ip_trusts_configured_reverse_proxy(monkeypatch):
    import auth
    monkeypatch.setenv("TRUSTED_PROXIES", "192.168.1.10")
    assert auth.client_ip("192.168.1.10", {"x-forwarded-for": "5.6.7.8"}) == "5.6.7.8"
    assert auth.client_ip("192.168.1.99", {"x-forwarded-for": "5.6.7.8"}) == "192.168.1.99"
    # client-supplied fake address first, real one appended by the proxy last
    assert auth.client_ip("192.168.1.10", {"x-forwarded-for": "1.1.1.1, 5.6.7.8"}) == "5.6.7.8"
    # a proxied request is never "local", even if the proxy sits on this PC
    assert not auth.is_direct_local("192.168.1.10", {"x-forwarded-for": "127.0.0.1"})


def test_client_ip_trusts_forwarded_only_from_localhost():
    import auth
    assert auth.client_ip("127.0.0.1", {"x-forwarded-for": "100.64.0.5"}) == "100.64.0.5"
    assert auth.client_ip("100.64.0.9", {"x-forwarded-for": "1.2.3.4"}) == "100.64.0.9"
    assert auth.is_direct_local("127.0.0.1", {})
    assert not auth.is_direct_local("127.0.0.1", {"x-forwarded-for": "100.64.0.5"})
    assert auth.is_direct_local("127.0.0.1", {"x-forwarded-for": "127.0.0.1"})  # Vite proxy on the PC
    assert not auth.is_direct_local("127.0.0.1", {"tailscale-user-login": "me@example.com"})
    assert not auth.is_direct_local("192.168.1.5", {})


def test_login_flow_and_ws(client):
    r = client.post("/api/login", json={"password": "wrong", "device": "phone"})
    assert r.status_code == 401
    r = client.post("/api/login", json={"password": "correct horse", "device": "phone"})
    assert r.status_code == 200
    token = r.json()["token"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json() == {"type": "auth_ok"}

    sessions = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"}).json()
    assert sessions["sessions"][0]["device"] == "phone"
    assert client.post("/api/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def _ws_rejected(client, token, headers=None):
    with client.websocket_connect("/ws", headers=headers or {}) as ws:
        ws.send_json({"type": "auth", "token": token})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        return e.value.code == 4401


def test_ws_rejects_bad_token_and_old_static_token(client, monkeypatch):
    monkeypatch.setenv("ENZO_TOKEN", "old-static-token")
    assert _ws_rejected(client, "old-static-token")
    assert _ws_rejected(client, "")


def test_ws_rejects_foreign_origin(client):
    token = client.post("/api/login", json={"password": "correct horse"}).json()["token"]
    assert _ws_rejected(client, token, {"origin": "https://evil.example"})


def test_login_rate_limited(client):
    for _ in range(3):
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    # even the right password is refused while blocked
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 429


def test_no_password_blocks_proxied_clients(client, monkeypatch):
    monkeypatch.delenv("ENZO_PASSWORD_HASH")
    assert client.post("/api/login", json={"password": "x"}).status_code == 503
    # TestClient's peer is "testclient", i.e. not this PC → blocked
    assert _ws_rejected(client, "")


# ── roles: phone/browser sessions vs the PC helper ──────────

def test_roles_are_kept_apart(client):
    user = client.post("/api/login", json={"password": "correct horse", "device": "phone"}).json()["token"]
    helper = client.post("/api/login", json={"password": "correct horse", "device": "pc", "role": "helper"}).json()["token"]

    with client.websocket_connect("/ws") as ws:                      # user token on /ws: fine
        ws.send_json({"type": "auth", "token": user})
        assert ws.receive_json() == {"type": "auth_ok"}
    with client.websocket_connect("/pc") as ws:                      # helper token on /pc: fine
        ws.send_json({"type": "auth", "token": helper})
        assert ws.receive_json()["type"] == "auth_ok"

    assert _ws_rejected_path(client, "/pc", user)                    # a phone can't act as the PC helper
    assert _ws_rejected_path(client, "/ws", helper)                  # the helper can't chat as a user


def _ws_rejected_path(client, path, token):
    with client.websocket_connect(path) as ws:
        ws.send_json({"type": "auth", "token": token})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        return e.value.code == 4401


def test_pc_socket_needs_a_password_even_from_localhost(client, monkeypatch):
    monkeypatch.delenv("ENZO_PASSWORD_HASH")
    assert _ws_rejected_path(client, "/pc", "")
