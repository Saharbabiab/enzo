"""The app's panel pages (panel_api.py): login needed; memory, lessons and playbook ideas change only as asked."""
import os

from test_auth import env, client  # noqa: F401  (fixtures)


def test_panel_needs_a_signed_in_user(client, env):
    assert client.get("/api/panel/memory").status_code == 401
    helper = env.create_session("pc helper", role="helper")["token"]
    assert client.get("/api/panel/memory", headers={"Authorization": f"Bearer {helper}"}).status_code == 401


def test_panel_memory_lessons_playbooks_and_turns(client, env, tmp_path, monkeypatch):
    from agent import facts, lessons, playbooks, tools
    h = {"Authorization": f"Bearer {env.create_session('phone')['token']}"}

    f, _ = facts.add("He likes the Lakers", source="note-taker", trust="auto")
    row = next(x for x in client.get("/api/panel/memory", headers=h).json()["facts"] if x["id"] == f["_id"])
    assert row["trust"] == "auto"
    assert client.put(f"/api/panel/memory/{f['_id']}", json={"text": "He likes the Celtics"}, headers=h).status_code == 200
    assert facts.get(f["_id"])["text"] == "He likes the Celtics" and facts.get(f["_id"])["trust"] == "trusted"
    assert client.delete(f"/api/panel/memory/{f['_id']}", headers=h).status_code == 200
    assert facts.get(f["_id"])["forgotten_at"]                                     # hidden, not deleted
    assert all(x["id"] != f["_id"] for x in client.get("/api/panel/memory", headers=h).json()["facts"])
    assert client.post("/api/panel/memory/nope/trust", headers=h).status_code == 404

    monkeypatch.setattr(lessons, "LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(lessons, "DATA", tmp_path)
    client.post("/api/panel/lessons", json={"text": "Answer shorter in the morning"}, headers=h)
    notes = client.get("/api/panel/lessons", headers=h).json()["lessons"]
    mine = next(n for n in notes if n["text"] == "Answer shorter in the morning")
    seed = next(n for n in notes if n["src"] == "seed")
    assert client.delete(f"/api/panel/lessons/{seed['id']}", headers=h).status_code == 404   # built-in stays
    assert client.delete(f"/api/panel/lessons/{mine['id']}", headers=h).status_code == 200
    assert all(n["text"] != "Answer shorter in the morning" for n in lessons.notes())

    monkeypatch.setattr(playbooks, "OWN_DIR", str(tmp_path / "skills"))
    for name in ("movie_night", "bad_idea"):
        os.makedirs(tmp_path / "skills" / "_proposed" / name)
        (tmp_path / "skills" / "_proposed" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Dim the lights\ntools: media_control\n---\n1. Pause music", encoding="utf-8")
    monkeypatch.setattr(tools.registry(), "reload", lambda: None)
    ideas = client.get("/api/panel/playbooks", headers=h).json()["proposals"]
    assert {i["name"] for i in ideas} == {"movie_night", "bad_idea"} and ideas[0]["description"] == "Dim the lights"
    assert client.post("/api/panel/playbooks/movie_night/approve", headers=h).status_code == 200
    assert client.post("/api/panel/playbooks/bad_idea/reject", headers=h).status_code == 200
    assert (tmp_path / "skills" / "movie_night" / "SKILL.md").exists() and lessons.proposals() == []
    assert client.post("/api/panel/playbooks/movie_night/approve", headers=h).status_code == 404

    assert "traces" in client.get("/api/panel/traces", headers=h).json()


def test_agent_loop_tells_the_app_which_tool_runs():
    import asyncio
    from agent import context
    from agent.agent_loop import LoopGuard, run_tool_calls
    shown = []

    class Reg:
        async def call(self, name, args):
            return "[OK]: done"

    async def go():
        s = context.new_session()

        async def activity(tool):
            shown.append(tool)
        s.activity = activity
        await run_tool_calls(Reg(), LoopGuard(), [("get_current_weather", {"city_name": "Tel Aviv"})], [])
    asyncio.run(go())
    assert shown == ["get_current_weather"]


def test_panel_home_and_status_only_real_data(client, env, monkeypatch):
    """Home tiles come from the real services; a part the server cannot get is null (the app hides it)."""
    import agent.google_services as g
    from agent import briefing
    from agent.skills import weather
    h = {"Authorization": f"Bearer {env.create_session('phone')['token']}"}
    monkeypatch.setenv("DEFAULT_CITY", "Beer Sheva")
    monkeypatch.setattr(weather, "_now_in", lambda c: {"place": "Beer Sheva", "temp": 24.0, "condition": "Clear"})
    monkeypatch.setattr(g, "connected", lambda: True)
    monkeypatch.setattr(g, "today_events", lambda: [{"start": "16:00", "title": "Meeting"}])
    monkeypatch.setattr(g, "unread_count", lambda: (_ for _ in ()).throw(RuntimeError("gmail down")))
    monkeypatch.setattr(briefing, "ready_briefing", lambda: None)
    monkeypatch.setattr(g, "login_age_days", lambda: 5.0)
    monkeypatch.setattr(g, "LOGIN_VALID_DAYS", 7.0)
    monkeypatch.setattr(g, "auth_problem", "")
    r = client.get("/api/panel/home", headers=h).json()
    assert r == {"weather": {"place": "Beer Sheva", "temp": 24.0, "condition": "Clear"},
                 "events": [{"start": "16:00", "title": "Meeting"}], "unread": None, "briefing_ready": False}
    s = client.get("/api/panel/status", headers=h).json()
    assert set(s) == {"brain", "claude_spent", "claude_budget", "google"} and s["brain"] in ("local", "claude")
    assert s["google"]["connected"] and s["google"]["days_left"] == 2.0
    assert client.get("/api/panel/home").status_code == 401


def test_reconnect_google_from_the_app(client, env, tmp_path, monkeypatch):
    """Reconnect Google: a signed-in device gets Google's page; only Google's answer with that one-time state saves a login."""
    import agent.google_services as g
    from google_auth_oauthlib import flow as F
    h = {"Authorization": f"Bearer {env.create_session('phone')['token']}"}
    monkeypatch.setattr(g, "WEB_CREDENTIALS_FILE", str(tmp_path / "web.json"))
    monkeypatch.setattr(g, "TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setattr(g, "AUTH_TIME_FILE", str(tmp_path / "auth_time.json"))
    monkeypatch.delenv("GOOGLE_WEB_CLIENT_JSON", raising=False)
    assert client.post("/api/panel/google/connect", headers=h).status_code == 409       # no Web client yet
    monkeypatch.setenv("GOOGLE_WEB_CLIENT_JSON", '{"web": {"client_id": "e"}}')
    assert g.can_renew_from_app()                                                         # pasted into .env works too
    monkeypatch.delenv("GOOGLE_WEB_CLIENT_JSON")
    assert client.post("/api/panel/google/connect").status_code == 401                   # signed out
    (tmp_path / "web.json").write_text('{"web": {"client_id": "id", "client_secret": "s", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}')
    url = client.post("/api/panel/google/connect", headers=h).json()["url"]
    assert url.startswith("https://accounts.google.com/") and "access_type=offline" in url
    state = next(iter(g._pending))

    class Creds:
        def to_json(self):
            return '{"refresh_token": "new"}'
    monkeypatch.setattr(F.Flow, "fetch_token", lambda self, **k: None)
    monkeypatch.setattr(F.Flow, "credentials", property(lambda self: Creds()))
    assert "old or was not started" in client.get("/api/google/callback?state=forged&code=x").text
    assert not (tmp_path / "token.json").exists()
    assert "connected again" in client.get(f"/api/google/callback?state={state}&code=x").text
    assert "new" in (tmp_path / "token.json").read_text() and g.login_age_days() < 0.01
    assert "old or was not started" in client.get(f"/api/google/callback?state={state}&code=x").text   # one use only
