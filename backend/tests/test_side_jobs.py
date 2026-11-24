"""Ollama stopped (2026-11-24): learning facts and folding old chat fall back to Claude Haiku."""
import asyncio
import json

from agent import agent_loop, big_brain, memory


def _ollama_off(*a, **k):
    raise ConnectionError("Ollama is stopped")


def test_side_text_uses_haiku_when_the_local_model_is_off(monkeypatch):
    asked = []
    monkeypatch.setattr(agent_loop, "background_chat", _ollama_off)
    monkeypatch.setattr(big_brain, "side_job", lambda p, max_tokens=300: asked.append(p) or "summary")
    assert agent_loop.side_text("fold these turns") == "summary" and asked == ["fold these turns"]
    monkeypatch.setattr(agent_loop, "background_chat", lambda *a, **k: {"message": {"content": "local"}})
    assert agent_loop.side_text("x") == "local" and len(asked) == 1               # the free model first when it is on


def test_learning_facts_uses_haiku_when_the_local_model_is_off(monkeypatch):
    monkeypatch.setattr(agent_loop, "shared_prefix", lambda: ([], []))
    monkeypatch.setattr(agent_loop, "keep_prefix_cached", lambda: None)
    monkeypatch.setattr(memory.ollama_client, "chat", _ollama_off)
    sent = []
    monkeypatch.setattr(big_brain, "side_job", lambda p, max_tokens=300: sent.append(p) or json.dumps(
        {"interests": ["chess"], "facts": {"job": "student"}, "people": {"Maya": "sister"}}))
    interests, facts, people = asyncio.run(memory.extract_profile_updates("I play chess with my sister Maya after class"))
    assert interests == ["chess"] and facts == {"job": "student"} and people == {"Maya": {"relation_to_user": "sister"}}
    assert "I play chess" in sent[0] and "JSON only" in sent[0]
