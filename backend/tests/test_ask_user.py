"""T-15: when unsure, the model asks ONE question with 2-3 choices (ask_user) and the turn ends there, so it cannot guess."""
import asyncio
from types import SimpleNamespace as NS


def test_ask_user_formats_one_spoken_question():
    from agent.tools import ask_user, risk
    assert ask_user("Which Ron do you mean, sir?", ["Ron Cohen", "Ron Levi", "Ron Mizrahi", "Ron X"]) == \
        "Which Ron do you mean, sir: Ron Cohen, Ron Levi or Ron Mizrahi?"
    assert ask_user("What time, sir", None) == "What time, sir?"
    assert ask_user("Which song?", "Yellow | Fix You") == "Which song: Yellow or Fix You?"   # a string from a small model
    assert ask_user("", []).endswith("?")
    assert risk("ask_user") == ("read", set())


def test_ask_user_ends_the_turn_with_the_question(monkeypatch):
    from agent import agent_loop
    from agent.context import new_session
    new_session()
    chats = []

    def chat(messages, tools):
        chats.append(1)
        calls = [NS(function=NS(name="ask_user", arguments={"question": "Which Ron, sir?", "choices": ["Ron Cohen", "Ron Levi"]}))]
        return NS(message=NS(content="", tool_calls=calls if len(chats) == 1 else None), prompt_eval_count=1, eval_count=1, prompt_eval_duration=0)
    monkeypatch.setattr(agent_loop, "_chat", chat)
    reply, log = asyncio.run(agent_loop.run_agent("call Ron", [{"role": "user", "content": "call Ron"}]))
    assert reply == "Which Ron, sir: Ron Cohen or Ron Levi?" and len(chats) == 1 and "[ask_user]" in log


def test_bench_accepts_ask_user_for_ask_ok_cases():
    from agent.bench import judge
    case = {"tool": "phone_call", "ask_ok": True}
    assert judge(case, [("ask_user", {"question": "Which Ron?"})])[0]
    assert not judge({"tool": "phone_call"}, [("ask_user", {"question": "Which Ron?"})])[0]
