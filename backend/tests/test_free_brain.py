"""The free OpenAI-compatible backup brain: off until configured, runs tools, moves on when a model is rate-limited."""
import asyncio
from types import SimpleNamespace as NS


def test_free_brain_is_off_until_configured(monkeypatch):
    from agent import free_brain
    monkeypatch.delenv("FREE_LLM_BASE_URL", raising=False)
    assert not free_brain.available()
    monkeypatch.setenv("FREE_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("FREE_LLM_MODELS", "a, b")
    assert free_brain.available() and free_brain.models() == ["a", "b"]


def test_free_brain_skips_a_busy_model_and_runs_tools(monkeypatch):
    from agent import context, free_brain, tools
    context.new_session()
    monkeypatch.setenv("FREE_LLM_BASE_URL", "http://router:4000/v1")
    monkeypatch.setenv("FREE_LLM_MODELS", "busy,good")
    asked = []

    class Busy(Exception):
        status_code = 429

    async def create(model, messages, tools):
        asked.append(model)
        if model == "busy":
            raise Busy()
        if not any(m["role"] == "tool" for m in messages):
            call = NS(id="c1", function=NS(name="get_current_weather", arguments='{"city_name": "Ramat Gan"}'))
            return NS(choices=[NS(message=NS(content="", tool_calls=[call]))])
        return NS(choices=[NS(message=NS(content="Ramat Gan is 16 degrees, sir.", tool_calls=None))])
    monkeypatch.setattr(free_brain, "_client", lambda: NS(chat=NS(completions=NS(create=create))))
    ran = []

    async def call(name, args):
        ran.append((name, args))
        return "Real-time weather for Ramat Gan: 16C"
    monkeypatch.setattr(tools.registry(), "call", call)
    answer, log = asyncio.run(free_brain.run_free("weather in ramat gan", [{"role": "user", "content": "weather in ramat gan"}]))
    assert answer == "Ramat Gan is 16 degrees, sir." and ran == [("get_current_weather", {"city_name": "Ramat Gan"})]
    assert asked == ["busy", "good", "good"]          # the busy one is tried once, then it stays on the one that works
