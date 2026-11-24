"""The briefing's calendar, news and fun fact are kept from 07:00; the opening (weather, reminders, email) is written when he hears it."""
import asyncio
from datetime import datetime


def test_briefing_kept_part_is_reused_and_opening_is_live(tmp_path, monkeypatch):
    import agent.briefing as br
    import agent.tools as tools
    monkeypatch.setattr(br, "READY_FILE", str(tmp_path / "ready.json"))
    asked = []

    async def data(live=True, kept=True, include_news=True):
        asked.append((live, kept))
        return f"weather now: {len(asked)}" if live else "calendar + news"
    monkeypatch.setattr(tools, "briefing_data", data)

    async def write(prompt):
        return "Good morning, sir, 18 degrees." if "opening" in prompt else "Dentist at ten. Fun fact."
    monkeypatch.setattr(br, "_write", write)
    monkeypatch.setattr("agent.memory.get_profile_summary", lambda *a: "")
    monkeypatch.setattr("agent.lessons.proposals", lambda: [])

    asyncio.run(br.prepare_briefing(br.ISRAEL_TZ.localize(datetime.now().replace(hour=7))))
    assert asked == [(False, True)]                                   # 07:00: calendar + news only
    monkeypatch.setattr(br, "ready_briefing", lambda now=None: "Dentist at ten. Fun fact.")
    assert asyncio.run(br.get_briefing()) == "Good morning, sir, 18 degrees. Dentist at ten. Fun fact."
    assert asyncio.run(br.get_briefing()) and asked[1:] == [(True, False), (True, False)]   # opening fresh each time, kept part not redone


def test_the_briefing_is_read_out_as_it_is(monkeypatch):
    """The model reworded or dropped the briefing ("How is your guard duty going?"): its text is now the reply."""
    from types import SimpleNamespace as NS
    from agent import agent_loop, tools as tools_mod
    from agent.context import new_session
    new_session()
    chats = []

    def chat(messages, tools):
        chats.append(1)
        calls = [NS(function=NS(name="morning_briefing", arguments={}))] if len(chats) == 1 else None
        return NS(message=NS(content="How is your guard duty going?", tool_calls=calls), prompt_eval_count=1, eval_count=1, prompt_eval_duration=0)

    async def call(name, args):
        return "Good morning, sir. Guard duty at eight."
    monkeypatch.setattr(agent_loop, "_chat", chat)
    monkeypatch.setattr(tools_mod.registry(), "call", call)
    reply, _ = asyncio.run(agent_loop.run_agent("good morning", [{"role": "user", "content": "good morning"}]))
    assert reply == "Good morning, sir. Guard duty at eight." and len(chats) == 1


def test_heard_briefing_is_a_fact_in_the_tool_result_not_a_note_naming_the_tool(monkeypatch):
    """Naming morning_briefing in the tool check made the model pick it for "hey enzo" (7 of 9, 0 of 6 without).
    So the check never mentions it, and the tool itself says he already heard it (not read as-is: he may ask again)."""
    from agent import agent_loop, briefing, tools as tools_mod
    from datetime import datetime
    seen = {}

    async def pick(messages, answer, question, names, schemas, what):
        seen.update(names=names, question=question)
        return ""
    monkeypatch.setattr(agent_loop, "_pick", pick)
    today = datetime.now(briefing.ISRAEL_TZ).date().isoformat()
    monkeypatch.setattr(briefing, "_last_date", lambda: today)
    schemas = [{"function": {"name": n}} for n in ("morning_briefing", "get_current_weather")]
    asyncio.run(agent_loop.choose_tool([], "Hello, sir.", schemas))
    assert "morning_briefing" in seen["names"] and "briefing" not in seen["question"]

    async def brief():
        return "Good morning. Dentist at 16:00."
    monkeypatch.setattr(briefing, "get_briefing", brief)
    marked = []
    monkeypatch.setattr(briefing, "_mark_done", marked.append)
    out = asyncio.run(tools_mod.morning_briefing())
    assert out.startswith("[ALREADY HEARD]") and "Dentist" in out and not agent_loop._as_is([("morning_briefing", out)])
    monkeypatch.setattr(briefing, "_last_date", lambda: "2000-01-01")
    out = asyncio.run(tools_mod.morning_briefing())
    assert out == "Good morning. Dentist at 16:00." and marked == [today]


def test_weather_without_location_tells_the_model_his_home_city(monkeypatch):
    from agent import context
    from agent.skills import weather
    context.new_session()
    monkeypatch.setenv("DEFAULT_CITY", "Beer Sheva")
    monkeypatch.setattr(weather, "_weather_at", lambda *a: "should not be fetched")
    out = weather.get_current_weather("")
    assert "Beer Sheva" in out and "ask him" in out and "should not" not in out


def test_weather_at_the_phone_location_names_the_place(monkeypatch):
    from agent import context
    from agent.skills import weather
    s = context.new_session()
    s.latitude, s.longitude = 32.08, 34.82
    monkeypatch.setattr(weather, "_place_name", lambda lat, lon: "Ramat Gan, Israel")
    monkeypatch.setattr(weather, "_weather_at", lambda lat, lon, name: f"Real-time weather for {name}: 16C")
    assert "Ramat Gan" in weather.get_current_weather("") and s.place == "Ramat Gan, Israel"


def test_next_turn_continues_the_last_turns_exact_messages(monkeypatch):
    """qwen3.5 resumes its cache only near the end of an earlier request: a rebuilt history meant a full re-read
    (~35 s a turn measured), continuing the last turn's messages ~3-5 s. A different last request rebuilds."""
    from agent import agent_loop, context
    s = context.new_session()
    sent = []

    def chat(messages, tools):
        sent.append([dict(m) for m in messages])
        class M: content, tool_calls = "Done, sir.", None
        class R: message, prompt_eval_count, eval_count, prompt_eval_duration = M, 0, 0, 0
        return R
    monkeypatch.setattr(agent_loop, "_chat", chat)
    async def pick(*a, **k):
        return ""
    monkeypatch.setattr(agent_loop, "_pick", pick)
    asyncio.run(agent_loop.run_agent("first", [{"role": "user", "content": "first"}]))
    first = sent[-1]
    hist = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "Done, sir."}, {"role": "user", "content": "second"}]
    asyncio.run(agent_loop.run_agent("second", hist))
    assert sent[-1][:len(first)] == first and sent[-1][len(first)] == {"role": "assistant", "content": "Done, sir."}
    other = [{"role": "user", "content": "something else"}, {"role": "assistant", "content": "Ok."}, {"role": "user", "content": "third"}]
    asyncio.run(agent_loop.run_agent("third", other))
    assert sent[-1][1] == {"role": "user", "content": "something else"}        # rebuilt from the chat history
    agent_loop.session().transcript = ("third", [{"role": "user", "content": "x"}])
    monkeypatch.setattr(agent_loop, "_chat", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    try:
        asyncio.run(agent_loop.run_agent("fourth", other))
    except RuntimeError:
        pass
    assert agent_loop.session().transcript is None                         # a turn that did not end locally is never continued
